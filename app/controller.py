"""与 UI 无关的分拣状态机骨架。"""

import time
from enum import Enum
from typing import Callable, Dict, Optional

from .detectors import Detection
from .protocol import Command, Packet, WasteType, WASTE_NAMES
from .transport import Transport, TransportError


class SystemState(Enum):
    IDLE = "等待识别"
    WAIT_ACK = "等待下位机确认"
    EXECUTING = "下位机执行中"
    COOLDOWN = "等待物体离开"
    COMPRESSING = "压缩中"
    PAUSED = "已暂停"
    ERROR = "通信异常"


EventCallback = Callable[[str, dict], None]


class SortingController:
    def __init__(
        self,
        transport: Transport,
        event_callback: Optional[EventCallback] = None,
        stable_frames: int = 3,
        ack_timeout: float = 2.0,
        max_retries: int = 2,
        compress_after: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.transport = transport
        self.event_callback = event_callback or (lambda _name, _data: None)
        self.stable_frames = stable_frames
        self.ack_timeout = ack_timeout
        self.max_retries = max_retries
        self.compress_after = compress_after
        self.clock = clock
        self.counts = {item: 0 for item in WasteType}
        self.bin_full = {item: False for item in WasteType}
        self.state = SystemState.IDLE
        self.pending = None  # type: Optional[WasteType]
        self._candidate = None  # type: Optional[WasteType]
        self._stable_count = 0
        self._missing_frames = 0
        self._last_packet = None  # type: Optional[Packet]
        self._deadline = 0.0
        self._retry_count = 0
        self._compress_sent = False
        self._sequence = 0
        self._active_sequence = None  # type: Optional[int]

    def _emit(self, name: str, **data) -> None:
        self.event_callback(name, data)

    def _set_state(self, state: SystemState) -> None:
        if state != self.state:
            self.state = state
            self._emit("state", value=state.value)

    def reset(self) -> None:
        self.counts = {item: 0 for item in WasteType}
        self.bin_full = {item: False for item in WasteType}
        self.pending = None
        self._candidate = None
        self._stable_count = 0
        self._missing_frames = 0
        self._last_packet = None
        self._retry_count = 0
        self._compress_sent = False
        self._active_sequence = None
        self._set_state(SystemState.IDLE)
        self._emit("counts", values=self.named_counts())
        self._emit("bins", values=self.named_bins())

    def named_counts(self) -> Dict[str, int]:
        return {WASTE_NAMES[key]: value for key, value in self.counts.items()}

    def named_bins(self) -> Dict[str, bool]:
        return {WASTE_NAMES[key]: value for key, value in self.bin_full.items()}

    def process_detection(self, detection: Optional[Detection]) -> None:
        """稳定帧判定 + 冷却消抖 + 分类指令触发。

        规则：
        - 连续 stable_frames 帧识别为同一类别，才发送一次分类指令；
        - 等待 ACK / 执行 / 压缩 / 异常期间不接受新物体；
        - 收到 ACTION_DONE 后进入 COOLDOWN，直到画面里没有物体
          （detection 为 None）才回到 IDLE，避免同一物体重复发送。
        """
        # 正在和下位机交互，或已经异常，不处理新画面
        if self.state in (SystemState.WAIT_ACK, SystemState.EXECUTING,
                          SystemState.COMPRESSING, SystemState.ERROR):
            return

        # 画面里没有物体：结束冷却，准备接受下一个物体
        if detection is None:
            if self.state == SystemState.COOLDOWN:
                self._set_state(SystemState.IDLE)
            self._candidate = None
            self._stable_count = 0
            return

        # 冷却期说明上一个物体还没离开，不能重复发送
        if self.state == SystemState.COOLDOWN:
            return

        # 连续帧类别一致才累加稳定计数；类别一变就重新开始数
        if detection.waste_type != self._candidate:
            self._candidate = detection.waste_type
            self._stable_count = 1
        else:
            self._stable_count += 1

        if self._stable_count >= self.stable_frames:
            if self.bin_full.get(detection.waste_type):
                # 该类别桶已满：不发送，进入冷却等物体离开
                self._set_state(SystemState.COOLDOWN)
                self._emit("log", message="{}桶已满，跳过该物体".format(WASTE_NAMES[detection.waste_type]))
                return
            self._send_classification(detection.waste_type)

    def _send_packet(self, packet: Packet, wait_for_reply: bool = True) -> None:
        try:
            self.transport.send(packet)
        except TransportError as exc:
            self._set_state(SystemState.ERROR)
            self._emit("log", message="发送失败：{}".format(exc))
            return
        self._emit("tx", packet=packet.encode().hex(" ").upper())
        if wait_for_reply:
            self._last_packet = packet
            self._deadline = self.clock() + self.ack_timeout
            self._retry_count = 0

    def _send_classification(self, waste_type: WasteType) -> None:
        """检查满载、递增序号，并发送 WASTE_RESULT。"""
        if self.bin_full.get(waste_type):
            self._set_state(SystemState.COOLDOWN)
            return

        # 每次产生“新动作”序号 +1；8 位序号超过 255 自动回绕
        self._sequence = (self._sequence + 1) & 0xFF
        self.pending = waste_type
        self._active_sequence = self._sequence
        packet = Packet(Command.WASTE_RESULT, bytes((self._sequence, int(waste_type))))
        self._set_state(SystemState.WAIT_ACK)
        self._send_packet(packet, wait_for_reply=True)

    def poll(self) -> None:
        """轮询接收；WAIT_ACK 超时后按原序号重发，超过次数进入 ERROR。"""
        for packet in self.transport.poll():
            self._emit("rx", packet=packet.encode().hex(" ").upper())
            self.handle_packet(packet)

        # 从发出指令到收到 ACTION_DONE 是同一个事务：等 ACK 和执行阶段都可超时重发
        if self.state not in (SystemState.WAIT_ACK, SystemState.EXECUTING) or self._last_packet is None:
            return

        if self.clock() < self._deadline:
            return

        if self._retry_count >= self.max_retries:
            self._set_state(SystemState.ERROR)
            self._emit("log", message="下位机无响应，已重发{}次，进入异常状态".format(self.max_retries))
            return

        # 重发必须复用原来的 Packet（原序号），不能生成新序号
        self._retry_count += 1
        try:
            self.transport.send(self._last_packet)
        except TransportError as exc:
            self._set_state(SystemState.ERROR)
            self._emit("log", message="重发失败：{}".format(exc))
            return
        self._emit("tx", packet=self._last_packet.encode().hex(" ").upper())
        self._deadline = self.clock() + self.ack_timeout

    def handle_packet(self, packet: Packet) -> None:
        """处理 ACK、ACTION_DONE、BIN_FULL 和 ERROR。"""
        if packet.command == Command.ACK:
            # ACK 载荷 = ORIGINAL_COMMAND, SEQUENCE
            if len(packet.payload) >= 2:
                original_command = packet.payload[0]
                sequence = packet.payload[1]
                if (original_command == int(Command.WASTE_RESULT)
                        and sequence == self._active_sequence
                        and self.state == SystemState.WAIT_ACK):
                    self._set_state(SystemState.EXECUTING)
            return

        if packet.command == Command.ACTION_DONE:
            # 只有序号对得上且确实在执行中，才算数（防止重复/过期消息）
            if not packet.payload:
                return
            sequence = packet.payload[0]
            if sequence != self._active_sequence or self.state != SystemState.EXECUTING:
                return
            waste_type = self.pending
            if waste_type is not None:
                # 计数唯一入口：收到 ACTION_DONE 才 +1
                self.counts[waste_type] = self.counts.get(waste_type, 0) + 1
                self._emit("counts", values=self.named_counts())
                self._maybe_compress()
            self._set_state(SystemState.COOLDOWN)
            self._active_sequence = None
            self.pending = None
            self._last_packet = None
            return

        if packet.command == Command.BIN_FULL:
            if not packet.payload:
                return
            try:
                waste_type = WasteType(packet.payload[0])
            except ValueError:
                self._emit("log", message="收到未知类别的满载消息")
                return
            self.bin_full[waste_type] = True
            self._emit("bins", values=self.named_bins())
            self._emit("log", message="{}桶已满载".format(WASTE_NAMES[waste_type]))
            return

        if packet.command == Command.ERROR:
            try:
                message = packet.payload.decode("utf-8")
            except UnicodeDecodeError:
                message = "（错误信息无法解码）"
            self._emit("log", message="下位机错误：{}".format(message))
            self._set_state(SystemState.ERROR)
            return

    def _maybe_compress(self) -> None:
        """加分项：任一类别计数达到阈值后，发送一次 START_COMPRESS。"""
        if self._compress_sent:
            return
        if not any(value >= self.compress_after for value in self.counts.values()):
            return
        self._compress_sent = True
        self._sequence = (self._sequence + 1) & 0xFF
        packet = Packet(Command.START_COMPRESS, bytes((self._sequence,)))
        self._emit("log", message="计数达到{}，发送压缩指令".format(self.compress_after))
        self._send_packet(packet, wait_for_reply=False)
