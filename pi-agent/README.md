# 老三设备代理

Python 3.11 可安装包。老大 Mini 提供页面、认证、ASR/TTS 和后续完整对话；老三通过控制/视频/音频三条主动 WSS 连接承担设备、采集与动作。旧机器人服务保持停用。

## 已实现

- V4L2 MMAP MJPEG 640×480/10fps，保留驱动 sequence、单调时钟 timestamp 和 flags；拍照取得请求后的新帧。
- M2 ALSA 16kHz 单声道 S16_LE，20ms PCM 包；MCU 校验/握手 ACK 和显式采集时的临时唤醒。
- 扬声器校验 WAV、摘要、播放 epoch 和一次性凭证；支持中断。
- UART 独占、T1001 遥测、圆盘方向到 T1/T133 的映射；松手、失焦、会话撤销、失联及超时停止。
- Pi 独立看门狗和经过实测的固件超时；无有效实机记录时禁用运动。
- 系统温度、内存、负载、风扇、限频信息。缺失数据保持未知。

默认不采集媒体。必须由 Mini 的有效登录会话显式开启，最后一个使用者退出或授权心跳消失后关闭。音视频、照片和文本不在代理留存。

## 运行

```sh
uv sync --locked
uv run tvpet-pi --config /home/ws/deploy/ai-tv-pet-agent/private/agent.json
```

实际长期服务从 `/home/ws/deploy/ai-tv-pet-agent/.venv/` 中的 wheel 运行，使用 [systemd 模板](deploy/ai-tv-pet-agent.service)。[配置示例](config.example.json) 仅有路径占位符；设备 token、CA 和实机记录放在私有运行目录，不提交 Git。

轮与云台均由网页圆盘控制，无角度输入框。轮命令上限为厂家 T1 幅度 0.10，其含义随固件底盘类型变化，不能统一标为 m/s。已验收且用户批准的硬件通过 release_policy=persistent 长期保存资格，不再因 8 小时或 Pi 重启要求人工放行。每次启动停止设备并应用固件超时，取得有效遥测后自动开放；硬件反馈异常时暂停，正常后自动恢复，手动急停仍由用户解除。验收证据的硬件参数、权限与限速/停止测量继续校验。详细验收命令、实际结果和回滚见 [部署与验证](../docs/control-console-deployment.md)。

OpenCV/小模型 worker、VAD、自动对话和电视适配器仍待实现，能力状态为未启用。原 `CameraPort`/`MicrophonePort`/`SpeakerPort` 和 `PiSessionController` 的显式会话契约继续保留。

### 本地姿态 worker（0.3.1）

独立 pose-venv 运行 `python -m tvpet_pi.pose_worker MODEL`，由代理通过私有管道提供 JPEG，worker 不接触相机/UART/网络。代理配置 `pose_python`、`pose_model` 非空时启用；停止代理会停止 worker。CPU 低优先级、每秒最多五次、失败退避十秒。已验证 Pi 5 / Python 3.11 / mediapipe 0.10.18 的 ARM64 wheel；原生依赖固定清单见 `deploy/pose-requirements.txt`。模型为 Google 官方 pose_landmarker_lite/float16/1，SHA256 `59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a`。安装项目 wheel 到代理与 pose 两个环境，不能只更新其中一个。姿态是启发式待验收标签，得分不是准确率。

0.3.1 使用 geometry-v2，修复多人靠近时旧轨迹反复占用匹配、导致时序姿态一直无法判断的问题。稳定的相邻人物保持各自轨迹，交叉或无法区分时清除歧义历史；站着、坐着、躺着与动作分开返回。只有脸或腿部关键点缺失时仍可能无法判断。

### 前置 ToF 低矮障碍停车（0.10.0）

DFRobot SEN0628 8×8 ToF 矩阵经 [XIAO 桥接固件](../firmware/tof-bridge/README.md) 从 USB 串口进来。配置 `tof_port` 写 `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`（留空即整个功能关闭）、`tof_baseline` 为地面基准文件（默认 `~/workspace-agent/agent-data/ai-ugv-car/pi-agent/tof-baseline.json`）、`tof_cliff_stop` 是否把「丢失地面」也当停车条件。代理独占串口，打开会复位 XIAO，约 10 s 后出帧；超过 1 s 无帧上报 `fault`。

标定与观察（先 `sudo systemctl stop ai-tv-pet-agent.service`，车停在平地）：

```sh
tvpet-tof --port /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_10:BD:A3:CF:46:78-if00 calibrate
tvpet-tof --port /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_10:BD:A3:CF:46:78-if00 watch --seconds 30
```

判定规则、阈值和停车路径见 [任务自主设计 §3.6](../docs/task-autonomy-design.md)。触发时走 `Board` 看门狗的普通停车（与失联停止同一条），只拦前进；导航持有租约时作废 arm 会话（`fault` 为 `tof_low_obstacle` / `tof_cliff`）。
