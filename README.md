# 智能垃圾分拣上位机｜招新考核

无硬件即可完成闭环验收的上位机项目：视频/摄像头输入 → YOLO 推理 → 分拣状态机 →
二进制协议通信 → 模拟下位机 → 计数与界面反馈。

```text
视频或摄像头 → Predict/best.pt → 状态机 → TCP 模拟下位机 → UI
                                      └→ Nano 上可替换为串口（SerialTransport）
```

## 验证状态

| 项目 | 状态 |
|---|---|
| 公开测试 `python -m unittest discover -s tests -v` | ✅ **10/10 通过** |
| 本机运行（Windows + Python 3.11.17 + torch 2.15.0+cpu） | ✅ **已实测**：真实 YOLO 识别、TX/RX 通信、`ACTION_DONE` 后计数、进度条跳转、摄像头切换 |
| Docker（`docker compose up --build`） | ⚠️ **未在本机验证**：开发机无 WSL2 环境。Dockerfile 与 compose 配置未作改动 |
| Jetson Nano（`Dockerfile.nano`） | ⚠️ **未验证**：无板卡 |

> 本仓库的全部功能验证均在本机运行方式下完成，功能链路不依赖运行方式。

## 仓库内容

- 比赛 YOLO 权重 `Predict/best.pt`；
- 由真实比赛照片制作的 `assets/assessment_demo.mp4`；
- 原项目长视频 `vedio/dep.mp4`，用于视频跳转、暂停、重播和资源释放验收；
- 普通电脑 Docker 环境与 Jetson Nano/JetPack 4 镜像配置；
- 独立的 TCP 下位机模拟器 `simulator/lower_simulator.py`；
- PyQt 界面、后台线程、视频源与传输层；
- 协议文档 `PROTOCOL.md` 与公开单元测试 `tests/`。

## 本机运行（推荐，已实测）

已验证环境：**Windows 10/11 + CPython 3.11.17**。其他 3.10–3.12 版本预期可用，但未逐一验证。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt

# 终端 1：启动模拟下位机（保持运行）
.\.venv\Scripts\python.exe -m simulator.lower_simulator

# 终端 2：启动上位机（另开一个终端）
.\.venv\Scripts\python.exe -m app.main
```

界面出现后：点「导入视频」选择 `assets/assessment_demo.mp4` → 点「开始识别」。
日志区会依次出现 `TX AA 55 01 ...`（WASTE_RESULT）→ `RX ...02 01 xx`（ACK）→
`RX ...03 xx`（ACTION_DONE），此时对应类别计数 +1。

常用参数（见 `app/main.py`）：`--confidence` 置信度阈值、`--source`/`--source-value`
输入源、`--transport serial --serial-port COM3` 切换真实串口、`--sim-port` 模拟器端口。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

覆盖：协议半包/粘包/噪声/校验错重同步（2）、状态机稳定帧与超时重发与满载（3）、
视频读取/跳转/重播与 YOLO 适配器（5），共 **10** 个用例。

## Docker（未在本机验证）

```bash
docker compose up --build
```

浏览器打开 `http://localhost:6080/vnc.html?autoconnect=1&resize=scale`。停止：

```bash
docker compose down
```

## Jetson Nano（未验证）

`Dockerfile.nano` 面向经典 Jetson Nano 的 JetPack 4.x/L4T 32.x。只有在真实板卡上才能最终
验证 GPU、摄像头与串口驱动：

```bash
cat /etc/nv_tegra_release
docker compose -f compose.yaml -f compose.nano.yaml up --build
```

使用实际摄像头或串口时需增加 `/dev/video0` 或 `/dev/ttyUSB0` 映射。普通电脑完成考核不要求 Nano 硬件。

## 实现要点

- `app/protocol.py`：`Packet.encode` 按 `AA 55 | LEN | BODY | CHK` 封包；`StreamParser.feed`
  以 `bytearray` 缓冲 + `find(HEADER)` 定界，四层防御（帧头 → LENGTH 合法 → 整帧收全 → 校验和），
  任何一层不通过只丢 1 字节重新同步。
- `app/detectors.py`：`model.predict(source=frame)`，`results[0].boxes` 取 `conf/cls/xyxy`，
  映射到四类垃圾并按置信度降序。
- `app/controller.py`：连续 `stable_frames` 帧同类才发 `WASTE_RESULT`；超时按**原序号**重发，
  超过 `max_retries` 进 ERROR；**只有 `ACTION_DONE` 才计数**；`COOLDOWN` 等物体离开才回 `IDLE`。
- `app/worker.py`：全部推理与通信在 `QThread` 中执行，界面只接收信号，避免阻塞。
- `vedio/video_module.py`：OpenCV 逐帧 BGR 输出，支持范围校验的跳帧与重播。

## 约束（考核要求）

- 必须实际调用给定 YOLO 权重；不得用颜色阈值、标注文件或固定答案替代推理。
- 计数只能在下位机返回 `ACTION_DONE` 后更新。
- 不得把耗时推理放进 Qt 主线程。
- 不得删除或绕过公开测试。

## 提交物

1. 3–4 分钟连续运行录屏；
2. `设计说明.pptx`（系统架构、数据流、协议、状态机、UI、部署、测试与已知问题）；
3. 本仓库链接。

最终评分与验收场景见 [TASKS.md](TASKS.md)，协议细节见 [PROTOCOL.md](PROTOCOL.md)。
