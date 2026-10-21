# 老三主控台 · Mini 核心

Python 3.11 / FastAPI / SQLAlchemy / Alembic / Argon2id。中文响应式页面，设备使用三条主动 WSS 连接。运行目录与源码分离。

```sh
uv sync --locked
export CONSOLE_DATA_DIR=/Users/agent001/workspace-agent/deploy/ai-tv-pet-console/state
uv run robot-console admin init
CONSOLE_DEV_HTTP=1 CONSOLE_ORIGIN=http://127.0.0.1:8788 uv run robot-console serve
```

首次初始化没有默认密码，终端隐藏输入至少 12 个字符，数据库仅保存哈希。上例仅用于 loopback 开发，Pi 仍必须通过 WSS 接入。真实运行必须设置 `CONSOLE_DATA_DIR` 指向 deploy 目录；外部访问必须使用 HTTPS。

正式入口为 `https://car.aimaker.vip/`，公共代理支持视频/音频/控制 WSS，并拒绝 Pi 的 `/internal/` 设备接口。Pi 仍通过私有 LAN 的 WSS 接入。配置模板在 `deploy/nginx-car.aimaker.vip.conf`。

底盘、云台均使用圆盘。Pi 的有效硬件验收记录决定能否控制，页面和 Mini 无法解除这个门槛。完整步骤、当前功能与实机证据见 [部署与验证](../docs/control-console-deployment.md)。

人脸视频识别复用 Mini 已有家庭服务；设置 `CONSOLE_FACE_SERVICE_URL=http://127.0.0.1:8000` 后，开启视频即显示同帧的人脸框和成员名字。未匹配者显示“未登记”。只允许 loopback 服务地址，图片和结果仅保留在内存；详见 [人脸视频设计](../docs/face-video-design.md)。

### 本地姿态审核采样（0.4.0）

设置 `CONSOLE_POSE_REVIEW_DIR` 显式开启后台视频审核租约，每 30 秒至多保存一张有人照片及同名 JSON，无人不保存，不留存旧帧。设置 `CONSOLE_POSE_REVIEW_FONT` 为本机中文字体路径。部署目录为 runtime/tmp/pose-review，目录 0700、文件 0600，绝不挂载到静态路由。2 GiB 容量或剩余空间不足 1 GiB 时暂停，不自动删除。删除该环境变量并重启服务即可停用；浏览器退出只关闭浏览器访问，已明确授权的后台采集继续。`/api/v1/status` 的 pose_review 返回运行状态、已保存数量和错误。

### 人物照片标注（0.5.0）

登录后进入 `/photos`，分页查看照片和识别信息，逐人修正人物、姿态及动作，或排除误检照片。新增采样保留无标注原片；历史只有审核图的样本明确提示。人工确认在 `CONSOLE_DATA_DIR/photo-labels` 独立保存图片、预测、人工标签和历史修订，临时采样删除后仍保留。导出的 `dataset.jsonl` 仅含最新确认结果；训练前按 `original_available` 筛选原片。修改冲突需重新载入。不会自动训练或修改人脸库。详情见 [标注设计](../docs/photo-annotation-design.md)。

2026-10-03：审核采样改为仅检测到人体或人脸时保存，至少间隔 30 秒；无人不保存。历史照片保留。Mini console 0.4.1。

### 家庭统一登录（0.6.2）

`CONSOLE_SSO_CONFIG_FILE` 指向当前运行用户所有、权限严格为 `0600` 的普通私有 JSON 文件（不能是符号链接，最大 16 KiB）。不设置该变量时沿用原本地登录；一旦设置，配置无效就拒绝启动，不回退本地密码。内容示例仅为占位符：

```json
{
  "issuer_origin": "https://home.aimaker.vip",
  "backchannel_url": "http://127.0.0.1:80",
  "client_id": "car",
  "client_secret": "REPLACE_WITH_SHARED_RANDOM_SECRET_AT_LEAST_32_CHARACTERS",
  "redirect_uri": "https://car.aimaker.vip/auth/sso/callback"
}
```

只允许上述固定 HTTPS 域名、回调与本机 HTTP 后端，要求 `CONSOLE_ORIGIN=https://car.aimaker.vip` 和安全 Cookie。私有密钥与家庭控制台客户端注册一致，实际文件保存在部署私有目录，不进入源码。交换授权码、查询会话和退出请求通过 loopback，禁用环境代理及重定向；向本机 Nginx 传递固定 `Host: home.aimaker.vip` 和 `X-Forwarded-Proto: https`。Nginx 应只接受受信 loopback 来源的该 HTTPS 转发声明，并关闭 `/auth/sso/` 的带查询串访问日志。

入口 `/`、`/photos` 自动转到家庭控制台登录。事务使用独立的 Secure、HttpOnly、SameSite=Strict、无 Domain 的 Cookie，服务端保存随机 state 和 S256 PKCE verifier，5 分钟过期且仅用一次，返回路径仅允许 `/` 或 `/photos`。两个子域必须始终使用 HTTPS。仅家庭控制台 `admin` 可进入，`viewer` 不提升为管理员。原 admin 密码、数据库 generation、Pi 设备 token 和私有 WSS 认证不变；启用 SSO 后原密码登录/改密及未关联的旧浏览器会话全部拒绝，改密入口引导至家庭控制台。

每个受保护 HTTP 请求实时核验上游会话；浏览器控制/音视频 WSS 连接前核验，运行中最长缓存 2 秒并由独立监视任务复核。失效、降权或认证服务不可用时停止浏览器控制/媒体访问，保留 Pi 租约/急停/失联保护和已单独授权的后台采样。上游授权只在内存关联本地会话；服务重启后需要重新 SSO，不持久化上游 token。API `/api/v1/auth/session` 返回实际用户名、`auth_mode: sso` 和现有 CSRF 证明。

退出先验证本地会话、原 Origin 与 CSRF，再关闭本地会话/控制/媒体并请求家庭控制台撤销关联登录；不依赖上游 introspection 可用。上游退出失败时本地仍退出，页面暂停轮询和控制重连，持续显示失败信息与家庭控制台退出入口，不自动重新 SSO。家庭控制台退出、密码变更或账号降权会在后续核验中撤销 car 权限。

本地测试使用模拟 issuer 和模拟设备，不访问真实机器人：`uv run pytest robot-console/tests/test_sso.py robot-console/tests/test_security.py robot-console/tests/test_online.py`；退出页面的轮询/重连竞态通过 `node robot-console/tests/test_sso_frontend.mjs` 验证，电池卡估算通过 `node robot-console/tests/test_battery_frontend.mjs` 验证。发布需保留原私有数据库/配置及上一版环境；关闭 SSO 的回滚必须明确恢复原版本及配置，不能把配置错误当作回滚开关。
