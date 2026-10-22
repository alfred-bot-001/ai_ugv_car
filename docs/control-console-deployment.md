# 老三主控台部署与验证

## 2026-10-04 照片标注发布（Mini 0.5.0）

已将 `ai_tv_pet_console-0.5.0-py3-none-any.whl` 安装到 Mini 的 deploy `.venv`，服务从持久运行目录启动。保留 0.4.2 回滚包，升级备份位于 `backups/photos-20261004-085631/`，管理员密码哈希与会话 generation 未改变，Pi 代理维持 0.3.0。

- 入口：`https://car.aimaker.vip/photos`，主控台侧栏和手机视频采样区均有入口。
- 私有目录：原始采样 `tmp/pose-review`；新原片 `tmp/pose-review/originals`；人工标签与保留样本 `state/photo-labels`。目录权限实测 0700，新原片文件 0600。
- Python 全部 125 项测试通过，覆盖率 86.98%，Ruff、格式检查、Mypy、JavaScript 语法检查通过。
- 独立应用与 28 张合成图完成 16 项桌面/手机浏览器流程：更正与保存、下一张、未保存提示、修订冲突、原片切换、排除、跨页、导出、会话失效及文本安全；无页面错误、无外部请求、无横向溢出。检查期间未修改真实家庭照片的人工标签。
- 实机读取列表时 445 张、人工确认 0；等待 35 秒取得 1 张新增原片，受保护图片 API 返回字节与原片文件一致。历史照片没有原片时返回明确缺失状态。
- 公网 `/health` 为 `agent_online=true`、`mode=control`；边缘推理和后台采样 active，控制/视频/音频 WSS 已重连。未登录照片 API 为 401，私有文件静态路径为 404。验证会话独立撤销，未发送移动指令。

训练清单采用 `tvpet-photo-labels.v1`，仅含最新确认修订，文件路径相对于 `state/photo-labels`。临时源删除后已标注样本仍可读取；历史无原片及手工新增无坐标人物在数据中明确标识。详细数据与接口见 [照片标注设计](photo-annotation-design.md)。

Mini 控制台版本 0.6.1，Pi 代理版本 0.3.1；更新 2026-10-04。照片人工标注、批量管理、本地姿态模型与每日最多 50 张审核采样已部署，见文末记录；完整对话和电视展示适配器继续按 [技术设计](control-console-design.md) 开发。

## 1. 运行分工

| 模块 | 部署目录 | 服务 | 职责 |
| --- | --- | --- | --- |
| Mini 核心 | `/Users/agent001/workspace-agent/deploy/ai-tv-pet-console/` | `com.ai-tv-pet.console` | 页面、固定 admin、SQLite、设备注册、WSS、ASR/TTS、家庭人脸识别 |
| Pi 代理 | `/home/ws/deploy/ai-tv-pet-agent/` | `ai-tv-pet-agent.service` | 设备/动作/遥测、V4L2、M2、扬声器及本地停止 |
| 电视服务 | 既有独立部署 | 既有服务 | 展示；新主控台的联动适配器尚未接入 |

源码在 `projects/ai_ugv_car`（2026-10-14 前在 `projects/ai-tv-pet`；包名与部署路径沿用 `ai-tv-pet-*`）；长期服务导入 deploy `.venv` 中的 wheel，不能用源码目录、editable 安装或开发进程充当部署。新服务使用 Python 3.11，电视旧 Python 3.9 环境独立。

Mini LaunchAgent 和 Pi systemd 已启用并运行，TLS `/health` 返回 200、agent_online=true；未登录状态接口返回 401。正式 admin 已按用户请求设置随机临时密码，并验证 HTTPS 登录；验证会话已撤销，临时验证库及测试 Cookie 已删除。

主控台正式入口：`https://car.aimaker.vip/`。浏览器通过 Cloudflare 的受信任 HTTPS 证书访问；`CONSOLE_ORIGIN` 必须设为 `https://car.aimaker.vip`。

公共链路为 Cloudflare → 既有 TCP 443 relay → Nginx 8443 → `https://127.0.0.1:8788`。Nginx 保留 WebSocket 升级并关闭媒体缓冲，使用 Mini 私有 CA 校验后端证书。公共 `/internal/` 返回 404，Pi 的控制/媒体设备端仍通过 `wss://192.168.1.123:8788` 直连 LAN，验证 CA 与主机名。浏览器统一使用域名，不再用 IP 地址登录；IP 改变时更新私有证书 SAN 和 Pi 配置。

已移除 Mini `/etc/hosts` 中旧的 `car.aimaker.vip` → `192.168.1.123` 映射并刷新 DNS，使 Mini 也经过 Cloudflare。共享 Nginx `unified-gateway.conf` 的其他主机以及原 `/tv/` → 9444 路由保留。配置模板见 [Nginx car 入口](../robot-console/deploy/nginx-car.aimaker.vip.conf)。路由改动前的 Nginx、hosts 和 LaunchAgent 备份在 deploy `backups/20261001-155426-domain/`。没有修改管理员密码、Pi 设备凭据或其他域名证书。

首次重装服务遇到 launchd 的异步卸载竞态，立即 bootstrap 返回错误 5；安装器已增加最多 6 秒的有界重试并增加回归测试。当前 LaunchAgent 与 Pi 已运行。

## 2. 账号与数据

- 用户名固定 `admin`，没有默认密码。正式 `state/` 已建库并注册设备；按用户请求设置随机临时密码，仅保存哈希，用户登录后修改。
- 全新部署时在 Mini 运行 `deploy/ai-tv-pet-console/setup-admin.command`，终端隐藏输入至少 12 位密码；仅保存 Argon2id 哈希。当前管理员已初始化，不重复执行。
- 页面的“修改密码”验证旧密码，成功后撤销全部浏览器会话并停止在线控制/媒体，重新登录。
- Cookie 为 Secure、HttpOnly、SameSite=Strict；会话绝对期限 12 小时、空闲 30 分钟，状态轮询不延长空闲期限。变更使用 CSRF 和精确 Origin；登录有限流。
- `private/`、`state/`、日志目录权限 0700，token/私钥/数据库权限 0600。测试账号只在独立验证库，结束联调删除验证库与测试 Cookie。
- 默认不保存录音、视频、转写或发声文本；审计只记操作类别和有限元数据。用户另行授权每 30 秒有人照片审核采样和照片人工标注，采样存放 `tmp/pose-review`，人工数据存放 `state/photo-labels`；数据和图片仅登录后的私有接口可访问，不挂载静态路径，不上传模型供应商。

## 3. 构建与安装

开发和验证：

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=tvpet_console --cov=tvpet_pi --cov-fail-under=80
uv build --all-packages --wheel --out-dir dist
```

首次在两个 deploy 目录各建 Python 3.11 `.venv`。从锁文件导出各包的固定依赖并安装，然后安装两个 wheel；Mini 需 console + pi-agent 协议包，Pi 只装 pi-agent。Mini 的可选 speech 依赖和模型只装在 Mini。模型路径由 `CONSOLE_ASR_MODEL` 指定，不向云端上传录音。

Mini 更新示例（服务停止后）：

```sh
uv pip install --python /Users/agent001/workspace-agent/deploy/ai-tv-pet-console/.venv/bin/python \
  --no-deps --reinstall dist/ai_tv_pet_console-0.3.1-py3-none-any.whl \
  dist/ai_tv_pet_pi_agent-0.2.2-py3-none-any.whl
```

发布 wheel 和固定依赖清单保存在 deploy `releases/0.2.0/`。相同版本内的联调修订可覆盖该目录；对正式升级应提升版本并保留上一版本，避免失去回滚依据。`.env.example` 和 `pi-agent/config.example.json` 只有路径与配置示例。

### Mini 启动

管理员密码使用上面的本地初始化入口。设备凭据由 CLI 在私有文件生成，不输出令牌：

```sh
CONSOLE_DATA_DIR=/Users/agent001/workspace-agent/deploy/ai-tv-pet-console/state \
  /Users/agent001/workspace-agent/deploy/ai-tv-pet-console/.venv/bin/robot-console \
  agent register --id laosan \
  --token-file /Users/agent001/workspace-agent/deploy/ai-tv-pet-console/private/agent.token
```

已注册设备不重复执行此命令。CA 公钥和该设备 token 安全复制至 Pi 私有目录；CA 签名私钥和服务器私钥只留 Mini。

```sh
uv run python robot-console/deploy/install_launchd.py \
  --runtime /Users/agent001/workspace-agent/deploy/ai-tv-pet-console \
  --origin https://car.aimaker.vip \
  --cert /Users/agent001/workspace-agent/deploy/ai-tv-pet-console/private/mini-cert.pem \
  --key /Users/agent001/workspace-agent/deploy/ai-tv-pet-console/private/mini-server.key \
  --asr-model /Users/agent001/workspace-agent/deploy/ai-tv-pet-console/models/whisper-small \
  --face-service-url http://127.0.0.1:8000
```

安装器生成 `~/Library/LaunchAgents/com.ai-tv-pet.console.plist` 和隐藏密码输入的 `setup-admin.command`。用户 LaunchAgent 在该用户登录后运行。日志在 deploy `logs/`。

### Pi 启动

将 [配置示例](../pi-agent/config.example.json) 复制到运行目录 `private/agent.json`，设置 WSS Mini 地址、CA、token 和设备路径，权限 0600。无需浏览器或公网入站访问 Pi。

将 [systemd 模板](../pi-agent/deploy/ai-tv-pet-agent.service) 安装为 `/etc/systemd/system/ai-tv-pet-agent.service`，然后：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now ai-tv-pet-agent.service
```

服务用 ws 用户、私有临时目录和有限可写路径；SIGINT 退出执行清理，异常退出由 systemd 重启。每次启动先写轮零速，运动证据不匹配时保持观察模式；音视频不自动采集。

## 4. 当前页面和接口

| 页面操作 | 已实现路由/传输 |
| --- | --- |
| 登录、会话、退出、改密 | `/api/v1/auth/login`、`session`、`logout`、`password` |
| 硬件状态 | `GET /api/v1/status`，Pi 状态超过 5 秒显示离线 |
| 视频、听音 | `/ws/video`、`/ws/audio`，登录 Cookie + Origin + 首包 CSRF |
| 拍照 | `POST /api/v1/snapshot`，取请求之后的真实驱动帧并下载 |
| 圆盘控制 | `POST /api/v1/control/lease` + `/ws/control`，提交方向 x/y |
| 停止、急停、解除 | `/api/v1/control/stop`、`estop`、`reset` |
| 输入文字发声 | `/api/v1/speech/say`，400 字以内、最长 60 秒、本地合成 |
| 声音转文字 | `/api/v1/speech/transcribe`，明确采集 5 秒，Mini 本地识别 |
| 进程/应用健康 | `/health`；设备状态及登录保护另外检查 |

底盘、云台均用圆盘：鼠标、触摸或方向键，离中心越远动作越明显，无角度输入。每次按压取得新的 Pi 租约，先刷新遥测，动作只接受属于本次租约的 450ms 一次性 challenge。松手、失焦、后台、断线和 Pi 看门狗均停止，重新按压才继续（2026-10-22 起取消 2 秒按压预算，按住多久走多久）。急停锁需显式解除，解除本身不移动。

Pi 的媒体和控制 WSS 为三条独立连接，设备 token 和控制连接生成的 link_id 共同授权媒体通道。视频/音频各只保留最新一帧/一包，慢客户端不累积历史。视频保留 V4L2 sequence、单调 timestamp、flags；音频 16kHz 单声道 S16_LE，每包 20ms。最后一个浏览器退出后释放采集设备。

## 5. 硬件问题与实机证据

测试前用户已确认车轮架空、云台周围无遮挡。以下只证明受限测试范围，不等于地面驾驶或长期运行验收。

| 项目 | 修复或检查 | 结果 |
| --- | --- | --- |
| M2 串口 | 使用稳定 `/dev/m2_mic`，当前 ttyACM0；MCU 握手及时 ACK，显式采集临时唤醒 | 校验与唤醒成功；没有刷固件、改增益或改唤醒词 |
| M2 音频 | ALSA `hw:CARD=XFMDPV0018,DEV=0`，精确 16k/mono/S16_LE | 扬声器回放时 RMS 约 1009、峰值 15609，安静 RMS 约 0.7；低环境电平不作为设备故障 |
| 相机 | 直接 V4L2 MMAP，保留驱动帧信息 | 640×480 JPEG、浏览器视频、新帧拍照通过；稳定采集帧年龄约 96ms，不等于端到端延迟 |
| 云台 | T4=2 启用对应模块反馈，T133 移动、T135 停止 | 小幅角度反馈变化，停止后稳定；网页圆盘实测约 3.78° |
| 电机 | T1 受限幅度 0.10，编码器与里程反馈 | 正反向均有反馈；网页圆盘两轮里程分别变化约 0.049/0.051，停止后 L/R 回零 |
| 固件失联保护 | 500ms 首次测量超门槛，改 T136=300ms | 单条运动命令后不续发，约 0.463 秒反馈停止，通过 0.8 秒门槛 |
| 本地转写 | Whisper small，完整采集窗口，静音抑制 | 5 秒 API 回放测试识别“你好老三,测试麦克风”；真人场景仍待体验 |
| 网页控制握手 | 租约绑定新 challenge，取得租约前请求新遥测 | 底盘/云台连续分次按压通过；没有 JS 错误 |
| 温度/内存/电源 | `/sys`、`/proc`、vcgencmd、T1001 电压 | 采样约 46°C、1.0/15.8GiB、无限频；随运行实时变化 |

厂家 T1 在不同底盘类型中可解释为 PID 速度或归一化 PWM，当前固件完整版本未知。产品采用 `t1_scalar_limited` profile，限制幅度为 0.10，反馈绝对值超过 0.20 或超过 600ms 没有新遥测时停止并锁定。页面不把这个输入幅度称为统一的 m/s。没有改底盘类型、PID、机械尺寸、舵机 ID 或永久标定。

### 重新验收运动

已验收且用户批准的记录使用 release_policy=persistent，长期保存控制资格。Pi 重启后先停止设备、应用固件超时，再根据新遥测自动开放。权限错误、硬件参数不符或原测量证据不合格仍锁定；正常硬件不因时间或重启要求人工验收。硬件更换或缺少合格证据时，重新验收仍需确认架空车轮、云台无遮挡，再停止代理、运行受限脚本、启动代理：

```sh
sudo systemctl stop ai-tv-pet-agent.service
/home/ws/deploy/ai-tv-pet-agent/.venv/bin/python \
  /home/ws/deploy/ai-tv-pet-agent/releases/0.2.2/verify_motion.py \
  --wheels-lifted --gimbal-clear \
  --output /home/ws/deploy/ai-tv-pet-agent/private/motion-evidence.json
sudo systemctl start ai-tv-pet-agent.service
```

脚本只做低幅度正反转和 3° 云台动作；20 秒独立停止定时器与 finally 停止兜底，证据文件 0600。失败不生成可用门禁。软件急停与云台停止不能代替断电；云台在进程被强制杀死后的独立固件停止保障仍待单独核实。

## 6. 质量与范围

- Ruff 检查/格式与 Mypy strict 通过；30 个测试通过，覆盖率 83.35%，CI 门槛 80%。
- 测试覆盖认证/CSRF/撤销、输入/报文界限、延迟/重放拒绝、租约/停止、V4L2 帧新鲜度、M2 握手、采集释放、播放校验及设备协议。
- 桌面 1600px 与手机 390px 视口验证，无横向溢出；Figma 资源均加载。视频实机与控件实机测试单独执行。
- 页面语音转写是手动 5 秒任务，发声是手动文本；已升级为 Whisper small CPU/int8，完整 5 秒窗口、beam=5，避免 VAD 裁掉 M2 音节；静音电平直接返回空结果。网页 API 实机回放识别出“你好老三,测试麦克风”，合成与转写均返回 200。真人自然语音和不同距离/噪声仍需现场体验。
- 完整模型对话、Pi OpenCV/小模型、VAD/唤醒词触发、电视联动、持续负载 P95 延迟和地面制动距离尚未完成。公网域名已接入并验证。

### 公网入口验证（2026-10-01）

- 无证书绕过的 HTTPS：首页 200、`/health` 200、admin 已初始化、agent_online=true。
- 未登录设备详情 401；公共设备通道 `/internal/` 404；有效会话的账号/状态 200；缺 CSRF 或错误 Origin 的变更 403。
- 控制 WSS 认证和 Pi challenge 通过；音频 WSS 收到 640 字节 PCM；真实 Chromium 浏览器收到 640×480 视频，无 JS 错误、未忽略 TLS 错误。
- 使用内存中的短期测试会话完成鉴权验证，之后撤销该会话；没有改用户密码、触发轮/云台移动、保存媒体或记录 Cookie。
- 原 `https://car.aimaker.vip/tv/api/health` 仍为 200，与切换前相同。
- 正式入口使用 HTTPS。HTTP 80 的外部请求当前超时，未宣称 HTTP 自动跳转已就绪。

## 7. 运维、升级和回滚

Mini 检查 `launchctl print gui/$(id -u)/com.ai-tv-pet.console`、监听 8788、域名 TLS `/health`、公共鉴权/媒体路由和 deploy 日志；单有进程不能证明应用与路由正常。Pi 检查 systemd、三条 WSS、独占资源与 journal。检查 launchd/systemd 中没有指向 `projects/` 的长期程序路径。

升级前停止动作、媒体和新服务；备份 SQLite 和上一版本 wheel 到私有 releases/backup，安装已验证的固定依赖与新包，再启动并检查来源、健康、状态、登录与停止。回滚同样保持停止，恢复上一 wheel/数据库兼容版本并重新验证。没有上一版新主控台可恢复时，将新服务停用，保持设备停止。

旧机器人 13 个服务必须保持 disabled/inactive，旧 5000/5001/8766/8767 端口关闭，不能作为失败时的自动降级。旧部署的归档和停用范围见 [停用记录](robot-services-retirement.md)。

## 2026-10-02 家庭人脸识别

Mini 控制台 0.3.0 已安装在原 deploy 环境。`CONSOLE_FACE_SERVICE_URL=http://127.0.0.1:8000` 连接既有 `local.family-face-recognition` 服务；健康和已登记成员接口已核对，库中 `max/son/wife` 分别有 26/110/21 条特征。既有模型与特征库沿用原服务部署路径，控制台 wheel 不包含照片、模型或人脸特征。

开启视频后自动识别人脸并同步绘制框和中文名字：max → 爸爸，son → 儿子，wife → 妈妈；未匹配到成员显示“未登记”。停止视频、最后一个观看者离开、登出、改密和设备断开会清空结果。网页只连接主控台同源 WSS，原服务地址不会直接暴露给浏览器。拍照仍下载原始新帧。

新浏览器使用 `console.video.v2`：大端 4 字节 JSON 长度 + UTF-8 JSON + 原 JPEG。JSON 包含 sequence、recognition、faces（bbox/label/known）和 inference_ms；旧 `console.v1` 原始 JPEG 保持兼容。原摄像头仍 10 FPS，识别画面最高 5 FPS，实际帧率取决于推理和网络。原服务相似度阈值为 0.4，分数不是身份正确的概率；三人实景表现待现场验证。

已完成 47 个测试，覆盖率 84.42%，Ruff 格式/检查和严格 Mypy 通过。新增测试覆盖帧与结果对应、中文成员名、未知人脸、非法响应、超时/故障恢复、并发观看、会话失效与原协议兼容。桌面及 390px 手机的模拟画面绘制、像素坐标、断流清理/恢复及故障降级已通过。可复测：

```sh
uv run python scripts/verify_face_video_ui.py --output /tmp/tvpet-face-ui
```

发布包和固定依赖清单在 deploy `releases/0.3.0/`；原 LaunchAgent 与实际安装的控制台代码保存在 `backups/20261002-face-0.3.0-verified/`。更新后已确认 Pi 重连，管理员密码哈希和 generation 未变。此更新只安装 Mini 控制台，Pi 的设备代码和已退役服务无需变更。

上线验证：经 `https://car.aimaker.vip` 完成身份校验和真实小车视频推理，5 秒取得 21 个识别帧（约 4.09 FPS），推理中位数 74ms；公网手机页面显示 640×480 canvas，TLS 验证通过、无 JS 错误。最后一个观看者关闭后识别状态回到 idle，临时验证会话已撤销，家庭库哈希前后一致；没有保存摄像头画面。

## 2026-10-02 首次运动放行（Pi 0.2.1，已由下一节替代）

用户要求“没问题就释放”。已核对原验收与当前 boot_id 一致，串口、受限幅度、300ms 固件超时和约 0.463 秒停止测量证据均合格。当前连续遥测正常，左右轮反馈为零，云台稳定，电压约 12.22V，CPU 约 49°C，throttled=0x0。

先停止代理并发送底盘零指令及云台停止，在 UART 独占期间重新应用 T136=300ms，取得 14 条新遥测并检查停止状态与位置范围；未发送非零底盘或云台移动指令。安装 Pi 0.2.1 后在私有 motion-evidence.json 增加 release_policy=current_boot、operator_released_at 和本次静止检查摘要，原 verified_at、expires_at、编码器和停止时间测量保持不变。

放行持续到 Pi 重启，代理服务重启不会撤销同一次开机的放行。缺证据、证据权限错误、原停止时间/限速/固件超时不合格或 boot_id 改变仍锁定；2 秒按住预算、松手停止、遥测与 challenge 过期停止及失联停止保护保持。

60 项测试通过，覆盖率 84.71%，Ruff 格式/检查和严格 Mypy 通过。新增回归覆盖放行、过期、重启、非法放行时间、错误硬件参数和权限、损坏记录。Pi 发布包在 releases/0.2.1/，原代理代码和原验收记录在 backups/20261002-operator-release/。

部署后验证：状态接口返回 wheels_ready=true、gimbal_ready=true，连续静止遥测正常；公网 390px 手机页面的两个圆盘 aria-disabled=false，底盘和云台均显示“可以控制”，无 JS 错误。验证未发送移动命令，临时验证会话已撤销。

## 2026-10-02 持久控制资格（Mini 0.3.1 / Pi 0.2.2）

用户要求硬件正常时不因重启锁定。已改用 release_policy=persistent：验收结果持久保存，保留原实机测量及来源 boot_id，不要求当前 boot_id 与验收时一致，也不因 8 小时到期要求重新放行。受限验收脚本新生成的合格记录也使用该策略。

每次启动先发送底盘零指令、云台停止，并重新应用 300ms 固件超时。页面只有在 UART 正常、取得 1.5 秒内的有限遥测、速度/电压及云台反馈正常时显示“可以控制”。开始控制时另请求新遥测；控制期间的 600ms 遥测截止、challenge、2 秒预算、限速、松手/失联停止保持。暂时遥测异常暂停控制，恢复后自动就绪；手动急停仍手动解除。页面分别显示“等待硬件恢复”与“硬件待检查”，不把临时反馈异常误报为未验收。

62 项测试通过，覆盖率 84.77%，Ruff/Mypy 通过；已模拟不同 boot_id 和启动时钟验证资格持久保留，验证反馈中断/异常暂停、恢复后自动就绪，以及手动急停保留。桌面和手机模拟 UI 检查通过。没有执行 Pi 整机重启或新的移动测试。

Pi 0.2.2 与 Mini 0.3.1 已从 wheel 部署，服务重启后 Pi 重连。部署期间再次采集 14 条静止遥测并应用固件超时；原验收时间/测量保持不变，管理员密码哈希与 generation 不变。备份分别位于 Pi backups/20261002-persistent-release/ 和 Mini backups/20261002-persistent-console/。发布目录含新 wheel、固定依赖清单与验收脚本。

上线复核：Mini 状态接口返回底盘/云台 ready=true；公网 390px 手机页面两个圆盘均启用、显示“可以控制”，无 JS 错误。临时验证会话已撤销，未发送移动指令。
## 2026-10-02：姿态与动作审核已部署

Mini console **0.4.0**，Pi agent **0.3.0**。运行位置沿用现有 deploy；发布 wheel 分别保存在 releases/0.4.0 和 releases/0.3.0。Pi 原生依赖使用独立 pose-venv，MediaPipe 0.10.18 ARM64 wheel；锁定清单为 pi-agent/deploy/pose-requirements.txt，模型固定 Pose Landmarker Lite v1，校验和见 Pi README。

Mini 设置 CONSOLE_POSE_REVIEW_DIR 为 `/Users/agent001/workspace-agent/deploy/ai-tv-pet-console/tmp/pose-review`，CONSOLE_POSE_REVIEW_FONT 为 `/System/Library/Fonts/STHeiti Medium.ttc`。每 30 秒保存标注 JPG 与同名 JSON，重启从最近文件时间延续间隔。目录 0700、文件 0600，HTTP 对应路径返回 404。文件不发布、不上传、不写入 Git。

验证结果：

- Ruff check/format、Mypy strict 通过；72 tests passed，覆盖率 85.03%。
- 合成浏览器桌面/390px 手机测试验证骨架、中文标签、坐标、故障恢复和布局；中文采样照片的视觉检查只使用合成图。
- Pi 用官方公开人体示例验证：一人、33 个关键点，单帧约 69–75 ms。这不是家庭动作准确率结论。
- 实机私有 WSS 4.08 秒接收 20 帧、来源序号无重复；空场景推理中位 38 ms。关闭预览后继续落盘，最后间隔 30.10 秒，已连续保存至少 15 组样本。当前现场样本检出人数为 0，标注未检测到人体。
- worker nice=10，RSS 约 229 MiB，ps 累计 CPU 约单核 20.6%，温度 49.4°C，throttled=0x0。底盘/云台 ready；未发送非零运动命令。管理员哈希和 generation 未变。
- 修复连接关闭期间 send_json 异常，后台采集继续重试；验证服务正常关闭、重连。公网仅检查 health、静态代码更新和 internal 路由拒绝；实机视频测试经 127.0.0.1 TLS/WSS，未向公网请求审核照片。

原始备份：Mini `backups/pose-20261002-172027/console.plist`，Pi `backups/pose-20261002-172028/agent.json`。回滚使用这些配置、Mini 0.3.1 / Pi 0.2.2 原 wheel 后重启对应服务；照片保留。

停用采样：删除 LaunchAgent 环境 CONSOLE_POSE_REVIEW_DIR 并重启 console。停用模型：清空 Pi 配置 pose_python/pose_model 并重启 agent。2 GiB 容量或剩余空间不足 1 GiB 时暂停保存，不自动删除已有样本。

待用户验收：成人/儿童站坐躺蹲、蜷缩候选、挥手，低机位、遮挡、多人交叉。几何规则得分不是准确率。
## 2026-10-03：仅有人时保存

Mini 0.4.1 已部署。只有同帧有效人体或人脸结果非空才保存，无人直接跳过，有人时仍至少间隔 30 秒。既有照片保留。页面显示 waiting_person 对应的中文提示。

76 tests passed，coverage 85.11%，Ruff/format/Mypy 及桌面/手机页面测试通过。实机连续 38 秒模型 active、审核 waiting_person，新增文件 0。统计 2762 张 JPG 及同名 JSON：178 组检出人体或人脸，2584 组未检出人，合计 150722231 字节。照片未读取、未上传。公网健康及新版脚本通过检查。

备份为 Mini runtime/backups/person-only-20261003-162417，原 0.4.0 wheel 保留。管理员哈希与 generation 不变，Pi 未升级。
# 2026-10-04 重连修复（主控台 0.4.2）

老三重启后持续重连被拒绝（403），设备凭据比对一致。主控台断线清理在等待已经失败的心跳任务时可能提前退出，遗留控制连接槽。0.4.2 将欢迎消息纳入清理范围，并确保心跳任务失败不会阻断连接状态清理。

已部署到 Mini 的持久运行目录，保留 0.4.1 回滚包；管理员凭据未改变。新增欢迎消息失败与心跳失败后的重新连接测试，完整测试 77 项通过，覆盖率 85.12%。公网 `/health` 实测 `agent_online=true`、`mode=control`，老三视频和音频通道重新建立。未发送移动指令。

## 2026-10-04 人物照片批量管理（Mini 0.6.0 / Pi 0.3.1）

Mini 已安装 console 0.6.0 和协议包 0.3.1，继续使用 `deploy/ai-tv-pet-console/.venv` 与 `com.ai-tv-pet.console`，TLS 8788。照片标注入口为 `https://car.aimaker.vip/photos`。Pi 的代理 `.venv` 和独立姿态 `pose-venv` 都安装 agent 0.3.1，systemd active/enabled、NRestarts=0，运行路径在 `/home/ws/deploy/ai-tv-pet-agent`。

- 人物分类只计人工已确认照片，支持爸爸、妈妈、儿子与自定义名称；同一人物的身份与姿态组合匹配，跨页勾选最多 200 张。
- ZIP 导出仅含人工确认数据，包含图像、可用原片、原始预测、人工标签与 JSONL，并保留多人全图标签和匹配人物编号。展开上限 256 MiB，下载断开或取消也清理临时 ZIP。
- 删除对话框默认只删除人工标注，也可选择照片、原片、模型结果及完整样本包。所有删除移入私有回收区，校验整批修订、失败回滚；未恢复事务阻止后续读写，恢复后保留原修订。实机核验没有执行删除或保存人工标签。
- Pi geometry-v2 修复多人靠近时旧轨迹妨碍关联导致时序历史反复清空的问题。模型与原生依赖保持原版本，代理私有配置与控制资格字节不变。姿态单独显示站着、坐着、躺着；关键点不足保持无法判断，旧照片保留当时结果。

验证：180 项 Python 测试通过，覆盖率 87.85%；Ruff check/format、Mypy strict 与前端语法检查通过。原浏览器 16 项流程及新增批量 17 项流程通过，使用合成临时样本，桌面和 390px 手机无溢出、无 JS 错误或外部请求。

部署后：公网 `/health` 为 200、agent_online=true、mode=control；有效临时会话访问照片页、图片、人工人物/姿态筛选成功；静态 JS/CSS 与已验证源码逐字节一致。未登录批量接口 401，无 CSRF 删除 403，私有数据与设备路由 404。本次只读核验共 533 张照片、人工确认 0，分类将在用户确认保存后产生。私有 WSS 5 秒收到 24 帧，全部使用 geometry-v2；该视频观察没有检出人体，新采样 JSON 同样确认规则更新。实际家庭姿态准确度仍需用户人工审核。底盘与云台 ready=true、左右轮反馈为零，没有发送移动命令。临时会话已单独撤销，管理员密码哈希与 generation 未变。

发布与回滚包：Mini `releases/0.6.0/` 包含两个 wheel 和 uv.lock，Pi `releases/0.3.1/` 包含 agent wheel；上一版 Mini 0.5.0 / Pi 0.3.0 wheel 保留。Mini 数据库及 LaunchAgent 备份在 `backups/batch-20261004-094047/`，Pi 配置及运动资格备份在 `backups/pose-tracking-20261004-094027/`。本次安装失败会自动恢复上一版；后续人工批量操作产生新删除标记后，恢复旧版前须先备份并核对采样目录、photo-labels 及未完成事务，旧版没有批量管理契约。

## 2026-10-04 每日最多 50 张（Mini 0.6.1 / Pi 0.3.1）

用户授权每天最多采集 50 张，多余停止。Mini 已安装 console 0.6.1；Pi 与协议包维持 0.3.1。本次只更新 Mini，不更改设备配置、管理员凭据或人工标签。采样仍要求同帧检测到人体或人脸、至少间隔 30 秒；每天按 `Asia/Singapore` 的自然日计算，原片/审核图/JSON 三文件算一张。

- 配额位于 `state/sampling-quota/quota.sqlite3`，目录 0700、数据库与锁文件 0600。首次初始化从源采样、保留样本和回收区元数据按样本 ID 去重计数。重启或删除照片不会重置额度；账本异常显示原因并暂停保存。
- 达到当天上限后停止无人观看的后台视频租约和采样；手动视频可打开，满额后观看也不会新增照片。新加坡时间次日 00:00 自动恢复额度和后台采样。既有超过上限的历史照片全部保留。
- 204 项测试通过，覆盖率 88.17%；Ruff、格式、Mypy、JavaScript 语法检查通过。11 项合成浏览器检查覆盖每日进度、满额/暂停状态及桌面/手机布局。
- 最终 wheel 与全部 48 个源码包文件逐字节一致，安装后的 48 个文件再次核对一致；launchd 运行来源为 deploy `.venv`，数据库完整性检查通过，无待恢复提交。服务重启后 Pi 已重连，管理员密码哈希与 generation 保持不变。
- 实机状态为 `daily_saved=693`、`daily_limit=50`、`day=2026-10-04`、`state=daily_limit`。连续超过 35 秒没有新增采样文件，后台摄像头停止；临时授权手动视频取得 3 帧 geometry-v2 数据后关闭，额度与照片数量保持不变。底盘与云台 ready、左右轮遥测为零，未发送移动命令。
- 公网 `/health` 为 200、`agent_online=true`，已部署 `app.js` 与源码一致，未登录状态 API 为 401。验证会话已单独撤销，没有保存真实人工标签或删除照片。

发布包和锁文件位于 `releases/0.6.1/`，上一版 console 0.6.0 保留。最终更新前数据库与 LaunchAgent 备份为 `backups/daily50-20261004-161944/`。回滚至 0.6.0 会失去每日额度限制；回滚前须停用后台采样并保存配额账本，后续恢复 0.6.1 时继续使用原账本。

## 2026-10-14 ROS 导航桥正式纳入（Pi 0.4.0）

2026-10-13 曾直接热改车上已安装的代理（新增 `nav_socket.py`、修改 `hardware.py`，版本号仍为 0.3.1）。0.4.0 把这些改动正式纳入仓库，模块为 `tvpet_pi/nav.py`：行为与已验收版本一致，请求改用 pydantic 校验，arm 解析和轮速 PI 拆为纯函数，新增 11 项测试（全部 248 项通过，覆盖率 88.7%）。导航桥只在私有配置设置了 `nav_dir` 时才启动。

- 备份：热改版代码和原配置在 Pi `backups/20261014-pre-0.4.0/`（`venv-tvpet_pi/`、`pose-venv-tvpet_pi/`、`agent.json`）。
- 发布：Pi `releases/0.4.0/ai_tv_pet_pi_agent-0.4.0-py3-none-any.whl`，同时装入 `.venv` 与 `pose-venv`；私有 `agent.json` 新增 `"nav_dir": "/home/ws/deploy/ai-tv-pet-agent/nav"`（权限仍为 0600）。不在安装记录里的旧 `nav_socket.py` 已删除，备份中保留。
- 实机核验：服务 active；`board.sock` 为 0600；`status` 返回完整 T1001 遥测（年龄 0.27 s，`nav_owned=false`）；`arm.json` 为 `enabled:false` 时运动请求返回 `motion is disarmed`；非法请求被拒绝；Mini `/health` 显示 `agent_online: true`。
- 回滚：停服务，把备份目录复制回两个环境的 `site-packages/tvpet_pi/`，或安装 `releases/0.3.1` 的 wheel 并去掉 `nav_dir`；最后启动服务。
- Mini 的协议包仍为 0.3.1；0.4.0 没有改动 Mini 与 Pi 之间的协议，Mini 不需要更新。

## 2026-10-17 加装 USB HUB 与深度相机后的摄像头修正

加装 VL817 USB HUB 和 Orbbec 深度相机后，深度相机的彩色镜头占用了 `/dev/video0`，原车摄像头变为 `/dev/video2`。Pi 配置原写 `/dev/video0`，导致视频、人脸与姿态采样改用深度相机画面。已将 `agent.json` 的 `camera` 改为 by-id 路径 `/dev/v4l/by-id/usb-Generic_USB_Camera_200901010001-video-index0` 并重启 `ai-tv-pet-agent`；备份为 `private/agent.json.bak-20261017-224724-camera`。验证：`tvpet-pi` 持有 `/dev/video2`，`/dev/video0` 空闲，Mini 收到 control/video/audio 三个通道重连。同时检查：雷达 2 秒 822 帧正常，EXT5V 5.42 V、`throttled=0x0`；插入 HUB 时 USB3 口报过一次 over-current，之后稳定，需留意 HUB 供电。回滚：恢复备份文件并重启服务。

## 2026-10-21 S0 安全补强、USB 麦克风与电池卡（Mini 0.6.3 / Pi 0.4.3 / ugv-nav 0.3.5）

从 main `98b95fc` 构建三个 wheel 部署。只部署，没有做运动测试；`arm.json` 全程为 `enabled:false`。

- **Pi 代理 0.4.3**：卡住检测（`StallMonitor`）、带原因的安全停车（`fault`，新 arm 会话才清除）、可配置的麦克风和扬声器。备份在 Pi `backups/20261021-pre-0.4.3/`（两个环境的 `tvpet_pi/`、`agent.json`、旧 `99-ugv-car.rules`），wheel 在 `releases/0.4.3/`。`agent.json` 改为 `m2: ""`、`microphone: plughw:CARD=ugvmic,DEV=0`、`speaker: plughw:CARD=ugvspk,DEV=0`（仍为 0600）。`/etc/udev/rules.d/99-ugv-car.rules` 新增两条 ALSA 声卡名规则，`udevadm trigger --subsystem-match=sound --action=change` 当场生效。核验：服务 active，导航套接字 `status` 带 `fault: null`，电压 11.92 V；`ugvmic` 录 2 s 正常（环境 −47 dBFS），`ugvspk` 可打开；Mini `/health` `agent_online: true`。
- **ugv-nav 0.3.5**：两道卡住检测的容器侧、`ugv-loc-health`、地点文件与 `place`/`goto`、禁行区掩码与 KeepoutFilter、电量门槛。旧源码备份 `/home/ws/deploy/ugv-nav/backups/20261021-pre-0.3.5/src.tgz`，旧镜像 `ai-ugv-car/ugv-nav:0.3.4` 保留；构建日志 `build-0.3.5.log`；主机 `.venv` 的 `ugv-navctl` 同步升级到 0.3.5。以建图模式启动：Nav2 `Managed nodes are active`；`loc_health` 运行，`/localization/health` 为 healthy；`/scan` 10.0 Hz；两层代价地图加载了 `keepout_filter`，没有掩码时只反复打印 `Filter mask was not received` 警告，不影响运行。
- **Mini 主控台 0.6.3**：电池卡显示电压估算格数，麦克风文字不再写 M2。wheel 在 `releases/0.6.3-ai_ugv_car-98b95fc/`；公网 `app.js` 已是新版本，`/health` 恢复 `agent_online: true`。
- **回滚**：Pi 代理装回 `releases/0.4.1` 的 wheel（两个环境），恢复备份的 `agent.json` 和 udev 规则后 `udevadm trigger`，重启服务；ugv-nav 设 `UGV_NAV_VERSION=0.3.4` 重建容器，主机 `.venv` 装回 0.3.4；Mini 装回 `releases/0.6.2` 后重启 launchd。
- **待办**：S0-1～S0-7 实车验收（需要用户在场、清场）；阈值按验收数据校准。


## 2026-10-21 厂家方式驱动轮子与实车运动测试（Pi 0.4.5 / ugv-nav 0.3.9）

用户在场、清场后测试（先在书房一角，后由用户把车搬到客厅）。地图只在 Pi 上：`s0-test-20261021`、`s0-test2-20261021`、`s0-living-20261021`。

- **ugv-nav 0.3.6～0.3.8**：卡住停车写日志、去掉桥接里的重复停车、`ros_ops` 只认本次导航之后的故障（0.3.6）；车身卡住只看轮子行程，不看轮子转角（0.3.7，原地转起步时轮子会先打滑）；`ugv-navctl probe` 单步测试厂家 T13/T1 指令并汇总轮速（0.3.8）。
- **Pi 代理 0.4.4～0.4.5**：导航套接字新增有界的 `probe`（单次 ≤2 s，授权 ≤10 s）（0.4.4）；方案 A（0.4.5）去掉 Pi 侧速度闭环和 2 倍转向增益，按固件 T13 公式 `L/R = X ∓ Z·0.172/2` 算两侧速度，每侧要么为 0、要么不低于 0.07 m/s（低于约 0.05 m/s 时固件 PID 会时转时停，实测停转占 47–94%）。wheel 在 Pi `releases/0.4.4`、`releases/0.4.5`。
- **ugv-nav 0.3.9**：AMCL 改用 `OmniMotionModel`（`alpha5` 0.2），`ugv-loc-health` 状态变化时写日志。原因：原地转时 RF2O 每转 0.05–0.09 rad 报告后退 0.5–1.9 cm，差速模型把它当成接近 180° 的转向来加噪声，转 0.7 rad 后 AMCL 标准差就到 0.20 m / 0.25 rad，触发 `localization_lost`（当时扫描匹配仍有 87%）；换成全向模型后同样的转动只到 0.09 m / 0.11 rad。旧源码备份 `backups/20261021-pre-0.3.9/src.tgz`，构建日志 `build-0.3.9.log`，旧镜像 0.3.8 保留，主机 `.venv` 同步到 0.3.9。回滚：`UGV_NAV_VERSION=0.3.8` 重建容器，主机 `.venv` 装回 0.3.8。
- **实测结论**：原地转（两侧 ±0.10 m/s）轮速平稳，9 次中只有 1 次某侧出现 18% 的停转时间；直线行驶平稳。原地转并不在原地：每转 1 rad 车身平移 10–15 cm，并且始终绕车身右侧的一个点转（左转时后退，右转时前进），而两侧编码器的转速是对称的，所以是右侧轮子没有真正推动地面。悬空测试（每侧正反转各 5～10 圈，按编码器计圈）中用户看到右后轮明显比右前轮快，查明是轮子打滑，用户当场修好。修好后在木地板上原地左转 367°（约 7.5 s），车身只平移 3.0 cm（约 0.5 cm/rad，修复前 7–15 cm/rad），车身转速约 1.1 rad/s，接近两侧 ±0.10 m/s 的理论值。后果是 Nav2 在终点对朝向时反复滑出 6 cm 的位置容差，来回打转直到超时。卡住保护在书房实际挡住了一次椅子脚轮（雷达扫描高度之下）。
- **其他**：Pi 在 18:25 修车断电后重启，`ugv-nav` 容器按 `restart: "no"` 没有自启，需要手动 `ugv-navctl mapping`/`localize`。云台摄像头（`usb-Generic_USB_Camera_200901010001`，USB `1-1.2`）17:15–17:20 反复重新识别后掉线，Pi 重启后已重新识别；仍需检查云台转动时线有没有被扯；没有禁行区掩码时 KeepoutFilter 每 2 s 打印一次警告。

## 2026-10-21 地图与导航实时页（Mini 0.7.0 / Pi 0.5.0 / ugv-nav 0.4.1）

用户要求在主控台加导航和建图的实时监控页。实现了 [map-console-design.md](map-console-design.md) M1 的只读部分，另加摄像头画面和导航日志。页面是 `https://car.aimaker.vip/navigation`，左侧栏「地图导航」。

- **数据链路**：容器内 `ugv-nav-relay` 只订阅、不发布，发送地图（北向上的三色 PNG，≤1 Hz）、位姿（10 Hz）、地图坐标下的激光点（≤180 点，5 Hz）、Nav2 路径、模式和定位健康、`/rosout` 日志（INFO 及以上，去颜色码，30 s 内重复的合并计数，≤20 条/s），写入 Pi `ai-tv-pet-agent/nav/stream.sock`（0600）。Pi 代理缓存后，只在 Mini 心跳表示有人打开页面时，经新 WSS 通道 `/internal/v1/agents/laosan/nav` 转发；这条通道出错只会自己隔 5 s 重连，不影响控制、视频和音频。Mini 只在内存里保留最新数据（设备离线即清空），浏览器经 `/ws/nav`（登录、Origin、CSRF 同视频）先拿快照再收增量。每一跳都校验：已知类型、元数据 ≤64 KB、不允许 NaN、只有地图可带 PNG 且 ≤1 MB。
- **隐私**：地图离开 Pi 只到 Mini 内存和已登录的浏览器，不写盘、不进 Git。
- **Mini 0.7.0**：`releases/0.7.0-ai_ugv_car-2577a7b/` 内有主控台 0.7.0 和 Pi 代理 0.5.0 两个 wheel（主控台引用 `tvpet_pi.navframe`，Mini 环境原装 Pi 代理 0.3.1，一并升级）。`launchctl bootstrap` 第一次报 `5: Input/output error`（bootout 尚未完成），隔 2 s 重试成功。核验：`/health` `agent_online: true`；公网 `navigation.js`、`navmap.js`、`navigation.css`、`assets/map.svg` 均为 200；未登录访问 `/navigation` 303 到统一登录。回滚：装回 `releases/0.6.3-ai_ugv_car-98b95fc/` 的主控台 wheel（Pi 代理包 0.5.0 可保留）后重启 launchd。
- **ugv-nav 0.4.0 → 0.4.1**：0.4.1 给监控节点的所有回调加保护，出错只记警告（同类 30 s 一次），不会因 launch 的「任一进程退出即整栈退出」把建图或导航拖垮。旧源码备份 `backups/20261021-pre-0.4.0/src.tgz`，构建日志 `build-0.4.0.log`、`build-0.4.1.log`，旧镜像 0.3.9、0.4.0 保留，主机 `.venv` 同步到 0.4.1。回滚：`UGV_NAV_VERSION=0.3.9` 重建容器，主机 `.venv` 装回 0.3.9。
- **Pi 代理 0.5.0**：先停容器，再装两个环境、重启服务，最后 `ugv-navctl mapping`（这个房间转圈测试时的临时地图未保存，随之清空）。wheel 在 `releases/0.5.0/`，回滚装回 `releases/0.4.5/`。核验：服务 active；`stream.sock` 0600；容器 `nav_relay` 已连接（`ss -x` 可见）；代理对外 4 条连接（控制、视频、音频、地图）。
- **验证**：门禁 335 项通过，覆盖率 82%；本机用假设备喂合成房间做了 Playwright 桌面（1440×900）和手机（390×844）预览，无横向溢出、无控制台错误。实车上的网页显示需用户登录后确认。
- **注意**：部署时驱动板读到的电池电压只有 7.1 V 且仍在下降，充电没有生效，需用户检查。

## 2026-10-21 前置 Orbbec 彩色相机接入主控台（Mini 0.8.0 / Pi 0.6.0）

- **做法**：Orbbec 初代 Gemini 的彩色部分是普通 UVC 相机（MJPEG 640×480），复用 Pi 代理的 V4L2 采集类。Pi 代理新增可选配置 `front_camera`（为空即关闭），走独立的 `front` 媒体通道：只在有人观看时打开，靠 Mini 心跳续期（与云台相机相同），不做姿态推理；这条通道被拒或出错只会自己重连，不影响控制链路。Mini 新增设备通道 `front` 和浏览器 `/ws/front`（登录、Origin、CSRF 同视频），不做人脸识别，也不参与后台采样。设备总览的视频面板和地图页都可以切换「云台 / 前置」，地图页默认前置。
- **实机发现**：这台相机每帧在 JPEG 结束标记后补 3～5 个零字节（凑 8 字节对齐），原来的严格检查会把整帧判为无效。现在只去掉 EOI 后最多 64 个零字节，其他多余字节照拒。修复后在车上用正式采集线程取帧正常（约 11.9 KB/帧）。
- **配置**：Pi `private/agent.json` 新增 `front_camera` = `/dev/v4l/by-id/usb-Sonix_Technology_Co.__Ltd._USB_2.0_Camera_AY27552002R-video-index0`（仍为 0600）；原文件备份在 `backups/20261021-pre-0.6.0/agent.json`。
- **部署**：Mini `releases/0.8.0-ai_ugv_car-e255c75/`（主控台 0.8.0 和 Pi 代理 0.6.0 两个 wheel），`/health` 正常，公网脚本已是新版。Pi wheel 在 `releases/0.6.0/`；先停容器、装两个环境、重启服务，再 `ugv-navctl mapping`。核验：服务 active，代理对外 5 条连接（控制、视频、音频、地图、前置），导航中继已连接。门禁 341 项通过，覆盖率 82%；本机 Playwright 预览两个页面的前置画面正常。
- **回滚**：Mini 装回 `releases/0.7.0-ai_ugv_car-2577a7b/`；Pi 装回 `releases/0.5.0/` 并恢复备份的 `agent.json`（去掉 `front_camera` 也可单独关闭前置相机）。
- **未做**：深度图。新版 Orbbec SDK 不认这台初代 Gemini，要正式部署 OpenNI2 驱动；之前的测试环境在 `/tmp`，Pi 重启后已清空。

## 2026-10-22 摇杆速度滑杆（Mini 0.8.1 / Pi 0.6.1）

用户反馈网页摇杆太慢。原因：网页摇杆的 T1 上限写死为 0.10 m/s，而导航已按 2026-10-14 批准的 `PWM_LIMIT = 0.18` 运行。用户批准：底盘圆盘下方加「速度上限」滑杆，默认仍为 0.10，设置记在浏览器 `localStorage`（`wheels-speed`）；按压 2 秒预算、松手/失焦/断线/遥测过期/急停等停止机制不变。范围定为 0.07～0.18：上限与导航相同、低于 0.3 m/s 失控阈值；下限原议 0.05，因 2026-10-21 探测发现固件 PID 在约 0.06 m/s 以下会间歇停轮，改为与导航 `WHEEL_FLOOR` 相同的 0.07（Claude 的调整，已向用户说明）。另外「拖到边上自己弹回」是按压 2 秒预算到期后网页主动归位并发停止指令，不是故障，用户未要求放宽。

- **协议**：`Stick` 新增可选字段 `speed`（`tvpet_pi.protocol`，`JOYSTICK_SPEED_MIN/DEFAULT/MAX = 0.07/0.10/0.18`，带日期与理由注释），缺省 0.10，超出范围由 Pydantic 拒绝；云台命令忽略该字段。Mini 只校验并原样转发。
- **Pi**：`Board.move()` 的 T1 上限改为 `stick.speed`，`nav.py` 仍显式传 `maximum=PWM_LIMIT`；实测轮速 0.3 m/s 的失控停车阈值不变。摇杆的差速混合没有改，部分偏转时单侧仍可能落在 0.07 以下的抖动区。
- **验证**：门禁通过，新增 `test_speed_frontend.mjs`；本机以 `CONSOLE_DEV_HTTP=1` 预览，桌面与 375px 手机视口滑杆显示正常、刷新后保留，无控制台错误。
- **部署（2026-10-22，main `1e2303b`）**：Pi 先装 0.6.1（旧版 Pi 收到 `speed` 字段会拒绝整条命令），再装 Mini 0.8.1。Pi：备份 `backups/20261022-pre-0.6.1/`（两个环境的 `tvpet_pi/` 和 `agent.json`），wheel 在 `releases/0.6.1/`，两个环境装好后重启服务；容器当时未运行，没有地图丢失。Mini：`releases/0.8.1-ai_ugv_car-1e2303b/`（主控台 0.8.1、Pi 代理 0.6.1 两个 wheel 和 `uv.lock`），`launchctl bootout/bootstrap` 一次成功。核验：Pi 服务 active、`ugv-navctl status` 正常、对外 5 条连接；Mini 本机与公网 `/health` 均 `agent_online: true`，公网 `app.js` 与源码逐字节一致、`style.css` 含滑杆样式。未发送移动指令，`arm.json` 未动。
- **回滚**：Mini 装回 `releases/0.8.0-ai_ugv_car-e255c75/` 后重启 launchd；Pi 装回 `releases/0.6.0/`（两个环境）后重启服务。先回滚 Mini 再回滚 Pi（旧 Mini 不发 `speed`，新 Pi 兼容）。
- **待办**：首次用 0.18 实车试驾需用户在场、清场，观察制动距离是否仍在接受范围。

## 2026-10-22 摇杆按住即持续控制（Mini 0.8.2 / Pi 0.6.2，未部署）

用户决定：底盘和云台两个圆盘都取消「每次最长 2 秒」，按多久走多久。用户看到的「控制被拒绝：设备未验收、租约或 challenge 已失效」，就是网页 1.9 秒自动归位与 Pi 2 秒预算赛跑的结果：按住到末尾时最后一条命令晚于 Pi 的截止到达，被 `Guard.accept` 拒绝后以提示弹出。

- **Pi**：`Guard.acquire()` 不再设 2 秒按压与 10 秒租约截止（两者为无穷大，注释写明日期与理由）。撤销仍来自 0.45 s 一次性 challenge、0.6 s 心跳、固件 300 ms 超时、松手/失焦/后台/断线和急停；`nav.py` 的授权会话照旧覆盖这两个截止，导航行为不变。
- **页面**：去掉 1.9 秒自动归位计时器，帮助文字改为「按住多久走多久，松手即停」。
- **验证**：门禁通过；`test_safety.py` 新增 10 分钟持续按住用例，`test_speed_frontend.mjs` 断言页面不再有自动归位计时器。未连真车。
- **部署阻塞**：Mini/Pi 当前运行的是另一个会话未推送的 `feat/map-controls`（Mini 本地分支，主控台 0.9.0 / Pi 代理 0.7.0，地图页控制）。从 main 构建会覆盖它，所以本次没有部署。该分支需要先变基到 main 再发版；它的地图页摇杆 `navigation.js` 里也有自己的 `setTimeout(releaseStick,1900)`，要一并去掉。
