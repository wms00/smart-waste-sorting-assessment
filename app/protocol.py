"""上位机通信协议骨架。

帧格式请阅读仓库根目录的 ``PROTOCOL.md``。本文件中的两个 TODO 是通信
模块的核心考核点：封包和流式拆包。请勿假设一次 ``recv`` 恰好得到一帧。
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import List


HEADER = b"\xAA\x55"
MAX_BODY_LENGTH = 64


class Command(IntEnum):
    WASTE_RESULT = 0x01
    ACK = 0x02
    ACTION_DONE = 0x03
    BIN_FULL = 0x04
    START_COMPRESS = 0x05
    HEARTBEAT = 0x06
    ERROR = 0x7F


class WasteType(IntEnum):
    HAZARDOUS = 0x01
    OTHER = 0x02
    RECYCLABLE = 0x03
    KITCHEN = 0x04


WASTE_NAMES = {
    WasteType.RECYCLABLE: "可回收垃圾",
    WasteType.KITCHEN: "厨余垃圾",
    WasteType.HAZARDOUS: "有害垃圾",
    WasteType.OTHER: "其他垃圾",
}


@dataclass(frozen=True)
class Packet:
    command: Command
    payload: bytes = b""

    def encode(self) -> bytes:
        """按协议生成完整二进制帧：AA 55 + LENGTH + BODY + CHECKSUM。"""
        # COMMAND 占 1 字节，和 PAYLOAD 拼成帧体（body）
        body = bytes((int(self.command),)) + self.payload
        length = len(body)
        # LENGTH 字段只有 1 字节，协议规定范围 1～64，超长必须报错而不是发出坏帧
        if length < 1 or length > MAX_BODY_LENGTH:
            raise ValueError("帧体长度 {} 超出协议范围 1~{}".format(length, MAX_BODY_LENGTH))
        # CHECKSUM = 从 LENGTH 到 PAYLOAD 的累加和低 8 位，也就是 (length + body 各字节之和) & 0xFF
        checksum = (length + sum(body)) & 0xFF
        return HEADER + bytes((length,)) + body + bytes((checksum,))


class StreamParser:
    """把任意分片的 TCP/串口字节流增量解析为 Packet。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.error_count = 0

    def reset(self) -> None:
        self._buffer.clear()
        self.error_count = 0

    def feed(self, data: bytes) -> List[Packet]:
        """增量喂入字节流，返回本次能完整解析出的所有 Packet。

        支持：半包（数据不够先缓存）、粘包（一次含多帧）、噪声（帧前杂字节）、
        校验错/非法长度/未知命令后重新同步。
        """
        self._buffer.extend(data)
        packets = []

        while True:
            # 1. 找帧头；没有帧头就只保留可能是帧头前缀的最后一个字节
            header_index = self._buffer.find(HEADER)
            if header_index < 0:
                if len(self._buffer) >= len(HEADER):
                    del self._buffer[: -(len(HEADER) - 1)]
                break

            # 2. 帧头前面的都是噪声，丢掉并计数
            if header_index > 0:
                del self._buffer[:header_index]
                self.error_count += 1

            # 3. 至少要有 帧头(2) + LENGTH(1) 才能读长度
            if len(self._buffer) < len(HEADER) + 1:
                break
            length = self._buffer[len(HEADER)]

            # 4. LENGTH 非法：丢掉帧头第一个字节，重新同步
            if length < 1 or length > MAX_BODY_LENGTH:
                del self._buffer[:1]
                self.error_count += 1
                continue

            # 5. 整帧未到齐（半包）：等待更多数据
            total = len(HEADER) + 1 + length + 1
            if len(self._buffer) < total:
                break

            # 6. 校验和错误：丢掉帧头第一个字节，重新同步
            frame = self._buffer[:total]
            body = frame[len(HEADER) + 1 : len(HEADER) + 1 + length]
            expected = (length + sum(body)) & 0xFF
            if frame[-1] != expected:
                del self._buffer[:1]
                self.error_count += 1
                continue

            # 7. 帧完整且校验通过：消费整帧，尝试转成 Packet
            del self._buffer[:total]
            try:
                packets.append(Packet(Command(body[0]), bytes(body[1:])))
            except ValueError:
                # 未知命令字：帧本身合法但没有对应命令，跳过并计数
                self.error_count += 1

        return packets
