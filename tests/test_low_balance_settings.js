const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'index.html'), 'utf8');
const options = ['seedream-5.0-pro', 'seedream-5.0-lite', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst'];

function harness(settings = {}) {
  const fields = Object.fromEntries([
    'canvas_cli_enabled', 'task_workers', 'task_queue_capacity', 'poll_interval_seconds',
    'task_timeout_seconds', 'daily_checkin_enabled', 'low_balance_disable_threshold',
    'chrome_executable', 'chrome_user_data_root', 'allow_video_reference_inputs',
    'prompt_media_reference_cleanup_enabled', 'strict_prompt_media_references',
    'enable_sd_2_5_480p_x', 'result_s3_center_api_base', 'result_s3_bucket',
    'result_s3_key_prefix', 'result_s3_center_token', 'low_balance_image_rescue_threshold',
    'low_balance_seedream_target_model',
  ].map((name) => [name, {value: '', checked: false, innerHTML: ''}]));
  const listeners = {};
  const form = {elements: {namedItem: (name) => fields[name]}, addEventListener: (name, fn) => { listeners[name] = fn; }};
  const dialog = {open: false, showModal() { this.open = true; }, close() { this.open = false; }};
  const requests = [], notifications = [];
  const state = {settings: {low_balance_seedream_target_options: options, ...settings}};
  const context = vm.createContext({state, $: (selector) => ({
    '#settingsForm': form, '#settingsDialog': dialog, '#s3TokenHint': {textContent: ''},
    '#settingsButton': {addEventListener: (name, fn) => { listeners.open = fn; }},
  }[selector]), toast: (...args) => notifications.push(args), refresh: async () => {},
  api: async (endpoint, request) => { const body = JSON.parse(request.body); requests.push({endpoint, ...request, body}); return {...state.settings, ...body}; }});
  vm.runInContext(source.match(/^function escapeHtml\(.*$/m)[0], context);
  const helperStart = source.indexOf('function lowBalanceModelOptions(');
  const helperEnd = source.indexOf('$("#integrationDocsButton")', helperStart);
  assert.ok(helperStart > 0 && helperEnd > helperStart);
  vm.runInContext(source.slice(helperStart, helperEnd), context);
  const submitStart = source.indexOf('$("#settingsForm").addEventListener("submit"');
  const submitEnd = source.indexOf('$("#taskForm").addEventListener(', submitStart);
  vm.runInContext(source.slice(submitStart, submitEnd), context);
  return {context, fields, form, dialog, requests, notifications, state, open: () => listeners.open(),
    submit: () => listeners.submit({preventDefault() {}, currentTarget: form})};
}

test('settings open keeps Pro5 identity/default50 and renders only backend candidates', () => {
  const h = harness();
  h.open();
  assert.equal(h.fields.low_balance_image_rescue_threshold.value, '50');
  assert.equal(h.fields.low_balance_seedream_target_model.value, 'seedream-5.0-pro');
  for (const model of options) assert.ok(h.fields.low_balance_seedream_target_model.innerHTML.includes(model));
  assert.ok(!h.fields.low_balance_seedream_target_model.innerHTML.includes('flash'));
  assert.ok(h.dialog.open);
});

test('zero round-trips and PATCH contains editable settings, never readonly options', async () => {
  const h = harness({low_balance_image_rescue_threshold: 0, low_balance_seedream_target_model: 'seedream-5.0-lite'});
  h.open();
  assert.equal(h.fields.low_balance_image_rescue_threshold.value, '0');
  await h.submit();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].endpoint, '/api/settings');
  assert.equal(h.requests[0].method, 'PATCH');
  assert.equal(h.requests[0].body.low_balance_image_rescue_threshold, 0);
  assert.equal(h.requests[0].body.low_balance_seedream_target_model, 'seedream-5.0-lite');
  assert.ok(!('low_balance_seedream_target_options' in h.requests[0].body));
  assert.ok(!h.dialog.open);
});

test('decimal threshold and chosen Flare persist without changing global disable threshold', async () => {
  const h = harness({low_balance_disable_threshold: 0});
  h.open();
  h.fields.low_balance_image_rescue_threshold.value = '12.5';
  h.fields.low_balance_seedream_target_model.value = 'gpt-image-2.5-flare';
  await h.submit();
  assert.equal(h.requests[0].body.low_balance_image_rescue_threshold, 12.5);
  assert.equal(h.requests[0].body.low_balance_seedream_target_model, 'gpt-image-2.5-flare');
  assert.equal(h.requests[0].body.low_balance_disable_threshold, 0);
});

test('invalid thresholds never issue PATCH or close the dialog', async () => {
  for (const value of ['', ' ', '-1', 'NaN', 'Infinity', 'not-a-number']) {
    const h = harness();
    h.open(); h.fields.low_balance_image_rescue_threshold.value = value;
    await h.submit();
    assert.equal(h.requests.length, 0, value);
    assert.equal(h.notifications.at(-1)[1], true, value);
    assert.ok(h.dialog.open, value);
  }
});

test('missing backend options preserve the configured target and reject saving', async () => {
  const h = harness({low_balance_seedream_target_options: undefined, low_balance_seedream_target_model: 'seedream-5.0-lite'});
  h.open();
  assert.equal(h.fields.low_balance_seedream_target_model.value, 'seedream-5.0-lite');
  assert.match(h.fields.low_balance_seedream_target_model.innerHTML, /disabled/);
  await h.submit();
  assert.equal(h.requests.length, 0);
  assert.ok(h.notifications.at(-1)[1]);
});

test('unavailable configured model is escaped and cannot silently select another model', async () => {
  const model = 'unavailable"><img src=x>';
  const h = harness({low_balance_seedream_target_model: model});
  h.open();
  assert.ok(!h.fields.low_balance_seedream_target_model.innerHTML.includes('<img'));
  assert.ok(h.fields.low_balance_seedream_target_model.innerHTML.includes('&lt;img'));
  assert.equal(h.fields.low_balance_seedream_target_model.value, model);
  await h.submit();
  assert.equal(h.requests.length, 0);
});

test('malformed candidate metadata does not create arbitrary model controls', () => {
  const h = harness({low_balance_seedream_target_options: ['seedream-5.0-pro', 'seedream-5.0-pro', null, '<img src=x>']});
  h.open();
  assert.equal((h.fields.low_balance_seedream_target_model.innerHTML.match(/<option/g) || []).length, 1);
});

test('HTML labels distinguish the independent rule, zero-off and actual model effects', () => {
  assert.match(html, /name="low_balance_image_rescue_threshold"[^>]*value="50"/);
  assert.match(html, /name="low_balance_seedream_target_model"/);
  assert.ok(html.includes('填 0 关闭'));
  assert.ok(html.includes('不改变自动禁用门槛'));
  assert.ok(html.includes('默认保持 Pro5'));
});
