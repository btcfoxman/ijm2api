(function (root) {
  const unknown = (task) => task?.execution_backend === 'canvas_cli'
    && task?.error_code === 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN';
  const terminal = new Set(['succeeded', 'failed', 'expired', 'cancelled']);
  const label = (task) => ({result_transfer_failed: '结果待转存', result_pending: '结果待取回'}[
    task?.execution_diagnostics?.reconciliation_pause_reason] || '提交待核实');
  const state = {
    unknown,
    label,
    active: (task) => !terminal.has(task?.status) && !unknown(task),
    progress: (task) => unknown(task) ? label(task) : `${task?.progress || 0}%`,
    canReconcile: (task) => unknown(task)
      && task?.execution_diagnostics?.reconciliation_paused === true,
  };
  root.CanvasTaskState = state;
  if (typeof module !== 'undefined') module.exports = state;
})(globalThis);
