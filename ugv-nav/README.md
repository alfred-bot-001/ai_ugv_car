# ugv-nav · 老三 ROS2 容器

ROS2 Humble 容器，负责激光雷达、TF，以及后续阶段的 SLAM 与 Nav2。容器只映射雷达，**不接触** `/dev/ttyAMA0`、M2 或声卡，运动始终经过 `pi-agent`。设计与验收记录见 [ROS2 建图与自主导航设计](../docs/ros-navigation-design.md)。

当前阶段：**N0**，只运行雷达驱动、`robot_state_publisher` 和 `joint_state_publisher`。

| 路径 | 内容 |
| --- | --- |
| `Dockerfile` | 基于 `ros:humble-ros-base-jammy`，从 `ugv_ws@9d2e894` 稀疏检出 `ldlidar`、`ugv_description` 并编译 |
| `launch/` | ROS launch 文件（N0：`n0_lidar.launch.py`） |
| `src/ugv_nav_tools/` | 本项目的 Python 工具包，以 wheel 形式装进镜像；纯逻辑与 rclpy 部分分开，纯逻辑可在开发机测试 |
| `tests/` | 由仓库根目录的 `uv run pytest` 运行 |
| `udev/99-ugv-car.rules` | `/dev/ugv_lidar` 稳定名称 |
| `compose.yaml` | host 网络、`ROS_LOCALHOST_ONLY=1`、只读根文件系统、不自动重启 |

## 构建与部署

在开发机的仓库根目录执行：

```sh
uv run ruff check . && uv run mypy && uv run pytest
uv build --package ai-ugv-car-nav-tools --wheel --out-dir ugv-nav/dist
rsync -a --delete --exclude __pycache__ --exclude '*.egg-info' ugv-nav/ agent003:/home/ws/deploy/ugv-nav/src/
```

在 Pi（`ssh agent003`）上执行：

```sh
cd /home/ws/deploy/ugv-nav/src
sudo docker build -t ai-ugv-car/ugv-nav:0.1.0 . > ../build-0.1.0.log 2>&1
sudo docker compose up -d
sudo docker exec ugv-nav bash -c "source /opt/ugv_ws/install/setup.bash && ugv-scan-report --seconds 10"
```

首次部署时安装 udev 规则：

```sh
sudo install -m 0644 udev/99-ugv-car.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty
```

停止：`sudo docker compose down`。升级时提升镜像版本号（`UGV_NAV_VERSION`），保留上一版镜像用于回滚。

## 注意

- 驱动运行时**不要**对 `/dev/ugv_lidar` 执行 `stty`。串口参数对所有打开它的进程共享，会让驱动读到乱码。
- Ubuntu 22.04 自带的 pip 22.0 从源码安装本包时会把包名识别成 `UNKNOWN`，所以镜像只安装由 `uv build` 预先构建的 wheel。
- 容器内是 Python 3.10，`ugv_nav_tools` 的 `requires-python` 为 `>=3.10`，ruff 的目标版本也是 py310。
