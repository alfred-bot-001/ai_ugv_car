# ai_ugv_car 开发约定（给 Claude Code 和接手的人）

小车「老三」（Waveshare UGV Rover 六轮）的操纵与管理项目。进度看 [TODO.md](TODO.md)，架构看 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，导航看 [docs/ros-navigation-design.md](docs/ros-navigation-design.md)，部署细节看 [docs/control-console-deployment.md](docs/control-console-deployment.md)。

## 沟通

- 面向用户的回答、计划、总结一律用简体中文；代码、命令、标识符保持原样。

## 仓库结构

| 目录 | 包 / 版本 | 运行在 |
| --- | --- | --- |
| `robot-console/` | `tvpet_console`（ai-tv-pet-console 0.9.0），FastAPI 主控台，网页在 `src/tvpet_console/web/` | Mini（ssh `agent001`） |
| `pi-agent/` | `tvpet_pi`（ai-tv-pet-pi-agent 0.7.0），设备代理，唯一的底盘 UART 所有者，含导航桥 `nav.py` | 车上 Pi 5（ssh `agent003`，用户 ws，免密 sudo） |
| `ugv-nav/` | `ugv_nav_tools`（ai-ugv-car-nav-tools 0.5.1）+ ROS2 Humble 容器（SLAM Toolbox、Nav2、RF2O、EKF）+ 主机服务 `ugv-navd` | Pi 的 docker 容器 `ugv-nav` |

包名、launchd 标签、systemd 服务名、部署目录**刻意沿用 ai-tv-pet-***，线上部署依赖它们，不要改名。

## Python 规矩（用户明确要求）

- 所有 Python（包括 ROS 侧小工具）都放进 uv workspace 的包里：src 布局、pyproject、`[project.scripts]` 入口、以 wheel 部署；不写游离脚本。
- 纯逻辑与 I/O（rclpy、串口、socket）分开，纯逻辑必须有单元测试。
- 提交前跑完整门禁，全部通过才提交：

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check . && uv run mypy && \
uv run pytest --cov=tvpet_console --cov=tvpet_pi --cov=ugv_nav_tools --cov-fail-under=80
```

- 升级行为时提升对应包的版本号，并在 docs 记录部署、备份和回滚位置。

## Git

- 推送用 bot 账号：remote 必须是 `git@github-alfred-bot-001:alfred-bot-001/ai_ugv_car.git`（`~/.ssh/config` 里的别名指向 bot 密钥；默认 github.com 密钥没有权限）。
- 提交说明用英文，结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 只在用户要求时提交或推送。

## 部署（概要）

- **Mini 主控台**：运行目录 `/Users/agent001/workspace-agent/deploy/ai-tv-pet-console/`，launchd `com.ai-tv-pet.console`，公网 `https://car.aimaker.vip/`。本机 `uv build` 出 wheel，复制到 Mini，停服务后 `uv pip install --no-deps --reinstall` 进 `.venv`，wheel 存 `releases/`；重启用 `launchctl bootout/bootstrap gui/$(id -u)`。Mini 的 uv 在 `/opt/homebrew/bin/uv`。
- **Pi 代理**：`/home/ws/deploy/ai-tv-pet-agent/`，systemd `ai-tv-pet-agent.service`；wheel 同时装进 `.venv` 和 `pose-venv`；私有配置 `private/agent.json`（0600，含 `nav_dir`）。
- **ugv-nav**：`uv build --package ai-ugv-car-nav-tools --wheel --out-dir ugv-nav/dist` → `rsync --exclude .env ugv-nav/ agent003:/home/ws/deploy/ugv-nav/src/` → Pi 上 `sudo docker build` → `ugv-navctl mapping`。主机操作工具 `/home/ws/deploy/ugv-nav/.venv/bin/ugv-navctl`（status/stop/mapping/save/localize/pose/where/goal/patrol）。**容器重启会清空未保存的 SLAM 地图**，先确认再重启。地图页的建图、保存、加载、设位置、去这里、取消由主机服务 `ugv-navd`（systemd，用户 ws，socket `/home/ws/deploy/ugv-nav/run/ops.sock`，容器不挂载）调同一套 NavCtl 执行；单元文件在 `ugv-nav/systemd/`，代码更新后装进主机 `.venv`（`/home/ws/deploy/ai-tv-pet-agent/.venv/bin/pip --python .venv/bin/python install --no-deps --force-reinstall <wheel>`）并重启它。
- 每次升级前备份上一版（Pi 在 `backups/`、`releases/`），部署后实测健康状态再报告。

## 安全红线

- **让车动之前必须确认用户在场、场地已清空**，每次新的运动测试都要重新确认；授权只由 `ugv-navctl` 写 `arm.json`，平时保持 `{"enabled": false}`。
- 浏览器永远碰不到 ROS 和 UART：不开放 rosbridge / foxglove_bridge，命令一律经 Mini → Pi 代理。
- 驱动运行时**不要对雷达串口（`/dev/ugv_lidar`，即 ttyUSB0）执行 `stty`**，会把容器里的驱动弄坏。
- ttyACM1 是底盘 ESP32 的 USB 串口，绕过 Pi 代理的保护；建议的 root-only udev 规则用户尚未批准，不要擅自加或用它控制车。
- 运动参数（速度、停车距离、半径）放宽或收紧都要用户批准，并在配置注释里写明日期和理由。
- 旧机器人服务和旧容器 `ai-home-nav` 保持停用，不作为失败时的降级方案。

## 隐私

- 地图（`room-*.pgm/yaml`）就是家里平面布局，只存在 Pi 的数据目录 `/home/ws/agent-data/ai-ugv-car/maps/`（巡航报告在同级 `reports/`；数据与应用分开，不放进 `deploy/`），**不进 Git、不上传**，除非用户同意。
- 不把 IP、token、密码、证书写进仓库；只写「去哪个私有文件找」。

## 硬件要点

- LiDAR（LD19/LD06 类，230400 baud，约 10 Hz）在 ttyUSB0 → `/dev/ugv_lidar`；麦克风已换成杰理 USB 麦克风（ALSA `ugvmic`，只录音、48 kHz；原讯飞 M2 和 `/dev/m2_mic` 已拆除），扬声器 ALSA `ugvspk`；前置 Orbbec 初代 Gemini 的彩色相机是普通 UVC（`agent.json` 的 `front_camera`，by-id `usb-Sonix_Technology_Co.__Ltd._USB_2.0_Camera_AY27552002R`，每帧在 EOI 后补 3～5 个零字节），深度要用 OpenNI2 驱动、尚未接入；底盘走 Pi 的 `/dev/ttyAMA0`，由 Pi 代理独占。
- ESP32 是较旧的 ugv_base_ros 固件：`T1` 是 m/s 目标速度 + PID（死区 PWM 23）；遥测 T1001 的 `odl/odr` 单位是米，`v` 是电池电压（伏）；没有电流和电量百分比。
- 已知未解决：低矮障碍卡车的「轮转车不动」检测、巡航末段位姿跳变、电量估算（见 TODO）。
