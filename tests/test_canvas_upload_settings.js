const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'index.html'), 'utf8');
const limits = [['canvas_upload_concurrency', 3, 8], ['canvas_account_upload_concurrency', 4, 16], ['canvas_total_upload_concurrency', 8, 32]];

function harness(settings = {}) {
  const fields = Object.fromEntries(limits.map(([name]) => [name, {value: ''}]));
  const form = {elements: {namedItem: name => fields[name]}};
  const context = vm.createContext({form, settings});
  const start = source.indexOf('const canvasUploadLimits =');
  const end = source.indexOf('function renderRegionGenerationSettings(', start);
  assert.ok(start > 0 && end > start);
  vm.runInContext(source.slice(start, end), context);
  vm.runInContext('renderCanvasUploadSettings(form, settings)', context);
  return {fields, payload: () => JSON.parse(vm.runInContext('JSON.stringify(canvasUploadSettingsPayload(form))', context))};
}

test('upload settings render defaults and preserve configured integer boundaries', () => {
  assert.deepEqual(harness().payload(), Object.fromEntries(limits.map(([name, value]) => [name, value])));
  const configured = Object.fromEntries(limits.map(([name, , max]) => [name, max]));
  assert.deepEqual(harness(configured).payload(), configured);
});

test('upload controls reject blank, fractional and out-of-range values before PATCH', () => {
  for (const [name, , max] of limits) {
    for (const raw of ['', ' ', '0', '-1', '1.5', String(max + 1), 'NaN', 'Infinity', 'oops']) {
      const h = harness();
      h.fields[name].value = raw;
      assert.throws(() => h.payload(), /整数/, `${name}=${raw}`);
    }
  }
});

test('HTML provides distinct task, account and total upload limits with matching bounds', () => {
  for (const [name, value, max] of limits) {
    assert.match(html, new RegExp(`name="${name}"[^>]*min="1"[^>]*max="${max}"[^>]*step="1"[^>]*value="${value}"[^>]*required`));
  }
  assert.ok(html.includes('每任务素材上传并发'));
  assert.ok(html.includes('每账号素材上传并发'));
  assert.ok(html.includes('全部账号素材上传并发'));
  assert.ok(source.includes('...canvasUploads,'));
});
