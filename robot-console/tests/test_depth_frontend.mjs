// Depth view helpers. Run with: node robot-console/tests/test_depth_frontend.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/depth.js', import.meta.url), 'utf8');
const context = {TextDecoder, DataView, Uint8Array, JSON, Math, Number, Error};
vm.runInNewContext(source + '\nthis.parse=parseDepthPacket;this.text=depthText;this.scale=depthScaleText;', context);

const meta = new TextEncoder().encode(JSON.stringify({sequence: 1, centre_mm: 1234, nearest_mm: 456, valid: 0.627}));
const packet = new ArrayBuffer(4 + meta.length + 4);
new DataView(packet).setUint32(0, meta.length);
new Uint8Array(packet).set(meta, 4);
new Uint8Array(packet).set([0xff, 0xd8, 1, 2], 4 + meta.length);
const parsed = context.parse(packet);
assert.equal(parsed.meta.centre_mm, 1234);
assert.deepEqual([...parsed.jpeg], [0xff, 0xd8, 1, 2]);
assert.equal(context.text(parsed.meta), '中心 1.23 m · 最近 0.46 m · 有效 63%');
assert.equal(context.text({centre_mm: null, nearest_mm: 0}), '中心 测不到 · 最近 测不到');
assert.deepEqual([...context.scale({near_mm: 300, far_mm: 4000})], ['0.3 m', '4 m']);
assert.deepEqual([...context.scale(null)], ['0.3 m', '4 m']);
assert.throws(() => context.parse(new ArrayBuffer(3)));
const bad = new ArrayBuffer(10);
new DataView(bad).setUint32(0, 99);
assert.throws(() => context.parse(bad));
console.log('depth frontend ok');
