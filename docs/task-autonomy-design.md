# 老三任务与巡航自主设计（S0 安全补强 / S1 地点与巡航）

版本：0.1；日期：2026-10-15；状态：**设计稿，未实现**。本文承接 [ROS2 建图与自主导航设计](ros-navigation-design.md) 第 7 节 N4“与视觉/对话联动、定时巡航”，把“能自主走到坐标”推进到“能在房间里按名字执行任务和巡航”。大模型与 MCP 一侧写在 AI_Home 仓库的“小车任务 Agent 设计”（`AI_Home/docs/ugv-task-agent-design.md`；该仓库没有对外的 GitHub 地址，这里只写路径）。开发前先读 [架构文档](ARCHITECTURE.md)。

## 1. 结论

- 采用四层结构，越往下越快、权限越高，下层可以否决上层：

  | 层 | 位置 | 周期 | 职责 | 状态 |
  | --- | --- | --- | --- | --- |
  | L1 反射层 | `pi-agent`（`NavBridge`、`Board`） | 毫秒级 | 授权、限速、deadline、急停、失联停车、失控/卡住检测 | 已有；S0 补卡住检测 |
  | L2 导航层 | `ugv-nav` 容器 + 主机 `ugv-navctl` | 约 100 ms | SLAM/AMCL、Nav2 规划避障、运动门禁、航点巡航 | 已有；S0 补定位健康、禁行区，S1 补地点与巡航执行器 |
  | L3 语义层 | Pi worker + Mini 推理 | 约 1 s | 识别结果入图、物品观察 | S2，本文不展开 |
  | L4 认知层 | AI_Home Agent Runtime（Mini） | 秒～分钟 | 大模型理解指令、规划任务、调用任务级工具、汇报 | S3，见 AI_Home 设计 |

- **大模型永远不输出速度或坐标**。它只能引用本文定义的“地点”和“路线”名字，由 `ugv-navctl` 校验后执行；运动仍然逐段由 `arm.json` 授权，`pi-agent` 是最终裁决者（ARCHITECTURE 约束 3）。
- S0 先补首次巡航暴露的两个安全缺口（低矮障碍卡车、位姿跳变），再加禁行区和低电量处理。S0 完成前不做 S1 的无人值守巡航。
- S1 引入与地图绑定的**地点文件**和按地点名书写的**路线 v2**，把现有 `patrol` 扩展为带前置检查、失败策略、定点观察和报告的巡航执行器，并给 AI_Home 提供一个固定命令集的**任务接口**。

## 2. 现状基线（2026-10-14 夜）

| 组件 | 现有行为 | 代码 |
| --- | --- | --- |
| 运动授权 | `ugv-navctl` 是 `arm.json` 唯一写入者；每个目标一个 ≤120 s 窗口，`finally` 中撤销并停车 | `ugv-nav/src/ugv_nav_tools/navctl.py` |
| Pi 侧裁决 | 校验 arm（0600、未过期、已标定）、硬件资格、急停、遥测 ≤0.3 s、命令间隔 ≤0.25 s；限速 0.12 m/s、0.6 rad/s，只前进；轮速 PI 闭环 | `pi-agent/src/tvpet_pi/nav.py` |
| 失控检测 | 任一轮实测 > `RUNAWAY_SPEED` 0.3 m/s 时急停 | `pi-agent/src/tvpet_pi/hardware.py` |
| 容器侧门禁 | 激光里程或扫描超过 0.35 s 未更新则停；前方 0.30 m 内有障碍时只允许经地图验证的原地转向 | `ugv-nav/src/ugv_nav_tools/motion_gate.py` |
| 导航调用 | 先规划，再 `NavigateToPose`；到点判定为 Nav2 成功且误差 ≤10 cm、0.18 rad；结束时总是停车 | `ugv-nav/src/ugv_nav_tools/ros_ops.py` |
| 巡航 | 坐标航点（≤20 点、≤10 圈），任一段失败即停止，报告写入 `reports/patrol-*.json` | `route.py`、`navctl.py` |

首次巡航发现的问题（[导航设计 §7.4](ros-navigation-design.md)）：

1. **低矮障碍卡车**：约 50 s 内左轮 0.06～0.19 m/s、右轮为 0，车不动，雷达前方为空，直到 Nav2 约 60 s 的进度检查才中止。
2. **位姿跳变**：第 4 段期间位姿约 1 分钟内跳变约 0.9 m，疑似原地转向时 SLAM 匹配错误。
3. 巡航只能写坐标，换地图后路线全部失效；没有前置检查、电量判断和观察动作。

## 3. S0 安全补强

S0 的所有检测都只会**停车**，从不发起运动。每项检测有独立参数常量和单元测试，触发后在报告中写明原因。

### 3.1 卡住检测（两道独立检测）

**A. Pi 侧：单轮堵转**（`pi-agent`，不依赖 ROS）

`NavBridge._command` 已经拿到每侧目标轮速和实测轮速。新增 `StallMonitor`：

- 条件：某一侧目标 |target| ≥ 0.04 m/s，PI 输出已到 `PWM_LIMIT` 的 90% 以上，而该侧实测 |measured| < 0.01 m/s。
- 持续 `STALL_WHEEL_SECONDS = 1.5` s 即普通停车（不是急停），撤销导航租约，状态写入 `stall: "wheel"`。
- 原地转向起步时静摩擦大，PI 积分需要时间。1.5 s 先作为初值，在 S0 验收时用实车起步曲线校准；目标是不误报正常起步。
- 停车后同一 arm 会话不再接受运动，必须由 `ugv-navctl` 重新授权（复用现有“control revoked; re-arm required”机制）。

**B. 容器侧：轮子在转、车体不动**（`bridge_node` + `motion_gate`，覆盖两侧都打滑的情况）

桥已经订阅激光里程（`seen.laser_odom_at`），也能拿到轮式里程。新增纯函数 `stalled(window)`，在滑动窗口内比较：

- 轮式里程累计行程 ≥ 0.10 m，或累计转角 ≥ 0.5 rad；
- 同一窗口内激光里程位移 < 0.03 m 且转角 < 0.08 rad；
- 窗口长度 `STALL_BODY_SECONDS = 2.0` s。

满足时调用 `client.stop()`，并把 `command_note` 设为 `stalled: wheels turning, body still`。`ros_ops.navigate` 读取桥状态，在结果中加 `"stall": true`；`patrol` 遇到 stall 一律终止整次巡航，不重试（需要人去处理障碍）。

**实现记录（2026-10-21，pi-agent 0.4.3 / ugv-nav 0.3.5，未上车验收）**：

- A 的饱和条件改为“PI 积分项达到上限的 90%”，不用“输出达到 `PWM_LIMIT` 的 90%”。按现有增益，目标轮速低于约 0.114 m/s 时输出永远到不了 0.162，而导航常用 0.10 m/s，原条件会让检测形同虚设。其余条件不变：目标 ≥ 0.04 m/s、实测 < 0.01 m/s、持续 1.5 s。
- 两道检测都只停车，并在 pi-agent 记下 `fault`（`stall_wheel_left/right` 或 `stall_body`），本次 arm 会话作废；`ugv-navctl` 下一段新建 arm 会话时清除。容器侧通过 `{"op":"stop","reason":"stall_body"}` 通知 pi-agent。
- `ugv-ros-op navigate` 每 0.5 s 查一次 `fault`，出现就取消 Nav2 目标，并把 `fault` 写进结果；巡航本来就在第一段失败时终止。
- 代码：`pi-agent/src/tvpet_pi/nav.py`（`StallMonitor`）、`ugv-nav/src/ugv_nav_tools/stall.py`、`bridge_node.py`、`ros_ops.py`。

**验收**：在雷达扫描面以下放一个低矮障碍（例如门槛条或软垫），低速驾驶撞上。要求两道检测中至少一道在 **3 s 内**停车并报告原因；正常起步、原地转向和地毯过渡各 10 次不误报。

### 3.2 定位健康监测

SLAM 建图和 AMCL 定位两种模式都会发布 `map→odom` 变换，用它统一检测“定位突然跳变”。新增容器内节点 `ugv-loc-health`（纯逻辑放在 `loc_health.py`，rclpy 部分单独放，和现有包结构一致），每 0.2 s 计算一次：

| 指标 | 判定 | 说明 |
| --- | --- | --- |
| `map→odom` 跳变 | 1 s 内平移变化 > 0.25 m，或转角变化 > 0.35 rad | 正常修正是平滑的；10-14 的事故约 0.9 m |
| 扫描与地图匹配率 | 扫描端点落在地图占用格（容差 1 格）中的比例 < 0.45，持续 2 s | 只在定位模式下判定；建图模式下地图本身在变化 |
| AMCL 协方差 | x/y 标准差 > 0.30 m，或 yaw 标准差 > 0.35 rad | 只在定位模式下判定 |

任一项触发时：停车并取消 Nav2 目标；桥拒绝后续非零 `/cmd_vel`，直到健康恢复且收到新的 arm 会话；状态发布为 `localization: lost` 并附带触发的指标。

S0 **只停车并报告**，不自动重新定位。S1 提供人工确认后执行的 `ugv-navctl relocalize`：原地低速旋转一圈（复用经验证的原地转向空间检查），期间不前进；匹配率恢复到 0.6 以上才算成功。

**实现记录（2026-10-21，ugv-nav 0.3.5，未上车验收）**：`ugv_nav_tools/loc_health.py`（纯逻辑）和 `loc_health_node.py`（入口 `ugv-loc-health`，由 `n3_navigation` 启动，`--localization` 跟随启动参数）。每 0.2 s 判定一次；不健康时，若 pi-agent 还没有 `fault`，就发送 `{"op":"stop","reason":"localization_lost"}`。新 arm 会话开始时如果仍不健康会再次停车，所以“健康恢复且新的 arm 会话”两个条件都满足才能运动。状态发布在 `/localization/health`（JSON）。跳变判定在跳变后保持约 1 s，直到旧样本移出窗口。

阈值来源：0.25 m / 0.35 rad 先取到点容差的约 2.5 倍，需要用 S0 采集的正常巡航记录核对误报率，并在本文中记录实测分布。

### 3.3 禁行区

使用 Nav2 Humble 自带的 Keepout Filter（`costmap_filters_info_server` + 掩码地图），两层代价地图都启用。

- 禁行区以多边形写在 S1 的地点文件中（§4.1，`zones`），由 `ugv-navctl zones apply MAP` 生成与地图同分辨率、同原点的 `maps/<map>.keepout.pgm/.yaml`。没有地点文件时不生成掩码，行为与现在一致。
- 典型用途：沙发和床底（低于雷达）、电线区、楼梯口、宠物食盆。
- 禁行区只影响规划，不能替代 L1 停车。运动门禁仍然生效。
- **实现记录（2026-10-21，ugv-nav 0.3.5，未上车验收）**：`ugv-navctl place MAP zone ID NAME X,Y …` 记录多边形，`ugv-navctl place MAP apply-zones` 生成 `maps/<map>.keepout.pgm/.yaml`（`map_files.py`，格子中心在多边形内即禁行）。两层代价地图都配置了 `KeepoutFilter`；`ugv-navctl localize MAP` 发现掩码文件时给启动参数加 `keepout:=…`，`n3_navigation` 才启动掩码服务和过滤信息服务，否则和以前完全一样。家里第一处禁行区是阳台（有门槛和台阶）。
- 验收：在地图上画一块挡住直线路径的禁行区，导航目标设在禁行区另一侧；路径必须绕行，若无法绕行则规划失败且车不动。

### 3.4 电量与返航

遥测 `v` 是电池电压（`hardware.py` 已经校验 `v > 0`）。S0 只做“不出发”和“停下”，不做自动充电，车上也没有充电座。

- 新增 Pi 配置 `battery.min_start_v`、`battery.min_leg_v`、`battery.critical_v`，网页不能修改。**初值需要在 S0 用实车放电曲线确定**，在此之前取保守值并写在配置注释中。
- `ugv-navctl` 在每次授权前读取 10 s 内电压的中位数：低于 `min_start_v` 时拒绝开始巡航；巡航中低于 `min_leg_v` 时停止剩余路段，若地点文件有 `home` 则只导航回 home 一段；低于 `critical_v` 时原地停车并报告。
- 电机负载会拉低电压，因此只用停车状态下的电压做判定。
- **实现记录（2026-10-21，ugv-nav 0.3.5，未上车验收）**：阈值按主控台电池格数（用户 2026-10-21 采纳）：`BATTERY_START_V` 10.0 V（2 格，低于不出发）、`BATTERY_LEG_V` 9.5 V（1 格，巡航中低于就终止剩余站点，地点文件有 `home` 时只再导航回 home 一段）、`BATTERY_CRITICAL_V` 9.2 V（原地停车，连 home 也不回）。目前写在 `navctl.py` 常量里，还没有单独的 Pi 配置文件。电压取两轮静止（< 0.01 m/s）时的读数，3 s 内至少 5 个取中位数；原设计的 10 s 窗口会让每一段多等 10 s，所以缩短。阈值要用实车放电曲线核对。巡航报告新增 `abort_reason`（`battery`，或 pi-agent `fault` 的原因）。

### 3.5 S0 验收汇总

| 编号 | 场景 | 要求 |
| --- | --- | --- |
| S0-1 | 低矮障碍卡车 | ≤3 s 停车，报告 `stall` 原因；巡航终止不重试 |
| S0-2 | 正常起步、原地转向、地毯 | 各 10 次，无卡住误报 |
| S0-3 | 人为制造定位跳变（定位中搬动小车约 1 m） | ≤1 s 停车，`localization: lost`；新 arm 前拒绝运动 |
| S0-4 | 完整巡航 3 次 | 无定位健康误报，记录三项指标的分布 |
| S0-5 | 禁行区 | 绕行或规划失败不动 |
| S0-6 | 低电压（用配置把阈值临时调高来模拟） | 拒绝出发；巡航中只返回 home |
| S0-7 | 既有停止场景 | [导航设计 N3](ros-navigation-design.md) 中的停止场景全部回归通过 |

所有实车验收需要用户在场并清场，与 N3 相同。

## 4. S1 地点与巡航

### 4.1 地点文件

每张地图一个地点文件 `maps/<map>.places.json`，与地图放在同一个 0700 目录：

```json
{
  "version": 1,
  "map": "room-20261014",
  "map_sha256": "…",
  "places": [
    {"id": "home", "name": "起点", "aliases": ["家", "原点"], "kind": "home", "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0}},
    {"id": "sofa", "name": "沙发前", "aliases": ["客厅"], "kind": "observe", "pose": {"x": 1.9, "y": 0.4, "yaw": 1.57}}
  ],
  "zones": [
    {"id": "under-bed", "name": "床底", "kind": "keepout", "polygon": [[2.0, 1.0], [3.2, 1.0], [3.2, 2.1], [2.0, 2.1]]}
  ]
}
```

规则：

- `map_sha256` 是 `map.pgm` 的哈希。加载时哈希不一致就拒绝使用，避免重建地图后沿用错位的旧地点。重建地图后需要重新标注地点（S1 不做自动迁移）。
- `id` 只能是 `[a-z0-9-]{1,32}`，供程序和 AI_Home 引用；`name`、`aliases` 是中文显示名和语音别名，全部在一张地图内唯一。
- 最多 50 个地点、20 个禁行区，每个多边形 3～32 个顶点。
- 保存地点时校验：坐标有限、在地图范围内，并且以该点为中心、半径 `TURN_CLEAR_RADIUS_M` 内的格子全部为已知空闲（直接复用 `motion_gate.rotation_space_clear`），保证到点后能原地转向和观察。
- `kind`：`home`（最多一个，返航和低电量的目标）、`observe`（到点后执行观察动作）、`waypoint`（只是途经）。
- 文件用临时文件 + `os.replace` 原子写入，权限 0600。

新增命令（都不会运动）：

| 命令 | 作用 |
| --- | --- |
| `ugv-navctl place list [--json]` | 列出当前地图的地点和禁行区 |
| `ugv-navctl place add ID NAME [--kind K] [--here \| X Y YAW]` | `--here` 取当前定位位姿；推车或遥控到位后记录最方便 |
| `ugv-navctl place rm ID` | 删除地点 |
| `ugv-navctl zones apply` | 由 `zones` 生成禁行区掩码（§3.3） |

**实现记录（2026-10-21，ugv-nav 0.3.5）**：`ugv_nav_tools/places.py`、`map_files.py`。在上面的格式上增加了两项，供“自主找人”使用：`rooms`（房间 `id`、`name`、`aliases`，例如卧室、书房、厨房、客厅、客房）；地点可选 `room`（所属房间）和 `scan`（观察时的云台水平角，度，-180～180，最多 9 个）。实际命令：

- `ugv-navctl place MAP list`
- `ugv-navctl place MAP room ID NAME [--alias A]…`
- `ugv-navctl place MAP add ID NAME [--kind home|observe|waypoint] [--room R] [--alias A]… [--scan=-90,0,90] (--here | --at X Y YAW)`；保存前检查转向空间
- `ugv-navctl place MAP zone ID NAME X,Y X,Y X,Y…`、`ugv-navctl place MAP rm ID`、`ugv-navctl place MAP apply-zones`
- `ugv-navctl goto PLACE`：只在“已在保存的地图上定位”时可用，目标只能来自地点文件。

S1 先用命令行标注；robot-console“地图与导航”页面（[导航设计 §5.3](ros-navigation-design.md)）实现后，在地图上点选地点，调用同一套校验。

### 4.2 按地点导航

`ugv-navctl goto ID` 等价于 `goal`，区别在于目标只能来自地点文件，并且前面多了一步前置检查（§4.4）。现有的 `goal X Y YAW` 保留给操作者调试，但**不对 AI_Home 开放**（§5）。

### 4.3 路线 v2

```json
{
  "version": 2,
  "map": "room-20261014",
  "name": "evening",
  "loops": 1,
  "stops": [
    {"place": "door", "observe": "photo"},
    {"place": "sofa", "observe": "sweep", "dwell_s": 5},
    {"place": "home"}
  ],
  "on_fail": "home",
  "max_duration_s": 900
}
```

- 路线文件放在 `routes/<name>.route.json`，与地图名绑定；地点不存在或地图不匹配时，在任何授权之前就拒绝。
- `observe`：`none`（默认）、`photo`（云台回正拍一张）、`sweep`（云台转到 -40°、0°、+40° 各拍一张）。
- `on_fail`：`stop`（原地停车，默认）或 `home`（只再尝试导航回 home 一段）。
- 上限：≤20 站、≤10 圈、`max_duration_s` ≤ 1800。
- 现有 v1 坐标路线继续支持（没有 `version` 字段即为 v1），只供操作者使用。

### 4.4 巡航执行器

`patrol` 改为一个显式状态机，纯逻辑放在 `patrol.py`，方便在开发机测试：

```text
preflight ──失败──▶ refused（车不动）
    │通过
    ▼
for 每一站:
    plan ─规划失败─▶ 失败策略
    arm(≤120 s) → navigate → verify(到点容差)
    observe（车停稳、已撤销授权后执行）
    记录本站结果
    ▼
done / aborted ─▶ on_fail 处理 ─▶ 报告
```

**前置检查**（每次巡航开始前，以及每一站授权前）：

1. 导航容器处于定位模式，且加载的地图名与路线一致；
2. 定位健康（§3.2）在最近 3 s 内正常；
3. 电压满足 §3.4；
4. 没有急停，手动圆盘没有占用底盘（`status` 中 `nav_owned` 和 `estopped`）；
5. 距离开始时间未超过 `max_duration_s`。

**失败策略**：

| 失败类型 | 处理 |
| --- | --- |
| 规划失败（目标不可达） | 跳过本站，记为 `unreachable`；连续 2 站不可达则终止 |
| Nav2 超时或中止，且没有卡住/定位丢失 | 等 3 s 后重试本站一次；仍失败则终止 |
| `stall`、`localization: lost`、急停、手动接管 | 立即终止，**不重试，也不执行 `on_fail: home`**，等人处理 |
| 电压低于 `min_leg_v` | 终止剩余站；按 §3.4 只返回 home |

这里的原则是：凡是表明“环境或定位已经不可信”的失败，都不让车继续自己动。

**观察动作**：车在站点停稳、arm 已撤销之后，由 `pi-agent` 执行固定的云台动作并拍照。`pi-agent` 是摄像头和云台的唯一所有者，因此在本地套接字上新增一个请求：

```json
{"op": "observe", "preset": "sweep", "patrol_id": "…", "stop_index": 2}
```

- 只在底盘静止（两轮实测 < 0.01 m/s）、没有手动云台租约、没有急停时接受；云台角度由 `pi-agent` 的固定预设决定，请求里不能带角度。
- 复用已有的采集通道取新帧（与“新帧拍照”相同的 sequence/timestamp 校验），保存为 `reports/patrol-<id>/stop-<n>-<k>.jpg`，目录 0700。
- 拍照失败只记录到报告里，不影响导航。

**照片保留**：巡航照片属于新的家庭影像数据，需要用户另行授权保留策略（ARCHITECTURE 约束 6）。在用户明确之前：照片只存在 Pi 本地，最多保留最近 10 次巡航或 7 天，取先到者；不自动上传 Mini，也不进入人脸或姿态训练数据。

### 4.5 巡航报告 v2

```json
{
  "version": 2,
  "patrol_id": "20261015-201500-evening",
  "route": "evening",
  "map": "room-20261014",
  "trigger": "operator | schedule | ai-home",
  "request_id": "…",
  "started_at": "2026-10-15T20:15:00+08:00",
  "ended_at": "…",
  "outcome": "completed | aborted | refused",
  "abort_reason": "stall | localization_lost | battery | estop | manual | timeout | unreachable | …",
  "battery_v": {"start": 0.0, "end": 0.0},
  "stops": [
    {"index": 1, "place": "door", "status": "reached | unreachable | failed | skipped",
     "duration_s": 24.0, "error_m": 0.041, "error_rad": 0.05,
     "photos": ["stop-1-0.jpg"], "notes": []}
  ]
}
```

报告里的“到达”只表示“Nav2 报告成功且误差在容差内”，与“现场确认”是不同证据（ARCHITECTURE 约束 7）。报告不写入照片内容，只写文件名。

### 4.6 定时巡航

S1 只实现**有人值守的定时触发**：

- Pi 上新增 systemd timer `ugv-patrol@<route>.timer`，到点运行 `ugv-navctl patrol --route NAME --trigger schedule`。
- 开关在 Pi 配置 `schedule.json` 中，默认 `enabled: false`，另有允许时段（例如 09:00–21:00）和最小间隔。
- **无人值守**（家中没人时自动巡航）需要 S0 全部验收通过、S1 连续 20 次有人值守巡航无安全事件，再由用户单独批准。批准记录写进本文。在此之前，定时巡航开始前通过音箱播报“老三准备开始巡航”，并留出 10 s，期间按网页急停或手动圆盘即可取消。

### 4.7 给 AI_Home 的任务接口

AI_Home 的 `ugv-mcp` 适配器不进入 ROS，也不写 `arm.json`。它只通过一个**固定命令网关**使用以下命令：

| 网关命令 | 映射 | 是否运动 |
| --- | --- | --- |
| `status` | 底盘、定位健康、电压、当前任务 | 否 |
| `places` | `place list --json` | 否 |
| `routes` | 列出 `routes/` 中与当前地图匹配的路线 | 否 |
| `reports [--limit N]`、`report ID` | 读取巡航报告 v2（不含照片） | 否 |
| `goto PLACE_ID --request-id R` | §4.2 | **会** |
| `patrol ROUTE --request-id R --trigger ai-home` | §4.4 | **会** |
| `cancel` | 等价于 `stop` | 停车 |

实现方式：Pi 上新增 `ugv-task-gate` 入口，作为专用 SSH 密钥的 `command=` 强制命令。它只解析上表中的命令，参数必须匹配 `[a-z0-9-]` 白名单，然后调用 `ugv-navctl` 的 Python API，并统一输出一行 JSON。不提供 `goal X Y`、`mapping`、`save`、`pose`、`localize`，也不提供 shell。

- `--request-id` 用于幂等：同一个 ID 在 24 h 内重复提交时，返回原任务状态，不会再次运动。
- 同一时刻只能有一个运动任务；正在运动时收到新的 `goto`/`patrol` 会被拒绝，需要先 `cancel`。
- 网关**不能绕过任何 L1/L2 检查**，也不新增速度、坐标或时长参数。

## 5. 安全不变式（S0/S1 均须保持，测试覆盖）

1. 只有 `ugv-navctl` 能写 `arm.json`；每个授权窗口 ≤120 s，`finally` 中一定撤销并停车。
2. `pi-agent` 的所有既有检查（资格、急停、租约、challenge、deadline、失控、遥测新鲜度）不因任何新功能放宽。
3. 任何新增检测只能导致停车，不能导致运动。
4. 从 AI_Home 或定时器发起的运动，只能引用地点 ID 和路线名，不能携带坐标、速度或时长。
5. 卡住、定位丢失、急停或手动接管之后，系统不会自行恢复运动。
6. 网页手动急停仍由用户解除，导航和 AI_Home 都不能解除。

## 6. 实施顺序与测试

| 步骤 | 内容 | 测试 |
| --- | --- | --- |
| S0.1 | `StallMonitor`（pi-agent）与 `stalled()`（ugv-nav） | 单元测试：起步、单轮堵转、双轮打滑、阈值边界 |
| S0.2 | `loc_health.py` 与 `ugv-loc-health` 节点 | 回放 10-14 巡航的 `map→odom` 记录（如有）或合成跳变序列 |
| S0.3 | 禁行区掩码生成与 Nav2 配置 | 多边形栅格化与地图原点/分辨率对齐的单元测试 |
| S0.4 | 电量门槛 | 配置校验、中位数窗口、只用停车电压 |
| S0.5 | 实车验收 S0-1～S0-7 | 用户在场 |
| S1.1 | 地点文件解析、校验、原子写入，`place` 命令 | 哈希不符、重名、越界、转向空间不足 |
| S1.2 | 路线 v2 与巡航状态机 | 每种失败类型对应的处理；v1 兼容 |
| S1.3 | `pi-agent` 的 `observe` 请求 | 运动中拒绝、云台租约冲突、拍照失败不影响导航 |
| S1.4 | 报告 v2、照片保留清理 | 保留上限与 7 天清理 |
| S1.5 | `ugv-task-gate` | 白名单、幂等、并发拒绝、非法参数 |
| S1.6 | 定时器（默认关闭）与开始前播报 | 时段、间隔、取消 |
| S1.7 | 实车：按地点名巡航 20 次 | 用户在场；记录成功率和每类失败 |

所有新增 Python 遵循现有工程约定：纯逻辑与 rclpy 分离，`uv run ruff check . && uv run mypy && uv run pytest` 通过，版本号随改动提升，wheel 部署并保留上一版本用于回滚。

## 7. 未决问题

1. 电池三档电压阈值：需要实车放电曲线（S0.4 前确认）。
2. 巡航照片的长期保留、上传 Mini 以及是否用于识别：需要用户明确授权。
3. 无人值守巡航的批准条件是否接受 §4.6 的“20 次无安全事件”。
4. 是否增加低成本硬件（前保险杠微动开关或 ToF 测距）来弥补雷达看不到的低矮障碍。这是 S0 卡住检测之外唯一能做到“碰到之前就停”的办法。
5. 10-14 的位姿跳变需要复现，确认是 SLAM 匹配问题还是 rf2o/EKF 问题，再决定 §3.2 阈值是否足够。
