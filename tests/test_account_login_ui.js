const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loginUI(fetchResult) {
  const requests = [];
  const frames = [];
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const listeners = new Map();
      let src = '';
      elements.set(id, {
        hidden: false, disabled: false, textContent: '',
        addEventListener(name, callback) { listeners.set(name, callback); },
        emit(name, event = {}) { return listeners.get(name)?.(event); },
        showModal() { this.open = true; },
        close() { this.open = false; },
        get src() { return src; },
        set src(value) { src = value; frames.push(value); },
      });
    }
    return elements.get(id);
  }
  const document = element('document');
  document.getElementById = element;
  const context = {
    document, window: {}, console,
    fetch: async (url, options) => {
      requests.push({url, method: options.method});
      const result = await fetchResult(url, options);
      return {ok: result.status < 400, status: result.status, json: async () => result.body};
    },
  };
  const script = fs.readFileSync(path.join(__dirname, '../app/static/account-login.js'), 'utf8');
  vm.runInNewContext(script, context);
  return {
    element, requests, frames,
    async open() {
      document.emit('click', {target: {closest: () => ({dataset: {id: '14'}})}});
      await new Promise(setImmediate);
    },
  };
}

function defaultReply(url) {
  if (url.endsWith('/login/start')) return {status: 200, body: {active: {session_id: 'test-session'}}};
  if (url.endsWith('/viewer')) return {status: 200, body: {url: '/account-login/viewer'}};
  if (url.endsWith('/cancel')) return {status: 200, body: {ok: true}};
  throw new Error('Unexpected mocked request');
}

test('failed verification renews the viewer and keeps the same session retryable', async () => {
  let finishes = 0;
  const ui = loginUI((url) => {
    if (url.endsWith('/finish')) {
      if (++finishes === 1) return {status: 409, body: {detail: 'safe credit read failure'}};
      return {status: 200, body: {id: 14, balance: 123}};
    }
    return defaultReply(url);
  });
  await ui.open();
  assert.match(ui.element('onlineAccountMessage').textContent, /Dreamina/);
  assert.doesNotMatch(ui.element('onlineAccountMessage').textContent, /视频项目/);
  await ui.element('onlineAccountFinish').emit('click');
  assert.equal(ui.requests.filter((r) => r.url.endsWith('/viewer')).length, 2);
  assert.deepEqual(ui.frames, ['/account-login/viewer?viewer_reload=1', '/account-login/viewer?viewer_reload=2']);
  assert.match(ui.element('onlineAccountMessage').textContent, /safe credit read failure/);
  assert.match(ui.element('onlineAccountMessage').textContent, /画面已恢复/);
  assert.equal(ui.element('onlineAccountFinish').disabled, false);
  await ui.element('onlineAccountFinish').emit('click');
  assert.equal(finishes, 2);
  assert.equal(ui.element('onlineAccountDialog').open, false);
  assert.equal(ui.element('onlineAccountFrame').src, 'about:blank');
});

test('viewer recovery failure preserves the verification error', async () => {
  let viewers = 0;
  const ui = loginUI((url) => {
    if (url.endsWith('/finish')) return {status: 409, body: {detail: 'safe credit read failure'}};
    if (url.endsWith('/viewer') && ++viewers > 1) return {status: 409, body: {detail: 'expired viewer'}};
    return defaultReply(url);
  });
  await ui.open();
  await ui.element('onlineAccountFinish').emit('click');
  assert.match(ui.element('onlineAccountMessage').textContent, /safe credit read failure/);
  assert.match(ui.element('onlineAccountMessage').textContent, /画面恢复失败/);
  assert.equal(ui.element('onlineAccountFinish').disabled, false);
});

test('cancelling an in-flight verification does not revive its viewer', async () => {
  let finishResponse;
  const ui = loginUI((url) => {
    if (url.endsWith('/finish')) return new Promise((resolve) => { finishResponse = resolve; });
    return defaultReply(url);
  });
  await ui.open();
  const finishing = ui.element('onlineAccountFinish').emit('click');
  await ui.element('onlineAccountCancel').emit('click');
  finishResponse({status: 409, body: {detail: 'safe failure'}});
  await finishing;
  assert.equal(ui.requests.filter((r) => r.url.endsWith('/viewer')).length, 1);
  assert.equal(ui.element('onlineAccountFrame').src, 'about:blank');
  assert.equal(ui.element('onlineAccountDialog').open, false);
});
