# tof-bridge：SEN0628 8×8 ToF 矩阵 → USB 串口桥

老三车头的低矮障碍传感器。DFRobot SEN0628（8×8 ToF 矩阵测距，I2C 地址 0x33）接到 Seeed XIAO ESP32-C5，XIAO 用原生 USB 接树莓派，pi-agent（`tvpet_pi.tof`）读文本帧并做本地停车。树莓派 40 针排针被驱动板占满，所以不走 I2C 直连。

不用 DFRobot_MatrixLidar 官方库：它每轮询一个状态字节固定 `delay(17)`，整帧只有约 4 Hz；本固件直接按传感器协议收发，台架实测 15 Hz（传感器 8×8 模式每 66 ms 出一帧）、请求到应答 4.5 ms、30 s 无坏帧。官方树莓派 Python 版本有多处 bug，同样不用。

## 接线

| 传感器（Gravity 4 针） | XIAO ESP32-C5 | 说明 |
| --- | --- | --- |
| VCC | 3V3 | 传感器 3.3～5 V 供电 |
| GND | GND | |
| C/R（SCL） | D5 = GPIO24 | `Wire.begin()` 默认 SCL |
| D/T（SDA） | D4 = GPIO23 | `Wire.begin()` 默认 SDA |

传感器拨码置 I2C；I2C 400 kHz 实测正常。

## 编译与烧录（Mac，arduino-cli）

板型 `esp32:esp32:XIAO_ESP32C5`，arduino-esp32 核心 3.3.12（核心包在 `~/Library/Arduino15`），USB CDC On Boot 开启（`CDCOnBoot=cdc`，也是该板的默认项）。

```sh
arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C5 --board-options CDCOnBoot=cdc firmware/tof-bridge
arduino-cli upload -p /dev/cu.usbmodem21401 --fqbn esp32:esp32:XIAO_ESP32C5 --board-options CDCOnBoot=cdc firmware/tof-bridge
```

Mac 上的端口用 `arduino-cli board list` 找（ESP32 Family Device）。烧录完 XIAO 自动复位。

## 串口识别

- Mac：`/dev/cu.usbmodem*`，`arduino-cli board list` 显示 `ESP32 Family Device`。
- 树莓派（Linux）：XIAO 原生 USB 是 Espressif 的 USB JTAG/serial 设备，稳定路径为  
  `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`（本车 MAC `10:BD:A3:CF:46:78`）。
  **一定按 by-id 配置**，不要写 `ttyACMn`：车上 `ttyACM0` 已被其他设备占用，而 D500 雷达的 CP2102 序列号是 0001，`ttyUSB*` 顺序也不可靠。
- 打开串口会让 XIAO 复位，约 10 s 后才出数据帧（1.5 s 启动 + 设 8×8 模式 + 传感器 5 s 稳定 + 前几次读重试）。pi-agent 的启动宽限期覆盖这段时间。
- pi-agent 是该串口的唯一所有者（`exclusive=True`），不能再有第二个进程读它；标定要先停 `ai-tv-pet-agent.service`。

## 输出格式（pi-agent 按此解析，勿改）

115200 8N1，每行以 `\n` 结束：

```
T,<seq>,<millis>,<d0>,…,<d63>,<sum16>    矩阵与上一帧不同
H,<seq>,<millis>,<d0>,…,<d63>,<sum16>    心跳：300 ms 内矩阵没变化时重发一次
#<文字>                                   状态行，解析器忽略
```

- 64 个值按行 y0..y7、每行 x0..x7，单位 mm，`4000` 表示无有效回波。
- `sum16` 是 64 个值之和的低 16 位，pi-agent 用它校验整行。
- 上电头几行是 ESP32 ROM 的启动日志，不以 `T`/`H`/`#` 开头，解析器当坏行计数后忽略。

## 传感器协议（实测，固件 V1.3 行为）

- 请求 `55 LEN_H LEN_L CMD ARGS…`，LEN = 参数数 + 1，大端，无校验。
- 应答 `STATUS CMD LEN_L LEN_H PAYLOAD`，STATUS `0x53` 成功、`0x63` 失败（payload[0] 为错误码）、`0xFF` 未就绪需继续轮询；LEN 小端。
- 设 8×8 模式 `55 00 05 01 00 00 00 08`，成功后等 5 s。
- 读全矩阵 `55 00 01 02`，payload 128 字节 = 64 个 uint16 小端。
- 复位后第一次读会收到一个 CMD 不匹配的残留应答（固件打印 `# read r=-12`），重试即可；连续 30 次读失败固件会重新设模式。

## 版本

- 1.0.0（2026-10-24）：首版，按 `/tmp` 台架 sketch `tof_bridge2` 整理，协议与输出格式不变。
