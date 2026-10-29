/* Shared administrator task cost projection. All calculations stay on the server. */
(() => {
  'use strict';
  const summaries = new Map();
  const generations = new Map();
  let sequence = 0;
  let headers = {}, listening = false, dialog;
  const labels = new Set(['实扣', '预扣', '公式推', '表格推', '未知']);
  const explanations = {实扣: '上游明确返回的实际扣款，扣除已确认退款。', 预扣: '上游明确报价或提交报告，尚未作为实际扣款确认。',
    公式推: '按适用的计费公式估算，尚非实际扣款。', 表格推: '按人工维护表和当前任务规格推算。', 未知: '缺少足够的消耗证据或已知任务规格。'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  function cell(taskId, summary) {
    if (summary) summaries.set(String(taskId), summary);
    const row = summary || summaries.get(String(taskId)) || {};
    const label = labels.has(row.source_label) ? row.source_label : '未知';
    const usage = row.usage == null && row.usage_display == null ? '-' : row.usage_display ?? '-';
    const showMoney = row.money_enabled === true;
    const money = row.cost == null && (!row.cost_display || row.cost_display === '-' || row.cost_display === '—') ? '-' : '￥' + row.cost_display;
    const title = (row.unit || '额度') + (showMoney ? ' / 人民币' : '') + '，点击查看来源明细';
    return `<button type="button" class="task-cost-cell" data-task-cost-id="${esc(taskId)}" title="${esc(title)}"><span class="task-cost-value">${esc(usage)}${showMoney ? '/' + esc(money) : ''}</span><span class="task-cost-source">${esc(label)}</span></button>`;
  }
  function installStyle() {
    if (document.getElementById('task-cost-shared-style')) return;
    const style = document.createElement('style');
    style.id = 'task-cost-shared-style';
    style.textContent = '.task-cost-cell{display:inline-flex;flex-direction:column;gap:3px;border:0;background:transparent;padding:0;color:inherit;text-align:left;font:inherit;cursor:pointer;white-space:nowrap}.task-cost-cell .task-cost-value{font-variant-numeric:tabular-nums;font-size:12px}.task-cost-cell .task-cost-source{font-size:11px;opacity:.64}.task-cost-dialog{max-width:min(850px,90vw);max-height:85vh;border:1px solid #d7dce3;border-radius:12px;padding:20px;color:#202733;background:#fff}.task-cost-dialog::backdrop{background:#17202b77}.task-cost-dialog pre{white-space:pre-wrap;word-break:break-word;max-height:55vh;overflow:auto;font-size:12px}.task-cost-dialog header{display:flex;align-items:center;justify-content:space-between;gap:24px}.task-cost-dialog h3{margin:0}.task-cost-dialog p{font-size:13px}';
    document.head.append(style);
  }
  function showDetails(taskId) {
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.className = 'task-cost-dialog';
      dialog.innerHTML = '<header><h3>消耗来源明细</h3><button type="button" data-cost-close>关闭</button></header><p data-cost-summary></p><p data-cost-explanation></p><p data-cost-price></p><details><summary>查看完整费用证据</summary><pre data-cost-details></pre></details>';
      dialog.querySelector('[data-cost-close]').addEventListener('click', () => dialog.close());
      document.body.append(dialog);
    }
    const row = summaries.get(String(taskId));
    const showMoney = row?.money_enabled === true;
    dialog.querySelector('[data-cost-summary]').textContent = row
      ? `${taskId} · ${row.usage_display}${showMoney ? '/' + (row.cost == null ? '-' : '￥' + row.cost_display) : ''} · ${row.source_label} · ${row.execution_source || row.channel || ''}`
      : `${taskId} · 消耗未知，尚未取得费用证据。`;
    dialog.querySelector('[data-cost-explanation]').textContent = explanations[row?.source_label] || explanations.未知;
    const pool = row?.details?.purchase;
    dialog.querySelector('[data-cost-price]').hidden = !showMoney;
    dialog.querySelector('[data-cost-price]').textContent = !showMoney ? '' : pool?.purchase_amount != null && pool?.purchase_units != null
      ? `采购单价：￥${pool.purchase_amount} ÷ ${pool.purchase_units} 额度。任务参考金额 = 本次消耗额 × 采购单价；仅使用同单位的额度采购价格。`
      : '金额未知：尚未配置适用的同单位额度采购价格。';
    const details = row ? {selected_observation: row.selected_observation,
      ...(showMoney ? row.details : {observations: row.details?.observations, advisory_only: true})} : {source: 'unknown'};
    dialog.querySelector('[data-cost-details]').textContent = JSON.stringify(details, null, 2);
    if (!dialog.open) dialog.showModal();
  }
  function bind() {
    installStyle();
    if (listening) return;
    listening = true;
    document.addEventListener('click', event => {
      const button = event.target.closest?.('[data-task-cost-id]');
      if (!button) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      showDetails(button.dataset.taskCostId);
    }, true);
  }
  async function hydrate(root = document, options = {}) {
    bind();
    headers = {...headers, ...(options.headers || {})};
    const endpoint = options.endpoint || '/api/cost-reference/task-costs';
    if (typeof endpoint !== 'string' || !endpoint.startsWith('/') || endpoint.startsWith('//') || /[\\\x00-\x20]/.test(endpoint)) return;
    const nodes = [...root.querySelectorAll('[data-task-cost-id]')];
    if (root.matches?.('[data-task-cost-id]')) nodes.unshift(root);
    const ids = [...new Set(nodes.map(node => node.dataset.taskCostId).filter(Boolean))];
    for (const node of nodes) {
      if (!node.classList.contains('task-cost-cell')) node.outerHTML = cell(node.dataset.taskCostId);
    }
    for (let start = 0; start < ids.length; start += 200) {
      const batch = ids.slice(start, start + 200);
      const generation = ++sequence;
      batch.forEach(id => generations.set(id, generation));
      try {
        const response = await fetch(endpoint, {
          method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', ...headers},
          body: JSON.stringify({task_ids: batch})
        });
        if (!response.ok) continue;
        const result = await response.json();
        for (const id of batch) {
          const row = result.tasks?.[id];
          if (generations.get(id) !== generation) continue;
          if (!row || typeof row !== 'object') continue;
          summaries.set(id, row);
          // IDs never enter a CSS selector: quotes/markup in legacy IDs remain data.
          for (const node of document.querySelectorAll('[data-task-cost-id]')) {
            if (node.dataset.taskCostId === id) node.outerHTML = cell(id, row);
          }
        }
      } catch (_) { /* Keep the explicit unknown state when unavailable. */ }
    }
  }
  globalThis.TaskCost = {cell, hydrate, bind};
})();
