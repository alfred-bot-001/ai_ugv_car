# AI UGV Car 架构

> 2026-10-14 从 [ai-tv-pet](https://github.com/alfred-bot-001/ai-tv-pet) 迁出：本仓库只保留小车（老三）的主控台 `robot-console/` 与设备代理 `pi-agent/`；电视宠物 `mini-server/`、`tv-apk/`、`tv-rig-demo/` 仍在原仓库。

更新：2026-10-04。系统分工已确认：老大 Mac Mini 是服务核心，电视宠物负责展示，老三负责设备、动作、信息采集、边缘计算及对话交互。主控台新增人物照片标注模块；部署、测试证据和剩余范围见 [部署与验证](control-console-deployment.md)，后续能力契约见 [主控台技术设计](control-console-design.md)。

## 当前实现

| 模块 | 运行位置 | 职责 |
| --- | --- | --- |
| `mini-server/`（ai-tv-pet 仓库） | Mac Mini | 电视管理员页面、配对、提醒、复杂动作、定时任务、语音生成和 SQLite |
| `tv-apk/`（ai-tv-pet 仓库） | 芝杜电视 | 悬浮宠物、字幕及播放；主动连接电视服务，回报执行状态 |
| `tv-rig-demo/`（ai-tv-pet 仓库） | 浏览器或独立 Android TV APK | 棕熊连续关节动画原型；离线运行，不连接 Mini，也不替换 `tv-apk/`。设计与限制见 [独立电视角色动画演示](https://github.com/alfred-bot-001/ai-tv-pet/blob/main/docs/tv-rig-demo-design.md) |
| `robot-console/` | Mac Mini | HTTPS 登录/改密、中文页面、SQLite、设备注册、独立 WSS、视频/听音/拍照、本地 ASR/TTS、家庭人脸识别适配器 |
| `pi-agent/` | 老三 Pi，systemd | 独占 UART/M2、系统遥测、V4L2/ALSA、播音、圆盘方向映射、租约/看门狗与失联停止 |

主控台公共入口为 `https://car.aimaker.vip/`，经 Cloudflare 和既有 Nginx 到 Mini 的 TLS 8788。浏览器 Origin 固定为域名；Pi 的三条设备 WSS 仍走私有 LAN，公共代理拒绝 `/internal/`。共享域名下原 `/tv/` 服务独立保留。

视频人脸识别复用 Mini 既有家庭服务和特征库，独立异步 worker 在授权观看或明确启用本地审核采样期间识别最新 JPEG；人脸框、名字与同一来源帧一起传给浏览器绘制。成功推理画面最高 5 FPS；服务故障保留无框视频并重试。无后台审核时，停止观看会清空结果。具体协议和生命周期见 [人脸视频设计](face-video-design.md)。Pi 代理为 0.3.1，持久保存已验收硬件的控制资格，重启后检查正常即自动开放。

人物照片标注由 Mini 独立模块负责：`/photos` 复用 admin 会话，私有 API 分页浏览原始推理及审核图、新增采样原片，逐人修正身份/姿态/动作。Mini console 0.6.1 支持人工人物分类、同人身份与姿态组合筛选、跨页勾选、图片与标签 ZIP 导出，以及默认只删除人工标注或选择连照片删除。删除采用版本校验和可恢复事务，回收区与训练数据均在 Mini 私有目录。人工数据与历史修订保存在 `state/photo-labels`，确认清单生成 JSONL 用于后续训练；不直接修改线上模型或人脸特征库。详情见 [照片标注设计](photo-annotation-design.md)。

Pi 姿态规则为 geometry-v2：一帧内先完成全局一对一轨迹关联，清理歧义旧轨迹，避免多人靠近时不断重建时序历史。站、坐、躺与挥手等动作独立分类；有效人体关键点不足时保持无法判断。更新必须同时安装代理环境和独立 pose-venv 的项目 wheel。历史采样保留当时模型/规则结果，准确度仍由家庭实拍人工标注评估。

电视协议以 [protocol-m1.md](https://github.com/alfred-bot-001/ai-tv-pet/blob/main/docs/protocol-m1.md) 为准，真实进展以 [TODO](../TODO.md) 和 [M1 验证](https://github.com/alfred-bot-001/ai-tv-pet/blob/main/docs/m1-verification.md) 为准。

## 系统目标架构

```text
浏览器 → 老大 Mini HTTPS → robot-console（Python FastAPI，服务核心）
                            ├─ 账号、设备、配置、任务、审计、对话编排
                            ├─ SQLite 与 Mini 本机语音/模型服务
                            ├─ 电视展示适配器 → mini-server → tv-apk
                            │                          角色/动画/字幕/声音及回执
                            └─ 设备 WSS：控制/状态、视频、音频独立通道
                                 ▲ 老三主动连接，双向受限通信
                                 ▼
                              pi-agent（老三，现场交互端）
                                 ├─ 系统与各设备信息采集
                                 ├─ V4L2 视频、M2 音频、扬声器
                                 ├─ 可选 OpenCV / 本地小模型独立 worker
                                 ├─ 本地 VAD/唤醒、即时回应、对话终端状态
                                 └─ 本地看门狗 → UART → ESP32 → 云台/底盘
```

新增模块 `robot-console/` 部署在 Mini，扩展现有 `pi-agent/` 为老三的设备/边缘/交互端。开发采用 Python 3.11、uv workspace、src 布局、Ruff、Mypy strict、pytest；已采用 uv.lock、CI 质量门槛、可安装 wheel 与独立 deploy 环境。电视 Python 3.9 服务独立运行，展示适配器及完整对话编排仍待实现。

| 节点 | 负责 | 权威状态 |
| --- | --- | --- |
| 老大 Mini | 页面/认证、数据、服务编排、完整对话、ASR/TTS、设备与模型策略、电视展示调度 | 账号、配置、任务、turn 及长期状态 |
| 老三 Pi | 动作/设备、各类信息及音视频采集、OpenCV/小模型快速识别、唤醒/听取/播音/本地即时交互 | 设备占用、遥测、控制租约、截止期限和停止锁 |
| 电视宠物 | 消费展示指令、绘制角色/动画/字幕、按输出策略播放展示声音并回执 | 当前呈现状态，不承载模型、对话决策或机器人控制 |

完整对话链路为 Pi 感知/交互 → Mini 识别与对话服务 → Pi 播音/受限动作和电视展示；本地小模型允许依据版本化策略快速回应，并向 Mini 标记 handled_locally，避免重复回复。初版对话内容仅在会话内存，长期记忆需另行明确保存策略。

## 关键约束

1. 老三使用 Waveshare 小车硬件；先核对 `irobot/ugv_rpi` 与 `irobot/ugv_base_general` 厂家代码，产品改动写在本仓库适配器。
2. UART、相机和音频设备各有明确所有者；旧机器人服务已按用户要求停用，见 [停用记录](robot-services-retirement.md)。新硬件代理直接采集和控制，不恢复旧服务。
3. 网页和模型请求不直接进入 UART。运动由 Pi 本地代理验证参数、租约、epoch、短时 challenge、deadline、反馈和停止条件；Mini/网络断开时 Pi 自行停止。
4. 已验收且用户批准的硬件使用 `release_policy=persistent` 持久保存控制资格，不因 8 小时到期或 Pi 重启要求人工放行。保留原 verified_at、expires_at 和测量数据，另记 operator_released_at，不伪造重新验收。每次启动先停止底盘/云台并重新应用固件超时；取得有效的新遥测后自动开放控制。UART/遥测故障、反馈异常或缺少合格硬件证据才暂停控制，正常遥测恢复后自动就绪；手动急停仍由用户解除。当前 T1 上限为厂家幅度 0.10，不能统一解释为 m/s；T136=300ms 时失联停止实测约 0.463 秒。租约、challenge、遥测截止与看门狗继续生效。
5. 管理员固定 admin，密码和 SQLite 存在 Mini，Argon2id 单向哈希；Pi 使用独立设备 token 主动 WSS 连接。改密撤销全部浏览器会话、控制和在线媒体。
6. 媒体仅在授权会话内传给浏览器；默认不留存录音、视频、转写和家庭照片。2026-10-02 用户另行授权后台姿态审核采样：每 30 秒至多保存一张有人照片及 JSON 到 Mini 的 deploy/tmp/pose-review 私有目录；2026-10-04 又授权主控台内浏览、人工标注和单独保留训练数据，并将自动审核采样限制为每天最多 50 张。每日额度按 `Asia/Singapore` 自然日统计，账本持久保存在 `state/sampling-quota/quota.sqlite3`；服务重启、删除照片或删除人工标注均不重置今日已用额度。达到上限后停止自动后台视频采集与审核保存，新加坡时间次日 00:00 自动恢复；授权用户的视频预览独立于后台额度。配额账本异常时暂停采样并显示错误，不能将未知统计当作零。新增采样保留无标注原片，历史原片缺失明确提示。照片接口要求有效登录，文件不挂载静态目录、不上传第三方。后台采集独立于浏览器会话，显式配置开关、断线租约停止、容量上限暂停，见 [姿态审核设计](pose-review-design.md)。
7. 状态必须带来源和新鲜度；识别到设备、采集正常、运动命令发出和现场验收是不同证据。
8. 本地源码在 projects；Mini 核心运行目录 `/Users/agent001/workspace-agent/deploy/ai-tv-pet-console/`，Pi 设备端运行目录 `/home/ws/deploy/ai-tv-pet-agent/`。Mini 用 launchd，Pi 用 systemd。升级和回滚均保持停止状态。
9. Pi 边缘推理使用独立 worker 和有界资源，共享采集源；MediaPipe Pose Lite 已部署独立低优先级进程，最多三人、目标 5 FPS；姿态规则、时序挥手和同帧人物关联见姿态审核设计。本地快速交互服从版本化策略，不能绕过控制安全层。离线本地策略单独授权、有限期且不继承在线运动租约。
10. 电视只通过 Mini 取得展示指令，与 Pi 没有直接控制连接。Pi 离线时 Mini 管理页面仍可用，电视展示故障不影响 Pi 停止机制。

## 文档维护

每次功能改动前读取本文和对应设计；实现后更新接口、部署步骤、验证证据及 TODO。设计中的未实现接口和性能指标仍是目标；已实现路由与实机记录以部署文档为准。

2026-10-03：审核采样改为仅检测到人体或人脸时保存，至少间隔 30 秒；无人不保存。历史照片保留。Mini console 0.4.1。
