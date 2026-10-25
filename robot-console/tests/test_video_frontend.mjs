// Shared live-video helpers (video.js): the gimbal camera's v2 frame splits into its
// metadata and JPEG, and the face overlay clamps boxes to the picture.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/video.js', import.meta.url), 'utf8');
const context = {TextDecoder, DataView, Uint8Array};
vm.runInNewContext(source + '\nthis.parseVideoPacket=parseVideoPacket;this.drawFaces=drawFaces;this.drawPoses=drawPoses;', context);
const {parseVideoPacket, drawFaces, drawPoses} = context;

function packet(meta, jpeg) {
  const body = new TextEncoder().encode(JSON.stringify(meta));
  const bytes = new Uint8Array(4 + body.length + jpeg.length);
  new DataView(bytes.buffer).setUint32(0, body.length);
  bytes.set(body, 4);
  bytes.set(jpeg, 4 + body.length);
  return bytes.buffer;
}

// A frame like the Mini sends: metadata first, the JPEG after it, untouched.
const frame = parseVideoPacket(packet({faces: [{label: '妈妈', known: true, bbox: [1, 2, 3, 4]}], inference_ms: 12}, new Uint8Array([0xff, 0xd8, 0xff])));
assert.equal(frame.meta.faces[0].label, '妈妈');
assert.equal(frame.meta.inference_ms, 12);
assert.deepEqual(Array.from(frame.jpeg), [0xff, 0xd8, 0xff]);

// Junk never reaches createImageBitmap: too short, a length past the end, or no JPEG at all.
assert.throws(() => parseVideoPacket(new Uint8Array([0, 0]).buffer), /视频帧格式无效/);
assert.throws(() => parseVideoPacket(new Uint8Array([0, 0, 0, 9, 123, 125]).buffer), /视频帧信息无效/);
assert.throws(() => parseVideoPacket(packet({}, new Uint8Array(0))), /视频帧信息无效/);

// Faces: boxes are clamped to the picture, empty ones skipped, labels kept inside.
const calls = [];
const fake = {
  measureText: text => ({width: text.length * 10}),
  strokeRect: (...args) => calls.push(['stroke', ...args]),
  fillRect: (...args) => calls.push(['fill', ...args]),
  fillText: (text, x, y) => calls.push(['text', text, x, y]),
};
drawFaces(fake, [
  {label: '妈妈', known: true, bbox: [-10, -10, 100, 50]},
  {label: '陌生人', known: false, bbox: [600, 400, 700, 500]},
  {label: 'bad', bbox: [10, 10, 5, 5]},
  {label: 'junk', bbox: ['a', 1, 2, 3]},
], 640, 480);
assert.deepEqual(calls.filter(c => c[0] === 'stroke'), [['stroke', 0, 0, 100, 50], ['stroke', 600, 400, 40, 80]]);
assert.equal(calls.filter(c => c[0] === 'text').length, 2);
assert.equal(fake.strokeStyle, '#ffd47c'); // the last face drawn was a stranger

// Poses: nothing is drawn without 33 keypoints, and at most three people get a line.
const poses = [];
const pen = {beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, arc() {}, fill() {}, fillRect: () => poses.push('bar'), fillText() {}};
drawPoses(pen, {people: [{keypoints: []}]}, 640, 480);
assert.equal(poses.length, 0);
const person = {track_id: 1, posture_label: '站', action_label: '走', keypoints: Array.from({length: 33}, () => [0.5, 0.5, 0, 0.9])};
drawPoses(pen, {people: [person, person, person, person]}, 640, 480);
assert.equal(poses.length, 3);
console.log('video frontend ok');
