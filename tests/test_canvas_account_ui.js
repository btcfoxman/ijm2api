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

function loadAccountRenderer(ui, accounts, settings) {
  Object.assign(ui.context, {
    state: { accounts, settings, accountFilter: 'enabled' },
    escapeHtml: value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char])),
    badge: status => `<span data-status="${status}">${status}</span>`,
    loginProgress: () => '', proxyLabel: value => value, fmtTime: () => '-', icons() {},
  });
  const script = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
  ui.run(script.slice(script.indexOf('function executionLabel('), script.indexOf('function mediaFromTask(')));
  ui.run('renderAccounts()');
}

function account(id, values = {}) {
  return { id, name: `Synthetic ${id}`, enabled: true, status: 'active', execution_mode: 'canvas_cli',
    cli_auth_status: 'ready', proxy_url: 'socks5://proxy.test:20001', balance_details: {},
    max_concurrency: 1, priority: 0, ...values };
}

test('migrated enabled accounts waiting for authorization are not reported as available', () => {
  const ui = harness();
  const accounts = Array.from({ length: 19 }, (_, i) => account(i + 1, {
    enabled: i < 17, status: i < 17 ? 'active' : 'login_required', cli_auth_status: 'pending',
  }));
  const before = JSON.stringify(accounts);
  loadAccountRenderer(ui, accounts, { canvas_cli_enabled: false, canvas_cli_runtime: { available: true } });
  assert.equal(ui.element('#metricAccounts').textContent, '0 / 19');
  assert.equal(ui.element('#enabledCount').textContent, 17, 'enabled intent remains unchanged');
  const rows = ui.element('#accountsBody').innerHTML;
  assert.equal((rows.match(/data-status="cli_auth_required"/g) || []).length, 17);
  assert.doesNotMatch(rows, /data-status="active"/);
  assert.match(rows, /接单已关闭/);
  assert.equal(JSON.stringify(accounts), before);
  assert.equal(ui.requests.length, 0, 'rendering availability must not patch account configuration');
});

test('available metric requires effective CLI readiness and keeps browser mode independent', () => {
  const ui = harness();
  loadAccountRenderer(ui, [
    account(1, { execution_mode: 'browser', cli_auth_status: 'pending' }),
    account(2), account(3, { cli_auth_status: 'expired' }),
    account(4, { enabled: false }), account(5, { status: 'network_error' }),
  ], { canvas_cli_enabled: true, canvas_cli_runtime: { available: true } });
  assert.equal(ui.element('#metricAccounts').textContent, '2 / 5');
  ui.run('state.settings.canvas_cli_enabled = false; renderAccounts()');
  assert.equal(ui.element('#metricAccounts').textContent, '1 / 5');
  assert.match(ui.element('#accountsBody').innerHTML, /接单已关闭/);
});

test('incompatible CLI runtime is clearly shown without disabling intended accounts', () => {
  const ui = harness();
  const accounts = [account(1), account(2, { execution_mode: 'browser' })];
  const before = JSON.stringify(accounts);
  loadAccountRenderer(ui, accounts, { canvas_cli_enabled: true,
    canvas_cli_runtime: { available: false, message: 'Native Canvas runtime version is incompatible' } });
  assert.equal(ui.element('#metricAccounts').textContent, '1 / 2');
  assert.match(ui.element('#accountsBody').innerHTML, /CLI 环境不兼容/);
  assert.match(ui.element('#accountsBody').innerHTML, /暂不能接单/);
  assert.match(ui.element('#accountsBody').innerHTML, /Native Canvas runtime version is incompatible/);
  assert.equal(JSON.stringify(accounts), before);
});

test('runtime unknown follows the available-not-false rule and preserves an unfinished login badge', () => {
  const ui = harness();
  loadAccountRenderer(ui, [account(1), account(2, { status: 'logging_in', cli_auth_status: 'pending' })],
    { canvas_cli_enabled: true, canvas_cli_runtime: { available: null } });
  assert.equal(ui.element('#metricAccounts').textContent, '1 / 2');
  assert.match(ui.element('#accountsBody').innerHTML, /data-status="logging_in"/);
});
