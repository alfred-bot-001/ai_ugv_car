// Battery card estimate: voltage bands, smoothing, and holding the reading while the wheels are driven.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function batteryLevel');
const end = source.indexOf('\n', source.indexOf('function batteryText'));
const end2 = source.indexOf('\n', end + 1);
const context = {};
vm.runInNewContext(source.slice(start, end2) + '\nthis.batteryLevel=batteryLevel;this.batteryText=batteryText;', context);
const {batteryLevel, batteryText} = context;

assert.equal(batteryLevel(12.22), 4);
assert.equal(batteryLevel(10.7), 3);
assert.equal(batteryLevel(10.0), 2);
assert.equal(batteryLevel(9.8), 1);
assert.equal(batteryLevel(9.3), 0);
assert.equal(batteryLevel(0), null);
assert.equal(batteryLevel(undefined), null);

let card = batteryText(undefined, false);
assert.equal(card.value, '—');
assert.equal(card.detail, '等待电压数据');

card = batteryText(12.2, false);
assert.equal(card.value, '12.20 V');
assert.equal(card.level, 4);
assert.equal(card.detail, '约 4/4 格 · 电压估算');

// A sag under load does not move the estimate while driving.
card = batteryText(10.4, true);
assert.equal(card.value, '10.40 V');
assert.equal(card.level, 4);
assert.match(card.detail, /行驶中保持/);

// One low sample after stopping is smoothed, not shown as a drop.
card = batteryText(11.0, false);
assert.equal(card.level, 4);

// A real low pack reaches the warning band after a few samples.
for (let i = 0; i < 30; i++) card = batteryText(9.2, false);
assert.equal(card.level, 0);
assert.equal(card.detail, '电量低，请尽快充电 · 电压估算');

// Lost telemetry resets the estimate; driving with no prior estimate says so.
batteryText(0, false);
card = batteryText(11.0, true);
assert.equal(card.level, null);
assert.equal(card.detail, '行驶中，停车后估算');
console.log('battery frontend ok');
