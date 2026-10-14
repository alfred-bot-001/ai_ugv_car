# 老三 ROS2 建图与自主导航设计

版本：0.2；日期：2026-10-14；状态：N0 已完成，N1 起待实现。本文是 [主控台技术设计](control-console-design.md) 第 1 节“自动驾驶、模型自主运动……放到后续独立设计”的那份独立设计。开发前先读 [架构文档](ARCHITECTURE.md)。

## 1. 结论

- 老三硬件满足 **2D 激光建图与 Nav2 自主导航**：Raspberry Pi 5 16GB、UGV Rover 六轮底盘、ROS 系列下位机固件（带轮式里程和 IMU 原始数据）以及一台正在工作的 LD19/LD06 类 2D 激光雷达。
- 没有 OAK-D Lite 等深度相机，**不做 RTAB-Map 3D 建图**。
- 厂家 `ugv_ws` 的 `ugv_bringup` 直接独占 `/dev/ttyAMA0`，没有租约、截止期限和失联停止，并依赖 3 秒的固件默认心跳。它违反架构约束 2、3，**不在老三上运行**。只复用厂家工作区里的激光驱动、激光里程计、EKF、SLAM、Nav2 参数和 URDF。
- 新增 Pi 本地的 **ROS 桥**：ROS 栈运行在独立容器中，`/cmd_vel` 和遥测都经过 `pi-agent` 中转。`pi-agent` 仍是 UART 的唯一所有者，也是运动的最终裁决者。浏览器和 Mini 只能下达高层任务（开始建图、保存地图、设定初始位姿、导航目标、取消），不能直接产生轮速。

## 2. 实机核查（2026-10-14）

所有检查都是只读的。探测 ESP32 时短暂停止了 `ai-tv-pet-agent.service`（约 6 秒），探测完立即启动；之后 `/health` 显示 `agent_online: true`。全程没有发送任何运动或写配置指令。

### 2.1 主机与外设

| 项目 | 结果 | 依据 |
| --- | --- | --- |
| 主机 | Raspberry Pi 5 Model B Rev 1.1，主机名 `ugvrpi` | `/proc/device-tree/model` |
| 底盘 | UGV Rover，六轮 4WD（用户确认） | 开机任务 `{"T":900,"main":2,"module":0}`，`main=2` 即 Rover |
| ESP32 运动板 | GPIO UART `/dev/ttyAMA0`，115200 | 由 `pi-agent` 独占 |
| **2D 激光雷达** | `/dev/ttyUSB0`，CP2102（`usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001`），230400 波特率 | 被动读 1 秒：19581 字节，416 个 `0x54 0x2C` 帧头，转速字段约 3566°/s ≈ 9.9 Hz。数据格式与 LD19/LD06 一致；具体型号按 Waveshare D500（LD19）配置，并在 N0 阶段确认 |
| M2 麦克风串口 | `/dev/ttyACM0`（WCH，`/dev/m2_mic`） | 由 `pi-agent` 使用 |
| 未知 CDC 串口 | `/dev/ttyACM1`（`1a86_USB_Single_Serial_56F2035721`） | 三种波特率下 1 秒都没有数据，用途待查 |
| 摄像头 | Realtek USB Camera，`/dev/video0` | 不是深度相机 |
| ROS / Docker | 主机没有 ROS2；Docker 29.9（`ws` 不在 docker 组，用 sudo）；已有 2026-10-13 的导航镜像与容器，见 §2.4 | `docker images`、`docker ps -a` |

注意：厂家默认雷达在 `/dev/ttyACM0`，这和我们的 M2 冲突。另外现有 udev 规则把 `/dev/xunfei_mic` 同时链接到了三个串口设备，属于规则错误，需要在 N0 修正。设备一律用 by-id 稳定名称，不能取 `/dev/ttyACM*` 的第一个匹配项（[主控台设计 §10.2](control-console-design.md)）。

### 2.2 ESP32 固件与遥测

探测指令为 `T130`×3、`T126`、`T139`、`T302`、`T405`、`T602`，都是只读查询。

| 查询 | 回复 |
| --- | --- |
| `T130` 基础遥测 | `{"T":1001,"L":0,"R":0,"gx":0,"gy":0,"gz":0,"ax":-0.1679,"ay":0.0204,"az":-0.1280,"mx":79.51,"my":30.99,"mz":88.87,"odl":-0.3492,"odr":-0.1527,"v":12.17,"pan":-28.13,"tilt":3.43}` |
| `T126` IMU | `{"T":1002,"temp":46.40}` |
| `T139` 速度系数 | `{"T":139,"L":1,"R":1}` |
| `T302` MAC | `24:DC:C3:C6:16:6C` |
| `T405` WiFi | `{"ip":"0.0.0.0","rssi":0}`，ESP32 WiFi 未连接 |
| `T602` 开机任务 | 只有 `{"T":900,"main":2,"module":0}` |

判断：

1. 字段集合（`gx..mz`、`odl/odr`，没有 `r/p/y/temp`）属于 **`ugv_base_ros` 系列**，不是主控台设计 §2.4 记录的 `ugv_base_general`。§2.4 的参考固件需要据此更正。
2. 这是一个**较旧的 ROS 固件**。厂家当前版本 `ugv_base_ros@2e7df97`（2025-11-28）把 `odl/odr` 输出为整数厘米（`en_odom*100`），把 `v` 输出为整数 ×100；车上输出的是浮点数（`odl=-0.349`、`v=12.17`）。厂家 `ugv_ws` 的 `odom_publisher` 会把 `odl/100` 当作米，套用到我们的固件上单位可能错 100 倍。2026-10-13 实测（§2.4）确认 `odl/odr` 单位是**米**、`v` 是伏，**不能**套用厂家 `/100` 换算；N1 仍需复核。
3. `ax/ay/az` 的合模约为 0.21，不像以 g 为单位的静止重力，也不像厂家 ROS 驱动假定的 `/8192` LSB 原始值；`gx/gy/gz` 恒为 0。**IMU 暂不进入 EKF**，等 N1 标定或升级固件后再评估。
4. 开机任务是 `module=0`，云台反馈是因为 `pi-agent` 每次启动都发送 `T4 cmd=2` 才有的。这个行为保持不变。
5. 固件的默认心跳 `HEART_BEAT_DELAY=3000ms`，`pi-agent` 启动时用 `T136=300` 覆盖，实测失联停止约 0.463 秒。导航模式必须保持这一点。
6. Rover（`mainType=2`）的厂家参数：轮径 `WHEEL_D=0.080 m`，`ONE_CIRCLE_PLUSES=660`，`TRACK_WIDTH=0.172 m`。`T13` 在固件内换算为 `L = X − Z·0.172/2`、`R = X + Z·0.172/2`，再进入速度 PID。

### 2.4 2026-10-13 的前期实车适配（已停用，保留为证据）

2026-10-13 曾在车上部署过一套适配，目录为 `/home/ws/deploy/ros2-nav`、`ros2-lidar`，容器名 `ai-home-nav`，并完成了短距离受监督验收。完整记录见该目录的 `README.md` 和 `reports/acceptance-20261013/`。用户确认这套已不再需要。2026-10-14 已将它停止并设为不自动重启（`restart=no`），镜像、目录、地图和报告全部保留，可以回退。本设计吸收的实测结论：

| 项目 | 2026-10-13 实测 | 对本设计的影响 |
| --- | --- | --- |
| 里程/电压单位 | `odl/odr` 为米、`v` 为伏 | 不用厂家 `/100` 换算 |
| `T1` 语义 | 实车上是 PWM 标量，不是 m/s；当时在桥里加了基于轮速反馈的 PI 闭环，输出限幅 ±0.08 | N1 比较 `T1`+PI 与 `T13` 两种方案后再定 |
| 转向 | 轮式与激光转角估计严重不一致；转向需要 2.0 倍补偿克服静摩擦 | EKF 的 yaw 只用 rf2o；轮式里程只用于 x 方向速度 |
| 雷达安装 | 相对车身中心 x=0、y=0、z=0.16 m，偏航 +90°；遮挡区 44°～136°（雷达坐标） | 厂家 URDF 是 x=0.032、z=0.121（相对 footprint），N1 实测后修正 |
| 停止 | 停发指令后约 0.52 秒静止 | 和 `T136=300` 加 Pi 看门狗的结果一致 |
| 导航 | AMCL + Nav2，0.06 m/s、0.25 rad/s，禁止倒车；18 cm 目标到点误差 4.39 cm | 证明链路可行，但只是短距离验收 |
| 跨容器 DDS | 雷达和导航分在两个容器时出现通信问题 | ugv-nav 采用单容器 |

**代码漂移（2026-10-14 已解决，Pi 0.4.0 的 `tvpet_pi/nav.py`，见[部署记录](control-console-deployment.md)）**：当时直接修改了车上已安装的 Pi 代理（`site-packages/tvpet_pi/`），新增 `nav_socket.py`（UNIX 套接字 `ai-tv-pet-agent/nav/board.sock`，默认只读遥测；运动需要 0600 权限的 `arm.json` 授权，最长 120 秒，并经过原有门禁），还改了 `hardware.py`（导航观察时 50ms 轮询，云台反馈允许 0.5° 误差），但版本号仍是 0.3.1。这些改动**不在本仓库中**，如果从仓库重新部署 Pi 代理，它们会被覆盖。N1 的第一步是把它们纳入仓库，补上测试，升为 0.4.0 再正式部署（§5.2 的桥就以它为基础）。

## 3. 厂家 ROS2 工作区（参考）

仓库 [`waveshareteam/ugv_ws`](https://github.com/waveshareteam/ugv_ws)，参考分支 `ros2-humble-develop-251125`，提交 `9d2e894`（2026-09-03）。环境是 ROS2 Humble / Ubuntu 22.04，出厂镜像是 Docker（容器 SSH 端口 23，`root/ws`）。

| 包 | 用途 | 本设计 |
| --- | --- | --- |
| `ldlidar` | LD06/LD19/STL27L → `/scan` | **复用**，端口改为雷达的 by-id 路径 |
| `rf2o_laser_odometry` | 激光扫描匹配里程 `odom_rf2o` | **复用** |
| `robot_localization`（EKF） | 融合成 `/odom`，配置见 `ugv_bringup/config/ekf.yaml` | **复用**参数，按 N1 标定结果调整 |
| `ugv_description` | URDF，`UGV_MODEL=ugv_rover`；TF 链为 `map→odom→base_footprint→base_link→base_lidar_link` | **复用**，核对雷达安装位姿 |
| `ugv_slam` | SLAM Toolbox / Cartographer / Gmapping；RTAB-Map 需要 OAK-D | **复用 SLAM Toolbox** |
| `ugv_nav` | Nav2：AMCL/EMCL/Cartographer/SLAM Toolbox 定位；DWA/TEB/RPP/MPPI 规划；禁行区、限速区 | **复用** AMCL + DWA 起步 |
| `explore_lite` | 边建图边自动探索 | 后续阶段 |
| `ugv_bringup` | 串口桥：`/cmd_vel`→`T13`，开启 `T131` 遥测流，发布 IMU/电压 | **不使用**，由本项目的 ROS 桥替代 |
| `ugv_web_app` / `ugv_chat_ai` / `ugv_vision` / `ugv_voice` / rosbridge | Web、AI、视觉、语音 | **不使用**；页面和媒体由主控台负责 |

出厂镜像的容器 SSH 使用固定弱口令，Jupyter 也没有 token，因此不使用出厂镜像，改为自建镜像（§5.1）。

## 4. 与现有架构的冲突

| 约束（[ARCHITECTURE](ARCHITECTURE.md)） | 厂家做法 | 本设计 |
| --- | --- | --- |
| 2. UART 只有一个明确所有者 | `ugv_bringup` 打开 `/dev/ttyAMA0` | ROS 容器不挂载 `/dev/ttyAMA0`，运动只经过 `pi-agent` |
| 3. 网页和模型请求不直接进入 UART；Pi 验证租约、deadline，失联自行停止 | `/cmd_vel` 收到就写串口；没有租约或截止期限；依赖 3 秒心跳 | `pi-agent` 新增导航租约和逐条 deadline，`T136=300` 保持不变 |
| 4. 已验收的运动资格和停止机制 | 没有 | 导航继承现有门禁；手动急停仍由用户解除 |
| 6. 媒体与数据最小化 | 不涉及 | 地图仅存 Pi/Mini 私有目录，不上传第三方 |
| 9. 边缘推理用独立 worker 和有界资源 | 单进程 | ROS 容器限制 CPU/内存，低于停止链路的优先级 |

## 5. 目标架构

```text
浏览器 ─HTTPS→ Mini robot-console
                 ├─ 地图管理：列表/查看/重命名/删除，地图图层 + 机器人位姿 + 路径
                 ├─ 任务：开始建图 / 保存地图 / 初始位姿 / 导航目标 / 取消 / 急停
                 └─ 控制 WSS（既有）── nav.* 消息 ──▶ 老三 pi-agent（唯一 UART 所有者）
                                                       ├─ 导航租约、速度上限、逐条 deadline
                                                       ├─ T13 / T1 / T130/T131 / T136 ──▶ ESP32
                                                       └─ 本地 UNIX 套接字（0600）
                                                              ▲   │ 遥测 20Hz、状态
                                                   cmd_vel、   │   ▼
                                                   任务结果   ugv-nav 容器（ROS2 Humble）
                                                              ├─ ugv_agent_bridge（rclpy）
                                                              │    /odom_wheel、/ugv/voltage ↔ /cmd_vel
                                                              ├─ ldlidar → /scan（独占雷达串口）
                                                              ├─ rf2o + EKF → /odom
                                                              ├─ slam_toolbox（建图）
                                                              └─ Nav2：AMCL + DWA（导航）
```

### 5.1 ugv-nav 容器

- 基础镜像是 `ros:humble-ros-base`（arm64）。只构建 §3 中标注“复用”的包，固定 `ugv_ws` 提交与 apt 包版本，镜像标签带版本号，并保留上一版用于回滚。
- `network_mode: host`，设置 `ROS_LOCALHOST_ONLY=1` 和固定的 `ROS_DOMAIN_ID`，DDS 流量不出本机。不安装 rosbridge，不开放任何端口，也不开 SSH 或 Jupyter。
- 只挂载两样设备：雷达 by-id 路径（经 udev 生成 `/dev/ugv_lidar`）和 `pi-agent` 的 UNIX 套接字。**不挂载** `/dev/ttyAMA0`、摄像头、M2 或声卡。
- 由 systemd 单元 `ugv-nav.service` 管理，默认不自启；只有 `pi-agent` 收到 Mini 的建图或导航任务时才启动，空闲 N 分钟后停止。设置 CPU、内存配额和 `Nice`，预算见 §8。
- 地图存放在 `/home/ws/deploy/ugv-nav/maps/<map_id>/`（`map.yaml`、`map.pgm`、`map.posegraph`、`meta.json`），目录 0700。

### 5.2 pi-agent 的 ROS 桥职责

`pi-agent` 新增 `nav` 适配器，通过本地 UNIX 套接字（长度前缀 JSON）与容器内的 `ugv_agent_bridge` 通信。

**遥测 → ROS**
- 导航或建图期间用 `T131 cmd=1` 加 `T142` 设置 50ms 的遥测流，平时恢复为按需 `T130`。每条遥测带上 Pi 的单调时间戳，转发给桥。
- 桥发布 `odom_wheel`：用 `odl/odr` 的增量和 `TRACK_WIDTH=0.172` 计算差速里程，单位采用 N1 的标定结果。同时发布电压。IMU 在 §2.2 第 3 点解决前不发布到 EKF。

**ROS → 运动**
- 桥把 `/cmd_vel` 的 `linear.x` 和 `angular.z` 连同序号和发送时间转给 `pi-agent`。
- `pi-agent` 只在有效**导航租约**内接受这些指令。租约由 Mini 的显式任务创建，与手动圆盘租约互斥：同一时刻只能有一个运动来源。
- 限幅：初期 |v| ≤ 0.20 m/s，|ω| ≤ 0.8 rad/s，并限制加速度（v 0.5 m/s²，ω 2.0 rad/s²）。上限放在 Pi 配置里，网页不能修改。
- 每条指令的 deadline 为 300ms。超时、序号回退、桥断开、容器退出、遥测超过 300ms 未更新、`/scan` 心跳超过 500ms 未更新（由桥上报，`pi-agent` 校验）、Mini 控制 WSS 断开或租约到期，都会立即发零速，并让导航进入 `paused`。
- 下发用 `T13`（m/s、rad/s，由固件 PID 闭环）。N1 阶段先在架空状态下核对 `T13` 的实际轮速，地面验收通过之前导航不开放。`T136=300` 不变，作为最后一道防线。
- 急停、手动急停锁和运动资格门禁全部沿用现有实现，导航不能解除急停。

**任务**
- `pi-agent` 把 Mini 的 `nav.start_mapping`、`nav.save_map`、`nav.load_map`、`nav.set_initial_pose`、`nav.goal`、`nav.cancel` 转成桥的 ROS 调用：调用 slam_toolbox 的 `serialize_map`，或使用 Nav2 的 `NavigateToPose` action。结果和进度（剩余距离、恢复行为、失败原因）回报给 Mini。

### 5.3 Mini 主控台

- 新页面“地图与导航”，复用 admin 会话、CSRF 和 Origin 校验。地图图片走私有 API，不挂载静态目录。
- 建图模式：操作者用现有底盘圆盘低速遥控，页面实时显示正在生成的栅格地图和机器人位姿，完成后保存并命名。
- 导航模式：选择地图，在图上点选初始位姿和目标；可以取消；页面显示路径、状态和失败原因。急停按钮始终可见。
- 地图元数据（名称、创建时间、所用参数、标定版本）保存在 Mini SQLite；栅格文件以 Pi 为准，Mini 缓存只读副本用于显示。
- 状态必须注明来源和新鲜度（ARCHITECTURE 约束 7）：“已发出目标”、“Nav2 已接受”、“已到达（Nav2 报告）”、“现场确认”是四种不同的证据。

## 6. 协议草案

Mini ↔ Pi 沿用现有控制 WSS，新增 `nav.*` 消息：

| 方向 | 消息 | 字段 |
| --- | --- | --- |
| Mini→Pi | `nav.lease` | `mode: mapping\|navigation`、`map_id?`，返回 `lease_id` |
| Mini→Pi | `nav.start_mapping` / `nav.save_map` | `lease_id`、`map_id`、`name` |
| Mini→Pi | `nav.set_initial_pose` / `nav.goal` | `lease_id`、`map_id`、`x`、`y`、`yaw`（map 坐标系） |
| Mini→Pi | `nav.cancel` / `nav.release` | `lease_id` |
| Pi→Mini | `nav.state` | `state: idle\|mapping\|localizing\|navigating\|paused\|failed`、`pose`、`pose_age_ms`、`scan_age_ms`、`odom_age_ms`、`reason` |
| Pi→Mini | `nav.map_update` | 降采样的 PNG 栅格、分辨率、原点；建图期间最高 1 Hz |

Pi ↔ 容器的 UNIX 套接字消息：`telemetry`、`cmd_vel`、`heartbeat{scan_age_ms, tf_ok}`、`task`、`task_result`。套接字权限 0600，属主 `ws`。

## 7. 分阶段计划与验收

| 阶段 | 内容 | 验收 | 是否运动 |
| --- | --- | --- | --- |
| **N0 雷达与容器** | 修正 udev（`/dev/ugv_lidar`、`/dev/m2_mic` 唯一映射）；构建 ugv-nav 镜像；只运行 `ldlidar` + `robot_state_publisher` | `/scan` 稳定约 10 Hz；RViz（开发机）中扫描方向与车头一致；确定 LD19 或 LD06；`pi-agent` 与 M2 不受影响 | 否 |
| **N1 里程与速度标定** | `pi-agent` 遥测流、ROS 桥、`odom_wheel`；架空核对 `T13` 轮速；地面推车或低速直线 1 m、原地旋转 360° | `odl/odr` 单位确定，直线误差 ≤ 3%、旋转误差 ≤ 5°；`T13` 实测速度与指令偏差有记录；IMU 是否可用的结论 | 架空，然后地面低速（需用户在场） |
| **N2 建图** | 主控台遥控 + slam_toolbox；保存、查看、删除地图 | 家中一个闭环区域建图成功，墙体不重影；地图只能在登录后访问 | 地面手动低速 |
| **N3 定位与导航** | AMCL + DWA；导航租约；限速 0.20 m/s | 5 次以上点到点到达；取消、急停、关浏览器、断 Mini 网络、杀掉容器、拔雷达、遥测中断时都能在 0.5 秒内停车；与手动控制互斥 | 地面自主（需用户在场并清场） |
| **N4 扩展** | 禁行区和限速区、`explore_lite` 自动探索、多地图、定时巡航、与视觉/对话联动 | 分项另行设计 | — |

### 7.1 N0 实施记录（2026-10-14）

- udev：新增 `/etc/udev/rules.d/99-ugv-car.rules`（`/dev/ugv_lidar → ttyUSB0`，按 CP2102 的 VID/PID/序列号匹配）。错误的 `99-xunfei-mic.rules` 改名为 `.disabled-20261014`（它把雷达和所有 ttyACM 都链接成 `/dev/xunfei_mic`，经确认没有程序使用）。`/dev/m2_mic` 不变。
- 镜像 `ai-ugv-car/ugv-nav:0.1.0`：基于 `ros:humble-ros-base-jammy`，从 `ugv_ws@9d2e894` 稀疏检出 `ldlidar` 和 `ugv_description` 并编译；本项目的 `ai-ugv-car-nav-tools` 以 wheel 形式安装。源码在 [`ugv-nav/`](../ugv-nav/)，部署目录 `/home/ws/deploy/ugv-nav/`。
- 容器 `ugv-nav`：host 网络、`ROS_LOCALHOST_ONLY=1`、`ROS_DOMAIN_ID=42`，只读根文件系统，只映射 `/dev/ugv_lidar`，加入 dialout 组；`restart: "no"`。
- 验收（`ugv-scan-report --seconds 10`）：**10.0 Hz**，每圈 480 点，436 个有效；驱动报告 `Actual BaudRate 230400`、通信正常；健康检查 healthy；CPU 约 1.4%。
- 遮挡：雷达坐标 **45°～150°** 大多是车体近距离回波或无效点，和 10-13 的 44°～136° 一致；换算到车身坐标（+90°）为 135°～240°，即车尾。
- TF：`base_footprint → base_lidar_link` = (0.032, −0.001, 0.121)，偏航 1.571，来自厂家 URDF；与 10-13 实测的 z=0.16 不同，N1 修正。
- 不受影响：`pi-agent` 仍独占 `ttyAMA0`、`ttyACM0`，主控台 `agent_online: true`。
- 型号：LD19 与 LD06 协议相同、数据上无法区分，按 Waveshare D500 记为 LD19；驱动以 `LDLiDAR_LD19` 运行正常。
- 遗留：`ttyACM1`（CH343，序列号 56F2035721）没有输出、没有程序使用，用途待现场确认，在此之前任何服务都不打开它。Pi 的内核没有启用 memory cgroup（`docker stats` 显示 0B），`mem_limit` 实际上不生效；N3 前评估是否在 `cmdline.txt` 启用 `cgroup_enable=memory`（需要重启）。
- 运维注意：雷达串口参数（`stty`）对所有打开它的进程共享。2026-10-14 排查时曾用 `stty` 改波特率，导致当时运行的 `ai-home-nav` 读到乱码，恢复 230400 后正常。**不要在驱动运行时对 `/dev/ugv_lidar` 执行 `stty`**。

### 7.2 N1 实施记录（2026-10-14，静止部分）

- **Pi 代理 0.4.0**：车上热改的导航桥正式纳入为 `tvpet_pi/nav.py`，见 §2.4 和[部署记录](control-console-deployment.md)。
- **ugv-nav 0.2.0**（`ai-ugv-car-nav-tools` 0.2.0）：
  - `board_client`：Pi 代理套接字客户端。
  - `wheel_odometry`：`odl/odr` 按米积分，单次跳变超过 0.5 m 的样本丢弃；只作诊断，不进 EKF。
  - `motion_gate`：移植自 10-13 桥并补测试。激光里程或扫描超过 0.35 s 未更新就停车；前方 ±45° 有效射线少于 10 条也停车；前方 0.40 m 内只允许地图确认四周空旷后、≤0.15 rad/s 的原地转向。
  - `bridge_node`：rclpy 节点。
  - 共 13 项新测试。
- 镜像新增 `rf2o_laser_odometry`（来自 `ugv_ws@9d2e894`）和 `robot_localization`。EKF 采用 10-13 验收过的配置（[`ugv-nav/config/ekf_laser.yaml`](../ugv-nav/config/ekf_laser.yaml)），只融合 RF2O 的 x/y/yaw，并独占 `odom → base_footprint` 这一 TF。
- 驱动角度裁剪：设备角度 208°～318°，对应 ROS 42°～152°。车尾车体回波改为 NaN，不再进入 RF2O。有效点从 436 降到 312。
- 容器以只读方式挂载 `ai-tv-pet-agent/nav`：可以连接 `board.sock`、读取 `arm.json`，但**不能写 arm.json**，因此容器无法自行授权运动。
- 静止实测：`/scan` 10.0 Hz、`/odom_rf2o` 10.0 Hz、`/odom` 20.0 Hz、`/odom_wheel` 和 `/ugv/voltage` 约 17 Hz。健康检查 healthy，容器 CPU 约 12%。静止 30 s 漂移：EKF 位置 0.39 cm、yaw +0.77°（约 1.5°/min）；轮式里程为 0。
- 尚未进行：任何运动。下一步是标定（`T1`+PI 实际速度、`odl/odr` 距离、转向），需要用户在场、清场，先架空，地面部分须先完成 C3 地面制动验收。

### 7.3 N2/N3 准备（2026-10-14，未运动）

- ugv-nav 0.3.0：镜像新增 SLAM Toolbox 和 Nav2（Humble apt 包）。[`config/`](../ugv-nav/config/) 收入 10-13 验收过的 `slam.yaml`、`nav.yaml`（RotationShim + DWB，0.06 m/s、0.25 rad/s，不倒车，`allow_unknown=false`）以及行为树；只改了路径，并去掉了未使用的 keepout/speed 过滤服务。launch 为 [`n3_navigation`](../ugv-nav/launch/n3_navigation.launch.py)，任一组件退出则整栈关闭。
- `ugv-ros-op`（容器内）：`pose`、`set-pose`、`cancel`、`navigate`。`navigate` 先规划，规划失败就不会发导航目标；结束后总是向 Pi 代理发 stop。
- `ugv-navctl`（Pi 主机 venv，`/home/ws/deploy/ugv-nav/.venv`）：唯一写 `arm.json` 的组件。每个目标只开一个 ≤120 s 的授权窗口，`finally` 中一定撤销授权并停车；巡航按航点逐段授权，任一段失败即停止，报告写入 `reports/patrol-*.json`。路线文件在授权前校验（≤20 点、≤10 圈、坐标有限）。共 20 项新测试。
- 实机（未运动）：`ugv-navctl mapping` 后 SLAM 注册雷达，Nav2 显示 `Managed nodes are active`，健康检查 healthy。`/map` 1 Hz（55×69 格，5 cm）；`where` 返回 (−0.02, 0.00, −0.03)；前方 0.25 m 规划成功（状态 4，10 个路径点），只规划不运动。容器 CPU 约 49%（4 核合计），负载 1.5。`arm.json` 始终为 `enabled:false`。

### 7.4 首次自主运动与巡航（2026-10-14 夜，用户在场并批准）

用户选择由系统驾驶建图，并批准放宽参数：“旋转和探测只要不要碰到雷达检测到的边缘就可以，还可以快一点”。

| 版本 | 变更 |
| --- | --- |
| Pi 代理 0.4.1 | 导航限速 0.12 m/s / 0.6 rad/s（Nav2 实际用 0.10 / 0.5）；导航轮速上限 0.08 → 0.18；`RUNAWAY_SPEED` 0.2 → 0.3（失控急停阈值）；网页圆盘仍为 0.10（有测试保证） |
| ugv-nav 0.3.1 | 车体轮廓：0.50×0.40 m 矩形 → 厂家半径 0.15 m + 2 cm = `robot_radius 0.17`；膨胀 0.35 → 0.30；Nav2 0.06/0.25 → 0.10/0.5；桥前方停车距离 0.40 → 0.30，原地转向空间半径 0.36 → 0.25 |
| ugv-nav 0.3.2 | 导航目标回复丢失时取消后重发一次；`ugv-ros-op` 任何异常都输出 JSON 并停车；到点位置容差 7.5 → 10 cm（六轮原地转向会滑移几厘米） |
| ugv-nav 0.3.3 | Humble/FastDDS 刚发现服务就请求会丢回复：发现后先等 0.5 s；取消改为尽力而为；巡航时单段异常记为失败，不再崩溃 |
| ugv-nav 0.3.4 | `bt_navigator.default_server_timeout` 20 → 200 ms（负载下 `follow_path` 确认超时导致立即中止） |

实测（全部由 `ugv-navctl` 授权，每段 ≤120 s，结束都恢复为 `enabled:false`）：

- 原地转向：0.50×0.40 m 的旧轮廓在家具附近被判定为“旋转碰撞”；换成 0.17 m 半径后，4 个方向全部成功，每段 2～29 s，误差 ≤7 cm。
- 平移：东 1.0 m（24 s，3.3 cm）、北 1.4 m（54 s，3.3 cm）。
- 低矮障碍卡车：雷达扫描面以下的物体卡住了车。记录显示约 50 s 内左轮 0.06～0.19 m/s、右轮 0，车却不动，雷达前方 0.76 m 内为空；最后由 Nav2 进度检查（约 60 s）中止。用户移开障碍后继续。**需要新增“轮子在转、车不动”的快速停车检测。**
- 巡航 `route-patrol-1`（东 → 北 → 南 → 原点，约 4.5 m）：前 3 段成功（24 / 54 / 40 s，误差 6.5 / 4.1 / 0.5 cm）；第 4 段（0.4 m）期间位姿约 1 分钟跳变约 0.9 m，Nav2 判定没有进展而中止，疑似南侧障碍旁原地转向时 SLAM 匹配错误，待复现。地图保存为 `maps/room-20261014`，墙体无重影。报告在 `reports/patrol-20261014-235756.json`。

**前置条件**：TODO 中“C3 扩展验收：地面低速/机械制动距离”必须在 N1 地面部分之前完成。目前只有车轮架空的停止测量。

## 8. 资源与风险

| 项目 | 说明 | 处理 |
| --- | --- | --- |
| CPU | Pi 5 上已有 `pi-agent`、MediaPipe 姿态 worker 和视频采集；slam_toolbox + Nav2 + EKF + rf2o 估计约需 1～2 个核心 | 容器配额先设 2 核、1.5 GiB；导航时把姿态 worker 降到 2 FPS 或暂停；记录实测负载 |
| 里程单位 | 旧固件与厂家驱动的换算不一致（§2.2） | N1 标定；必要时评估升级到最新 `ugv_base_ros`，升级本身需单独批准和回滚方案 |
| IMU | 读数可疑，陀螺恒为 0 | EKF 先只用轮式里程 + rf2o |
| 六轮滑移 | Rover 原地转向时轮式里程漂移大 | EKF 的 yaw 以 rf2o 为主（厂家配置同样如此），DWA 限制角速度 |
| 低矮障碍 | 2D 雷达看不到低于扫描平面的物体 | 首期只在清场区域运行；以后考虑用摄像头补充或设置禁行区 |
| 未知串口 `ttyACM1` | 用途不明 | N0 查明；在此之前任何服务都不打开它 |
| 出厂镜像安全 | 弱口令 SSH、无 token 的 Jupyter | 不使用，自建镜像 |

## 9. 参考

| 名称 | 位置 | 版本 |
| --- | --- | --- |
| 厂家 ROS2 工作区 | https://github.com/waveshareteam/ugv_ws | `ros2-humble-develop-251125@9d2e894` |
| 厂家 ROS 下位机固件 | https://github.com/waveshareteam/ugv_base_ros | `2e7df97`（2025-11-28），车上固件更旧 |
| 厂家通用下位机固件 | `/Users/agent001/workspace-agent/projects/irobot/ugv_base_general/` | `d308df9` |
| 厂家 Pi 上位机 | https://github.com/waveshareteam/ugv_rpi | `3ae9f20` |
| Nav2 | https://navigation.ros.org/ | Humble |
