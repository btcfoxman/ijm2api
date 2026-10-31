(function (root) {
  const terminal = new Set(['succeeded', 'failed', 'expired', 'cancelled', 'canceled']);
  const unknown = (task) => !terminal.has(task?.status)
    && task?.execution_backend === 'canvas_cli'
    && task?.error_code === 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN';
  const stageLabels = {
    generating: '生成中', querying: '查询结果', downloading: '下载中',
    downloading_retry: '下载重试', transferring: '转存中', transferring_retry: '转存重试',
  };
  const stageLabel = (task) => !terminal.has(task?.status) && !unknown(task)
    && ['running', 'submitted'].includes(task?.status) && task?.execution_backend === 'canvas_cli'
    && typeof task?.result_stage === 'string' && Object.hasOwn(stageLabels, task.result_stage)
    ? stageLabels[task?.result_stage] : undefined;
  const label = (task) => ({result_transfer_failed: '结果待转存', result_pending: '结果待取回'}[
    task?.execution_diagnostics?.reconciliation_pause_reason] || '查询重试中');
  const state = {
    unknown,
    label,
    stageLabel,
    active: (task) => !terminal.has(task?.status) && !unknown(task),
    progress: (task) => unknown(task) ? label(task) : `${task?.progress || 0}%`,
    canReconcile: (task) => unknown(task)
      && task?.execution_diagnostics?.reconciliation_paused === true,
  };
  root.CanvasTaskState = state;
  if (typeof module !== 'undefined') module.exports = state;
})(globalThis);
