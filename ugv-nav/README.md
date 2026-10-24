# ugv-nav · 老三 ROS2 容器

ROS2 Humble 容器，负责激光雷达、TF，以及后续阶段的 SLAM 与 Nav2。容器只映射雷达，**不接触** `/dev/ttyAMA0`、M2 或声卡，运动始终经过 `pi-agent`。设计与验收记录见 [ROS2 建图与自主导航设计](../docs/ros-navigation-design.md)。

当前阶段：**N2/N3**。三个 launch 逐层叠加：
- `n0_lidar`：雷达 + URDF/TF。
- `n1_odometry`：加 Pi 代理桥（`ugv-board-bridge`）、RF2O 激光里程计和 EKF。
- `n3_navigation`：加 SLAM Toolbox 建图或 AMCL 定位，以及 Nav2。

用哪一层、传什么参数，由 `ugv-navctl` 写入 compose 目录的 `.env`。车辆能否运动仍由 Pi 代理的 `arm.json` 决定；容器只读挂载这个目录，只有主机上的 `ugv-navctl` 能写它。

| 路径 | 内容 |
| --- | --- |
| `Dockerfile` | 基于 `ros:humble-ros-base-jammy`，从 `ugv_ws@9d2e894` 稀疏检出 `ldlidar`、`ugv_description` 并编译 |
| `launch/` | ROS launch 文件：`n0_lidar`（雷达 + TF）、`n1_odometry`（加桥、RF2O、EKF） |
| `config/` | EKF、SLAM Toolbox、Nav2 参数和行为树（10-13 实车验收版，只改了路径） |
| `src/ugv_nav_tools/` | 本项目的 Python 工具包，以 wheel 形式装进镜像；纯逻辑与 rclpy 部分分开，纯逻辑可在开发机测试 |
| `tests/` | 由仓库根目录的 `uv run pytest` 运行 |
| `udev/99-ugv-car.rules` | `/dev/ugv_lidar` 稳定名称 |
| `compose.yaml` | host 网络、`ROS_LOCALHOST_ONLY=1`、只读根文件系统、不自动重启 |

## 构建与部署

在开发机的仓库根目录执行：

```sh
uv run ruff check . && uv run mypy && uv run pytest
uv build --package ai-ugv-car-nav-tools --wheel --out-dir ugv-nav/dist
rsync -a --delete --exclude __pycache__ --exclude '*.egg-info' --exclude .env ugv-nav/ agent003:/home/ws/deploy/ugv-nav/src/
```

在 Pi（`ssh agent003`）上执行：

```sh
cd /home/ws/deploy/ugv-nav/src
sudo docker build -t ai-ugv-car/ugv-nav:0.3.0 . > ../build-0.3.0.log 2>&1
sudo docker compose up -d
sudo docker exec ugv-nav bash -c "source /opt/ugv_ws/install/setup.bash && ugv-scan-report --seconds 10"
```

首次部署时安装 udev 规则：

```sh
sudo install -m 0644 udev/99-ugv-car.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty
```

停止：`sudo docker compose down`。升级时提升镜像版本号（`UGV_NAV_VERSION`），保留上一版镜像用于回滚。

主机端操作工具装在 Pi 的独立 venv 中：

```sh
uv pip install --python /home/ws/deploy/ugv-nav/.venv/bin/python --reinstall dist/ai_ugv_car_nav_tools-*.whl
```

## 建图、导航与巡航（`ugv-navctl`）

在 Pi 上执行，命令为 `/home/ws/deploy/ugv-nav/.venv/bin/ugv-navctl`。坐标单位是米，角度是弧度，都在地图坐标系中。

| 命令 | 作用 | 车会动吗 |
| --- | --- | --- |
| `status` | 底盘遥测、急停、导航是否占用底盘 | 否 |
| `mapping` | 切换到建图模式（SLAM + Nav2） | 否；用主控台圆盘手动低速驾驶 |
| `save NAME` | 保存当前地图到 `maps/NAME.yaml` | 否 |
| `localize NAME` | 在已保存的地图上定位（AMCL + Nav2） | 否 |
| `pose X Y YAW` | 设置初始位姿 | 否 |
| `where` | 当前地图坐标 | 否 |
| `goal X Y YAW` | 先规划；成功后在 ≤120 s 的授权窗口内导航到目标 | **会** |
| `patrol ROUTE.json [--loops N]` | 按航点逐段导航，每段单独授权，任一段失败即停止；报告写入 `reports/` | **会** |
| `stop` | 撤销授权、停车、取消导航 | 停车 |
| `lidar on\|off` | 雷达 USB 口（VL817 集线器 `1-1` 的 1 号口）供电；`off` 让雷达电机停转。`mapping` / `localize` 会先自动上电并等 `/dev/ugv_lidar` 出现 | 否 |

主机服务 `ugv-navd` 除地图页的操作外还接受 Pi 代理的 `standby`（休眠：容器在跑则 `compose stop`，再切断雷达供电；正在建图或有任务在跑时拒绝）和 `wake`（立即上电），`status` 里的 `lidar` 字段是它最近一次设置的状态（`on` / `off` / `missing` / `unknown`）。需要 Pi 上装 `uhubctl`（`sudo apt install uhubctl`，2026-10-25 已装），ws 用户免密 sudo。

巡航路线文件示例：

```json
{"loops": 1, "waypoints": [{"x": 0.5, "y": 0.0, "yaw": 0.0}, {"x": 0.0, "y": 0.0, "yaw": 3.14}]}
```

最多 20 个航点、10 圈。速度上限 0.06 m/s、0.25 rad/s，不倒车。到点判定：Nav2 返回成功，且误差在 7.5 cm、0.18 rad 以内。

## 注意

- 驱动运行时**不要**对 `/dev/ugv_lidar` 执行 `stty`。串口参数对所有打开它的进程共享，会让驱动读到乱码。
- Ubuntu 22.04 自带的 pip 22.0 从源码安装本包时会把包名识别成 `UNKNOWN`，所以镜像只安装由 `uv build` 预先构建的 wheel。
- 容器内是 Python 3.10，`ugv_nav_tools` 的 `requires-python` 为 `>=3.10`，ruff 的目标版本也是 py310。
