// Front ToF strip: the polled `status.tof` block becomes a 64-cell heat map plus one line of
// text; anything but a healthy 64-value frame shows a state word, never an empty grid or an error.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('const TOF_INVALID');
const end = source.indexOf('let tofRenderedAt');
const context = {};
vm.runInNewContext(source.slice(start, end) + '\nthis.tofColor=tofColor;this.tofSummary=tofSummary;this.tofText=tofText;this.tofCellTitle=tofCellTitle;this.TOF_RENDER_MS=TOF_RENDER_MS;', context);
const {tofColor, tofSummary, tofText, tofCellTitle, TOF_RENDER_MS} = context;

// Colour: near is red (hue 0), far is blue (hue 240), clamped; no return and junk stay grey.
assert.equal(tofColor(100), 'hsl(0 75% 48%)');
assert.equal(tofColor(40), 'hsl(0 75% 48%)');
assert.equal(tofColor(800), 'hsl(120 75% 48%)');
assert.equal(tofColor(1500), 'hsl(240 75% 48%)');
assert.equal(tofColor(3999), 'hsl(240 75% 48%)');
assert.equal(tofColor(4000), '');
assert.equal(tofColor(0), '');
assert.equal(tofColor(undefined), '');
assert.equal(tofColor(NaN), '');

// A frame like the car's: rows 0-3 look ahead (no return), rows 4-7 see the floor nearer and nearer.
const frame = [];
for (let row = 0; row < 8; row++) for (let col = 0; col < 8; col++) frame.push(row < 4 ? 4000 : 150 + (7 - row) * 80);
const active = {state: 'active', hz: 15.1, values: frame, baseline: true, reason: '', obstacle_cells: [], cliff_cells: []};

let summary = tofSummary(active, null);
assert.equal(summary.state, 'active');
assert.equal(summary.label, '正常');
assert.equal(summary.hz, 15.1);
assert.equal(summary.values.length, 64);
assert.equal(summary.nearest, 150);
assert.equal(summary.nearestIndex, 56); // bottom row, first column
assert.equal(summary.verdict, '正常');
assert.equal(summary.stop, null);
assert.equal(tofText(summary), 'ToF 正常 · 15.1 Hz · 最近 150 mm（第8行第1列） · 判定 正常');

// A picked cell is read out; the hover title says the same.
assert.equal(tofText(summary, 56), 'ToF 正常 · 15.1 Hz · 最近 150 mm（第8行第1列） · 判定 正常 · 第8行第1列 · 150 mm');
assert.equal(tofCellTitle(0, 4000), '第1行第1列 · 无回波');
assert.equal(tofCellTitle(9, null), '第2行第2列 · 无数据');

// The detector's verdict and the cells behind it, with junk cell indexes dropped.
summary = tofSummary({...active, reason: 'tof_low_obstacle', obstacle_cells: [60, 61, 62, 99, -1, 'x']}, null);
assert.equal(summary.verdict, '低矮障碍');
assert.deepEqual(summary.obstacle, [60, 61, 62]);
assert.equal(tofText(summary), 'ToF 正常 · 15.1 Hz · 最近 150 mm（第8行第1列） · 判定 低矮障碍 · 障碍格 3');
summary = tofSummary({...active, reason: 'tof_cliff', cliff_cells: [56, 57, 58, 59]}, null);
assert.equal(summary.verdict, '悬崖');
assert.match(tofText(summary), / · 失地格 4$/);
assert.equal(tofSummary({...active, reason: 'something_new'}, null).verdict, 'something_new');

// No floor baseline yet: the sensor streams but cannot judge.
summary = tofSummary({...active, baseline: false}, null);
assert.equal(summary.verdict, '无基准');

// Every cell at 4000 (sensor pointed at the sky, or a black rug) is "no return", not 0 mm.
summary = tofSummary({...active, values: new Array(64).fill(4000)}, null);
assert.equal(summary.nearest, null);
assert.equal(summary.nearestIndex, -1);
assert.equal(tofText(summary), 'ToF 正常 · 15.1 Hz · 最近 无回波 · 判定 正常');

// Malformed values (wrong length, non-integers) are "no data" rather than a crash or a wrong colour.
assert.equal(tofSummary({...active, values: frame.slice(0, 63)}, null).values, null);
assert.equal(tofText(tofSummary({...active, values: null}, null)), 'ToF 正常 · 15.1 Hz · 无数据');
summary = tofSummary({...active, values: [1.5, 'a', null, -3, ...frame.slice(4)]}, null);
assert.deepEqual(summary.values.slice(0, 4), [4000, 4000, 4000, 4000]);

// Other states: off, booting, broken, offline, and a stale frame shipped with a fault is not drawn.
assert.equal(tofText(tofSummary({state: 'disabled'}, null)), 'ToF 未启用');
assert.equal(tofText(tofSummary(undefined, null)), 'ToF 未启用');
assert.equal(tofText(tofSummary({state: 'starting', hz: 0, values: null}, null)), 'ToF 启动中');
summary = tofSummary({state: 'fault', error: '等待传感器数据帧', hz: 0, values: frame}, null);
assert.equal(summary.values, null);
assert.equal(tofText(summary), 'ToF 故障：等待传感器数据帧');
assert.equal(tofText(tofSummary(active, null, false)), 'ToF 设备离线');
assert.equal(tofSummary({state: 42}, null).label, '未启用');

// The Board's last sensor stop rides along, with the Pi's reason codes in Chinese.
summary = tofSummary(active, {reason: 'tof_low_obstacle', at: 1_761_300_000});
assert.equal(summary.stop.text, '低矮障碍');
assert.equal(summary.stop.at, 1_761_300_000);
assert.match(tofText(summary), /^ToF 正常 · 15\.1 Hz · 最近 150 mm（第8行第1列） · 判定 正常 · 上次停车 低矮障碍 \d{2}:\d{2}:\d{2}$/);
assert.match(tofText(tofSummary({state: 'disabled'}, {reason: 'tof_cliff', at: 'bad'})), /^ToF 未启用 · 上次停车 悬崖$/);
assert.equal(tofSummary(active, {reason: 'stall_body', at: 1}).stop.text, 'stall_body');
assert.equal(tofSummary(active, {reason: '', at: 1}).stop, null);
assert.equal(tofSummary(active, null).stop, null);

// Repaints are capped at five a second.
assert.equal(TOF_RENDER_MS, 200);
console.log('tof frontend ok');
