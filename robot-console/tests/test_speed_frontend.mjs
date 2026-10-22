// Joystick speed slider: the wheels message carries the chosen T1 ceiling, the gimbal never does.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function clampSpeed');
const end = source.indexOf('\n', source.indexOf('function stickMessage'));
const context = {};
vm.runInNewContext(source.slice(start, end) + '\nthis.clampSpeed=clampSpeed;this.stickMessage=stickMessage;', context);
const {clampSpeed, stickMessage} = context;

// Anything unreadable falls back to the historical 0.10 default; the band is 0.07..0.18.
assert.equal(clampSpeed(undefined), 0.1);
assert.equal(clampSpeed('abc'), 0.1);
assert.equal(clampSpeed('0.15'), 0.15);
assert.equal(clampSpeed(0.3), 0.18);
assert.equal(clampSpeed(0.01), 0.07);

const wheels = stickMessage({target: 'wheels', x: 0.5, y: -1}, 'lease-x', 'chal-y', 7, 0.15);
// JSON compare: objects built inside the vm context have a different prototype.
assert.equal(JSON.stringify(wheels), JSON.stringify({target: 'wheels', x: 0.5, y: -1, lease: 'lease-x', challenge: 'chal-y', sequence: 7, speed: 0.15}));

const gimbal = stickMessage({target: 'gimbal', x: 0, y: 1}, 'lease-x', 'chal-y', 8, 0.15);
assert.equal('speed' in gimbal, false);
assert.equal(gimbal.sequence, 8);

// No auto-release timer: holding the stick drives until the pointer is released.
assert.equal(source.includes('setTimeout(releaseStick'), false);
const html = fs.readFileSync(new URL('../src/tvpet_console/web/dashboard.html', import.meta.url), 'utf8');
assert.equal(html.includes('每次最长 2 秒'), false);

console.log('speed frontend ok');
