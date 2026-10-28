const test = require('node:test');
const assert = require('node:assert/strict');
const state = require('../app/static/canvas-task-state.js');

test('unknown submission is not shown as generated progress or active execution', () => {
  const task = { execution_backend: 'canvas_cli', status: 'submitted', progress: 40,
    error_code: 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', execution_diagnostics: { reconciliation_paused: true } };
  assert.equal(state.progress(task), '查询重试中');
  assert.equal(state.active(task), false);
  assert.equal(state.canReconcile(task), true);
});
test('confirmed running clears unknown presentation and cannot be reconciled twice', () => {
  const task = { execution_backend: 'canvas_cli', status: 'running', progress: 45, error_code: '' };
  assert.equal(state.progress(task), '45%');
  assert.equal(state.active(task), true);
  assert.equal(state.canReconcile(task), false);
});
test('legacy browser tasks and terminal results retain their existing meaning', () => {
  assert.equal(state.unknown({execution_backend: 'browser', error_code: 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'}), false);
  assert.equal(state.active({status: 'succeeded', progress: 100}), false);
  assert.equal(state.progress({status: 'succeeded', progress: 100}), '100%');
});

test('closed unconfirmed submission is a terminal failure without manual reconciliation', () => {
  const task = {execution_backend: 'canvas_cli', status: 'failed', progress: 100,
    error_code: 'CLI_SUBMISSION_UNCONFIRMED', execution_diagnostics: {reconciliation_closed: true}};
  assert.equal(state.unknown(task), false);
  assert.equal(state.active(task), false);
  assert.equal(state.canReconcile(task), false);
  assert.equal(state.progress(task), '100%');
});
test('confirmed generation with unavailable delivery keeps its distinct result state', () => {
  const task = {execution_backend: 'canvas_cli', status: 'submitted',
    error_code: 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', execution_diagnostics: {
      reconciliation_paused: true, reconciliation_pause_reason: 'result_transfer_failed'}};
  assert.equal(state.label(task), '结果待转存');
  assert.equal(state.active(task), false);
  assert.equal(state.canReconcile(task), true);
});
