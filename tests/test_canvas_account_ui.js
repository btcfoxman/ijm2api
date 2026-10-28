const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function harness() {
  const elements = new Map();
  const requests = [];
  const assistCalls = [];
  const element = (selector) => {
    if (!elements.has(selector)) {
      const listeners = new Map();
      elements.set(selector, {
        hidden: false, disabled: false, textContent: '', value: '', open: false,
        classList: { toggle() {} },
        addEventListener(name, callback) { listeners.set(name, callback); },
        emit(name, event = {}) { return listeners.get(name)?.(event); },
        showModal() { this.open = true; },
        close() { this.open = false; this.emit('close'); },
        removeAttribute(name) { delete this[name]; },
        getBoundingClientRect() { return { width: 200, height: 100, left: 0, top: 0 }; },
      });
    }
    return elements.get(selector);
  };
  const context = vm.createContext({
    $, console, document: { addEventListener() {} }, window: { addEventListener() {} },
    setInterval: () => 1, clearInterval() {}, setTimeout: () => 2, clearTimeout() {},
    toast() {}, refresh: async () => {},
    api: async (url, options = {}) => {
      requests.push({ url, options });
      if (url.endsWith('/cli-auth')) return { state: 'manual_required', verification_uri: 'https://official.example.test/authorize', user_code: 'SYNTHETIC' };
      return { image: 'data:image/png;base64,synthetic', width: 800, height: 600 };
    },
    fetch: async (url, options) => { requests.push({ url, options }); return {}; },
    openBrowserAssist: async (...args) => assistCalls.push(args),
  });
  function $(selector) { return element(selector); }
  return { context, element, requests, assistCalls, run: code => vm.runInContext(code, context) };
}

function loadAssist(ui) {
  ui.run(fs.readFileSync(path.join(__dirname, '../app/static/browser-assist.js'), 'utf8'));
}

test('manual Canvas authorization uses the proxied account viewer without starting another login', async () => {
  const ui = harness(); loadAssist(ui);
  await ui.run('openBrowserAssist(14, { cliAuth: true })');
  assert.deepEqual(ui.requests.map(item => item.url), ['/api/accounts/14/cli-auth/browser']);
  assert.equal(ui.element('#browserAssistComplete').hidden, true);
  assert.match(ui.element('#browserAssistStatus').textContent, /CLI/);
  await ui.element('#browserAssistComplete').emit('click');
  assert.equal(ui.requests.length, 1, 'CLI authorization must not invoke browser login completion');
  ui.element('#browserAssistImage').emit('click', { clientX: 100, clientY: 50 });
  await new Promise(setImmediate);
  const action = ui.requests.find(item => item.url.endsWith('/browser/action'));
  assert.deepEqual(JSON.parse(action.options.body), { action: 'click', x: .5, y: .5 });
  assert.equal(action.url, '/api/accounts/14/browser/action');
});

test('authorization completion only closes its matching manual viewer and releases its lease', async () => {
  const ui = harness(); loadAssist(ui);
  await ui.run('openBrowserAssist(14, { cliAuth: true })');
  ui.run('closeCanvasAuthAssist(15)');
  assert.equal(ui.element('#browserAssistDialog').open, true);
  ui.run('closeCanvasAuthAssist(14)');
  assert.equal(ui.element('#browserAssistDialog').open, false);
  assert.equal(ui.requests.at(-1).url, '/api/accounts/14/browser/close');
  assert.equal(ui.requests.some(item => item.options.method === 'DELETE'), false);
  assert.equal(ui.requests.some(item => item.url.includes('/login/')), false);
});

test('ordinary browser assistance retains its own open and completion flow', async () => {
  const ui = harness(); loadAssist(ui);
  await ui.run('openBrowserAssist(7)');
  assert.equal(ui.requests[0].url, '/api/accounts/7/browser/open');
  assert.equal(ui.element('#browserAssistComplete').hidden, false);
  ui.run('closeCanvasAuthAssist(7)');
  assert.equal(ui.element('#browserAssistDialog').open, true);
  await ui.element('#browserAssistComplete').emit('click');
  assert.equal(ui.requests.some(item => item.url === '/api/accounts/7/browser/complete'), true);
});

test('authorization dialog presents remote assistance, never a local navigation or private credentials', async () => {
  const ui = harness();
  const script = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
  const start = script.indexOf('let cliAuthAccountId = null;');
  const end = script.indexOf('document.addEventListener("click", async (event) => {', start);
  assert.ok(start >= 0 && end > start);
  ui.run(script.slice(start, end));
  await ui.run('startCliAuth(3)');
  await ui.element('#cliAuthAssist').emit('click');
  assert.equal(ui.assistCalls.length, 1);
  assert.equal(ui.assistCalls[0][0], 3);
  assert.equal(ui.assistCalls[0][1].cliAuth, true);
  ui.run('renderCliAuth({state:"manual_required", user_code:"PUBLIC-CODE", verification_uri:"https://official.example.test/auth", device_code:"PRIVATE-DEVICE", access_token:"PRIVATE-TOKEN"})');
  const displayed = [...['#cliAuthCode', '#cliAuthMessage', '#cliAuthAssist']].map(id => ui.element(id).textContent).join(' ');
  assert.match(displayed, /PUBLIC-CODE/);
  assert.doesNotMatch(displayed, /PRIVATE/);
  assert.equal(ui.element('#cliAuthAssist').href, undefined);
});
