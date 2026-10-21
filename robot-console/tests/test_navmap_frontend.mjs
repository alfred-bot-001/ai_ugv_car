// Map page helpers: packet parsing, view transforms, zoom, fit, recoloring and wording.
// Run with: node robot-console/tests/test_navmap_frontend.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/navmap.js', import.meta.url), 'utf8');
const names = ['parseNavPacket', 'worldToScreen', 'screenToWorld', 'zoomAt', 'mapBounds', 'fitView',
  'recolorMap', 'formatAge', 'areaText', 'healthText', 'faultText', 'modeText', 'logLevelClass', 'scaleBar'];
const context = {TextDecoder, DataView, Uint8Array, JSON, Math, Number, Date, String, Array};
vm.runInNewContext(source + '\n' + names.map(n => `this.${n}=${n};`).join(''), context);
const h = context;

// Packets: "!I" metadata length, JSON, payload.
const meta = new TextEncoder().encode(JSON.stringify({kind: 'map', width: 2}));
const buffer = new ArrayBuffer(4 + meta.length + 3);
new DataView(buffer).setUint32(0, meta.length);
new Uint8Array(buffer).set(meta, 4);
new Uint8Array(buffer).set([1, 2, 3], 4 + meta.length);
const parsed = h.parseNavPacket(buffer);
assert.equal(parsed.meta.kind, 'map');
assert.deepEqual([...parsed.payload], [1, 2, 3]);
assert.throws(() => h.parseNavPacket(new ArrayBuffer(2)));
const bad = new ArrayBuffer(8);
new DataView(bad).setUint32(0, 100);
assert.throws(() => h.parseNavPacket(bad));

// View: y points up in the world and down on screen; the round trip is exact.
const view = {cx: 1, cy: 2, scale: 50, width: 400, height: 300};
assert.deepEqual([...h.worldToScreen(view, 1, 2)], [200, 150]);
assert.deepEqual([...h.worldToScreen(view, 2, 3)], [250, 100]);
const [wx, wy] = h.screenToWorld(view, 250, 100);
assert.ok(Math.abs(wx - 2) < 1e-9 && Math.abs(wy - 3) < 1e-9);

// Zoom keeps the point under the cursor in place and clamps the scale.
const zoomed = h.zoomAt(view, 300, 50, 2);
assert.equal(zoomed.scale, 100);
const before = h.screenToWorld(view, 300, 50), after = h.screenToWorld(zoomed, 300, 50);
assert.ok(Math.abs(before[0] - after[0]) < 1e-9 && Math.abs(before[1] - after[1]) < 1e-9);
assert.equal(h.zoomAt(view, 0, 0, 1e6).scale, 600);
assert.equal(h.zoomAt(view, 0, 0, 1e-6).scale, 8);

// Fit centers the map and scales it inside the margins.
const bounds = h.mapBounds({origin: [-1, -2], width: 100, height: 40, resolution: 0.05});
assert.deepEqual({...bounds}, {minX: -1, minY: -2, maxX: 4, maxY: 0});
const fit = h.fitView(view, bounds, 20);
assert.equal(fit.cx, 1.5);
assert.equal(fit.cy, -1);
assert.equal(fit.scale, 72);

// Recolor: the three relay palette entries, tolerating small decoder shifts.
const pixels = new Uint8ClampedArray([245, 247, 244, 255, 32, 52, 45, 255, 207, 210, 206, 255, 10, 200, 10, 255]);
h.recolorMap(pixels, [[1, 1, 1, 255], [2, 2, 2, 255], [0, 0, 0, 0]]);
assert.deepEqual([...pixels], [1, 1, 1, 255, 2, 2, 2, 255, 0, 0, 0, 0, 10, 200, 10, 255]);

// Wording.
assert.equal(h.formatAge(0.5), '刚刚');
assert.equal(h.formatAge(12.4), '12 秒前');
assert.equal(h.formatAge(125), '2 分钟前');
assert.equal(h.formatAge(NaN), '—');
assert.equal(h.areaText({free_cells: 400, resolution: 0.05}), '1.0 m²');
assert.equal(h.healthText(null).level, 'neutral');
assert.deepEqual({...h.healthText({mode: 'localization', health: {healthy: true, localization: true, match: 0.874}})},
  {text: '定位正常 · 匹配 87%', level: 'good'});
assert.equal(h.healthText({health: {healthy: false, reason: 'scan matches the map only 33%', match: 0.33}}).text,
  '激光与地图只匹配 33% · 匹配 33%');
assert.equal(h.healthText({health: {healthy: false, reason: 'AMCL uncertainty 0.29 m / 0.46 rad'}}).text,
  '定位不确定度过大（0.29 m / 0.46 rad）');
assert.equal(h.healthText({health: {healthy: false, reason: 'map->odom jumped 0.40 m / 0.10 rad within 1 s'}}).level, 'fault');
assert.equal(h.faultText({reason: 'stall_body'}), '车轮在转但车没动，已安全停车');
assert.equal(h.faultText({reason: 'stall_wheel_right'}), '右轮转不动，已安全停车');
assert.equal(h.faultText(null), '');
assert.equal(h.modeText({mode: 'localization', map: 's0-living'}), '定位导航 · s0-living');
assert.equal(h.modeText({mode: 'mapping'}), '建图中');
assert.equal(h.logLevelClass('WARN'), 'warn');
assert.equal(h.logLevelClass('FATAL'), 'error');
assert.equal(h.logLevelClass('INFO'), 'info');
assert.equal(h.scaleBar(60), 2);
assert.equal(h.scaleBar(300), 0.2);
console.log('navmap ok');
