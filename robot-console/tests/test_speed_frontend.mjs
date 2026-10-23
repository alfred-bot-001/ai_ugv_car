// Joysticks: the wheels message carries the chosen T1 ceiling, the gimbal never does;
// the knob stays put and the held states are relayed on a timer (no handshake).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function clampSpeed');
const end = source.indexOf('\n', source.indexOf('function stickMessage'));
const context = {};
vm.runInNewContext(source.slice(start, end) + '\nthis.clampSpeed=clampSpeed;this.stickMessage=stickMessage;', context);
const {clampSpeed, stickMessage} = context;

// Anything unreadable falls back to the historical 0.10 default; the band is 0.07..1.3
// (the factory rating of the UGV Rover, raised from 0.18 on 2026-10-23).
assert.equal(clampSpeed(undefined), 0.1);
assert.equal(clampSpeed('abc'), 0.1);
assert.equal(clampSpeed('0.15'), 0.15);
assert.equal(clampSpeed(0.3), 0.3);
assert.equal(clampSpeed(1.3), 1.3);
assert.equal(clampSpeed(2), 1.3);
assert.equal(clampSpeed(0.01), 0.07);

const wheels = stickMessage('wheels', {x: 0.5, y: -1}, 'lease-x', 7, 0.15);
// JSON compare: objects built inside the vm context have a different prototype.
assert.equal(JSON.stringify(wheels), JSON.stringify({target: 'wheels', x: 0.5, y: -1, lease: 'lease-x', sequence: 7, speed: 0.15}));

const gimbal = stickMessage('gimbal', {x: 0, y: 1}, 'lease-x', 8, 0.15);
assert.equal('speed' in gimbal, false);
assert.equal('challenge' in gimbal, false);
assert.equal(gimbal.sequence, 8);

// No auto-release timer, no per-command handshake: the held states go out on a timer
// and pointer release leaves the knob where it is (only a centred knob stops).
for (const page of ['app.js', 'navigation.js']) {
  const text = fs.readFileSync(new URL('../src/tvpet_console/web/' + page, import.meta.url), 'utf8');
  assert.equal(text.includes('setTimeout(releaseStick'), false, page);
  assert.equal(text.includes('challenge'), false, page);
  assert.ok(text.includes('setInterval(sendStick'), page);
  assert.ok(text.includes("data.code==='lease'"), page);
  assert.ok(text.includes('endDrag'), page);
  assert.equal(text.includes("element.addEventListener(kind,releaseStick)"), false, page);
}
for (const page of ['dashboard.html', 'navigation.html']) {
  const html = fs.readFileSync(new URL('../src/tvpet_console/web/' + page, import.meta.url), 'utf8');
  assert.equal(html.includes('每次最长 2 秒'), false, page);
  assert.equal(html.includes('松手即停'), false, page);
}

console.log('joystick frontend ok');
