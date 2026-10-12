const state = { accounts: [], tasks: [], models: [], settings: {}, accountFilter: "enabled", selectedModel: "sd-2-0-mini-480p", detail: null, auditKey: "caller_request", costReferences: [] };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const terminal = new Set(["succeeded", "failed", "expired"]);
const publicErrors = { CONTENT_MODERATION_FAILED: "检测到视频有敏感或违规内容，请修改后重试，积分已返还～", MEDIA_DOWNLOAD_FAILED: "素材下载失败，请检查~", PROVIDER_INVALID_REQUEST: "处理失败，请检查图音视频格式和大小" };

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
function fmtTime(value) { return value ? new Date(Number(value) * 1000).toLocaleString() : "-"; }
function fmtCost(value) { const number = Number(value || 0); return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, "").replace(/\.$/, ""); }
function elapsed(task) { const end = Number(task.completed_at || Date.now() / 1000); const total = Math.max(Math.floor(end - Number(task.created_at || end)), 0); return total < 60 ? `${total}秒` : `${Math.floor(total / 60)}分${total % 60}秒`; }
function badge(status) { const labels = { active: "可用", pending: "待检测", login_pending: "待登录", logging_in: "登录中", login_failed: "登录失败", challenge_required: "需验证", profile_resetting: "重置中", queued: "排队", preparing: "准备", submitted: "已提交", running: "生成中", succeeded: "完成", failed: "失败", expired: "超时", login_required: "需登录", network_error: "网络异常", suspended: "上游封禁", disabled: "禁用", disabled_low_balance: "低额度" }; return `<span class="badge ${escapeHtml(status)}">${escapeHtml(labels[status] || status || "未知")}</span>`; }
function proxyLabel(value) { try { const url = new URL(value); return `${url.hostname}:${url.port}`; } catch { return value || "直连"; } }
function failureMessage(task) {
  if (task.public_error?.message) return task.public_error.message;
  if (task.error_code === "VIDEO_EDIT_CONSTRAINT") return "参考视频被识别为编辑任务，所选时长或画幅与原视频不兼容；请明确要求参考生成，或使用自适应画幅。";
  if (task.error_code === "UPSTREAM_TEMPORARY") return "上游视频生成暂时失败，请稍后重试。";
  if (/content (?:was flagged by our moderation system|violates safety rules)/i.test(task.error_message || "")) return publicErrors.CONTENT_MODERATION_FAILED;
  if (/\bduration\s+must\s+be\s+between\s+\d+(?:\.\d+)?\s*s\s+and\s+\d+(?:\.\d+)?\s*s\b/i.test(task.error_message || "")) return "素材时长不支持，请修改后再试";
  return publicErrors[String(task.error_code || "").toUpperCase()] || "生成失败，积分已返还，请重试~";
}

function renderAccounts() {
  const enabled = state.accounts.filter((item) => item.enabled); const disabled = state.accounts.filter((item) => !item.enabled);
  const items = state.accountFilter === "enabled" ? enabled : disabled;
  $("#enabledCount").textContent = enabled.length; $("#disabledCount").textContent = disabled.length;
  $("#accountsBody").innerHTML = items.map((account) => {
    const quota = account.balance_details || {};
    const detail = `VIP ${quota.vip_credit ?? 0} / Gift ${quota.gift_credit ?? 0} / Add-on ${quota.purchase_credit ?? 0}`;
    return `<tr><td><span class="cell-title"><span class="account-id mono">#${account.id}</span>${escapeHtml(account.name)}</span><span class="cell-sub">${escapeHtml(account.email || account.user_id || "")}</span></td>
      <td>${badge(account.enabled ? account.status : ["disabled_low_balance", "suspended"].includes(account.status) ? account.status : "disabled")}<span class="cell-sub" title="${escapeHtml(account.last_error)}">${escapeHtml(account.last_error)}</span></td>
      <td title="${escapeHtml(detail)}"><span class="cell-title mono">${escapeHtml(account.last_balance ?? "-")}</span><span class="cell-sub">${escapeHtml(detail)}</span></td>
      <td><span class="slots ${account.active_tasks ? "busy" : ""}"><i></i><b class="mono">${account.active_tasks || 0} /</b><input class="concurrency-input mono" data-account-concurrency="${account.id}" type="number" min="1" value="${account.max_concurrency}" title="修改账号并发"></span></td><td class="mono">${account.total_uses || 0}</td>
      <td><span class="cell-title mono proxy" title="${escapeHtml(account.proxy_url)}">${escapeHtml(proxyLabel(account.proxy_url))}</span></td><td><span class="cell-title mono">${account.cdp_port || "自动"}</span><span class="cell-sub">${account.project_id || "打开视频项目"}</span></td><td>${fmtTime(account.updated_at)}</td>
      <td><div class="row-actions"><button class="icon-button" data-action="check" data-id="${account.id}" title="检测"><i data-lucide="activity"></i></button><button class="icon-button" data-action="login" data-id="${account.id}" title="CDP 登录"><i data-lucide="monitor-up"></i></button><button class="icon-button${account.status === "challenge_required" ? " needs-verification" : ""}" data-action="browser-assist" data-id="${account.id}" title="人工验证"><i data-lucide="mouse-pointer-2"></i></button><button class="icon-button" data-action="reset-profile" data-id="${account.id}" title="重置 Profile"><i data-lucide="fingerprint"></i></button><button class="icon-button" data-action="toggle" data-id="${account.id}" data-enabled="${account.enabled}" title="${account.enabled ? "禁用" : "启用"}"><i data-lucide="${account.enabled ? "pause" : "play"}"></i></button>${account.enabled ? "" : `<button class="icon-button danger" data-action="delete" data-id="${account.id}" title="删除"><i data-lucide="trash-2"></i></button>`}</div></td></tr>`;
  }).join("");
  $("#accountsEmpty").classList.toggle("show", !items.length); $("#metricAccounts").textContent = `${enabled.filter((item) => item.status === "active").length} / ${state.accounts.length}`;
  $("#metricBalance").textContent = state.accounts.reduce((sum, item) => sum + Number(item.last_balance || 0), 0).toLocaleString(); icons();
}

function mediaFromTask(task) {
  const inputs = Array.isArray(task.media_inputs) ? task.media_inputs.map((item) => ({ ...item, result: false })) : [];
  const results = (task.result_urls || []).map((url, index) => ({ kind: "video", label: `结果${index + 1}`, url, result: true })); return [...inputs, ...results];
}
function taskSpec(task) { const request = task.request || {}; return [task.model, request.duration ? `${request.duration}S` : "", request.resolution, request.aspect_ratio].filter(Boolean).join(" · "); }
function renderTasks() {
  const items = state.tasks.slice(0, 20); $("#tasksBody").innerHTML = items.map((task) => {
    const media = mediaFromTask(task); const inputs = media.filter((item) => !item.result); const results = media.filter((item) => item.result);
    const result = task.status === "failed" || task.status === "expired" ? `<span class="error-stack"><span class="error-public">响应：${escapeHtml(failureMessage(task))}</span><span class="error-upstream" title="${escapeHtml(task.error_message)}">上游：${escapeHtml(task.error_message || "未知错误")}</span></span>` : `<span class="result-links">${results.map((item) => `<a href="${escapeHtml(item.url)}" target="_blank">视${results.length > 1 ? results.indexOf(item) + 1 : ""}</a>`).join("")}</span>`;
    return `<tr><td><button class="cell-title link-button mono" data-action="detail" data-id="${escapeHtml(task.id)}">${escapeHtml(task.id.slice(0, 20))}</button><span class="cell-sub mono">${escapeHtml((task.channel || "待分配").toUpperCase())}${task.account_id ? ` · #${task.account_id} · ${escapeHtml(task.account_name || "")}` : ""}</span></td>
      <td class="prompt-cell"><span class="cell-title" title="${escapeHtml(task.prompt)}">${escapeHtml(task.prompt || "-")}</span><span class="task-meta-line"><span class="task-spec">${escapeHtml(taskSpec(task))}</span>${inputs.length ? `<span class="media-text">${inputs.map((item, index) => `<button data-action="media" data-id="${escapeHtml(task.id)}" data-media-index="${index}">${{ image: "图", video: "视", audio: "音" }[item.kind]}${item.label}</button>`).join(" ")}</span>` : ""}</span></td>
      <td>${badge(task.status)}</td><td><div class="progress-stack"><span class="progress-value"><b class="mono">${task.progress || 0}%</b><span class="progress-track"><i style="width:${Math.min(Number(task.progress || 0), 100)}%"></i></span></span><span class="elapsed">${terminal.has(task.status) ? "耗时" : "已用"} ${elapsed(task)}</span></div></td>
      <td><span class="time-stack"><span>创建 ${fmtTime(task.created_at)}</span><span>更新 ${fmtTime(task.updated_at)}</span></span></td><td>${result}</td></tr>`;
  }).join("");
  $("#tasksEmpty").classList.toggle("show", !items.length); $("#taskCount").textContent = `最近 ${items.length} 条`; $("#metricRunning").textContent = items.filter((item) => !terminal.has(item.status)).length; $("#metricComplete").textContent = items.filter((item) => item.status === "succeeded").length; icons();
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
function renderAudit() { const values = { caller_request: state.detail?.caller_request, upstream_request: state.detail?.upstream_request, upstream_response: state.detail?.upstream_response, caller_response: state.detail ? callerResponse(state.detail) : {} }; $("#auditCode").textContent = JSON.stringify(values[state.auditKey] || {}, null, 2); }
function showDetail(task) {
  state.detail = task; state.auditKey = "caller_request"; $("#detailId").textContent = task.id; $("#detailTitle").textContent = `${task.model} · 视频`;
  const facts = [["状态", task.status], ["进度", `${task.progress || 0}%`], ["耗时", elapsed(task)], ["渠道", (task.channel || "-").toUpperCase()], ["账号", task.account_id ? `#${task.account_id}` : "-"], ["上游任务", task.generation_id || "-"]];
  $("#detailFacts").innerHTML = facts.map(([key, value]) => `<div><span>${key}</span><b title="${escapeHtml(value)}">${escapeHtml(value)}</b></div>`).join(""); $("#detailPrompt").textContent = task.prompt || "-"; $("#detailMeta").textContent = taskSpec(task);
  const request = task.request || {}; const media = [...(request._images || []).map((item) => ({ ...item, kind: "image" })), ...(request._videos || []).map((item) => ({ ...item, kind: "video" })), ...(request._audio || []).map((item) => ({ ...item, kind: "audio" }))];
  $("#detailMedia").innerHTML = media.map((item, index) => `<button data-detail-media="${index}">${{ image: "图", video: "视", audio: "音" }[item.kind]}${index + 1}</button>`).join(""); $$("#auditTabs button").forEach((button) => button.classList.toggle("active", button.dataset.key === state.auditKey)); renderAudit(); $("#detailDialog").showModal(); icons();
}
function previewMedia(item, anchor) { if (!item) return; const popover = $("#mediaPopover"); const url = escapeHtml(item.url || item.value || ""); popover.innerHTML = item.kind === "video" ? `<video src="${url}" controls autoplay muted></video>` : item.kind === "audio" ? `<audio src="${url}" controls autoplay></audio>` : `<img src="${url}" alt="素材预览">`; const rect = anchor.getBoundingClientRect(); popover.style.left = `${Math.min(rect.left, innerWidth - 340)}px`; popover.style.top = `${Math.min(rect.bottom + 6, innerHeight - 260)}px`; popover.hidden = false; }

function renderCosts() { const rows = state.costReferences || []; $("#costReferenceBody").innerHTML = rows.map((item) => { const builtin = item.source === "builtin_rate"; return `<tr><td class="mono">${escapeHtml(item.model)}</td><td class="mono">${builtin ? "按整秒" : `${item.duration || 0}S`}</td><td>${escapeHtml(item.resolution || "-")}</td><td><strong class="cost-value mono">${builtin ? `⌊${fmtCost(item.rate_per_second)} × 秒数⌋` : fmtCost(item.cost)}</strong></td><td class="mono">${builtin ? "-" : fmtCost(item.latest_cost)}</td><td>${builtin ? "内置" : item.samples || 0}</td><td>${fmtTime(item.updated_at)}</td><td class="mono">${escapeHtml(String(item.last_task_id || "-").slice(0, 18))}</td></tr>`; }).join(""); $("#costReferenceEmpty").classList.toggle("show", !rows.length); $("#costReferenceStatus").textContent = `${rows.length} 条参考`; }

async function refresh() { $("#refreshStatus").textContent = "刷新中..."; try { const [accounts, tasks, models, settings] = await Promise.all([api("/api/accounts"), api("/api/tasks?limit=20"), api("/api/models"), api("/api/settings")]); Object.assign(state, { accounts, tasks, models, settings }); renderAccounts(); renderTasks(); $("#metricModels").textContent = models.length; $("#refreshStatus").textContent = `更新于 ${new Date().toLocaleTimeString()}`; } catch (error) { toast(error.message, true); $("#refreshStatus").textContent = "刷新失败"; } }
async function accountAction(button) { const id = Number(button.dataset.id); try { if (button.dataset.action === "delete") { if (!confirm("确认删除该账号及托管 Profile？")) return; await api(`/api/accounts/${id}`, { method: "DELETE" }); } else if (button.dataset.action === "toggle") await api(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: button.dataset.enabled !== "true" }) }); else if (button.dataset.action === "login") await api(`/api/accounts/${id}/cdp/reconnect`, { method: "POST" }); else if (button.dataset.action === "check") await api(`/api/accounts/${id}/check`, { method: "POST" }); else if (button.dataset.action === "reset-profile") { const account = state.accounts.find((item) => item.id === id); const form = $("#profileResetForm"); form.account_id.value = id; form.proxy_url.value = account.proxy_url || ""; $("#profileResetAccount").textContent = `#${id} · ${account.name}`; $("#profileResetStatus").textContent = account.status; $("#profileResetDialog").showModal(); return; } toast("操作已提交"); await refresh(); } catch (error) { toast(error.message, true); } }

document.addEventListener("click", async (event) => {
  const close = event.target.closest(".close"); if (close) close.closest("dialog")?.close();
  const accountButton = event.target.closest("[data-action='check'],[data-action='login'],[data-action='reset-profile'],[data-action='toggle'],[data-action='delete']"); if (accountButton) await accountAction(accountButton);
  const detail = event.target.closest("[data-action='detail']"); if (detail) try { showDetail(await api(`/api/tasks/${detail.dataset.id}`)); } catch (error) { toast(error.message, true); }
  const media = event.target.closest("[data-action='media']"); if (media) { const task = state.tasks.find((item) => item.id === media.dataset.id); previewMedia(mediaFromTask(task).filter((item) => !item.result)[Number(media.dataset.mediaIndex)], media); }
  const detailMedia = event.target.closest("[data-detail-media]"); if (detailMedia) { const request = state.detail.request || {}; const items = [...(request._images || []).map((item) => ({ ...item, kind: "image" })), ...(request._videos || []).map((item) => ({ ...item, kind: "video" })), ...(request._audio || []).map((item) => ({ ...item, kind: "audio" }))]; previewMedia(items[Number(detailMedia.dataset.detailMedia)], detailMedia); }
  if (!event.target.closest("#mediaPopover,[data-action='media'],[data-detail-media]")) $("#mediaPopover").hidden = true;
});
document.addEventListener("change", async (event) => {
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
$("#addAccountButton").addEventListener("click", () => { $("#accountForm").reset(); $("#accountForm").elements.namedItem("max_concurrency").value = 1; $("#accountDialog").showModal(); });
$("#batchButton").addEventListener("click", () => { $("#batchResult").hidden = true; $("#batchResult").textContent = ""; $("#batchDialog").showModal(); });
$("#newTaskButton").addEventListener("click", () => { renderModels(); validateJson(); $("#taskDialog").showModal(); }); $("#requestJson").addEventListener("input", validateJson);
$("#settingsButton").addEventListener("click", () => { const form = $("#settingsForm"); Object.entries(state.settings).forEach(([key, value]) => { const input = form.elements.namedItem(key); if (!input) return; if (input.type === "checkbox") input.checked = Boolean(value); else input.value = value ?? ""; }); $("#settingsDialog").showModal(); });
$("#integrationDocsButton").addEventListener("click", async () => { try { $("#integrationDocsContent").textContent = await apiText("/api/integration-docs"); $("#integrationDocsDialog").showModal(); } catch (error) { toast(error.message, true); } });
$("#copyIntegrationDocs").addEventListener("click", async () => { await navigator.clipboard.writeText($("#integrationDocsContent").textContent); toast("Markdown 已复制"); });
$("#costReferenceButton").addEventListener("click", async () => { $("#costReferenceDialog").showModal(); state.costReferences = await api("/api/model-costs?limit=500"); renderCosts(); }); $("#costReferenceRefresh").addEventListener("click", async () => { state.costReferences = await api("/api/model-costs?limit=500"); renderCosts(); });
$("#accountForm").addEventListener("submit", async (event) => { event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget)); try { data.max_concurrency = Number(data.max_concurrency || 1); data.cdp_port = Number(data.cdp_port || 0); await api("/api/accounts", { method: "POST", body: JSON.stringify(data) }); event.currentTarget.reset(); $("#accountDialog").close(); toast("Account saved. Open web login next."); await refresh(); } catch (error) { toast(error.message, true); } });
$("#batchForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; const panel = $("#batchResult"); try { const result = await api("/api/accounts/batch-import", { method: "POST", body: JSON.stringify({ text: form.elements.namedItem("text").value }) }); const errorText = (result.errors || []).join("\n"); panel.hidden = false; panel.classList.toggle("error", Boolean(errorText)); panel.textContent = `Input ${result.input_count} / Saved ${result.count}${errorText ? `\n${errorText}` : ""}`; await refresh(); if (!errorText) setTimeout(() => $("#batchDialog").close(), 700); } catch (error) { panel.hidden = false; panel.classList.add("error"); panel.textContent = error.message; toast(error.message, true); } });
$("#profileResetForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; try { await api(`/api/accounts/${form.account_id.value}/profile/reset`, { method: "POST", body: JSON.stringify({ proxy_url: form.proxy_url.value || null, start_login: form.start_login.checked }) }); $("#profileResetDialog").close(); toast("Profile 已重置"); await refresh(); } catch (error) { toast(error.message, true); } });
$("#settingsForm").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; const field = (name) => form.elements.namedItem(name); const payload = { task_workers: Number(field("task_workers").value), task_queue_capacity: Number(field("task_queue_capacity").value), poll_interval_seconds: Number(field("poll_interval_seconds").value), task_timeout_seconds: Number(field("task_timeout_seconds").value), daily_checkin_enabled: field("daily_checkin_enabled").checked, low_balance_disable_threshold: Number(field("low_balance_disable_threshold").value), chrome_executable: field("chrome_executable").value, chrome_user_data_root: field("chrome_user_data_root").value, allow_video_reference_inputs: field("allow_video_reference_inputs").checked, prompt_media_reference_cleanup_enabled: field("prompt_media_reference_cleanup_enabled").checked }; try { state.settings = await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload) }); $("#settingsDialog").close(); toast("Settings saved"); await refresh(); } catch (error) { toast(error.message, true); } });
$("#taskForm").addEventListener("submit", async (event) => { event.preventDefault(); if (!validateJson()) return; try { const task = await api("/api/tasks", { method: "POST", body: $("#requestJson").value }); $("#taskDialog").close(); toast(`任务已创建：${task.id}`); await refresh(); } catch (error) { toast(error.message, true); } });
$("#clearTasks").addEventListener("click", async () => { try { const result = await api("/api/tasks", { method: "DELETE" }); toast(`已清理 ${result.deleted} 个任务`); await refresh(); } catch (error) { toast(error.message, true); } });

icons(); refresh(); setInterval(refresh, 8000);
