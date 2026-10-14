# 老三旧机器人服务停用记录

日期：2026-10-01；用户指令：“停掉现有的机器人服务，放弃不用了。”

## 已执行

停用前，通过 companion 应用 `/api/stop` 请求停止，再从其状态接口确认 `stop_latched=true`、`state=stopped`、`velocity=[0.0,0.0]`，原底盘模式为 simulation。此项确认应用状态；未发送实际移动命令。

以下服务均执行 `disable --now`，核实 `ActiveState=inactive`、`UnitFileState=disabled`、`MainPID=0`：

| 范围 | 服务 |
| --- | --- |
| 系统 | companion-app、companion-motion、companion-capture、companion-debug |
| 系统 | legacy-monitor、irobot-display-screensaver |
| 系统，原已禁用 | camera-hub、face-tracker、ugv-remote |
| 用户 ws | companion-display、companion-display-web、irobot-remote-chat-agent-presence |
| 用户 ws，原已禁用 | irobot-remote-chat-agent |

合计 13 个服务。旧桌面自启 `irobot-touch-landscape.desktop.disabled` 设置 `Hidden=true`、`X-GNOME-Autostart-enabled=false`，避免其生成的自启入口恢复旧机器人页面。

## 验证结果

- `/dev/video0`、`/dev/ttyAMA0`、`/dev/m2_mic` 和 USB 音频卡 2/3/4 的 PCM 节点：`sudo fuser -v` 没有设备持有者。
- 5000、5001、8766、8767：无监听。
- 未发现命令行引用旧 companion-v2、irobot、ugv-remote、remote-car-client 或 legacy-monitor 运行目录的残留进程。
- SSH 服务仍为 active；主机网络及系统音频基础服务未变更。
- 旧服务代码和数据未删除；原 unit、自启配置及停用前后状态快照保存在 Pi 私有目录 `/home/ws/deploy/retired-robot-services/20261001-101956/`，目录 0700、快照 0600。该快照不进入 Git。

## 后续约定

新主控台直接实现硬件适配器和采集 worker，不依赖旧机器人端口、相机 hub 或 companion 服务。部署、升级和系统重启验收都应核对这些旧服务仍禁用；新主控台回滚只回滚自身 release，不能自动恢复用户已放弃的旧服务。

本次未执行整机重启。已确认禁用配置和当前资源释放，重启后的状态应在新主控台部署验收时再次核对。
