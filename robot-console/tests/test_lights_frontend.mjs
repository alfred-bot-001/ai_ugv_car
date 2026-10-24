// Light switches on the live video: each click flips one channel, and the reply the
// browser just got wins over a stale polled status for a few seconds (no flicker).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function lightMessage');
const end = source.indexOf('\n', source.indexOf('function lightState'));
const context = {};
vm.runInNewContext(source.slice(start, end) + '\nthis.lightMessage=lightMessage;this.lightState=lightState;', context);
const {lightMessage, lightState} = context;

// One channel per message; unknown state counts as off, so the first click turns it on.
assert.equal(JSON.stringify(lightMessage('base', {base: false, head: true})), JSON.stringify({base: true}));
assert.equal(JSON.stringify(lightMessage('head', {base: false, head: true})), JSON.stringify({head: false}));
assert.equal(JSON.stringify(lightMessage('base', undefined)), JSON.stringify({base: true}));

// Polled status is the truth, unless a fresher reply is still within its hold window.
assert.equal(lightState('base', {base: true}, {}, 1000), true);
assert.equal(lightState('base', {}, {}, 1000), false);
assert.equal(lightState('base', {base: false}, {base: {on: true, until: 3000}}, 1000), true);
assert.equal(lightState('base', {base: false}, {base: {on: true, until: 3000}}, 3001), false);
assert.equal(lightState('head', {head: true}, {base: {on: false, until: 3000}}, 1000), true);
console.log('lights frontend ok');
