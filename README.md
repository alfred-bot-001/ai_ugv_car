# AI UGV Car

老三小车（Waveshare UGV + 树莓派）的操纵与管理项目。老大 Mac Mini 运行主控台，提供 HTTPS 登录、设备状态、视频/拍照、听音/转写、发声、圆盘控制、家庭人脸与人物照片标注；老三 Pi 运行设备代理，主动通过 WSS 连接 Mini，独占 UART/M2/摄像头，并在本地执行租约、看门狗和失联停止。

正式入口：[car.aimaker.vip](https://car.aimaker.vip/)。当前版本为主控台 0.6.2 / Pi 0.4.1 / ugv-nav 0.3.4。

| 代码包 | 运行位置 | 内容 |
| --- | --- | --- |
| [`robot-console/`](robot-console/) | Mac Mini，launchd | FastAPI 主控台：登录/SSO、SQLite、设备 WSS、音视频、ASR/TTS、人脸、姿态审核与照片标注 |
| [`pi-agent/`](pi-agent/) | 老三 Pi，systemd | WSS 设备代理：V4L2/M2/ALSA、独占 UART、遥测、姿态 worker、租约与本地停止保护 |
| [`ugv-nav/`](ugv-nav/) | 老三 Pi，Docker | ROS2 Humble 容器：激光雷达、TF，后续建图与导航；不接触 UART |
| [`scripts/`](scripts/) | 开发机 / Pi | 实机运动验收、浏览器人脸叠加验证、离线姿态相册 |

本项目 2026-10-14 从 [ai-tv-pet](https://github.com/alfred-bot-001/ai-tv-pet) 迁出。电视宠物（`mini-server/`、`tv-apk/`）仍留在原仓库。为了不影响已部署的服务，Python 包名（`ai-tv-pet-console`、`ai-tv-pet-pi-agent`）、模块名（`tvpet_console`、`tvpet_pi`）、launchd 标签和部署目录都沿用原名。

## 开发

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Python 3.11、uv workspace、src 布局，Mypy strict，CI 要求覆盖率 ≥ 80%。开发前先读 [架构文档](docs/ARCHITECTURE.md)；部署、首次密码设置与实机证据见 [部署与验证](docs/control-console-deployment.md)，能力契约见 [主控台技术设计](docs/control-console-design.md)，进度见 [TODO](TODO.md)。

专题设计：[人脸视频](docs/face-video-design.md) · [姿态审核采样](docs/pose-review-design.md) · [人物照片标注](docs/photo-annotation-design.md) · [旧机器人服务停用记录](docs/robot-services-retirement.md) · [ROS2 建图与自主导航（设计稿）](docs/ros-navigation-design.md)。
