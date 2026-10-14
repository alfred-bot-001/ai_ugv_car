// Deterministic DOM/network stubs exercise the logout-versus-poll race without a browser/device.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/tvpet_console/web/app.js', import.meta.url), 'utf8');

async function checkLogout(statusCode, bodyRace = false) {
  const elements = new Map();
  const timers = [];
  const redirects = [];
  const requests = [];
  const sockets = [];
  let finishPendingPoll;
  const pendingPoll = new Promise(resolve => { finishPendingPoll = resolve; });
  function element() {
    return {
      textContent: '', hidden: false, disabled: false, dataset: {}, style: {}, children: [],
      classList: { remove() {}, add() {} }, setAttribute() {}, addEventListener() {},
      querySelector: () => element(),
      getContext: () => ({ clearRect() {} }), append(...children) { this.children.push(...children); },
    };
  }
  function select(selector) {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  }
  const response = (status, data) => ({status, ok: status < 400, json: async () => data});
  class Socket {
    constructor() { sockets.push(this); }
    close() { this.onclose?.(); }
    send() {}
  }
  vm.runInNewContext(source, {
    document: {
      querySelector: select, querySelectorAll: () => [], addEventListener() {},
      createElement: () => element(),
    },
    window: {addEventListener() {}},
    location: {protocol: 'https:', host: 'car.aimaker.vip', replace: value => redirects.push(value)},
    WebSocket: Socket,
    setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout() {},
    fetch: async path => {
      requests.push(path);
      if (path === '/api/v1/auth/session') return response(200, {csrf: 'test-csrf'});
      if (path === '/api/v1/status') return bodyRace ? {status: 200, ok: true, json: () => pendingPoll} : pendingPoll;
      assert.equal(path, '/api/v1/auth/logout');
      return response(statusCode, statusCode === 200 ? {ok: true} : {detail: '上游退出失败，请确认退出'});
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sockets.length, 1);
  assert.equal(requests.at(-1), '/api/v1/status');
  await select('#logout').onclick();
  finishPendingPoll(bodyRace ? {online: false} : response(401, {}));
  await new Promise(resolve => setImmediate(resolve));
  for (const callback of timers) callback();
  assert.equal(sockets.length, 1, 'logout must not reconnect browser control');
  assert.equal(requests.filter(path => path === '/api/v1/status').length, 1, 'poll must stop');
  if (statusCode === 200) {
    assert.deepEqual(redirects, ['/']);
  } else {
    assert.deepEqual(redirects, [], 'partial logout must never immediately restart SSO');
    assert.match(select('#connection-banner').textContent, statusCode === 401 ? /登录已失效/ : /上游退出失败/);
    const link = select('#connection-banner').children.find(child => typeof child === 'object');
    assert.equal(link.href, 'https://home.aimaker.vip/');
  }
}

await checkLogout(503);
await checkLogout(401);
await checkLogout(503, true);
await checkLogout(200);
console.log('SSO logout UI: failure persistence, successful exit and pending-poll race passed');
