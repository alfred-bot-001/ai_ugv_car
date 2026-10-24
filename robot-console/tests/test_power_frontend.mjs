// Sleep mode on the overview page: the diagnostics row says whether the car is asleep
// (LiDAR USB power cut after thirty quiet minutes) and how long it has been idle.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function powerText');
const end = source.indexOf('\n', start);
assert.ok(start > 0, 'powerText is defined');
const context = {};
vm.runInNewContext(source.slice(start, end) + '\nthis.powerText=powerText;', context);
const {powerText} = context;

assert.equal(powerText(undefined), '未知');
assert.equal(powerText({state: 'awake', idle_seconds: 42, lidar: 'on'}), '清醒 · 空闲 0 分钟');
assert.equal(powerText({state: 'awake', idle_seconds: 1500, lidar: 'on'}), '清醒 · 空闲 25 分钟');
assert.equal(powerText({state: 'asleep', idle_seconds: 7200, lidar: 'off'}), '休眠中 · 雷达已断电 · 空闲 120 分钟');
// The LiDAR state is only worth a word when it disagrees with the sleep state.
assert.equal(powerText({state: 'asleep', idle_seconds: 1800, lidar: 'on'}), '休眠中 · 雷达仍供电 · 空闲 30 分钟');
assert.equal(powerText({state: 'awake', idle_seconds: 0, lidar: 'off'}), '清醒 · 雷达已断电 · 空闲 0 分钟');
assert.equal(powerText({state: 'awake', idle_seconds: 0, lidar: 'missing'}), '清醒 · 雷达未找到 · 空闲 0 分钟');
console.log('power frontend ok');
