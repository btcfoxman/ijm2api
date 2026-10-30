const TASK_LIMIT_STORAGE_KEY = "ijm2api.recentTaskLimit";
function savedTaskLimit() {
  try {
    const value = Number(localStorage.getItem(TASK_LIMIT_STORAGE_KEY));
    return Number.isInteger(value) && value >= 1 && value <= 1000 ? value : 50;
  } catch { return 50; }
}
const state = { accounts: [], tasks: [], models: [], settings: {}, loginJobs: [], batchReport: null, accountFilter: "enabled", selectedModel: "sd-2-0-mini-480p", detail: null, auditKey: "caller_request", costReferences: [], taskLimit: savedTaskLimit() };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const terminal = new Set(["succeeded", "failed", "expired"]);
const publicErrors = { CONTENT_MODERATION_FAILED: "检测到内容可能违规，请修改提示词或参考素材后重试~", MEDIA_DOWNLOAD_FAILED: "素材下载失败，请检查~", PROVIDER_INVALID_REQUEST: "处理失败，请检查图音视频格式和大小" };

function icons() { if (window.lucide) window.lucide.createIcons(); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char])); }
function api(path, options = {}) {
  return fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } }).then(async (response) => {
    const text = await response.text(); let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
    if (response.status === 401) { location.replace("/login"); throw new Error("登录会话已失效"); }
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload));
    return payload;
  });
}
function apiText(path) { return fetch(path).then(async (response) => { const text = await response.text(); if (response.status === 401) location.replace("/login"); if (!response.ok) throw new Error(text || `HTTP ${response.status}`); return text; }); }
function toast(message, error = false) { const item = $("#toast"); item.textContent = message; item.className = `toast show${error ? " error" : ""}`; clearTimeout(toast.timer); toast.timer = setTimeout(() => { item.className = "toast"; }, 3200); }
function fmtTime(value) {
  if (!value) return "-";
  const numeric = typeof value === "number" || /^\d+(?:\.\d+)?$/.test(String(value));
  const date = new Date(numeric ? Number(value) * (Number(value) < 1e12 ? 1000 : 1) : value);
  if (!Number.isFinite(date.getTime())) return "-";
  const pad = (part) => String(part).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function fmtCost(value) { const number = Number(value || 0); return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, "").replace(/\.$/, ""); }
function elapsed(task) { const end = Number(task.completed_at || Date.now() / 1000); const total = Math.max(Math.floor(end - Number(task.created_at || end)), 0); return total < 60 ? `${total}秒` : `${Math.floor(total / 60)}分${total % 60}秒`; }
function badge(status, displayLabel) { const labels = { cli_auth_required: "\u5f85 CLI \u6388\u6743", active: "可用", pending: "待检测", login_pending: "待登录", logging_in: "登录中", login_failed: "登录失败", challenge_required: "需验证", verification_required: "需安全验证", setup_required: "需设置", agreement_required: "待确认协议", profile_resetting: "重置中", queued: "排队", preparing: "准备", submitted: "已提交", running: "生成中", succeeded: "完成", failed: "失败", expired: "超时", login_required: "需登录", network_error: "网络异常", suspended: "上游封禁", disabled: "禁用", disabled_low_balance: "低额度" }; return `<span class="badge ${escapeHtml(status)}">${escapeHtml(displayLabel || labels[status] || status || "未知")}</span>`; }
function loginStageLabel(stage) { const labels = { queued: "等待自动登录", opening_browser: "准备浏览器", browser: "准备浏览器", starting: "准备浏览器", login: "模拟登录", email: "输入邮箱", password: "输入密码", settings: "设置去水印和协议", logging_in: "模拟登录", authenticating: "验证登录", watermark: "关闭水印", agreement: "确认 AI 生成协议", verifying: "核验账号设置", ready: "登录及设置完成", interrupted: "已中断，可重试", setup_required: "设置未完成", agreement_required: "协议待确认", challenge_required: "需人工验证", verification_required: "需安全验证", login_required: "需重新登录", login_failed: "登录失败", network_error: "网络异常" }; return labels[stage] || stage || ""; }
function loginProgress(account) {
  const stage = loginStageLabel(account.login_stage);
  if (!stage) return "";
  return [stage, account.watermark_ready ? "去水印已就绪" : "", account.agreement_ready ? "协议已确认" : ""].filter(Boolean).join(" · ");
}
function renderBatchResult() {
  if (!state.batchReport) return;
  const report = state.batchReport;
  const lines = [`共 ${report.input_count || 0} 行 / 保存 ${report.count || 0} / 重复 ${report.duplicate_count || 0} / 已排队 ${report.login_started_count || 0}`];
  for (const row of report.results || []) {
    const job = state.loginJobs.find((item) => Number(item.account_id) === Number(row.account_id));
    const account = state.accounts.find((item) => Number(item.id) === Number(row.account_id));
    const progress = job ? loginStageLabel(job.phase) : account ? loginStageLabel(account.login_stage) : "";
    lines.push(`第 ${row.line} 行${row.account_id ? ` · #${row.account_id}` : ""}${row.login_identifier ? ` · ${row.login_identifier}` : ""}：${progress || row.message || "已处理"}${job?.message ? ` · ${job.message}` : ""}`);
  }
  lines.push(...(report.errors || []));
  const panel = $("#batchResult");
  panel.hidden = false;
  panel.classList.toggle("error", Boolean(report.errors?.length));
  panel.textContent = lines.join("\n");
}
function proxyLabel(value) { try { const url = new URL(value); return `${url.hostname}${url.port ? `:${url.port}` : ""}`; } catch { return value ? "代理已配置" : "直连"; } }
function failureMessage(task) {
  if (task.public_error?.message) return task.public_error.message;
  if (task.error_code === "VIDEO_EDIT_CONSTRAINT") return "参考视频被识别为编辑任务，所选时长或画幅与原视频不兼容；请明确要求参考生成，或使用自适应画幅。";
  if (task.error_code === "UPSTREAM_TEMPORARY") return "上游视频生成暂时失败，请稍后重试。";
  if (/content (?:was flagged by our moderation system|violates safety rules)/i.test(task.error_message || "")) return publicErrors.CONTENT_MODERATION_FAILED;
  if (/\bduration\s+must\s+be\s+between\s+\d+(?:\.\d+)?\s*s\s+and\s+\d+(?:\.\d+)?\s*s\b/i.test(task.error_message || "")) return "素材时长不支持，请修改后再试";
  return publicErrors[String(task.error_code || "").toUpperCase()] || "生成失败，积分已返还，请重试~";
}

function executionLabel(backend) { return backend === "canvas_cli" ? "Canvas CLI" : backend === "browser" ? "网页" : "待分配"; }
function cliAuthLabel(status) { return ({ pending: "待授权", required: "待授权", ready: "已授权", expired: "授权已过期", error: "授权异常", queued: "等待授权", authorizing: "授权中", manual_required: "等待确认授权", failed: "授权失败", cancelled: "已取消授权" })[status] || "待授权"; }

function accountAvailability(account, settings = state.settings) {
  const cli = account.execution_mode !== "browser";
  const runtime = settings.canvas_cli_runtime || {};
  const authorized = account.cli_auth_status === "ready";
  const gateOpen = settings.canvas_cli_enabled === true;
  const runtimeAvailable = runtime.available !== false;
  const enabledAndActive = Boolean(account.enabled) && account.status === "active";
  let status = account.enabled || account.status === "cli_auth_required" || ["disabled_low_balance", "suspended", "logging_in", "login_pending", "login_required", "login_failed", "challenge_required", "verification_required", "setup_required", "agreement_required", "network_error"].includes(account.status) ? account.status : "disabled";
  if (cli && enabledAndActive) {
    status = !authorized ? "cli_auth_required" : !runtimeAvailable ? "cli_runtime_unavailable" : !gateOpen ? "cli_paused" : "active";
  }
  const notice = cli && !runtimeAvailable
    ? `CLI 运行环境不兼容，暂不能接单${runtime.message ? `：${runtime.message}` : ""}`
    : cli && !authorized ? "请完成 Canvas CLI 授权" : cli && !gateOpen ? "Canvas CLI 接单已关闭" : "";
  const summary = cli ? [cliAuthLabel(account.cli_auth_status), !gateOpen ? "接单已关闭" : "", !runtimeAvailable ? "CLI 环境不兼容" : ""].filter(Boolean).join(" · ") : "";
  return { available: enabledAndActive && (!cli || authorized && gateOpen && runtimeAvailable), status, notice, summary };
}

function accountAvailabilityBadge(value) {
  if (value.status === "cli_runtime_unavailable") return '<span class="badge network_error">环境异常</span>';
  if (value.status === "cli_paused") return '<span class="badge disabled">已暂停</span>';
  const compact = { cli_auth_required: "待授权", verification_required: "待验证", agreement_required: "待确认" };
  if (compact[value.status]) return badge(value.status, compact[value.status]);
  return badge(value.status);
}

function renderAccounts() {
  const enabled = state.accounts.filter((item) => item.enabled); const disabled = state.accounts.filter((item) => !item.enabled);
  const items = state.accountFilter === "enabled" ? enabled : disabled;
  $("#enabledCount").textContent = enabled.length; $("#disabledCount").textContent = disabled.length;
  $("#accountsBody").innerHTML = items.map((account) => {
    const quota = account.balance_details || {};
    const amount = (value) => value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
      ? "-" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
    const detail = Array.isArray(quota.details)
      ? quota.details.map((item) => `${amount(item.available)}${item.expiresAt ? ` · 到期 ${fmtTime(item.expiresAt)}` : ""}`).join(" / ") || "上游未提供额度明细"
      : [["套餐", quota.vip_credit], ["赠送", quota.gift_credit], ["加购", quota.purchase_credit]]
        .filter(([, value]) => value !== undefined && value !== null).map(([label, value]) => `${label} ${amount(value)}`).join(" · ") || "上游未提供额度明细";
    const quotaSummary = Array.isArray(quota.details)
      ? quota.details.length ? `${quota.details.length}项额度` : "明细未提供"
      : [["套餐", quota.vip_credit], ["赠送", quota.gift_credit], ["加购", quota.purchase_credit]]
        .filter(([, value]) => Number(value) > 0).map(([label]) => label).join(" / ")
          || (["vip_credit", "gift_credit", "purchase_credit"].some((key) => quota[key] !== undefined && quota[key] !== null) ? "无可用额度" : "明细未提供");
    const progress = loginProgress(account);
    const availability = accountAvailability(account);
    const statusDetail = [availability.notice, progress, account.last_error].filter(Boolean).join(" · ");
    const identity = account.login_identifier || account.email || account.user_id || "";
    return `<tr><td><span class="cell-title"><span class="account-id mono">#${account.id}</span>${escapeHtml(account.name)}</span><span class="cell-sub" title="${escapeHtml(identity)}">${escapeHtml(identity)}</span></td>
      <td title="${escapeHtml(statusDetail)}">${accountAvailabilityBadge(availability)}</td>
      <td title="${escapeHtml(detail)}"><span class="cell-title mono">${escapeHtml(amount(account.last_balance))}</span><span class="cell-sub">${escapeHtml(quotaSummary)}</span></td>
      <td><input class="priority-input mono" data-account-priority="${account.id}" type="number" min="0" max="2147483647" step="1" value="${account.priority ?? 0}" title="优先级越高越先执行；相同优先级按余额和账号编号排序" aria-label="账号 #${account.id} 执行优先级"></td>
      <td><span class="slots ${account.active_tasks ? "busy" : ""}"><i></i><b class="mono">${account.active_tasks || 0} /</b><input class="concurrency-input mono" data-account-concurrency="${account.id}" type="number" min="1" value="${account.max_concurrency}" title="修改账号并发"></span></td><td class="mono">${account.total_uses || 0}</td>
      <td><span class="cell-title mono proxy" title="${escapeHtml(proxyLabel(account.proxy_url))}">${escapeHtml(proxyLabel(account.proxy_url))}</span></td><td title="${escapeHtml([availability.summary, account.cli_auth_error].filter(Boolean).join(" · "))}"><div class="execution-controls"><select class="execution-select" data-account-mode="${account.id}" aria-label="账号 #${account.id} 提交模式"${account.active_tasks || account.status === "logging_in" ? " disabled" : ""}><option value="canvas_cli"${account.execution_mode !== "browser" ? " selected" : ""}>Canvas CLI</option><option value="browser"${account.execution_mode === "browser" ? " selected" : ""}>网页提交</option></select><button class="link-button" data-action="cli-auth" data-id="${account.id}"${account.active_tasks || account.status === "logging_in" ? " disabled" : ""}>CLI 授权</button></div>${account.execution_mode === "browser" ? `<span class="cell-sub">CDP ${account.cdp_port || "按需启动"}</span>` : ""}</td><td>${fmtTime(account.updated_at)}</td>
      <td><div class="row-actions">${account.password_configured ? `<button class="icon-button" data-action="auto-login" data-id="${account.id}" title="自动登录并完成去水印 / AI 协议设置"${account.status === "logging_in" ? " disabled" : ""}><i data-lucide="log-in"></i></button>` : ""}<button class="icon-button" data-action="check" data-id="${account.id}" title="检测"><i data-lucide="activity"></i></button><button class="icon-button" data-action="login" data-id="${account.id}" title="CDP 登录"><i data-lucide="monitor-up"></i></button><button class="icon-button" data-action="web-login" data-id="${account.id}" title="网页登录 / 打开账号网页"><i data-lucide="monitor"></i></button><button class="icon-button${account.status === "challenge_required" ? " needs-verification" : ""}" data-action="browser-assist" data-id="${account.id}" title="人工验证"><i data-lucide="mouse-pointer-2"></i></button><button class="icon-button" data-action="reset-profile" data-id="${account.id}" title="重置 Profile"><i data-lucide="fingerprint"></i></button><button class="icon-button" data-action="toggle" data-id="${account.id}" data-enabled="${account.enabled}" title="${account.enabled ? "禁用" : "启用"}"><i data-lucide="${account.enabled ? "pause" : "play"}"></i></button>${account.enabled ? "" : `<button class="icon-button danger" data-action="delete" data-id="${account.id}" title="删除"><i data-lucide="trash-2"></i></button>`}</div></td></tr>`;
  }).join("");
  $("#accountsEmpty").classList.toggle("show", !items.length); $("#metricAccounts").textContent = `${state.accounts.filter((item) => accountAvailability(item).available).length} / ${state.accounts.length}`;
  $("#metricBalance").textContent = state.accounts.reduce((sum, item) => sum + Number(item.last_balance || 0), 0).toLocaleString(undefined, { maximumFractionDigits: 2 }); icons();
}

function mediaFromTask(task) {
  const inputs = Array.isArray(task.media_inputs) ? task.media_inputs.map((item) => ({ ...item, result: false })) : [];
  const results = (task.result_urls || []).map((url, index) => ({ kind: "video", label: `结果${index + 1}`, url, result: true })); return [...inputs, ...results];
}
function taskSpec(task) { const request = task.request || {}; return [task.model, request.duration ? `${request.duration}S` : "", request.resolution, request.aspect_ratio].filter(Boolean).join(" · "); }
function costObservationCell(task) {
  return globalThis.TaskCost?.cell(task.id, task.task_cost) || '<span class="cell-sub">未知</span>';
}

function upstreamIdentifierCell(task) {
  const ids = task.upstream_identifiers || {};
  const line = (label, value, hint = '') => `<span class="upstream-id-line"><span>${label}</span><button type="button" class="link-button mono" data-copy-upstream-id="${escapeHtml(value)}" title="${escapeHtml(hint || value)}">${escapeHtml(value)}</button></span>`;
  const rows = [];
  if (ids.task_id) rows.push(line('上游任务ID', ids.task_id));
  for (const value of ids.resource_ids || []) rows.push(line('上游资源ID', value));
  if (!rows.length) rows.push('<span class="cell-sub">上游任务ID：尚未取得</span>');
  if (ids.submission_id) rows.push(line('提交ID', ids.submission_id, `本地预分配的 Canvas 提交标识，用于查询原提交；不是上游生成任务ID。${ids.submission_id}`));
  return `<span class="upstream-identifiers">${rows.join('')}</span>`;
}

async function copyUpstreamIdentifier(value) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(value); return; } catch (_) { /* LAN HTTP fallback below. */ }
  }
  const previous = document.activeElement;
  const input = document.createElement('textarea');
  input.value = value; input.readOnly = true;
  input.style.cssText = 'position:fixed;left:-9999px;top:0';
  document.body.append(input);
  try {
    input.select(); input.setSelectionRange(0, input.value.length);
    if (!document.execCommand('copy')) throw new Error('复制失败，请选中上游标识复制');
  } finally { input.remove(); previous?.focus(); }
}

function renderTasks() {
  const items = state.tasks.slice(0, 20); $("#tasksBody").innerHTML = items.map((task) => {
    const unknown = globalThis.CanvasTaskState.unknown(task);
    const media = mediaFromTask(task); const inputs = media.filter((item) => !item.result); const results = media.filter((item) => item.result);
    const result = unknown ? `<span class="error-upstream" title="${escapeHtml(task.error_message)}">待核实：${escapeHtml(task.error_message || "原提交回执未确认")}</span>` : task.status === "failed" || task.status === "expired" ? `<span class="error-stack"><span class="error-public">响应：${escapeHtml(failureMessage(task))}</span><span class="error-upstream" title="${escapeHtml(task.error_message)}">上游：${escapeHtml(task.error_message || "未知错误")}</span></span>` : `<span class="result-links">${results.map((item) => `<a href="${escapeHtml(item.url)}" target="_blank">视${results.length > 1 ? results.indexOf(item) + 1 : ""}</a>`).join("")}</span>`;
    return `<tr><td><button class="cell-title link-button mono" data-action="detail" data-id="${escapeHtml(task.id)}">${escapeHtml(task.id.slice(0, 20))}</button><span class="cell-sub mono">${escapeHtml((task.channel || "待分配").toUpperCase())} · ${executionLabel(task.execution_backend)}${task.account_id ? ` · #${task.account_id} · ${escapeHtml(task.account_name || "")}` : ""}</span></td>
      <td class="upstream-id-column">${upstreamIdentifierCell(task)}</td>
      <td class="prompt-cell"><span class="cell-title" title="${escapeHtml(task.prompt)}">${escapeHtml(task.prompt || "-")}</span><span class="task-meta-line"><span class="task-spec">${escapeHtml(taskSpec(task))}</span>${inputs.length ? `<span class="media-text">${inputs.map((item, index) => `<button data-action="media" data-id="${escapeHtml(task.id)}" data-media-index="${index}">${{ image: "图", video: "视", audio: "音" }[item.kind]}${item.label}</button>`).join(" ")}</span>` : ""}</span></td><td class="task-cost-column">${costObservationCell(task)}</td>
      <td>${unknown ? `<span class="badge submitted">${globalThis.CanvasTaskState.label(task)}</span>` : badge(task.status)}</td><td><div class="progress-stack"><span class="progress-value"><b class="mono">${globalThis.CanvasTaskState.progress(task)}</b>${unknown ? '' : `<span class="progress-track"><i style="width:${Math.min(Number(task.progress || 0), 100)}%"></i></span>`}</span><span class="elapsed">${terminal.has(task.status) ? "耗时" : "已用"} ${elapsed(task)}</span></div></td>
      <td><span class="time-stack"><span>创建 ${fmtTime(task.created_at)}</span><span>更新 ${fmtTime(task.updated_at)}</span></span></td><td>${result}</td></tr>`;
  }).join("");
  $("#tasksEmpty").classList.toggle("show", !items.length); $("#taskCount").textContent = `最近 ${items.length} 条`; $("#metricRunning").textContent = items.filter((item) => globalThis.CanvasTaskState.active(item)).length; $("#metricComplete").textContent = items.filter((item) => item.status === "succeeded").length; icons();
  globalThis.TaskCost?.hydrate(document, {});
}

function defaultPayload() {
  const model = state.models.find((item) => item.id === state.selectedModel) || state.models[0] || {};
  const capabilities = model.capabilities || {}; const limits = capabilities.media_limits || {};
  const payload = { model: model.id || state.selectedModel, prompt: "A blue bird on a branch, slow camera push.", duration: 4, resolution: (capabilities.resolutions || ["480p"])[0], aspect_ratio: "16:9", background: true };
  if (limits.images > 0) payload.image_urls = [];
  if (limits.videos > 0) payload.video_urls = [];
  if (limits.audios > 0) payload.audio_urls = [];
  if (capabilities.generate_audio === false) payload.generate_audio = false;
  return payload;
}
function renderModels(resetPayload = true) {
  if (!state.models.some((model) => model.id === state.selectedModel) && state.models[0]) state.selectedModel = state.models[0].id;
  $("#modelTabs").innerHTML = state.models.map((model) => `<button class="${model.id === state.selectedModel ? "active" : ""}" type="button" data-model="${escapeHtml(model.id)}">${escapeHtml(model.meta?.label || model.id)}</button>`).join("");
  const selected = state.models.find((item) => item.id === state.selectedModel); const caps = selected?.capabilities || {}; const limits = caps.media_limits || {};
  $("#sampleMediaHint span").textContent = `支持 ${limits.images || 0} 图、${limits.videos || 0} 视频、${limits.audios || 0} 音频；${(caps.resolutions || []).join("/")}；素材超限策略由设置控制。`;
  $("#metricModels").textContent = state.models.length; if (resetPayload) $("#requestJson").value = JSON.stringify(defaultPayload(), null, 2); icons();
}
function validateJson() { try { JSON.parse($("#requestJson").value); $("#jsonValidation").textContent = "JSON 有效"; $("#jsonValidation").className = "validation"; return true; } catch (error) { $("#jsonValidation").textContent = error.message; $("#jsonValidation").className = "validation error"; return false; } }

function callerResponse(task) { const value = { id: task.id, object: "video.generation", model: task.model, status: task.status, progress: task.progress, data: (task.result_urls || []).map((url) => ({ url })) }; if (terminal.has(task.status) && task.status !== "succeeded") value.error = { code: task.error_code, message: failureMessage(task) }; return value; }
function renderAudit() { const values = { caller_request: state.detail?.caller_request, upstream_request: state.detail?.upstream_request, upstream_response: state.detail?.upstream_response, execution_diagnostics: state.detail?.execution_diagnostics, caller_response: state.detail ? callerResponse(state.detail) : {} }; $("#auditCode").textContent = JSON.stringify(values[state.auditKey] || {}, null, 2); }
function showDetail(task) {
  state.detail = task; state.auditKey = "caller_request"; $("#detailId").textContent = task.id; $("#detailTitle").textContent = `${task.model} · 视频`;
  const facts = [["提交模式", executionLabel(task.execution_backend)], ["状态", globalThis.CanvasTaskState.unknown(task) ? globalThis.CanvasTaskState.label(task) : task.status], ["进度", `${globalThis.CanvasTaskState.progress(task)}`], ["耗时", elapsed(task)], ["渠道", (task.channel || "-").toUpperCase()], ["账号", task.account_id ? `#${task.account_id}` : "-"], ["上游任务", task.generation_id || "-"]];
  $("#detailFacts").innerHTML = facts.map(([key, value]) => `<div><span>${key}</span><b title="${escapeHtml(value)}">${escapeHtml(value)}</b></div>`).join(""); $("#detailPrompt").textContent = task.prompt || "-"; $("#detailMeta").textContent = taskSpec(task);
  const request = task.request || {}; const media = [...(request._images || []).map((item) => ({ ...item, kind: "image" })), ...(request._videos || []).map((item) => ({ ...item, kind: "video" })), ...(request._audio || []).map((item) => ({ ...item, kind: "audio" }))];
  $("#detailMedia").innerHTML = media.map((item, index) => `<button data-detail-media="${index}">${{ image: "图", video: "视", audio: "音" }[item.kind]}${index + 1}</button>`).join(""); $$("#auditTabs button").forEach((button) => button.classList.toggle("active", button.dataset.key === state.auditKey)); renderAudit(); $("#reconcileSubmission").hidden = !globalThis.CanvasTaskState.canReconcile(task); $("#reconcileSubmission").disabled = false; if (!$("#detailDialog").open) $("#detailDialog").showModal(); icons();
}
function previewMedia(item, anchor) { if (!item) return; const popover = $("#mediaPopover"); const url = escapeHtml(item.url || item.value || ""); popover.innerHTML = item.kind === "video" ? `<video src="${url}" controls autoplay muted></video>` : item.kind === "audio" ? `<audio src="${url}" controls autoplay></audio>` : `<img src="${url}" alt="素材预览">`; const rect = anchor.getBoundingClientRect(); popover.style.left = `${Math.min(rect.left, innerWidth - 340)}px`; popover.style.top = `${Math.min(rect.bottom + 6, innerHeight - 260)}px`; popover.hidden = false; }

function renderCosts() { const rows = state.costReferences || []; $("#costReferenceBody").innerHTML = rows.map((item) => { const builtin = item.source === "builtin_rate"; return `<tr><td class="mono">${escapeHtml(item.model)}<span class="cell-sub">${executionLabel(item.execution_backend || "browser")}</span></td><td class="mono">${builtin ? "按整秒" : `${item.duration || 0}S`}</td><td>${escapeHtml(item.resolution || "-")}</td><td><strong class="cost-value mono">${builtin ? `⌊${fmtCost(item.rate_per_second)} × 秒数⌋` : fmtCost(item.cost)}</strong></td><td class="mono">${builtin ? "-" : fmtCost(item.latest_cost)}</td><td>${builtin ? "内置" : item.samples || 0}</td><td>${fmtTime(item.updated_at)}</td><td class="mono">${escapeHtml(String(item.last_task_id || "-").slice(0, 18))}</td></tr>`; }).join(""); $("#costReferenceEmpty").classList.toggle("show", !rows.length); $("#costReferenceStatus").textContent = `${rows.length} 条参考`; }

async function refresh() {
  $("#refreshStatus").textContent = "刷新中...";
  try {
    const [accounts, tasks, models, settings, loginResults] = await Promise.all([api("/api/accounts"), api(`/api/tasks?limit=${state.taskLimit}`), api("/api/models"), api("/api/settings"), api("/api/accounts/login-results")]);
    Object.assign(state, { accounts, tasks, models, settings, loginJobs: loginResults.jobs || [] });
    renderAccounts(); renderTasks(); renderBatchResult();
    $("#metricModels").textContent = models.length;
    $("#refreshStatus").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
  } catch (error) { toast(error.message, true); $("#refreshStatus").textContent = "刷新失败"; }
}
async function accountAction(button) { const id = Number(button.dataset.id); try { if (button.dataset.action === "cli-auth") { await startCliAuth(id); return; } else if (button.dataset.action === "delete") { if (!confirm("确认删除该账号及托管 Profile？")) return; await api(`/api/accounts/${id}`, { method: "DELETE" }); } else if (button.dataset.action === "toggle") await api(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: button.dataset.enabled !== "true" }) }); else if (button.dataset.action === "auto-login") await api(`/api/accounts/${id}/auto-login`, { method: "POST" }); else if (button.dataset.action === "login") await api(`/api/accounts/${id}/cdp/reconnect`, { method: "POST" }); else if (button.dataset.action === "check") await api(`/api/accounts/${id}/check`, { method: "POST" }); else if (button.dataset.action === "reset-profile") { const account = state.accounts.find((item) => item.id === id); const form = $("#profileResetForm"); form.account_id.value = id; form.proxy_url.value = account.proxy_url || ""; $("#profileResetAccount").textContent = `#${id} · ${account.name}`; $("#profileResetStatus").textContent = account.status; $("#profileResetDialog").showModal(); return; } toast("操作已提交"); await refresh(); } catch (error) { toast(error.message, true); } }

let cliAuthAccountId = null;
let cliAuthPollTimer = null;
function renderCliAuth(payload) {
  const status = payload.status || payload.state || "pending";
  const message = payload.message || payload.error || (["ready", "authorized"].includes(status) ? "CLI 授权完成。" : "等待完成账号授权…");
  $("#cliAuthMessage").textContent = typeof message === "string" ? message : "请查看账号授权状态";
  const code = payload.user_code || "";
  $("#cliAuthCode").textContent = code ? `授权码：${code}` : ""; $("#cliAuthCode").hidden = !code;
  const hasAuthorizationPage = Boolean(payload.verification_uri_complete || payload.verification_uri);
  $("#cliAuthAssist").hidden = !hasAuthorizationPage || ["ready", "authorized", "expired", "error", "failed", "cancelled", "required"].includes(status);
  $("#cliAuthCancel").hidden = ["ready", "authorized", "expired", "error", "failed", "cancelled", "required"].includes(status);
  return status;
}
async function startCliAuth(id) {
  clearTimeout(cliAuthPollTimer); cliAuthAccountId = id;
  $("#cliAuthTitle").textContent = `账号 #${id} CLI 授权`;
  renderCliAuth({ message: "正在准备账号授权…" }); $("#cliAuthDialog").showModal();
  try { const payload = await api(`/api/accounts/${id}/cli-auth`, { method: "POST" }); if (cliAuthAccountId !== id) return; renderCliAuth(payload); await pollCliAuth(); }
  catch (error) { renderCliAuth({ status: "error", message: error.message }); }
}
async function pollCliAuth() {
  if (!$("#cliAuthDialog").open || cliAuthAccountId === null) return;
  const id = cliAuthAccountId;
  try {
    const payload = await api(`/api/accounts/${id}/cli-auth`);
    if (cliAuthAccountId !== id || !$("#cliAuthDialog").open) return;
    const status = renderCliAuth(payload);
    if (["ready", "authorized", "expired", "error", "failed", "cancelled", "required"].includes(status)) {
      if (typeof closeCanvasAuthAssist === "function") closeCanvasAuthAssist(id);
      await refresh(); return;
    }
    cliAuthPollTimer = setTimeout(pollCliAuth, 3000);
  } catch (error) { renderCliAuth({ status: "error", message: error.message }); }
}
$("#cliAuthDialog").addEventListener("close", () => { clearTimeout(cliAuthPollTimer); cliAuthAccountId = null; });
$("#cliAuthAssist").addEventListener("click", async () => {
  if (cliAuthAccountId === null) return;
  try { await openBrowserAssist(cliAuthAccountId, { cliAuth: true }); }
  catch (error) { toast(error.message, true); }
});
$("#cliAuthCancel").addEventListener("click", async () => {
  clearTimeout(cliAuthPollTimer);
  if (cliAuthAccountId === null) return;
  try { await api(`/api/accounts/${cliAuthAccountId}/cli-auth`, { method: "DELETE" }); renderCliAuth({ status: "cancelled", message: "授权已取消" }); await refresh(); }
  catch (error) { renderCliAuth({ status: "error", message: error.message }); }
});

document.addEventListener("click", async (event) => {
  const close = event.target.closest(".close"); if (close) close.closest("dialog")?.close();
  const accountButton = event.target.closest("[data-action='cli-auth'],[data-action='auto-login'],[data-action='check'],[data-action='login'],[data-action='reset-profile'],[data-action='toggle'],[data-action='delete']"); if (accountButton) await accountAction(accountButton);
  const reconcile = event.target.closest('#reconcileSubmission');
  if (reconcile && state.detail) {
    reconcile.disabled = true;
    try { showDetail(await api(`/api/tasks/${state.detail.id}/reconcile`, { method: 'POST' })); toast('已查询原提交，未重新生成'); }
    catch (error) { toast(error.message, true); }
    finally { reconcile.disabled = false; await refresh(); }
    return;
  }
  const upstreamId = event.target.closest('[data-copy-upstream-id]');
  if (upstreamId) {
    try { await copyUpstreamIdentifier(upstreamId.dataset.copyUpstreamId); toast('上游标识已复制'); }
    catch (error) { toast(error.message || '复制失败', true); }
    return;
  }
  const detail = event.target.closest("[data-action='detail']"); if (detail) try { showDetail(await api(`/api/tasks/${detail.dataset.id}`)); } catch (error) { toast(error.message, true); }
  const media = event.target.closest("[data-action='media']"); if (media) { const task = state.tasks.find((item) => item.id === media.dataset.id); previewMedia(mediaFromTask(task).filter((item) => !item.result)[Number(media.dataset.mediaIndex)], media); }
  const detailMedia = event.target.closest("[data-detail-media]"); if (detailMedia) { const request = state.detail.request || {}; const items = [...(request._images || []).map((item) => ({ ...item, kind: "image" })), ...(request._videos || []).map((item) => ({ ...item, kind: "video" })), ...(request._audio || []).map((item) => ({ ...item, kind: "audio" }))]; previewMedia(items[Number(detailMedia.dataset.detailMedia)], detailMedia); }
  if (!event.target.closest("#mediaPopover,[data-action='media'],[data-detail-media]")) $("#mediaPopover").hidden = true;
});
document.addEventListener("change", async (event) => {
  const modeInput = event.target.closest("[data-account-mode]");
  if (modeInput) {
    modeInput.disabled = true;
    try { await api(`/api/accounts/${Number(modeInput.dataset.accountMode)}`, { method: "PATCH", body: JSON.stringify({ execution_mode: modeInput.value }) }); toast("账号提交模式已更新"); }
    catch (error) { toast(error.message, true); }
    await refresh(); return;
  }
  const priorityInput = event.target.closest("[data-account-priority]");
  if (priorityInput) {
    const id = Number(priorityInput.dataset.accountPriority); const value = Number(priorityInput.value);
    if (!priorityInput.value.trim() || !Number.isInteger(value) || value < 0 || value > 2147483647) {
      toast("账号优先级必须是 0 到 2147483647 的整数", true); await refresh(); return;
    }
    priorityInput.disabled = true;
    try { await api(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify({ priority: value }) }); toast(`账号 #${id} 优先级已调整为 ${value}`); await refresh(); }
    catch (error) { toast(error.message, true); await refresh(); }
    return;
  }
  const input = event.target.closest("[data-account-concurrency]"); if (!input) return;
  const id = Number(input.dataset.accountConcurrency); const value = Number(input.value);
  if (!Number.isInteger(value) || value < 1) { toast("账号并发必须是大于 0 的整数", true); await refresh(); return; }
  input.disabled = true;
  try { await api(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify({ max_concurrency: value }) }); toast(`账号 #${id} 并发已调整为 ${value}`); await refresh(); }
  catch (error) { toast(error.message, true); await refresh(); }
});
$("#accountTabs").addEventListener("click", (event) => { const button = event.target.closest("[data-filter]"); if (!button) return; state.accountFilter = button.dataset.filter; $$("#accountTabs button").forEach((item) => item.classList.toggle("active", item === button)); renderAccounts(); });
$("#auditTabs").addEventListener("click", (event) => { const button = event.target.closest("[data-key]"); if (!button) return; state.auditKey = button.dataset.key; $$("#auditTabs button").forEach((item) => item.classList.toggle("active", item === button)); renderAudit(); });
$("#modelTabs").addEventListener("click", (event) => { const button = event.target.closest("[data-model]"); if (!button) return; state.selectedModel = button.dataset.model; renderModels(true); validateJson(); });
$("#refreshButton").addEventListener("click", refresh); $("#accountsRefresh").addEventListener("click", refresh); $("#tasksRefresh").addEventListener("click", refresh);
$("#taskLimit").value = String(state.taskLimit);
$("#taskLimit").addEventListener("change", () => {
  const limit = Number($("#taskLimit").value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
    $("#taskLimit").value = String(state.taskLimit);
    toast("最近任务条数须为 1–1000 的整数", true);
    return;
  }
  state.taskLimit = limit;
  try { localStorage.setItem(TASK_LIMIT_STORAGE_KEY, String(limit)); } catch {}
  refresh();
});
$("#addAccountButton").addEventListener("click", () => { $("#accountForm").reset(); $("#accountForm").elements.namedItem("max_concurrency").value = 1; $("#accountDialog").showModal(); });
$("#batchButton").addEventListener("click", () => { renderBatchResult(); $("#batchDialog").showModal(); });
$("#newTaskButton").addEventListener("click", () => { renderModels(); validateJson(); $("#taskDialog").showModal(); }); $("#requestJson").addEventListener("input", validateJson);
function lowBalanceModelOptions(settings) {
  const values = settings?.low_balance_seedream_target_options;
  return Array.isArray(values) ? [...new Set(values.filter((value) => typeof value === "string" && /^[a-z0-9][a-z0-9._-]*$/.test(value)))] : [];
}
function renderLowBalanceSettings(form, settings) {
  form.elements.namedItem("low_balance_image_rescue_threshold").value = String(settings.low_balance_image_rescue_threshold ?? 50);
  const select = form.elements.namedItem("low_balance_seedream_target_model");
  const target = settings.low_balance_seedream_target_model || "seedream-5.0-pro";
  const options = lowBalanceModelOptions(settings);
  select.innerHTML = options.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}${value === "seedream-5.0-pro" ? "（保持原模型）" : ""}</option>`).join("");
  if (!options.includes(target)) select.innerHTML += `<option value="${escapeHtml(target)}" disabled>${escapeHtml(target)}（模型列表暂不可用）</option>`;
  select.value = target;
}
function lowBalanceSettingsPayload(form, settings) {
  const raw = form.elements.namedItem("low_balance_image_rescue_threshold").value.trim();
  const threshold = Number(raw);
  if (!raw || !Number.isFinite(threshold) || threshold < 0) throw new Error("余额门槛须为大于等于 0 的有效数字；0 表示关闭规则");
  const target = form.elements.namedItem("low_balance_seedream_target_model").value;
  if (!lowBalanceModelOptions(settings).includes(target)) throw new Error("实际生成模型不可用，请刷新设置后重新选择");
  return { low_balance_image_rescue_threshold: threshold, low_balance_seedream_target_model: target };
}
$("#settingsButton").addEventListener("click", () => {
  const form = $("#settingsForm");
  Object.entries(state.settings).forEach(([key, value]) => {
    const input = form.elements.namedItem(key);
    if (!input || key === "result_s3_center_token") return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";
  });
  renderLowBalanceSettings(form, state.settings);
  form.elements.namedItem("result_s3_center_token").value = "";
  $("#s3TokenHint").textContent = state.settings.result_s3_center_token_configured
    ? "已配置；留空保留当前密钥" : "尚未配置";
  $("#settingsDialog").showModal();
});
$("#integrationDocsButton").addEventListener("click", async () => { try { $("#integrationDocsContent").textContent = await apiText("/api/integration-docs"); $("#integrationDocsDialog").showModal(); } catch (error) { toast(error.message, true); } });
$("#copyIntegrationDocs").addEventListener("click", async () => { await navigator.clipboard.writeText($("#integrationDocsContent").textContent); toast("Markdown 已复制"); });
$("#costReferenceButton").addEventListener("click", async () => { $("#costReferenceDialog").showModal(); state.costReferences = await api("/api/model-costs?limit=500"); renderCosts(); }); $("#costReferenceRefresh").addEventListener("click", async () => { state.costReferences = await api("/api/model-costs?limit=500"); renderCosts(); });
$("#accountForm").addEventListener("submit", async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); try { data.max_concurrency = Number(data.max_concurrency || 1); data.cdp_port = Number(data.cdp_port || 0); await api("/api/accounts", { method: "POST", body: JSON.stringify(data) }); event.currentTarget.reset(); $("#accountDialog").close(); toast("Account saved. Open web login next."); await refresh(); } catch (error) { toast(error.message, true); } });
$("#batchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget; const panel = $("#batchResult"); const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    state.batchReport = await api("/api/accounts/batch-import", { method: "POST", body: JSON.stringify({ text: form.elements.namedItem("text").value }) });
    form.elements.namedItem("text").value = "";
    renderBatchResult(); await refresh();
  } catch (error) { panel.hidden = false; panel.classList.add("error"); panel.textContent = error.message; toast(error.message, true); }
  finally { submit.disabled = false; }
});
$("#profileResetForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { await api(`/api/accounts/${form.account_id.value}/profile/reset`, { method: "POST", body: JSON.stringify({ proxy_url: form.proxy_url.value || null, start_login: form.start_login.checked }) }); $("#profileResetDialog").close(); toast("Profile 已重置"); await refresh(); } catch (error) { toast(error.message, true); } });
$("#settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const field = (name) => form.elements.namedItem(name);
  let lowBalance;
  try { lowBalance = lowBalanceSettingsPayload(form, state.settings); }
  catch (error) { toast(error.message, true); return; }
  const payload = {
    ...lowBalance,
    canvas_cli_enabled: field("canvas_cli_enabled").checked,
    task_workers: Number(field("task_workers").value),
    task_queue_capacity: Number(field("task_queue_capacity").value),
    poll_interval_seconds: Number(field("poll_interval_seconds").value),
    task_timeout_seconds: Number(field("task_timeout_seconds").value),
    daily_checkin_enabled: field("daily_checkin_enabled").checked,
    low_balance_disable_threshold: Number(field("low_balance_disable_threshold").value),
    chrome_executable: field("chrome_executable").value,
    chrome_user_data_root: field("chrome_user_data_root").value,
    allow_video_reference_inputs: field("allow_video_reference_inputs").checked,
    prompt_media_reference_cleanup_enabled: field("prompt_media_reference_cleanup_enabled").checked,
    strict_prompt_media_references: field("strict_prompt_media_references").checked,
    enable_sd_2_5_480p_x: field("enable_sd_2_5_480p_x").checked,
    result_s3_center_api_base: field("result_s3_center_api_base").value.trim(),
    result_s3_bucket: field("result_s3_bucket").value.trim(),
    result_s3_key_prefix: field("result_s3_key_prefix").value.trim(),
  };
  const token = field("result_s3_center_token").value.trim();
  if (token) payload.result_s3_center_token = token;
  try {
    state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload) });
    $("#settingsDialog").close(); toast("Settings saved"); await refresh();
  } catch (error) { toast(error.message, true); }
});
$("#taskForm").addEventListener("submit", async (event) => { event.preventDefault(); if (!validateJson()) return; try { const task = await api("/api/tasks", { method: "POST", body: $("#requestJson").value }); $("#taskDialog").close(); toast(`任务已创建：${task.id}`); await refresh(); } catch (error) { toast(error.message, true); } });
$("#clearTasks").addEventListener("click", async () => { try { const result = await api("/api/tasks", { method: "DELETE" }); toast(`已清理 ${result.deleted} 个任务`); await refresh(); } catch (error) { toast(error.message, true); } });

icons(); refresh(); setInterval(refresh, 8000);

// Shared task cost presentation: advisory only, independent of generation.
globalThis.TaskCost?.hydrate(document, {});
