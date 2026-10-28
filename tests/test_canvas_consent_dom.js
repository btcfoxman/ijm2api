/* Execute the production consent selector against the observed official DOM. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'app', 'canvas_auth.py'), 'utf8');
const match = source.match(/_CONFIRM\s*=\s*r"""([\s\S]*?)"""/);
assert.ok(match, 'production consent selector must be present');
const selector = match[1];
const officialText = 'Connect your Dreamina account to Dreamina Canvas CLI. '
  + 'The CLI uses the same credits and credit rules as Dreamina on the web.';

function button(label, options = {}) {
  return {
    innerText: label, textContent: label, disabled: !!options.disabled,
    clicks: 0,
    getBoundingClientRect: () => ({width: options.hidden ? 0 : 120, height: 40}),
    getAttribute: key => key === 'aria-disabled' && options.ariaDisabled ? 'true' : null,
    click() { this.clicks += 1; },
  };
}

function run(buttons, options = {}) {
  const context = {
    location: {protocol: options.protocol || 'https:',
      hostname: options.hostname || 'dreamina.capcut.com',
      pathname: options.pathname || '/ai-tool/cli-auth-os'},
    document: {
      body: {innerText: options.text === undefined ? officialText : options.text},
      querySelector: () => options.captcha ? {} : null,
      querySelectorAll: () => buttons,
    },
    getComputedStyle: () => ({display: 'block', visibility: 'visible'}),
  };
  return vm.runInNewContext(selector, context, {timeout: 100});
}

test('the unique visible Agree dispatches consent and never clicks Decline', () => {
  const decline = button('Decline'), agree = button('Agree');
  assert.equal(run([decline, agree]), 'confirmed');
  assert.equal(agree.clicks, 1);
  assert.equal(decline.clicks, 0);
});

test('the exact Chinese 同意 label is supported on the same CLI page', () => {
  const decline = button('拒绝'), agree = button('同意');
  assert.equal(run([decline, agree]), 'confirmed');
  assert.equal(agree.clicks, 1);
  assert.equal(decline.clicks, 0);
});

test('Decline alone is never selected', () => {
  const decline = button('Decline');
  assert.equal(run([decline]), 'waiting');
  assert.equal(decline.clicks, 0);
});

test('multiple visible Agree buttons remain ambiguous and none are clicked', () => {
  const first = button('Agree'), second = button('Agree');
  assert.equal(run([first, second]), 'waiting');
  assert.equal(first.clicks + second.clicks, 0);
});

test('hidden and disabled buttons do not compete with the unique consent', () => {
  const hidden = button('Agree', {hidden: true}), disabled = button('Agree', {disabled: true});
  const ariaDisabled = button('Agree', {ariaDisabled: true}), agree = button('Agree');
  assert.equal(run([hidden, disabled, ariaDisabled, agree]), 'confirmed');
  assert.equal(agree.clicks, 1);
  assert.equal(hidden.clicks + disabled.clicks + ariaDisabled.clicks, 0);
});

test('a purchase or subscription prompt is never approved', () => {
  for (const extra of ['Purchase credits', 'Checkout', 'Subscribe', '支付', '购买', '订阅']) {
    const agree = button('Agree');
    assert.equal(run([agree], {text: officialText + ' ' + extra}), 'manual_required');
    assert.equal(agree.clicks, 0);
  }
});

test('a CAPTCHA or security challenge is left for the operator', () => {
  for (const options of [{captcha: true}, {text: officialText + ' Security verification'},
                         {text: officialText + ' 人机验证'}]) {
    const agree = button('Agree');
    assert.equal(run([agree], options), 'manual_required');
    assert.equal(agree.clicks, 0);
  }
});

test('an unrelated page does not approve a generic Agree', () => {
  const agree = button('Agree');
  assert.equal(run([agree], {text: 'Please agree to the general conditions.'}), 'waiting');
  assert.equal(agree.clicks, 0);
});

test('the selector requires the official HTTPS CLI authorization page', () => {
  for (const options of [{protocol: 'http:'}, {hostname: 'attacker.invalid'},
                         {pathname: '/ai-tool/generate'}]) {
    const agree = button('Agree');
    assert.equal(run([agree], options), 'manual_required');
    assert.equal(agree.clicks, 0);
  }
});
