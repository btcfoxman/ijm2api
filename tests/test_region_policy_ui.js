const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'index.html'), 'utf8');

function harness() {
  const fields = Object.fromEntries(['cn', 'us', 'other'].flatMap(region => [
    [`region_${region}_enabled`, {checked: false}], [`region_${region}_models`, {value: ''}],
  ]));
  const form = {elements: {namedItem: name => fields[name]}};
  const context = vm.createContext({form});
  const start = source.indexOf('function renderRegionGenerationSettings(');
  const end = source.indexOf('$("#settingsButton")', start);
  vm.runInContext(source.slice(start, end), context);
  return {fields, form, context, run: code => vm.runInContext(code, context)};
}

test('region editor defaults to empty mainland allowlist without enabling international limits', () => {
  const h = harness();
  h.run('renderRegionGenerationSettings(form, {})');
  assert.equal(h.fields.region_cn_enabled.checked, true);
  assert.equal(h.fields.region_cn_models.value, '');
  assert.equal(h.fields.region_us_enabled.checked, false);
  assert.equal(h.fields.region_other_enabled.checked, false);
});

test('region editor round-trips image aliases without seconds and independent video limits', () => {
  const h = harness();
  h.context.settings = {region_generation_policy: {
    cn: {enabled: true, models: {'sd-2-0': {min_duration: 5, max_duration: 10}, 'seedream-5.0-pro-2k': {}}},
    us: {enabled: true, models: {'sd-2-5': {min_duration: 4, max_duration: 30}}},
    other: {enabled: false, models: {}},
  }};
  h.run('renderRegionGenerationSettings(form, settings)');
  assert.match(h.fields.region_cn_models.value, /sd-2-0 5-10/);
  assert.match(h.fields.region_cn_models.value, /seedream-5.0-pro-2k$/);
  const payload = JSON.parse(JSON.stringify(h.run('regionGenerationSettingsPayload(form)')));
  assert.deepEqual(payload, h.context.settings);
});

test('invalid or duplicate editor rows cannot create a partial settings payload', () => {
  for (const row of ['sd-2-0 10-4', 'sd-2-0 3-15', 'sd-2-0 4-31', 'sd-2-0 4.5-10',
    'sd-2-0 4-Infinity', 'sd-2-0 4-15\nsd-2-0 5-10', '<img src=x>']) {
    const h = harness();
    h.fields.region_cn_models.value = row;
    assert.throws(() => h.run('regionGenerationSettingsPayload(form)'), undefined, row);
  }
});

test('new Jimeng transport defaults mainland and only its proxy is optional', () => {
  const h = harness();
  Object.assign(h.fields, {execution_mode: {value: 'jimeng_web'}, region: {value: 'us'}, proxy_url: {required: true}});
  const start = source.indexOf('function syncAccountTransportFields(');
  const end = source.indexOf('$("#accountForm")', start);
  h.run(source.slice(start, end));
  h.run('syncAccountTransportFields(form)');
  assert.equal(h.fields.region.value, 'us', 'explicit region does not infer the transport');
  assert.equal(h.fields.proxy_url.required, false);
  h.run('syncAccountTransportFields(form, true)');
  assert.equal(h.fields.region.value, 'cn');
  for (const mode of ['canvas_cli', 'browser']) {
    h.fields.execution_mode.value = mode;
    h.run('syncAccountTransportFields(form, true)');
    assert.equal(h.fields.proxy_url.required, true);
    assert.equal(h.fields.region.value, 'other');
  }
});

test('HTML exposes each region and clear empty-allowlist behavior', () => {
  for (const region of ['cn', 'us', 'other']) {
    assert.ok(html.includes(`name="region_${region}_enabled"`));
    assert.ok(html.includes(`name="region_${region}_models"`));
  }
  assert.match(html, /value="jimeng_web"/);
  assert.match(html, /启用且留空表示拒绝/);
  assert.match(html, /图片模型只填别名/);
});
