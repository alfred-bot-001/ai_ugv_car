# AI TV Pet 主控台技术设计（Mini 核心 / Pi 设备与边缘端）

版本：0.4；日期：2026-10-01；状态：0.2.0 基础主控台已实现并完成双机联调；完整对话、边缘推理和电视联动待实现。系统分工已确认：老大 Mac Mini 是服务核心，电视宠物是展示端，老三负责动作、设备信息、音视频采集、边缘计算和对话交互；旧机器人服务保持停用。

本设计采用 Python 后端，开发规范以 `/Users/agent001/workspace-agent/docs/Python_project_standard.md` 为准。目标硬件是老三的 Waveshare 树莓派小车。页面依据已完成的 [Figma 设计](https://www.figma.com/design/gozn4SHHmdysmiHfiReJkE)，包含登录、主控台和修改密码。本文中的端口、超时和性能指标是实现约定或验收目标；硬件实测事实在第 2 节单独记录。

## 1. 功能与交付边界

| 功能 | 本期行为 | 验收依据 |
| --- | --- | --- |
| 管理员登录 | 固定用户名 `admin`；首次在老大 Mini 初始化密码；支持修改密码及退出 | 未登录不能读取设备详情、媒体或控制硬件；改密撤销全部会话 |
| 视频与拍照 | 实时画面、分辨率、帧率、画面年龄；拍照下载 | 真正的新帧，断流显示提示，不能把缓存画面标为实时 |
| 硬件状态 | 摄像头、M2 麦克风、扬声器、底盘、云台、CPU、内存、温度、磁盘、网络 | 每个值有来源、单位、时间和有效性；缺失显示“未知” |
| 云台控制 | 圆盘拖动上下左右，松手停止；不输入角度 | 根据当前位置做受限相对调整；释放、失焦、断线停止 |
| 底盘控制 | 圆盘拖动前后左右、差速转向、停止、急停；不输入角度 | 低速、有控制租约、有本地和下位机超时保护 |
| 听声与转文字 | 打开机器人麦克风，在网页听音；明确开始识别后显示转写 | 真实 M2 音频到网页及 ASR；停止采集后不继续发送 |
| 输入文字发声 | 输入中文，合成语音后通过机器人扬声器播放；可以停止 | 合成成功、播放进程完成、现场可闻分别验收 |
| 运维信息 | 分开显示 Mini 核心、Pi 设备端和电视展示端的版本、运行目录与健康 | 页面能解释某个功能为什么不可用 |
| 边缘计算 | Pi 可选 OpenCV / 本地小模型：快速物体识别、VAD、唤醒、简单即时回应 | 有推理时间、模型版本和有效性；不得阻塞停止链路 |
| 对话与交互 | Pi 负责本地交互状态、听取/播音及受限即时回应；Mini 编排完整对话与展示 | 同一 turn_id 关联采集、转写、回复、播放和电视展示，避免重复回应 |

网页为中文响应式页面，手机采用纵向卡片和至少 44px 的触控区域。Figma 中的演示数值由实际 API 替换。

本期以家庭局域网为运行环境。Mini 统一承载管理、任务和对话服务；电视宠物负责画面、字幕、角色动画及 Mini 下发的展示音频；老三承担现场感知和物理/语音交互。现有电视服务和机器人主控台在 Mini 上使用独立进程，逐步通过明确接口联动；本期不强行迁移既有电视账号与数据。

本地模型可以识别并输出受限交互意图，简单回应可在 Pi 完成；完整模型对话、长期记忆和任务编排由 Mini 决定。自动驾驶、模型自主运动、浏览器麦克风通话、公网媒体中继放到后续独立设计。

## 2. 已核实的现状

### 2.1 仓库现状

- `mini-server/` 是现有电视控制服务，运行要求 Python 3.9+，使用标准库 HTTP 服务、SQLite 和自己的管理员会话。
- `pi-agent/` 已有 `src/tvpet_pi/` 包，包含 `CameraPort`、`MicrophonePort`、`SpeakerPort`、`PiSessionController` 及测试。这些原有接口继续保留；新增真实硬件适配器、WSS 传输和本地停止保护，见部署文档。
- `PiSessionController` 要求明确开启会话才调用媒体端口；新功能延续这个边界，并增加超时、引用计数和断线清理。
- 运动接口由独立主控台和 Pi 租约协议提供；不能把电视 `/api/commands` 用作底盘控制。

### 2.2 停用前的老三实机基线（历史记录）

以下来自本次会话在 2026-10-01 的实机检查，其中服务、M2 枚举、串口占用及运行模式在编写文档时再次核对。采集结果仅证明对应检查通过。

| 项目 | 已核实 | 尚待核实 |
| --- | --- | --- |
| 主机 | Raspberry Pi 5 Model B Rev 1.1，内存约 16GB；已有温度、风扇和降频读取方式 | 新主控台的持续负载 |
| 摄像头 | USB Camera；`/dev/video0`；当前画面为 640×480；既有采集可取得有效 JPEG | 新媒体接口的逐帧时间戳及长期稳定性 |
| M2 串口 | `/dev/m2_mic` 当前指向 `ttyACM1`；115200；此前被动读取收到 6 个校验有效心跳 | 唤醒角度、唤醒事件及稳定性 |
| M2 音频 | `XFM-DP-V0.0.18`；`plughw:CARD=XFMDPV0018,DEV=0`；此前 16kHz 单声道采集 2 秒成功 | 采样音量很低；清晰拾音、转写效果尚未通过 |
| 当前录音设备 | 现有 companion 应用仍使用 `plughw:CARD=Camera,DEV=0` | M2 尚未成为该应用输入源 |
| 扬声器 | USB PnP Audio Device；候选 `plughw:CARD=Device,DEV=0` | 当前链路的实际可闻播放 |
| 运动串口 | `/dev/ttyAMA0` 当前被 `companion-motion` 独占；云台运行模式为 hardware | 实际角度反馈及运动验收 |
| 底盘 | 现有应用 `wheel_mode=simulation` | 电机方向、编码器、速度单位、机械尺寸及制动 |
| 应用异常 | `stop_latched=true`，原因 `stop_audio_overrun`；音频健康为 false；`hardware_ready=false` | 音频处理故障修复及完整硬件就绪条件 |
| ASR / TTS | 既有后端声明 resident Whisper、macOS say 能力 | 主控台的服务契约、授权、识别及出声完整链路 |

上表描述停用前的检查基线。随后用户明确要求“停掉现有的机器人服务，放弃不用了”，已在 2026-10-01 停止并禁用 13 个系统/用户服务，验证摄像头、UART、M2 和相关 ALSA 音频节点均无占用，5000、5001、8766、8767 均无监听。完整记录见 [旧机器人停用记录](robot-services-retirement.md)。

旧运行文件仍保存在 `/home/ws/deploy/companion-v2/` 等目录，作为停用前证据；新主控台不依赖或恢复这些服务。旧架构、音频异常和模拟轮状态仅供诊断参考，不能作为新系统运行状态。

`hardware_ready=false` 在既有源码中是固定值，`gimbal_ready` 也不能替代真实反馈。新主控台必须自行构建有证据的状态，不能直接复制旧状态的名称作为验收结果。

### 2.3 0.2.0 当前实施记录

新代理独占 `/dev/ttyAMA0`、`/dev/m2_mic`（本次启动指向 ttyACM0）；摄像头与 M2 仅按需采集。新 WSS 三通道已连接 Mini。旧 13 个服务仍禁用，旧端口关闭。

- 相机使用真实 V4L2 sequence/timestamp，新帧拍照及浏览器 640×480 视频通过。
- M2 需要及时握手 ACK 和临时 manual_wakeup；未修改增益、固件和唤醒词。扬声器回放时 M2 RMS 约 1009、峰值 15609，安静时 RMS 约 0.7；低环境电平不能直接认定为故障。
- 底盘使用 `t1_scalar_limited` 验收 profile，T1 幅度限制 0.10，已验证编码器正反向。该幅度不代表所有底盘均为 0.10m/s。未修改底盘类型、PID 或几何标定。
- T136=500ms 的首次停止测量约 0.813 秒，未过 0.8 秒门槛；改为 300ms 后实测约 0.463 秒。2026-10-02 按用户要求使用持久验收资格 persistent：不因时间到期或 Pi 重启锁定；每次启动自动检查新遥测，正常即开放，原测量证据和本地停止保护保留。
- 云台使用 T4=2 启用相应反馈、T133 小幅移动、T135 停止；网页圆盘实测位置改变并停止。
- HTTP/WS 已实现路由、部署状态、识别模型及验证结果见 [部署与验证](control-console-deployment.md)。以下章节中较完整的任务、turn、边缘 worker 和电视适配器契约属于后续设计，不能当作当前实现清单。

### 2.4 厂商参考代码

| 参考 | 本地位置 | 本次核对提交 |
| --- | --- | --- |
| Raspberry Pi 上位机 | `/Users/agent001/workspace-agent/projects/irobot/ugv_rpi/` | `3ae9f203f61bfaaf123ee376f78247d007880b0c` |
| ESP32 General Driver | `/Users/agent001/workspace-agent/projects/irobot/ugv_base_general/` | `d308df91b333a513f45a238bef9ba9a0a5edf64c` |
| ESP32 ROS Driver（车上实际系列） | https://github.com/waveshareteam/ugv_base_ros | `2e7df97`；2026-10-14 实测车上为该系列较旧版本，见 [ROS 导航设计 §2.2](ros-navigation-design.md) |
| M2 SDK 与说明材料 | `/Users/agent001/workspace-agent/docs/M2-Micphone/` | 本地资料；协议适配前进一步核对 |

参考入口：`ugv_rpi/base_ctrl.py`；`General_Driver/json_cmd.h`、`uart_ctrl.h`、`gimbal_module.h`、`movtion_module.h`、`ugv_config.h`、`ugv_advance.h`。厂家仓库：[ugv_rpi](https://github.com/waveshareteam/ugv_rpi)、[ugv_base_general](https://github.com/waveshareteam/ugv_base_general)。厂商源码作为协议参考，不直接导入其带设备扫描、全局初始化和无限命令队列的控制器。产品适配代码写在本仓库 `pi-agent/`。

## 3. 架构与技术选择

### 3.1 运行拓扑

```text
手机 / 桌面浏览器
  │ HTTPS：登录、REST、WebSocket、JPEG 视频
  ▼
老大 Mac Mini：局域网 HTTPS 反向代理（证书及路由）
  │ loopback，建议 127.0.0.1:8788（部署前复核）
  ▼
老大 Mac Mini：robot-console，Python FastAPI，单个 Uvicorn worker
  ├─ Jinja2 + CSS + 浏览器 JavaScript
  ├─ 账号、会话、任务、审计：SQLite
  ├─ 语音：Mini 本机 speech-provider；可委托模型主机
  ├─ 对话与交互编排：会话、turn、任务、模型调用、展示事件
  ├─ 电视展示适配器 → mini-server → tv-apk 角色/动画/字幕/声音
  └─ 认证设备连接：控制/状态、视频、音频各自独立 WSS 通道
       ▲ 老三主动连接 Mini；上传状态/媒体，接收受限命令
       ▼
老三：pi-agent，硬件代理，独立进程及本地运动看门狗
  ├─ 系统指标：procfs / sysfs / psutil
  ├─ CameraPort：独立 V4L2 采集 worker，独占 USB 摄像头
  ├─ MicrophonePort：ALSA M2 + 独立 M2 串口协议读取
  ├─ SpeakerPort：ALSA 播放
  ├─ EdgeInferencePort：独立 OpenCV / 本地小模型 worker
  ├─ InteractionPort：VAD/唤醒/本地即时回应/对话终端状态
  └─ MotionPort：新代理独占 Waveshare UART
       ▼
ESP32 下位机 → 云台 / 左右轮 / 编码器 / IMU / 电池
```

页面、登录、数据库、任务调度、媒体转发、完整对话编排和 ASR/TTS 编排放在老大 Mini；Pi 负责硬件采集、播放、遥测、有界控制、可选边缘推理和现场对话交互。speech-provider 初版也在 Mini，以相同 Python 工程的独立入口和可选模型依赖运行；模型负载需要时可委托其他主机。既有 Python 3.9 电视服务独立运行。

浏览器只访问 Mini 的同源 HTTPS 入口，FastAPI 默认监听 Mini loopback 的 8788（部署前复核）。Pi 不承载管理页面、不接收入站浏览器连接；它主动建立到 Mini 私有设备入口的 WSS 连接，Mini 利用该连接下发受限指令和接收回执。管理员密码与云端语音密钥仅保存在 Mini。

Pi 离线时，Mini 仍能提供登录、配置、任务记录和故障页面；设备数据显示最后观测时间并标记 offline/stale，网页控制、实时媒体及远程机器人播音不可用。Mini 或网络故障时，Pi 本地停止运动、结束在线授权媒体和播放会话，不等待网页发出停止请求；离线本地交互仅按第 3.4 节单独授权的有限策略运行。公网开放属于后续阶段，设备入口不进入公网路由。

| 职责 | 老大 Mini | 老三 Pi |
| --- | --- | --- |
| 页面、HTTPS、管理员与 SQLite | 负责 | 不保存管理员密码、不提供管理页 |
| ASR/TTS、配置、审计与任务 | 编排及执行，可调用模型主机 | 采集/播放及策略允许的轻量本地交互 |
| 完整对话、长期记忆、展示指令 | 服务核心统一编排，关联 turn_id | 听取/播音、唤醒与本地即时回应；上报交互事件 |
| 边缘识别与小模型 | 下发受版本控制的策略，汇总结果 | OpenCV / 小模型独立 worker，执行快速识别或局部交互 |
| 视频、音频 | 授权转发给浏览器；视频默认不重编码 | 独占设备，采集并按有效会话上传 |
| 云台、轮控制与遥测 | 校验浏览器权限，转发受限意图，展示回执 | 最终校验并执行，独立控制租约与看门狗 |
| 断线处理 | 页面报告链路中断，不缓存待恢复的运动指令 | 撤销租约、停止、关闭采集/播放；重新连接后等待新授权 |

电视端消费 Mini 下发的展示指令并回报 received/started/completed/failed，不调用模型、不持有机器人设备凭据、不控制 UART。老三与电视之间没有直接控制通道；同步由 Mini 依据真实 Pi 播放/交互事件产生展示状态。

### 3.2 技术栈

| 层 | 选择 | 用途与约束 |
| --- | --- | --- |
| Python | 3.11 系列，实施时固定补丁版本 | 与 Pi 现有 Python 3.11 环境衔接；Mac 使用独立 uv 环境 |
| 工程 | uv workspace、`pyproject.toml`、`uv.lock`、src 布局 | 两个可安装包；CI 和部署 frozen 安装 |
| HTTP | FastAPI、Uvicorn、Pydantic v2、pydantic-settings | 类型化请求、配置和 OpenAPI；硬件任务由 lifespan 启停 |
| 页面 | Jinja2、原生模块 JavaScript、CSS | Python 提供页面与业务；浏览器脚本负责流、触控和音频，无额外前端构建要求 |
| 实时 | WebSocket；视频第一版使用 MJPEG | 状态、控制与音频各自连接；视频慢客户端不阻塞硬件 |
| 数据 | SQLite WAL、SQLAlchemy 2、Alembic | 账号、会话、任务和审计；迁移可追溯 |
| 登录 | argon2-cffi 的 Argon2id；服务端会话 | 无可解密密码；会话可立即撤销 |
| 硬件 | V4L2、pyserial、ALSA arecord/aplay、psutil | 相机独立 worker；用固定设备标识及参数数组；不执行用户 shell 文本 |
| 网络调用 | httpx、WebSocket 客户端适配器 | httpx 用于主机语音；设备主动 WSS，认证及证书验证，有界队列 |
| ASR | 主机上的独立 Whisper speech-provider | 新服务契约；旧机器人后端仅作实现参考 |
| TTS | 主机上的 ElevenLabs 提供者；macOS say 作为显式配置的降级 | 参考现有 `mini-server/speech.py`；密钥由主机私有配置供给 |
| 边缘推理 | Pi 可选 OpenCV headless；可选轻量推理运行时 | 独立 worker，按 ARM64 安装/模型基准选择；不放进默认依赖 |
| 检查 | Ruff、严格 Mypy、pytest、pytest-cov、Playwright | 单元、接口、模拟设备和浏览器验收 |

包版本在第一阶段经过 Python 3.11 / Linux ARM64 和 macOS 安装验证后锁定；不在设计中虚构“已兼容”的依赖版本。uv 的项目和锁文件约定见 [官方文档](https://docs.astral.sh/uv/concepts/projects/layout/)。

Gateway 初版使用一个 Uvicorn worker，防止重复创建轮询器和内存事件分发器；硬件仍由单独代理独占，不能依赖 worker 数量保证设备互斥。多 worker 是独立进程，部署机制见 [FastAPI 官方文档](https://fastapi.tiangolo.com/deployment/server-workers/)。生命周期清理使用 [lifespan](https://fastapi.tiangolo.com/advanced/events/)。

### 3.3 层次与接口

- `api/` 只处理认证、参数校验、调用服务、响应映射；路由中不读 UART、不运行 ASR。
- `services/` 实现控制租约、媒体会话、语音任务和账号变更。
- Mini 的 dialogue/interaction 服务编排完整 turn、模型调用和电视展示；Pi 的 interaction 服务处理现场唤醒、声音活动、播放状态及版本化策略允许的本地回应。
- `domain/` 定义类型化状态、事件、错误和 Protocol；使用依赖注入及假设备。
- `adapters/` 分别实现 Mini 的 SQLite/设备连接/主机语音 HTTP，以及 Pi 的 WSS/ALSA/UART/V4L2 接入。
- 同步串口、音频进程、数据库和密码计算进入有界线程或独立进程；运动看门狗不等待 HTTP、ASR、数据库或媒体发送。
- Mini Gateway 与 Pi 硬件代理使用第 9.3 节的 `agent.v1` 网络协议。Unix socket/管道只用于 Pi 本机 capture worker 和硬件代理的私有 IPC，不能承担跨机器连接。
- 网络命令携带 `agent_id`、`connection_id`、`request_id`、`gateway_boot_id`、`broker_boot_id`、`session_id`、`auth_generation`、`control_epoch`；Pi 按设备身份、当前连接和授权映射再次校验。双方重启或设备控制连接重建都会废弃旧 epoch，旧命令不可重放。
- Mini 网页操作成功必须依据 Pi 回执；网络发送成功不等于硬件执行。代理状态、控制权和安全上限以 Pi 为准，Mini 的页面缓存只能展示。

### 3.4 老三的边缘计算与对话交互

边缘计算是可配置能力，不把所有模型装进硬件代理进程。`EdgeInferencePort` 定义输入帧/样本、结果、预算和取消；实现可使用 OpenCV 快速视觉处理或经过实机基准的小模型。OpenCV 的 Python 图像处理及检测接口参考 [官方教程](https://docs.opencv.org/4.x/d6/d00/tutorial_py_root.html)。候选推理运行时、模型格式和文件在 C2 的 ARM64 验证后选定，设计阶段不宣称某个包或模型已可运行。

- 原始帧由同一个 capture worker 提供。浏览器观看与授权边缘任务各占一个媒体消费者，引用计数统一管理；边缘模型不能另开摄像头。音频 VAD/唤醒同样共享 M2 样本源。
- 轻量任务包括运动/物体类别识别、有限区域检测、VAD/唤醒、简单本地意图识别。初版不默认启用人脸身份识别，也不默认把画面上传云模型。
- Pi 的推理 worker 使用 latest-frame、队列 1，视觉初始采样 2～5fps、单次推理预算 200ms；预算是实测目标。模型超时、崩溃、过热或内存压力时关闭该能力，硬件停止和音视频服务继续工作。
- 结果为 `EdgeEvent`：event_id、agent_id、broker_boot_id、frame_id/utterance_id、模型版本、任务类型、类别/置信度/可选框、观测时间、inference_ms、expires_at。Mini 只处理仍有效的事件；检测结果不是事实确认，低置信度不会自动产生动作。
- Mini 下发版本化 `edge_policy`，限定允许模型、任务、频率、资源预算、会话授权及离线行为。Pi 只加载明确配置且校验哈希的模型文件；安装和升级通过部署流程，不允许网页提交任意模型路径、下载地址或执行代码。
- 初版边缘策略默认关闭，管理员明确开启检测或交互会话才启动采集。边缘会话与观看会话分别续期，Mini 失联后默认停止采集；需要离线本地交互时，单独配置有限期限的 offline policy，不继承网络控制租约。离线策略明确任务、允许短回应和到期时间；失联期间 Mini 的撤销不能立即送达，本地最多运行至该既定期限，重连先同步撤销状态。
- 本地快速交互可处理唤醒提示、预置短句、“停止”等受限意图及播放反馈。停止意图可进入本地停止路径；其他识别/模型结果只输出语义事件，动作仍经过确定性策略、租约、限位和停止检查。不会把模型生成的 JSON 直接写到串口。
- Pi 对话终端状态为 idle/listening/thinking/speaking/interrupted，上传给 Mini。完整链路为 Pi 采集/VAD → Mini ASR/对话编排 → Mini TTS → Pi 播放，Mini 同步电视表情、字幕和动画。小模型获策略授权时可本地生成短回应，标记 handled_locally；Mini 对相同 event_id/turn_id 去重，避免随后再次回复。
- 同一个 turn 默认只选 Pi 或电视作为声音输出目标，避免两个扬声器同时说话；电视可以同步显示字幕与动作。用户明确要求多端播音时，再通过服务核心的输出策略调度。
- 网络恢复不会回放旧检测、旧对话片段或旧动作。当前会话须重新授权；离线允许的本地回应保留来源和处理标记，联网后仅上报受限摘要，不自动上传旧录音。

## 4. 单一硬件所有者与首次启用

### 4.1 观察模式

第一阶段接入系统指标和独立相机采集。`pi-agent` 管理一个专用 capture worker，由该 worker 独占 V4L2 节点；从 USB 相机支持格式中选择 MJPEG 640×480、10～15fps，按实际协商结果返回格式。读取 `v4l2_buffer` 的 sequence、timestamp 和时间戳类型，映射到携带 boot 标识的帧元数据，验证帧的新鲜度。实施时选择经过 ARM64 验证的 V4L2 绑定或封装；不能用 HTTP 获取时间、JPEG 哈希变化或不支持时间戳的 OpenCV 属性代替真实帧信息。

capture worker 通过有界 IPC 提供最新帧与元数据；单设备只采集一次，再分发给所有页面，每个订阅者仅保存最新一帧。首次授权视频会话开启设备，最后一个消费者退出/到期后关闭；独立指标查询不打开相机。未经登录和媒体会话授权，Gateway 不把画面转发给浏览器。旧 companion-capture 保持禁用。

观察模式不向 UART 发送运动命令。若发现任何设备被其他进程占用，页面显示 busy 和所属服务，对应入口禁用；不自动重启旧服务或抢占未知进程。

### 4.2 首次硬件启用

旧服务停用已完成；首次启用新代理作为部署步骤执行，页面不提供任意 systemctl 或 shell 操作：

1. 实时核对停用记录中的旧服务仍为 inactive/disabled，确认 UART、相机和音频设备无人占用。
2. 新相机 worker 按授权媒体会话取得设备；异常退出时关闭 IPC，代理撤销视频会话。
3. 运动代理取得设备锁及串口独占，先写明确零速、清理历史队列，验证遥测及下位机停止保护。启用真实轮控制前须完成第 7 节验收。
4. 云台和底盘同属一个串口所有者；只有新代理可以写控制命令，旧机器人服务保持禁用。
5. M2 单独取得串口与 ALSA 所有权；扬声器由新代理统一调度。若设备忙，返回 `device_busy`，不自动抢占。
6. 升级失败回滚到新主控台的前一个 release，或退回 observe 模式。不会恢复用户已放弃的旧机器人服务。

锁采用服务间 `flock`、pyserial 独占和启动前设备占用检查；单独 `flock` 不能阻挡不合作的旧进程。任一检查失败，进入受限状态。服务重启后都要求重新授权和取得租约。

## 5. 硬件状态契约

每个组件返回统一结构：

```json
{
  "component": "microphone",
  "state": "available",
  "mode": "hardware",
  "source": "alsa",
  "observed_at": "2026-10-01T10:30:00+08:00",
  "age_ms": 120,
  "freshness_verified": true,
  "metrics": {"sample_rate_hz": 16000, "level_dbfs": -80.1},
  "reason_code": "pickup_not_verified"
}
```

该例仅说明字段。`available` 表示能打开，不代表清晰拾音已验收。统一状态为 `unknown / unavailable / available / active / degraded / busy / fault`；模式为 `hardware / simulation / bridge`。每项能力另带 `supported / enabled / verified / blocked_reason`，页面显示能力和阻碍原因。

| 组件 | 数据与来源 | 更新 / 过期规则 |
| --- | --- | --- |
| 摄像头 | 型号、实际格式、帧序号、尺寸、帧年龄、实际帧率、采集错误 | 逐帧；最后采集帧超过 2 秒标记 degraded |
| 麦克风 | ALSA 标识、开闭状态、格式、音量、削波、样本年龄、丢帧数；M2 心跳另列 | 音频每 20ms；采集中连续 1 秒无样本报 fault；关闭时不报断流 |
| M2 MCU | 串口路径、校验结果、心跳年龄、可选唤醒角度 | 心跳周期须实测后配置；初始过期阈值 5 秒，未确认周期时显示 unknown |
| 扬声器 | 设备可用、静音、音量、播放任务、播放回执 | 状态变更推送；无现场反馈时显示“播放结束，听感待验证” |
| 左右轮 | 指令速度、编码器反馈速度、控制租约、停止锁 | 遥测目标 5Hz；反馈超过 1 秒过期，禁止新移动 |
| 云台 | 目标 pan/tilt 与反馈 pan/tilt、限位、反馈有效性 | 遥测目标 5Hz；不能用目标角度覆盖反馈 |
| 系统 | CPU%、频率、总/用/可用内存、swap、CPU 温度、风扇、降频状态、磁盘 | 1Hz；超过 5 秒过期 |
| 网络 | 连接类型、信号、接口可用性、访问链路延迟 | 5 秒；不向页面返回凭据或完整邻居列表 |
| 电池 / IMU | 电压、板载温度、姿态；来自下位机遥测 | 5Hz；没有可靠数据时显示未知，不用固定百分比代替电量 |
| 服务 | 版本、runtime path、process/app/route 三种健康 | 5 秒；只展示当前主控台所需服务和有限错误摘要 |

每台机器使用自己的单调时钟计算本地超时，跨机不直接相减 monotonic 时间；UTC 时间用于展示和审计。帧和样本携带 Pi 的 boot 标识、序号、采集时间及源端年龄，Mini 另外记录 receive_age_ms 和设备链路 RTT。跨机精确帧年龄要求校时已验证且误差有界（目标 ≤100ms）；未验证时分别展示源端年龄/接收年龄，将 freshness_verified 标为 false。运动截止期限使用 Pi 本地挑战窗口，见第 7.2 节，不依赖两机墙上时钟一致。

页面把 Mini 主控服务健康与 Pi 主机/设备健康分开。Pi 超过 1 秒无新状态时标记链路 degraded，超过 3 秒标记 offline；旧 metrics 保留 last_known 标签与最后观测时间，不显示成当前值。CPU 温度和 ESP32 板载温度分开标注。

状态 API 由新适配器直接构建，不读取旧机器人完整状态或家庭对话、身份、人脸历史。健康接口不因为 TTS 后端不可用而把摄像头也显示为故障。

## 6. 媒体、麦克风和语音

### 6.1 视频与拍照

- 初版保留当前实际分辨率 640×480，目标 10～15fps；按相机支持格式配置，页面显示实测值。720p 是后续能力测试。
- 登录后创建媒体会话，再读取同源 MJPEG 端点。会话关联账号及独立 `media_session_id`，最多 2 个同时观看者，闲置会话 15 秒到期；页面每 5 秒续期。
- MJPEG 与控制 WebSocket 独立；视频队列大小为 1，丢旧帧，避免积压。注销、改密和会话过期时主动结束正在发送的媒体流。
- 数据链路为 Pi 相机 → 设备视频 WSS → Mini 单次接收与分发 → 浏览器 MJPEG。640×480 MJPEG 第一版透传 JPEG，不在 Mini 重编码；两名观看者共享一条设备视频上传。Pi 和 Mini 都只保留最新帧，发送超时后关闭滞后的媒体连接，而不是补传旧帧。
- 拍照必须通过 `POST` 请求创建，要求取得比请求基准更新的帧，最长等 2 秒；超时返回 `camera_frame_stale`。返回 `frame_id`、真实 `captured_at`、下载地址与分辨率。
- 照片默认只在内存保存最多 60 秒，授权下载接口返回 JPEG、`Cache-Control: no-store`。下载地址不携带凭据，过期返回 410；点击下载由浏览器保存。
- 拍照由 Mini 发带唯一 request_id 的请求，Pi 取得请求到达后新采集的帧并返回；Mini 按自己的总计时限制 2 秒并缓存该照片。不能从 Mini 最后缓存帧直接生成“刚拍”的结果。Pi 离线立即返回 device_offline。
- 视频无默认落盘。断流 UI 给出时间和原因，冻结画面加明确覆盖提示。

### 6.1.1 已实现的家庭人脸识别（2026-10-02）

控制台 0.3.0 接入 Mini 既有 `family-face-recognition`，使用原有家庭特征库，不重新登记。视频 WSS 的 `console.video.v2` 封装同一帧 JPEG 与人脸 bbox/显示名字，浏览器 canvas 同步绘制；独立 worker 最多 5 次/秒，请求 1 秒超时、故障后 3 秒重试、输入/输出只留最新结果。原摄像头继续 10 FPS，识别视频上限 5 FPS，原 `console.v1` 原始视频兼容保留。详见 [人脸视频设计](face-video-design.md)。

### 6.2 M2 设备接入

M2 有两条独立通路：USB Audio 提供音频，115200 串口提供心跳及可选唤醒事件。USB 枚举成功、串口心跳有效和音频清晰是三个独立结论。

优先输入 `plughw:CARD=XFMDPV0018,DEV=0`；输出候选 `plughw:CARD=Device,DEV=0`。避免固定 `hw:4,0` 等易变编号，启动核对厂商、产品及设备身份。M2 断开时显示 unavailable 并终止会话，不悄悄切回摄像头麦克风；回退设备由管理员明确选择。

初版以 `arecord` 有界子进程采集 PCM S16LE、16kHz、单声道，20ms 为一块。启动时验证硬件支持，必要时由 ALSA plug 转换。电平采用 RMS dBFS，同时展示峰值、削波和最近音频年龄；静音/低音量不会等同于设备掉线。

串口解析器支持拆包、粘包、长度上限、校验及重新同步；不自动发送改唤醒词、改固件等设置。旧 `face_tracker.py` 的 M2 监听器和 SDK 可作为协议参考，但新解析器必须独立测试。

### 6.3 网页听音和转写

1. 用户点“开始听音”或“开始转文字”，创建音频媒体会话，代理开始采集。
2. 同一个采集源分发给实时听音和识别；两个消费者共享引用计数，关闭一个不会误关另一个。
3. 音频通过 Pi 设备音频 WSS 上传 Mini，再由 Mini 的独立浏览器音频 WS 发送 PCM；Mini 同一份采集源同时供听音和 ASR 分段使用。先发格式协商消息，块头包含 stream epoch、序号及相对采样时间。浏览器 AudioWorklet 重采样并维护约 100～200ms 缓冲；用户手势启动 AudioContext。
4. 原始 PCM 16kHz 单声道约 32KB/s；最多 2 名听音者，慢消费者丢旧块并标识 discontinuity。处理队列满时报警并停止对应会话，不阻塞运动看门狗。
5. 初版 ASR 是分段转写：Pi 的本地 VAD 输出 speech_start/end，Mini 按事件和最大 15 秒限制切分音频；本地 VAD 未启用时由 Mini 有界分段。片段交给主机 ASR，完成后通过状态通道追加中文结果。不承诺模型提供逐字流式结果。
6. ASR 每设备最多 1 个任务、最多 2 个待处理片段，调用最长 30 秒。排队满或主机离线时提示“识别不可用”，独立听音仍可继续。
7. 转写默认仅存在会话内存；结束后清理。用户以后明确要求保存时再增加存储策略。

原始录音默认不落盘；模型必须使用临时文件时放 Mini 私有临时目录，任务结束删除。Gateway 重启、注销、改密、音频租约到期都关闭关联采集和传输。Pi 的控制连接丢失或 Mini 存活心跳过期时，由 Pi 主动停止在线授权的采集和播放，即使媒体连接尚未报错也不继续上传；单独有效 offline policy 的本地处理不上传。

### 6.4 文字发声

`POST /api/v1/speech/jobs` 接收最多 500 个字符。返回任务 ID 后执行：`queued → synthesizing → ready → playing → completed`，失败为 `failed`，主动停止为 `cancelled`。

TTS 在 Mini 主机生成 WAV，Gateway 从受信任的本机提供者拉取；验证类型、样本格式、最大 10MiB、最长 60 秒，再通过独立设备音频通道分块传到 Pi。接收端校验 job_id、授权会话、声明长度和 SHA-256，接收超时 20 秒；校验完整后才进入扬声器适配器。初版单次最多 1 个播放任务、2 个排队任务；用户可取消当前及队列任务。长任务持有自己的有限扬声器会话，不延长运动租约。设备离线时拒绝新任务；传输中断标 failed/cancelled，重连后不补播。

参考现有电视 TTS 的提供者逻辑与失败处理，抽取可测试适配器，不直接导入整个旧 HTTP 服务，也不认为电视已有接口可以直接给机器人播音。macOS say 仅运行在 Mac 主机；Pi 不依赖 `say`。云端降级必须由配置启用并在任务回执显示实际提供者。

播放时初版暂停 ASR 分段，保留独立听音功能并提示“正在播报”；播报结束后恢复仍有效的识别会话，减少机器人把自己声音识别成用户输入。ASR 文本没有运动执行入口。

### 6.5 主机语音服务契约

独立 `speech-provider` 初版绑定 Mini loopback（建议 127.0.0.1:8789，部署前复核），固定端点为 `POST /v1/transcribe`（受限 WAV → text/language/duration）和 `POST /v1/synthesize`（text/voice → WAV）。与 Gateway 使用专用、可撤销的本机服务令牌；主机内部使用 loopback HTTP，若迁往模型主机，使用固定私有地址、TLS 和独立认证。不依赖旧机器人后端，能力以新服务的真实测试为准。

主机先运行一次真实中文转写和一次合成格式测试，才能标记对应能力 verified。密钥与音色真实配置只在主机私有环境文件中；页面仅取得能力和提供者名称。任务取消、超时、非音频响应、过大音频和主机重启都要明确失败，不无限重试。

### 6.6 Mini 对话服务与电视展示

Mini 的 `DialoguePort` 接收 turn_id、当前转写、有限会话上下文及仍有效的结构化 EdgeEvent，输出受长度限制的回复文本和白名单展示建议。模型提供者通过独立适配器接入，密钥在 Mini；未配置时保留听音、转写、手动发声和 Pi 已授权的本地短回应，页面标记完整对话 unavailable。模型调用超时初值 30 秒，收到取消/会话过期后丢弃迟到回复；模型没有 UART、系统命令或任意设备调用权限。

Mini 对每个 turn 记录 input_received/transcribing/responding/synthesizing/playing/completed/interrupted 状态，关联 utterance_id、edge_event_id、speech_job_id 和 display_command_id。Pi 本地已经回应的 turn 不再创建重复语音任务；用户中断时先停止播放和受限交互，再取消模型/展示后续步骤。

`TvDisplayPort` 在 Mini 内把表情/动画/字幕/指定输出音频映射到现有电视服务允许的指令，用专用内部适配器处理认证和回执。页面和 Pi 不获取电视设备 token，不直接调用电视设备接口。电视未在线时标记 display unavailable，Pi 的交互/停止链路继续可用；电视重连不补播已过期对话。电视自行绘制接收到的状态并保持本地默认外观，不承担对话决策、任务中心或机器人控制。

## 7. 运动控制与停止

### 7.1 厂家协议约定

| 用途 | 下位机格式 | 必须注意 |
| --- | --- | --- |
| 左右轮速度 | `{"T":1,"L":0.0,"R":0.0}` | 编码器配置和速度单位必须实机核对；源码用 float 判断，零速也序列化为小数 |
| 线速 / 角速 | `{"T":13,"X":0.0,"Z":0.0}` | 源码注释 m/s、rad/s；非编码器产品不适用，初版不暴露 |
| 云台目标 | `{"T":133,"X":0.0,"Y":0.0,"SPD":10,"ACC":0}` | X/Y 为角度；源码 X 限制 -180～180、Y 限制 -30～90；产品取更窄范围 |
| 云台停止 | `{"T":135}` | 源码短暂关闭再恢复扭矩；须实机验证其停止和保持行为 |
| 请求遥测 | `{"T":130}` | 返回类型 `T=1001`；允许受限轮询，当前空闲 1Hz、租约期间 10Hz，取得租约前请求新遥测 |
| 设置下位机超时 | `{"T":136,"cmd":300}` | 首次接管的受限配置动作；不能让网页任意修改 |

`T=1001` 可有 `L/R/v/r/p/y/temp`，云台模块配置正确时才有 `pan/tilt`。收到遥测包不代表每个传感器都刚更新：厂商云台缓存可能保留失败前的值，适配器必须核对有效性；固件未提供足够依据时显示反馈有效性 unknown，不能称闭环已验收。

出厂上位机的 `gimbal_emergency_stop()` 发送 `T=0`，本次核对的下位机明确停止云台命令是 `T=135`。产品不照搬函数名推断实际行为；底盘用明确零速，云台用经验证的停止命令。初版不暴露 PID、舵机 ID、原始 PWM、任意 UART JSON 或机械参数写入。

### 7.2 控制状态机和租约

控制状态：`unavailable → stopped → armed → moving`；急停、会话撤销、控制断线、代理故障、反馈过期或租约到期进入 `estop_latched`。上电与进程重启初态为 stopped 且不能自动 armed。

- 同一设备最多一个控制租约，由 Pi 创建并最终裁决，绑定管理员会话、一个浏览器控制连接、`lease_id`、`connection_id`、`gateway_boot_id`、`broker_boot_id`、`control_epoch`。Mini 返回 Pi 真正创建的租约；同账号第二个页面仅观察，不自动抢租约。
- 用户按住圆盘时取得控制，拖动提交归一化 x/y 方向。每次更新包含递增 `sequence`、Pi 创建的租约和该租约的新 challenge；不允许角度或原始左右轮串口字段。浏览器不能提交原始串口字段。
- 浏览器每 150ms 发送一次有效移动意图，Mini 验证会话后立刻转发，不缓存等待设备恢复。普通 ping、状态请求和保持连接不能续命移动。
- Pi 每 150ms 在有效租约上发出一个随机 challenge_id，绑定当前 boot/epoch/连接，并按 Pi 单调时钟设置 450ms 到期；Mini 原样转给浏览器，浏览器发送新的意图时回传该 challenge。每个 challenge 只能接受一次，过期或跨连接的直接拒绝，Mini 不能给旧浏览器消息补上新 challenge。
- Pi 的移动 deadline 不晚于所用 challenge 的原始到期时间、租约到期和 2 秒按住预算；消息到达不会把期限重新延长 450ms。这样两段网络中积压的旧指令不能在恢复后重新驱动电机，也不依赖跨机时钟同步。
- 代理独立线程每 50ms 检查移动 deadline；过期立刻清空待发运动命令并发送停止。恢复网络后必须重新取得租约、重新按键；不自动续发。
- 排队采用 latest-wins；普通运动命令最多保留 1 个，停止优先队列；丢弃旧 seq、旧 boot/epoch、过期或撤销租约消息。
- 松手、`pointercancel`、窗口失焦、页面隐藏、退出登录发送停止；代理超时补足浏览器异常关闭时的清理。
- 当前以厂家 T1 输入幅度上限 0.10 做受限控制，编码器、停止时间和所用 profile 由实机记录确认；不声称为统一 m/s。单次连续按住最多 2 秒，届时需松开重新按住。未验收、重启或证据过期禁用运动。
- 圆盘云台按拖动方向相对调整，每个更新最多 2°，pan 范围先取 ±45°、tilt 先取 -15°～45°、速度先取 10°/s。限位以实机线缆和机械校准缩小；目标角度更新最多 10Hz，停止命令抢占目标更新。 2026-10-23 用户决定放宽到 pan ±170°、tilt -30°～90°（固件上限 ±180° / -30°～90°，两端各留 10° 不顶固件边界），舵机零位由 `tvpet-gimbal-center`（固件 T502）校到相机正前方；线缆余量在车旁确认。
- 若反馈无法证明云台位置，返回 target-only，移动只允许显式的已验收受限模式，不伪造实际角度。

Gateway 每 200ms 经设备控制通道发送存活及认证 epoch；Pi 超过 600ms 无心跳撤销全部 Gateway 在线租约，停止在线授权的采集和播放。设备控制 WS 断开时 Pi 立即执行同样清理，视频/音频 WS 仍连着也不构成授权。独立 offline policy 的本地消费者按自己的期限清理，不上传到断开的链路。运动通常更早由 challenge deadline 停止；Pi 本地看门狗与下位机保护独立于 Mini。

注销或改密先在 Mini 撤销账号会话，再向 Pi 发送撤销并等待有限时长的停止结果；设备链路失败或 600ms 内无回执时记录 stop_unconfirmed、关闭该设备控制连接并依赖 Pi 本地超时，页面不能显示“硬件已停止”。Pi 重连先同步最新 auth_generation，再接受新会话，离线期间撤销的会话不能恢复。

控制租约要求设备链路 RTT 与代理负载达到门禁（初始 RTT 目标 ≤200ms）；连续延迟超限或出现挑战窗口失效时停止并禁用移动，等待链路恢复后重新取得租约。音视频重连只影响对应媒体通道，不能续命或重新建立控制租约。

### 7.3 急停与下位机保护

急停是经过认证的专用高优先级请求，任何已登录管理员都可触发，不要求拥有运动租约。急停清空运动队列、撤销控制租约、发底盘零速及云台停止，并停止当前语音播放。返回 `requested / sent / feedback_confirmed / unconfirmed`，串口 write 成功只能说明 sent。

急停锁不会因为点击移动按钮或网络重连而解除。用户显式解除时，代理检查反馈新鲜、串口健康和保护配置有效，恢复 stopped 状态；解除不产生运动。

厂家默认 `HEART_BEAT_DELAY=3000ms`，保护实现只停止轮速。Pi 应用看门狗不能覆盖进程被 kill、系统卡死或 UART 掉线。因此，开启真实底盘前要验证实机固件支持 `T=136`，当前设为 300ms 并实测停止；该值在代理重新接管时重新设定。若不能验证，下位机保护仍为未验收，真实轮控制保持禁用。

进程故障后的车轮停止目标不超过 800ms，正常松手或连接断开后的应用停止发送目标不超过 600ms；均需实测记录指令和反馈时间。机械停止距离另外现场测量。云台没有在所读源码中确认同等独立失联保护，禁止无限连续云台扫描；其停止与硬断线行为必须单独验收，页面也明确这是软件急停，不能代替硬件断电措施。

## 8. 登录、密码与授权

- 用户名固定 `admin`，没有公共默认密码。首次在老大 Mini 用 CLI `robot-console admin init` 隐藏输入设置密码；未初始化时浏览器提示在 Mini 初始化，不提供任何人都能抢注册的公开 setup。
- 密码长度 12～128 字符，以 Argon2id 随机盐单向哈希保存在 Mini 数据库；Pi 不持有管理员密码/会话 Cookie。参数先用 `RFC_9106_LOW_MEMORY` 候选，在 Mini 上基准后固定；重哈希判断随成功登录执行。参数选择依据 [argon2-cffi 官方说明](https://argon2-cffi.readthedocs.io/en/stable/parameters.html)。
- 密码计算进入有界工作线程，最多同时 2 次，避免登录阻塞运动处理。每来源和账号分别限制失败频率；超过门限返回 429，不永久锁死管理员。
- 登录签发至少 256bit 随机会话 token；浏览器 HttpOnly、Secure、SameSite=Strict、Path=/ Cookie；数据库只存 token 的 SHA-256。绝对有效期 12 小时、空闲 30 分钟。WS ping 和自动轮询不延长账号的空闲期；明确操作可续期。
- 同源 JSON 写请求要求精确 Origin 校验及会话绑定 `X-CSRF-Token`；登录同样校验 Origin。公开 GET 仅允许登录页、静态文件、初始化状态和最小 liveness，不返回硬件或网络细节。
- WebSocket 握手校验有效 Cookie 和 Origin；接收连接后先校验短时会话绑定 CSRF 证明，证明通过前不发送数据也不执行操作。5 秒无证明关闭。Cookie/token 不放 URL。Starlette 的握手与关闭接口见 [官方文档](https://starlette.dev/websockets/)。
- 每个 WS 命令及媒体续期检查会话仍有效；定时检查长连接的绝对过期和认证 generation。改密事务校验旧密码、更新哈希、递增 generation、撤销全部会话，随后停止硬件会话和所有媒体连接，要求重新登录。
- `logout` 只撤销当前会话及其任务/租约。CLI 离线重置密码也递增 generation 并使代理停止；不能回显新密码。
- 不复用 `mini-server` 的数据库、Cookie 或管理员密码；机器人使用独立 `robot_console_session`。未来统一登录另行迁移。
- 浏览器 HTTPS 由 Mini 的可信局域网入口提供，证书信任需要在手机实测；开发 HTTP 仅用于本机模拟，真实硬件控制不开放到该开发模式。反向代理头仅信任 Mini loopback 代理，禁止宽泛 CORS。
- Pi 使用独立设备 token 主动认证，不能作为浏览器管理员登录。Mini 数据库只存 token_hash，Pi 私有 token 文件 0600；CLI 注册设备并签发一次性显示的随机 token，轮换/撤销会立即关闭所有设备连接。不把设备 token 写入 URL、日志或 Git。
- 设备 WSS 校验 Mini 证书链和 hostname，私有 CA 文件在 Pi 受控配置中，禁止跳过 TLS 校验。设备入口仅允许私有网络访问；未来公网页面的反向代理明确拒绝 `/internal/`，不因共享应用进程将设备通道也暴露出去。

## 9. API 与事件协议

### 9.1 浏览器接口

统一前缀 `/api/v1`；未授权 401、参数错误 422、设备忙/停止锁 409、限流 429、设备或主机不可用 503。错误结构为 `{error: {code, message, request_id}}`，不包含命令文本、密码、堆栈或主机凭据。

| 方法 / 路径 | 行为 |
| --- | --- |
| `GET /auth/setup-status` | 仅返回 initialized |
| `POST /auth/login` | 固定 admin 登录、创建 Cookie、返回 CSRF token |
| `GET /auth/me` | 当前账号、会话过期、CSRF token |
| `POST /auth/logout` | 撤销当前会话，停止其媒体和控制 |
| `PUT /auth/password` | old_password / new_password；撤销全部会话，要求重新登录 |
| `GET /system/status` | Mini/Pi/电视分项健康、版本、部署目录；Pi 离线返回明确状态及 last_known |
| `GET /hardware/capabilities` | supported/enabled/verified/blocked_reason |
| `GET /edge/status` | 模型/策略版本、启停、推理耗时、资源及最后事件 |
| `PUT /edge/policy` | 限定字段的边缘策略；对应设备在线才能激活采集和执行 |
| `POST /interaction/sessions` | 创建有限期本地交互/对话会话，明确视频/音频/边缘用途 |
| `DELETE /interaction/sessions/{id}` | 取消 turn、识别/播音及其本地消费者；保留其他授权消费者 |
| `POST /media/sessions` | 选择 video/listen/transcribe；逐项校验并返回会话 |
| `POST /media/sessions/{id}/renew` | 续期该用户授权的媒体会话 |
| `DELETE /media/sessions/{id}` | 关闭该会话及消费者 |
| `GET /media/sessions/{id}/video` | 已授权的 MJPEG，Cache-Control: no-store |
| `POST /camera/snapshots` | 请求新帧，返回短时照片 ID 及时间 |
| `GET /camera/snapshots/{id}` | 授权下载 JPEG，过期 410 |
| `POST /control/leases` | 为一个已认证 control WS 创建独占租约 |
| `DELETE /control/leases/{id}` | 停止并释放租约 |
| `POST /control/stop` | 停止当前运动；重试幂等 |
| `POST /control/estop` | 所有运动急停并锁定；不要求租约 |
| `POST /control/reset-estop` | 前置条件通过后解除为 stopped |
| `POST /speech/jobs` | 创建文字发声任务；同一 request_id 去重 |
| `GET /speech/jobs/{id}` | 合成/播放的真实状态和原因 |
| `POST /speech/jobs/{id}/cancel` | 停止并清理任务 |
| `GET /health/live` | 最小进程存活；可匿名 |
| `GET /health/ready` | 授权后分项 readiness；依赖受损仍可登录查看 |

### 9.2 WebSocket

三个通道分别为 `/api/v1/ws/status`、`/api/v1/ws/control`、`/api/v1/ws/audio`，共同使用 `console.v1` 协议，独立队列和限额。状态连接不能延长控制租约，音频也不能阻塞急停。

状态首次返回 snapshot，之后发送 `status.updated / device.offline / media.closed / control.changed / control.challenge / edge.event / interaction.updated / transcript.final / speech.updated / error`。事件带 `event_id`、`agent_id`、`boot_id`、`seq`、`occurred_at`；对话/展示事件还带 turn_id。断线后获取新快照，不回放运动命令。转写结果仅给拥有该媒体会话的用户连接。

示例移动消息：

```json
{
  "type": "motion.update",
  "request_id": "example-request",
  "lease_id": "example-lease",
  "broker_boot_id": "example-boot",
  "control_epoch": 2,
  "challenge_id": "example-pi-issued-challenge",
  "seq": 12,
  "intent": {"left_mps": 0.05, "right_mps": 0.05}
}
```

其他允许类型：`gimbal.target / motion.stop / control.release`。消息最大 4KiB，控制速率最多 10Hz；字段禁止额外键、非有限数字和越界值。客户端时间不用于延长截止期限，绝对轮速取代有累计风险的移动增量。

回执至少包含 `request_id / accepted / sent / feedback_confirmed / reason_code`，不能把 accepted 渲染成“移动成功”。停止请求被调度器优先处理；控制连接认证失效时先撤销租约，再关闭连接。

### 9.3 Mini 与 Pi 的设备协议

Pi 主动建立三个独立 WSS 连接，地址为 Mini 私有设备入口：

| 通道 | 路径 | 内容及约束 |
| --- | --- | --- |
| 控制/状态 | `/internal/v1/agents/{agent_id}/control` | `agent.v1` JSON；握手、心跳、状态、租约、挑战、停止、媒体授权、边缘策略及回执；运动/停止优先 |
| 视频 | `/internal/v1/agents/{agent_id}/video` | 二进制元数据 + 单帧 JPEG，单帧最大 2MiB，队列 1；按授权媒体会话开启 |
| 音频 | `/internal/v1/agents/{agent_id}/audio` | PCM 上传、受限 WAV 分块下发、播放回执；识别/播放会话绑定，队列和总长度有界 |

这些端点属于设备协议，不属于浏览器 API。握手校验设备 token、协议版本、agent_id 和 Pi boot，Mini 返回 gateway_boot_id、connection_id、当前 auth_generation 与服务能力。同一 agent_id 只允许一个有效控制连接；已连接设备的第二个控制会话拒绝并报警，不自动抢占运动控制。视频/音频连接须绑定当前控制连接，不能独立创建硬件授权。

允许命令类型包括 `session.open/renew/close`、`lease.acquire/release`、`motion.update/stop/estop/reset`、`gimbal.target`、`camera.snapshot`、`speech.prepare/cancel`、`edge.policy.update`、`interaction.start/stop`、Gateway 心跳和会话撤销。只接受 schema 白名单，禁止 arbitrary shell/UART。Mini 完成用户权限校验，Pi 再检查当前连接、boot/epoch、认证 generation、资源所有者及硬件安全条件。

控制消息带 request_id，由 Pi 返回 accepted/sent/feedback_confirmed/failed。状态与边缘事件采用 latest-wins 或有限丢弃，不能占满停止队列；高频原始媒体不进入控制通道。视频帧含 media_session_id、stream_epoch、frame_id、源端时间、格式和声明长度；音频块另含序号、采样格式及用途。二进制包采用 4 字节元数据长度 + UTF-8 JSON 元数据 + payload，解析前验证所有长度上限。

三个通道各设置有限库缓冲、发送时限和消费者队列；发送变慢时丢弃过期媒体，不能累积播放或旧运动指令。WebSocket 客户端适配器显式配置 max_size/max_queue/write_limit、TLS 与请求认证头，JPEG/PCM 通道关闭额外压缩；不依赖库默认 ping 作为 600ms 存活检测。配置入口参考 [websockets 客户端文档](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)，实际版本经 Python 3.11 验证后锁定。

设备控制通道失败后，Pi 立即撤销所有在线授权；网络故障的重连用 1～30 秒指数退避及抖动，重连只有新握手/状态快照，没有旧租约或任务恢复。认证拒绝、token 撤销、证书/协议错误进入明确配置故障，不无限尝试同一错误凭据。

Mini 缓存 Pi 状态是为了离线诊断；Pi 的采集引用计数、运动租约、deadline 和停止锁保持本地权威。无在线有效设备连接时，API 返回 device_offline，不把动作放进等待上线的任务队列。设置等非动作配置可持久保存为 pending，但采集/交互的 enable 必须在线重新授权才生效。

启动时双方握手协议版本，版本不兼容则拒绝控制并保留最小诊断能力。Gateway 不承担 Pi 本地看门狗的停止保证，Pi 也不能因 Mini 数据库写入失败漏掉停止。

## 10. 数据、配置和日志

### 10.1 持久化

Mini 保存 SQLite；Pi 不复制管理员数据库，也不承载任务中心。表包括：

- `admin_account`：固定 admin、password_hash、auth_generation、更新日期。
- `sessions`：token_hash、csrf_hash、auth_generation、创建/绝对/空闲过期、撤销日期。
- `settings`：允许变更的设备选择、有限音量/视频参数及版本；安全上限由受控配置限制。
- `agents`：设备 ID、token_hash、禁用/轮换状态、协议和最后上线时间；不保存设备 token 明文。
- `edge_policies`：策略版本、任务/模型清单、预算、授权用途及离线期限；Pi 只缓存受限执行策略。
- `interaction_sessions`：会话/turn 元数据、状态、执行/展示目标及本地处理标记；对话内容默认仅在内存，长期记忆需独立授权策略。
- `speech_jobs`：状态、实际提供者、错误码、起止时间；文本和音频默认不入表。
- `audit_events`：账号动作、请求 ID、动作类型、结果、错误码；不记原始转写、密码、token 或 TTS 内容。
- `schema_version` 由 Alembic 管理。

高频系统状态、音频、视频和运动意图不写数据库。运动租约仅在 Pi 代理内存，任务重启后标为 interrupted，不继续播音或运动。Pi 仅保存设备凭据、硬件校准/安全配置、模型文件及有限本地策略；其临时样本和执行队列不构成另一套服务核心。

WAL 数据库备份使用 SQLite backup API；不能只复制主文件忽略 WAL。备份及日志目录私有，审计默认保留 30 天，任务摘要 7 天，备份轮转及保留策略在部署配置限定。

### 10.2 配置

提供受版本管理的 `.env.example`，真实 `.env` 和私有配置在运行目录，0600 权限。当前 `.gitignore` 的 `.env.*` 规则需增加 `!.env.example` 例外，并忽略新增工具缓存、临时音频和覆盖率文件；实施时验证模板可追踪、真实配置不可追踪。建议命名：

Mini 核心配置：

```text
TVPET_CONSOLE_HOST=127.0.0.1
TVPET_CONSOLE_PORT=8788
TVPET_CONSOLE_DATA_DIR=<mini-runtime>/shared/data
TVPET_ALLOWED_ORIGINS=<trusted-mini-console-origin>
TVPET_AGENT_INGRESS_ORIGIN=<private-mini-agent-origin>
TVPET_SPEECH_BASE_URL=http://127.0.0.1:8789
TVPET_SPEECH_TOKEN_FILE=<mini-private-service-token-file>
```

Pi 设备配置：

```text
TVPET_AGENT_ID=laosan
TVPET_AGENT_BASE_URL=wss://<private-mini-agent-host>
TVPET_AGENT_TOKEN_FILE=<pi-runtime>/shared/config/device-token
TVPET_AGENT_CA_FILE=<pi-runtime>/shared/config/mini-ca.pem
TVPET_HARDWARE_MODE=observe
TVPET_M2_SERIAL=/dev/m2_mic
TVPET_RECORD_DEVICE=plughw:CARD=XFMDPV0018,DEV=0
TVPET_PLAY_DEVICE=plughw:CARD=Device,DEV=0
TVPET_EDGE_POLICY_FILE=<pi-runtime>/shared/config/edge-policy.json
```

UART 路径、云台限位、轮速度单位、下位机超时、相机帧来源通过 Pydantic 配置验证。observe 是默认；simulation 模式显示醒目标识；hardware 模式仍须逐项通过能力门禁。服务启动不得根据 `/dev/ttyACM*` 第一个匹配项随意选择 M2。

### 10.3 日志与健康

标准 logging 输出结构化 JSON：Mini 由 launchd 收集到私有、轮转的运行日志，Pi 输出到 journald。字段包含 host_role/agent_id/component/request_id/job_id/turn_id/boot_id/level/error_code。控制停止可以先执行再异步审计，日志失败不阻挡急停。HTTP 日志排除密码和媒体正文，身份及网络字段仅记必要摘要。

区分 process（Mini launchd/Pi systemd/PID）、application（事件循环/代理/数据库/模型 worker）和 route（浏览器→Mini、Pi→Mini、Mini→语音提供者、Mini→电视）健康。ready 返回分项结果；Pi 离线不会使 Mini 登录/数据库 readiness 失败，对应硬件/媒体能力标不可用。边缘模型故障不能使运动停止服务不可用。

## 11. 工程目录和质量门禁

```text
ai-tv-pet/
├── pyproject.toml                 # 新 uv workspace；仅成员纳入新工具门禁
├── uv.lock
├── .python-version
├── .pre-commit-config.yaml
├── robot-console/
│   ├── pyproject.toml
│   ├── src/tvpet_console/
│   │   ├── app.py / config.py
│   │   ├── api/                   # auth/status/media/control/speech/edge/interaction/ws
│   │   ├── domain/                # schemas/ports/errors
│   │   ├── services/              # auth/media/control/speech/dialogue/interaction
│   │   ├── agents/                # 设备注册、连接管理、agent.v1 路由
│   │   ├── adapters/              # database/agent link/speech HTTP/TV display
│   │   ├── templates/ / static/   # Figma 对应页面及 AudioWorklet
│   │   └── entrypoints/           # admin CLI、主机 speech-provider
│   ├── migrations/
│   └── tests/                     # unit/integration/browser
├── pi-agent/
│   ├── pyproject.toml
│   ├── src/tvpet_pi/
│   │   ├── media.py / session.py  # 保持已有接口及测试语义
│   │   ├── domain/ / broker/
│   │   ├── adapters/              # V4L2/ALSA/M2/UART/system
│   │   ├── capture/               # 独立采集 worker 与有界帧 IPC
│   │   ├── edge/                  # 可选 OpenCV/小模型 worker、预算及事件
│   │   ├── interaction/           # 本地 VAD/唤醒、即时回应、对话终端状态
│   │   ├── transport/             # 设备 WSS、认证、重连、独立通道
│   │   └── motion/                # lease/watchdog/priority scheduler
│   └── tests/                     # fake clock/serial/ALSA/protocol
├── deploy/console/                # Mini launchd、HTTPS、安装/回滚示例
├── deploy/pi-agent/               # Pi systemd、设备配置、安装/回滚示例
├── scripts/verify-console.sh
├── .github/workflows/console-ci.yml
├── docs/ARCHITECTURE.md
├── docs/control-console-design.md
├── docs/control-console-deployment.md   # 实施时补充实机步骤及证据
└── mini-server/、tv-apk/            # 保留各自现有构建和测试
```

新 Python 包全面类型注解，Mypy strict；依赖第三方类型缺失时隔离到适配器，提供局部 stub，不全局关闭检查。函数使用 Google 风格 docstring，资源通过 context manager/finally 清理。禁止裸 except、吞掉停止失败、无限缓冲、模块 import 时打开硬件。

实施时统一工具配置，不立即强制改写整个旧电视服务。CI 从 workspace 执行目标包检查；`verify-console.sh` 封装并输出版本和结果：

```sh
uv sync --frozen --all-packages --all-groups
uv run ruff check robot-console pi-agent
uv run ruff format --check robot-console pi-agent
uv run mypy robot-console/src pi-agent/src
uv run pytest robot-console/tests pi-agent/tests \
  --cov=tvpet_console --cov=tvpet_pi --cov-report=term-missing --cov-fail-under=80
```

Linux ARM64 上验证设备包安装、ALSA/串口/V4L2、进程清理及可选边缘依赖；macOS 上验证 Gateway、数据库、设备协议和主机语音依赖。OpenCV/小模型运行时使用单独 extras，不要求 Pi 安装 Mini 模型依赖，也不要求 Mini 安装 Linux 设备绑定。系统 ALSA 工具由部署预检负责。质量目标为新增整体覆盖率 ≥80%、控制/认证核心 ≥90%；覆盖率之外还必须有故障场景测试。

## 12. 部署与回滚

开发源码保留在本仓库，发布清单记录同一源码提交和兼容协议版本，两台机器安装各自需要的包与依赖。

Mini 核心运行目录：

```text
/Users/agent001/workspace-agent/deploy/ai-tv-pet-console/
├── releases/<release-id>/          # Gateway wheel、页面、锁及版本清单
├── current -> releases/<release-id>
└── shared/
    ├── config/                    # 环境、服务凭据、语音提供者配置
    ├── data/                      # SQLite：管理员、设备、任务、审计
    ├── tmp/                       # 临时语音；照片默认仅内存
    ├── logs/
    └── backups/
```

Pi 设备运行目录：

```text
/home/ws/deploy/ai-tv-pet-agent/
├── releases/<release-id>/          # Pi wheel、ARM64 依赖和版本清单
├── current -> releases/<release-id>
└── shared/
    ├── config/                    # 设备 token、CA、硬件校准和边缘策略
    ├── models/                    # 经验证的可选本地模型，不含管理员数据
    └── tmp/                       # 有限音频接收缓存，任务结束清理
```

Mini 使用 launchd：建议标签 `com.ai-tv-pet.console`、`com.ai-tv-pet.speech`；程序和 WorkingDirectory 指向 deploy/current，环境与私有配置由程序从 0600 配置文件读取，plist 不写密码/设备 token/云密钥。Gateway 仅监听 loopback，Mini HTTPS 代理提供浏览器和受网络限制的设备入口，语音提供者绑定另一 loopback 端口。升级先保证 Mini 页面/登录可用，再连接设备。

Pi 使用 `ai-tv-pet-agent.service`，服务用户最小授予 video/audio/dialout 权限；代理管理 capture/edge 子进程。`RuntimeDirectory` 管理本地 IPC，`EnvironmentFile` 指向私有配置，ExecStart/WorkingDirectory 指向 deploy/current。设备相关 systemd 限制必须实测；服务不以 root 运行，不提供设备控制 TCP 监听端口。

Pi 的 stop hook 优先停止，收到 SIGTERM 先清运动、在线媒体和播放再释放设备。SIGKILL 由下位机轮保护兜底；模型 worker 的失败单独处理。两端重启都不恢复控制租约、旧对话或播放任务；生产不使用 --reload。

部署流程：两端打包/frozen 安装 → 检查旧 Pi 服务仍禁用和 Mini 当前路由/端口 → Mini 数据库备份/迁移 → Mini 启动、CLI 初始化管理员/注册设备 → 配置 Pi 设备 token、CA 和 Mini 私有地址 → Pi 以 observe 模式连接 → 验证证书、身份、状态/媒体和断线门禁 → 独立执行硬件首次启用 → 开启已验收的边缘/对话/动作能力。所有真实凭据和证书配置只在部署时写入运行目录。

回滚分别切换 Mini 或 Pi 自身的 release，前置检查 agent.v1 兼容范围；无法兼容时 Pi 保持 stopped/离线，Mini 仍展示诊断。数据库迁移优先向后兼容，不可逆迁移先备份；不要让旧代码访问新 schema。旧机器人服务保持停用。不能通过回滚重新启用旧控制入口。

主机 speech-provider 的运行目录放在 `/Users/agent001/workspace-agent/deploy/ai-tv-pet-speech/` 或指定模型主机 deploy 目录，独立服务管理。Git 中只有代码、配置示例及匿名化验收步骤，不含真实密码、令牌、证书私钥、录音、家庭照片和完整对话。

## 13. 测试与验收

| 层次 | 必测场景 | 通过条件 |
| --- | --- | --- |
| 认证 | 首次 CLI 初始化、错误密码、限流、CSRF/Origin、会话到期、改密、logout | 全部 HTTP/WS/媒体访问同步失效；无密码/token 落日志 |
| 状态 | 设备未插、busy、字段缺失、旧帧、未知反馈、simulation | UI 和 API 同步显示真实状态及原因 |
| UART / M2 | 半包、粘包、错误校验、超长数据、拔插、浮点零速 | 有界解析、重新同步、旧命令不重放 |
| 控制模拟 | fake clock 超时、重复/乱序 seq、双页面、队列堵塞、跨 boot 命令 | 停止抢占、租约撤销、不会重连后运动 |
| 双机协议 | 设备 token 错误/撤销、TLS/hostname 错误、重复设备连接、两段网络积压、过期 challenge、时钟偏差 | Pi 拒绝旧授权/旧消息，Mini 不显示伪造新鲜状态；控制不依赖墙上时钟 |
| 故障集成 | Gateway/代理 kill、串口断开、相机停帧、ASR 挂起、DB 写入失败 | 运动停止链路独立，故障组件隔离，任务明确失败 |
| 媒体 | 多观看者、慢浏览器、退出/改密、拍照新帧、关闭引用计数 | 没有第二相机 owner、无无限缓冲、不继续泄露媒体 |
| 语音 | M2 实际中文、TTS 主机故障/取消/回退、播报与 ASR 互斥 | 标明真实提供者，任务取消释放设备 |
| 边缘与交互 | 模型缺失/超时/崩溃/高负载、共用相机、重复检测、VAD/唤醒、离线策略到期 | 看门狗不受推理阻塞；检测/本地回应有来源，同 turn 不重复播放 |
| 电视展示 | Mini 发字幕/表情/动画、Pi 播放事件、电视离线、展示任务过期 | 电视仅展示并回执；电视故障不影响 Pi 停止，没有设备直连控制 |
| 浏览器 | 桌面及 390px 手机宽度、按住/松开/失焦、音频启动 | 无横向溢出，停止随时可达，触控可操作 |
| 部署 | fresh install、升级、数据库恢复、服务重启/主机重启、HTTPS 路由 | deploy 路径正确，登录可用，启动停止且不恢复运动 |

Mini/Pi 断网要分别测试 Wi-Fi 丢失、半开 TCP、仅媒体阻塞、Mini 服务退出与 Pi 进程退出。测试应证明普通 ping 和媒体通道活动不能续命移动，迟到的 motion.update 即使 seq 较大也因 challenge 到期被拒绝；Pi 离线时 Mini 页面和历史记录仍可用。

实机按顺序进行：

1. 观察模式登录、独立相机视频及新帧拍照；至少连续 30 分钟，无持续重连/增长队列。
2. M2 在安静和正常说话时采样、网页听音、中文转写；记录格式、电平、距离和实际识别文本，不把本次低电平采样当作验收。
3. TTS 指定短句，检查合成格式、播放进程结果并现场确认声音可闻；测试停止。
4. 在受控测试条件下首次启用 UART，核对固件与遥测字段、云台机械限位、轮方向/编码器/单位；确认旧机器人服务保持停用。
5. 验证正常释放、失焦、控制掉线、旧消息、软件急停、SIGKILL 和 UART 断开。记录停止发送、反馈归零和实际停止；达不到停止约定时不开放真实底盘。
6. 多页面并发、双机重启与网络恢复；不能抢租约、重放或补播。Pi 断线时核对 Mini 页面仍可登录，Mini 断线时核对 Pi 本地停止。最后复核两端服务、路径、端口、健康和日志。
7. 可选 OpenCV/本地模型 worker 的物体识别、处理时间和 CPU/RAM 预算；中止模型不能阻断停止。测试本地即时回应、Mini 对话 turn 去重和电视字幕/动画同步；声音默认只在选定的一端播放。

性能目标为 Pi→Mini→浏览器的局域网视频延迟 P95 ≤500ms、状态延迟 ≤2 秒、浏览器→Mini→Pi→Mini→浏览器的普通控制回执 P95 ≤200ms。分别测量浏览器↔Mini、Mini↔Pi RTT，不能把单段时延当成完整链路。设备 JPEG 只上传一次，统计码率、慢客户端丢帧和两段发送队列。

Pi 基础 agent + capture worker（不含可选边缘模型）目标 RSS 合计 ≤350MiB；边缘 worker 单独设置内存/CPU预算，初始候选内存上限 512MiB，实机基准后固定。Mini Gateway（不含 ASR/模型进程）目标 RSS ≤350MiB，模型独立核算。持续负载不触发 Pi 降频，不使运动 deadline 失效。以上均是需记录的验收指标，非已实现性能。

## 14. 实施阶段

| 阶段 | 交付 | 门禁 |
| --- | --- | --- |
| C0 工程与账号 | uv workspace、Mini 数据库/CLI/登录改密、设备注册/WSS 握手、CI、模拟页面 | Ruff/Mypy/pytest、账号和设备认证生命周期通过 |
| C1 观察主控台 | Mini Figma 页面、Pi 状态/V4L2、双机视频/新帧拍照、离线页面 | 帧来源及时间已核实；相机单一 owner；两段链路故障正确 |
| C2 M2、边缘与交互 | M2 听音、Mini ASR/TTS/基础对话编排、Pi 播放/停止、本地 VAD/唤醒、可选 OpenCV/小模型接口 | 拾音/转写/出声；独立推理预算；turn 与本地回应去重 |
| C3 受限运动 | 新代理独占 UART、遥测、云台、轮控制、租约、急停、看门狗 | 下位机失联保护和全部停止场景通过 |
| C4 稳定部署 | macOS/ARM64 发布、Mini launchd/HTTPS、Pi systemd、备份/回滚、手机和电视联动验收 | 双机路径/端口/健康/日志/恢复证据完整，电视仅展示 |

优先落地 C0/C1，使登录、视频和真实状态先可用；随后验证 M2 音频和停止机制，再开放控制。各阶段完成时更新本设计、架构、部署说明和 TODO，明确“代码通过”“接口通过”“现场通过”三种证据。

## 15. 开工前需要解决的技术问题

- 新 V4L2 采集 worker 的 ARM64 依赖及帧元数据：必须验证时间戳、序号、真实帧率和资源关闭。
- M2 低音量原因：需要检查实际说话、通道/格式及 SDK 模式，不能只凭枚举选择就认定可用。
- 旧机器人服务停用状态：本次已验证，后续部署和重启验收仍需检查，避免资源竞争。
- 实机 ESP32 固件版本、编码器及 `T=136` 有效性：决定是否能开启轮控制。
- 云台反馈有效性和停止命令行为：旧缓存需要有有效性证据，原协议缺失时保留 unknown 或后续单独扩展固件。
- 新主机 ASR/TTS 接口与模型可用性、局域网证书信任：通过独立 smoke test 和手机访问验证解决。
- Mini 私有设备入口、Pi 主动 WSS、二进制流限额和时钟校验：先用模拟设备验证身份、双机断线和过期命令，再连接真实硬件。
- OpenCV/小模型的 ARM64 包、模型大小、推理预算与本地回应策略：先基准，再启用具体模型；完整对话和长期状态归 Mini。
- Mini 现有电视服务的展示适配契约：只传入白名单展示指令，明确声音输出目标和 turn 关联，不把 Pi 设备凭据发给电视。

这些问题可以并行调查，但对应能力在证据不足时保持受限，不影响 C0/C1 的开发。

## 2026-10-02 姿态审核扩展

主控台 0.4.0 / Pi 0.3.0 增加独立进程 MediaPipe Pose Lite、连续姿态判断与挥手判断。browser video v2 元数据新增 pose（最多三人，每人 33 个关键点）与 timestamp_ns；元数据上限由 4096 提高至 32768 字节。同帧身份匹配、私有每 30 秒审核采样与后台租约见 [设计](pose-review-design.md)。默认媒体不保存的规则，仅对用户明确授权的本地姿态审核采样作例外。

2026-10-03：审核采样改为仅检测到人体或人脸时保存，至少间隔 30 秒；无人不保存。历史照片保留。Mini console 0.4.1。
