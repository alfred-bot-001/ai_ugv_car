// tof-bridge: DFRobot SEN0628 8x8 ToF matrix (I2C 0x33) -> USB CDC text frames.
//
// Runs on a Seeed XIAO ESP32-C5 (arduino-esp32 3.3.12, FQBN esp32:esp32:XIAO_ESP32C5,
// USB CDC On Boot enabled). Talks the sensor's own request/response protocol instead
// of the DFRobot_MatrixLidar library, whose one-byte status polling with a fixed
// delay(17) caps the matrix at about 4 Hz; this bridge sustains the sensor's 15 Hz.
//
// Sensor protocol (bench-verified 2026-10-24, sensor firmware V1.3 behaviour):
//   request  : 55 LEN_H LEN_L CMD ARGS...        LEN = number of args + 1, big-endian
//   response : STATUS CMD LEN_L LEN_H PAYLOAD... LEN little-endian
//              STATUS 0x53 ok, 0x63 failed (payload[0] = error), 0xFF not ready yet
//   CMD 0x01 set mode, args 00 00 00 08 = 8x8; wait 5 s after success
//   CMD 0x02 read the whole matrix: 128-byte payload, 64 x uint16 little-endian,
//            rows y0..y7, each x0..x7, millimetres, 4000 = no valid return
//   After a reset the first read may return a stale response for another CMD; retry.
//
// Serial output (115200 8N1, newline-terminated, parsed by pi-agent tvpet_pi.tof):
//   T,<seq>,<millis>,<d0>,...,<d63>,<sum16>   a matrix that differs from the last one
//   H,<seq>,<millis>,<d0>,...,<d63>,<sum16>   heartbeat: unchanged for HEARTBEAT_MS
//   #<text>                                    status line, ignored by the parser
//   sum16 = sum of the 64 values, low 16 bits.
#include <Wire.h>

static const char *VERSION = "tof-bridge 1.0.0";
static const uint8_t SENSOR_ADDR = 0x33;
static const uint32_t I2C_HZ = 400000;
static const uint8_t CMD_SET_MODE = 0x01;
static const uint8_t CMD_READ_ALL = 0x02;
static const uint8_t STATUS_OK = 0x53;
static const uint8_t STATUS_FAIL = 0x63;
static const uint32_t RESPONSE_TIMEOUT_MS = 500;
static const uint32_t MODE_SETTLE_MS = 5000;  // the sensor needs 5 s after a mode change
static const uint32_t HEARTBEAT_MS = 300;     // repeat an unchanged matrix this often
static const uint32_t READ_PERIOD_MS = 5;     // the sensor itself produces a frame per 66 ms
static const uint32_t REINIT_AFTER_FAILS = 30;  // consecutive read failures -> set mode again
static const uint8_t CELLS = 64;

static uint16_t values[CELLS];
static uint16_t lastValues[CELLS];
static bool haveLast = false;
static uint32_t seq = 0;
static uint32_t lastEmitMs = 0;
static uint32_t fails = 0;
static uint32_t statPolls = 0, statUs = 0, statFrames = 0;

static void sendCommand(uint8_t cmd, const uint8_t *args, uint8_t nargs) {
  Wire.beginTransmission(SENSOR_ADDR);
  Wire.write(0x55);
  Wire.write((uint8_t)0);
  Wire.write((uint8_t)(nargs + 1));
  Wire.write(cmd);
  for (uint8_t i = 0; i < nargs; i++) Wire.write(args[i]);
  Wire.endTransmission(true);
}

// Read n bytes in <=64-byte requests; -1 when the sensor does not answer.
static int readBytes(uint8_t *dst, size_t n) {
  size_t got = 0;
  while (got < n) {
    size_t chunk = min((size_t)64, n - got);
    size_t r = Wire.requestFrom(SENSOR_ADDR, chunk, true);
    if (r == 0) return -1;
    for (size_t i = 0; i < r && got < n; i++) dst[got++] = Wire.read();
  }
  return (int)got;
}

// Poll for the response to `cmd`; returns the payload length or a negative code:
// -10/-11/-14 no I2C answer, -12 response for another command, -13 payload too long,
// -20 timeout, -(100 + error) when the sensor reports failure.
static int receiveResponse(uint8_t cmd, uint8_t *payload, size_t maxlen, uint32_t *polls) {
  uint32_t started = millis();
  *polls = 0;
  while (millis() - started < RESPONSE_TIMEOUT_MS) {
    uint8_t status;
    if (readBytes(&status, 1) < 0) return -10;
    (*polls)++;
    if (status == STATUS_OK || status == STATUS_FAIL) {
      uint8_t header[3];
      if (readBytes(header, 3) < 0) return -11;
      if (header[0] != cmd) return -12;
      uint16_t len = header[1] | (header[2] << 8);
      if (len > maxlen) return -13;
      if (len && readBytes(payload, len) < 0) return -14;
      if (status == STATUS_FAIL) return -(100 + (len ? payload[0] : 0));
      return len;
    }
    delay(1);  // 0xFF (or anything else): the sensor is still working on it
  }
  return -20;
}

// Put the sensor into 8x8 mode, retrying until it acknowledges, then let it settle.
static void configureSensor() {
  static const uint8_t mode8x8[4] = {0, 0, 0, 8};
  uint8_t payload[8];
  uint32_t polls;
  for (;;) {
    sendCommand(CMD_SET_MODE, mode8x8, sizeof mode8x8);
    int r = receiveResponse(CMD_SET_MODE, payload, sizeof payload, &polls);
    Serial.print("# setmode r=");
    Serial.print(r);
    Serial.print(" polls=");
    Serial.println(polls);
    if (r >= 0) break;
    delay(500);
  }
  delay(MODE_SETTLE_MS);
  haveLast = false;
  fails = 0;
  Serial.println("# ready");
}

static void emit(char tag, const uint16_t *v) {
  uint16_t sum = 0;
  Serial.print(tag);
  Serial.print(',');
  Serial.print(seq++);
  Serial.print(',');
  Serial.print(millis());
  for (uint8_t i = 0; i < CELLS; i++) {
    Serial.print(',');
    Serial.print(v[i]);
    sum += v[i];
  }
  Serial.print(',');
  Serial.println(sum);
}

void setup() {
  Serial.begin(115200);
  delay(1500);  // give the host's CDC enumeration time to see the boot lines
  Serial.print("# ");
  Serial.println(VERSION);
  Wire.begin();  // XIAO ESP32-C5 defaults: D4 = GPIO23 SDA, D5 = GPIO24 SCL
  Wire.setClock(I2C_HZ);
  Wire.setTimeOut(50);
  Serial.print("# i2c addr=0x33 sda=");
  Serial.print(SDA);
  Serial.print(" scl=");
  Serial.println(SCL);
  configureSensor();
}

void loop() {
  uint32_t polls;
  uint32_t started = micros();
  sendCommand(CMD_READ_ALL, nullptr, 0);
  int r = receiveResponse(CMD_READ_ALL, (uint8_t *)values, sizeof values, &polls);
  uint32_t elapsed = micros() - started;
  if (r != (int)sizeof values) {
    fails++;
    if (fails <= 5 || fails % 50 == 0) {
      Serial.print("# read r=");
      Serial.print(r);
      Serial.print(" polls=");
      Serial.print(polls);
      Serial.print(" fails=");
      Serial.println(fails);
    }
    if (fails % REINIT_AFTER_FAILS == 0) configureSensor();
    delay(20);
    return;
  }
  fails = 0;
  statPolls += polls;
  statUs += elapsed;
  statFrames++;
  bool changed = !haveLast || memcmp(values, lastValues, sizeof values) != 0;
  uint32_t now = millis();
  if (changed) {
    memcpy(lastValues, values, sizeof values);
    haveLast = true;
    emit('T', values);
    lastEmitMs = now;
  } else if (now - lastEmitMs >= HEARTBEAT_MS) {
    emit('H', values);
    lastEmitMs = now;
  }
  if (statFrames == 200) {
    Serial.print("# stats: avg polls=");
    Serial.print(statPolls / statFrames);
    Serial.print(" avg req->resp ms=");
    Serial.println(statUs / statFrames / 1000.0, 1);
    statPolls = statUs = statFrames = 0;
  }
  delay(READ_PERIOD_MS);
}
