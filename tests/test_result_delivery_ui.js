const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const taskState = require('../app/static/canvas-task-state.js');
const script = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');

function harness(tasks) {
  const elements = new Map();
  function element(selector) {
    if (!elements.has(selector)) elements.set(selector, {
      innerHTML: '', textContent: '', open: false, classList: {toggle() {}},
      showModal() { this.open = true; },
    });
    return elements.get(selector);
  }
  const context = vm.createContext({
    state: {tasks, detail: null}, $: element, $$: () => [],
    terminal: new Set(['succeeded', 'failed', 'expired', 'cancelled']),
    CanvasTaskState: taskState, document: {}, mediaFromTask: () => [],
    failureMessage: () => 'PUBLIC-FAILURE', executionLabel: () => 'Canvas CLI',
    upstreamIdentifierCell: () => '', costObservationCell: () => '', taskSpec: () => '720p',
    elapsed: () => '10s', fmtTime: () => '-', icons() {},
  });
  for (const name of ['escapeHtml', 'badge', 'callerResponse']) {
    const line = script.split('\n').find(line => line.startsWith(`function ${name}(`));
    assert.ok(line, `actual ${name} source exists`);
    vm.runInContext(line, context);
  }
  const start = script.indexOf('function renderTasks()');
  const end = script.indexOf('function defaultPayload()', start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(script.slice(start, end), context);
  const detailStart = script.indexOf('function showDetail(task)');
  const detailEnd = script.indexOf('\n}', detailStart) + 2;
  vm.runInContext(script.slice(detailStart, detailEnd), context);
  context.renderAudit = () => {};
  return {element, run: code => vm.runInContext(code, context)};
}

function task(values = {}) {
  return {id: 'delivery-example', model: 'sd-2-0-mini', status: 'running', progress: 95,
    prompt: 'Synthetic landscape', execution_backend: 'canvas_cli', result_urls: [], ...values};
}

test('six delivery states have distinct labels without changing progress or control', () => {
  for (const [stage, label] of Object.entries({generating:'生成中', querying:'查询结果',
    downloading:'下载中', downloading_retry:'下载重试', transferring:'转存中', transferring_retry:'转存重试'})) {
    const value = task({result_stage: stage});
    assert.equal(taskState.stageLabel(value), label);
    assert.equal(taskState.progress(value), '95%');
    assert.equal(taskState.active(value), true);
    assert.equal(taskState.canReconcile(value), false);
  }
});

test('actual list renderer displays delivery retry with canonical running CSS and numeric progress', () => {
  const tasks = [task({result_stage: 'downloading_retry'}),
    task({id: 'transfer', result_stage: 'transferring_retry'})];
  const before = JSON.stringify(tasks);
  const ui = harness(tasks); ui.run('renderTasks()');
  assert.match(ui.element('#tasksBody').innerHTML, /class="badge running">下载重试/);
  assert.match(ui.element('#tasksBody').innerHTML, /class="badge running">转存重试/);
  assert.match(ui.element('#tasksBody').innerHTML, /95%/);
  assert.equal(ui.element('#metricRunning').textContent, 2);
  assert.equal(JSON.stringify(tasks), before);
});

test('actual detail and caller preview use the same safe projection without changing canonical status', () => {
  const value = task({result_stage:'transferring_retry', result_delivery_diagnostics:{
    phase:'transferring', stage:'upload', retrying:true, last_error:{code:'RESULT_TRANSFER_FAILED', http_status:507}},
    upstream_context:{upload_checkpoints:{token:'PRIVATE-CHECKPOINT'}}});
  const ui = harness([value]); ui.run('showDetail(state.tasks[0])');
  assert.match(ui.element('#detailFacts').innerHTML, /转存重试/);
  const response = JSON.parse(ui.run('JSON.stringify(callerResponse(state.tasks[0]))'));
  assert.equal(response.status, 'running');
  assert.equal(response.result_stage, 'transferring_retry');
  assert.equal(response.result_delivery_diagnostics.last_error.http_status, 507);
  assert.doesNotMatch(JSON.stringify(response), /PRIVATE|upstream_context|upload_checkpoints/);
});

test('terminal canonical success overrides stale retry and stale unknown without a manual CTA', () => {
  const value = task({status:'succeeded', progress:100, result_stage:'transferring_retry',
    error_code:'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', execution_diagnostics:{reconciliation_paused:true}});
  const ui = harness([value]); ui.run('renderTasks(); showDetail(state.tasks[0])');
  assert.match(ui.element('#tasksBody').innerHTML, /class="badge succeeded">完成/);
  assert.doesNotMatch(ui.element('#tasksBody').innerHTML, /转存重试|待核实/);
  assert.match(ui.element('#detailFacts').innerHTML, /succeeded/);
  assert.equal(ui.element('#reconcileSubmission').hidden, true);
  assert.equal(taskState.progress(value), '100%');
  assert.equal(taskState.active(value), false);
});

test('legacy unknown retains its reconciliation label and active meaning', () => {
  const value = task({status:'submitted', result_stage:'transferring',
    error_code:'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', execution_diagnostics:{
      reconciliation_paused:true, reconciliation_pause_reason:'result_transfer_failed'}});
  const ui = harness([value]); ui.run('renderTasks(); showDetail(state.tasks[0])');
  assert.match(ui.element('#tasksBody').innerHTML, /结果待转存/);
  assert.equal(taskState.stageLabel(value), undefined);
  assert.equal(taskState.canReconcile(value), true);
  assert.equal(ui.element('#metricRunning').textContent, 0);
  assert.equal(ui.element('#reconcileSubmission').hidden, false);
});

test('unknown stages, prototype keys, non-Canvas and pre-submit tasks retain legacy rendering', () => {
  for (const values of [{result_stage:'toString'}, {result_stage:'<script>secret</script>'},
    {result_stage:['downloading']}, {result_stage:'downloading', execution_backend:'browser'},
    {result_stage:'downloading', status:'queued'}, {result_stage:'downloading', status:'failed'}]) {
    assert.equal(taskState.stageLabel(task(values)), undefined);
  }
  const ui = harness([task()]); ui.run('renderTasks()');
  assert.match(ui.element('#tasksBody').innerHTML, /class="badge running">生成中/);
});

test('HTML cache keys load both updated task state and renderer; CSS is unchanged', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /canvas-task-state\.js\?v=20261101-result-delivery/);
  assert.match(html, /app\.js\?v=20261101-result-delivery/);
  assert.match(html, /styles\.css\?v=20261030-upstream-ids/);
});
