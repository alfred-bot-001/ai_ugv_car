// Game-style keys (keypad.js): W/A/S/D are the wheels, I/J/K/L the gimbal (the arrows stay free to scroll), held keys
// add up inside the unit circle, and keys typed into fields or dialogs are ignored.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/keypad.js', import.meta.url), 'utf8');
const context = {};
vm.runInNewContext(source + '\nthis.KEY_BINDINGS=KEY_BINDINGS;this.keypadVector=keypadVector;this.keypadIgnores=keypadIgnores;', context);
const {KEY_BINDINGS, keypadVector, keypadIgnores} = context;
// Objects built inside the vm context have another prototype: compare the components.
const same = (vector, x, y) => assert.deepEqual([vector.x, vector.y], [x, y]);

assert.deepEqual(Object.keys(KEY_BINDINGS).sort(), ['KeyA', 'KeyD', 'KeyI', 'KeyJ', 'KeyK', 'KeyL', 'KeyS', 'KeyW']);
for (const code of ['KeyW', 'KeyA', 'KeyS', 'KeyD']) assert.equal(KEY_BINDINGS[code].target, 'wheels', code);
for (const code of ['KeyI', 'KeyJ', 'KeyK', 'KeyL']) assert.equal(KEY_BINDINGS[code].target, 'gimbal', code);
// The arrow keys are no longer bound: they scroll the page as usual.
for (const code of ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']) assert.equal(code in KEY_BINDINGS, false, code);

// Nothing held for a target → null (the page centres and drops that knob).
assert.equal(keypadVector(new Set(), 'wheels'), null);
assert.equal(keypadVector(new Set(['KeyI']), 'wheels'), null);
same(keypadVector(new Set(['KeyW']), 'wheels'), 0, 1);
same(keypadVector(new Set(['KeyS']), 'wheels'), 0, -1);
same(keypadVector(new Set(['KeyA']), 'wheels'), -1, 0);
same(keypadVector(new Set(['KeyD']), 'wheels'), 1, 0);
same(keypadVector(new Set(['KeyI']), 'gimbal'), 0, 1);
same(keypadVector(new Set(['KeyJ']), 'gimbal'), -1, 0);
same(keypadVector(new Set(['KeyK']), 'gimbal'), 0, -1);
same(keypadVector(new Set(['KeyL']), 'gimbal'), 1, 0);
assert.equal(keypadVector(new Set(['ArrowUp', 'ArrowLeft']), 'gimbal'), null);
// Opposite keys cancel; a diagonal is scaled onto the unit circle like a dragged knob.
same(keypadVector(new Set(['KeyW', 'KeyS']), 'wheels'), 0, 0);
const diagonal = keypadVector(new Set(['KeyW', 'KeyD', 'KeyI']), 'wheels');
assert.ok(Math.abs(diagonal.x - Math.SQRT1_2) < 1e-9 && Math.abs(diagonal.y - Math.SQRT1_2) < 1e-9);
same(keypadVector(new Set(['KeyW', 'KeyD', 'KeyI']), 'gimbal'), 0, 1);
assert.equal(keypadVector(new Set(['Space']), 'wheels'), null);

// Ignore rules: repeats, modifier chords, IME composition, fields, editable text, open dialogs.
const noDialog = {querySelector: () => null};
const withDialog = {querySelector: selector => (selector === 'dialog[open]' ? {} : null)};
const event = (overrides = {}) => ({repeat: false, altKey: false, ctrlKey: false, metaKey: false, isComposing: false, target: {tagName: 'BODY'}, ...overrides});
assert.equal(keypadIgnores(event(), noDialog), false);
assert.equal(keypadIgnores(event({target: {tagName: 'DIV'}}), noDialog), false);
assert.equal(keypadIgnores(event({repeat: true}), noDialog), true);
assert.equal(keypadIgnores(event({metaKey: true}), noDialog), true);
assert.equal(keypadIgnores(event({ctrlKey: true}), noDialog), true);
assert.equal(keypadIgnores(event({altKey: true}), noDialog), true);
assert.equal(keypadIgnores(event({isComposing: true}), noDialog), true);
for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) assert.equal(keypadIgnores(event({target: {tagName}}), noDialog), true, tagName);
assert.equal(keypadIgnores(event({target: {tagName: 'DIV', isContentEditable: true}}), noDialog), true);
assert.equal(keypadIgnores(event({target: null}), noDialog), false);
assert.equal(keypadIgnores(event(), withDialog), true);

// Both pages load the module and bind the keys at document level; the per-pad arrow
// handlers are gone, and the pads sit on the live picture.
for (const page of ['app.js', 'navigation.js']) {
  const text = fs.readFileSync(new URL('../src/tvpet_console/web/' + page, import.meta.url), 'utf8');
  assert.ok(text.includes("document.addEventListener('keydown',e=>{const binding=KEY_BINDINGS[e.code]"), page);
  assert.ok(text.includes('keypadVector(heldKeys,target)'), page);
  assert.equal(text.includes('ARROWS'), false, page);
}
for (const page of ['dashboard.html', 'navigation.html']) {
  const html = fs.readFileSync(new URL('../src/tvpet_console/web/' + page, import.meta.url), 'utf8');
  assert.ok(html.indexOf('/static/keypad.js') < html.indexOf(page === 'dashboard.html' ? '/static/app.js' : '/static/navigation.js'), page);
  const stage = html.indexOf('class="video-stage"');
  assert.ok(html.indexOf('id="stick-wheels"') > stage && html.indexOf('id="stick-gimbal"') > html.indexOf('id="stick-wheels"'), page);
  assert.ok(html.includes('class="stage-sticks"'), page);
  // The gimbal pad and the hints name I/J/K/L; no arrow glyphs are advertised as hotkeys.
  assert.ok(html.includes('<span class="north"><b>I</b></span><span class="south"><b>K</b></span><span class="west"><b>J</b></span><span class="east"><b>L</b></span>'), page);
  assert.ok(html.includes('<kbd>I</kbd> <kbd>J</kbd> <kbd>K</kbd> <kbd>L</kbd>'), page);
  assert.equal(html.includes('<kbd>↑</kbd>') || html.includes('方向键'), false, page);
}

console.log('keypad frontend ok');
