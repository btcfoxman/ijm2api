let browserAssistSession = null;
const assistDialog = $("#browserAssistDialog");
const assistImage = $("#browserAssistImage");

function assistBusy(session, busy) {
  if (browserAssistSession !== session) return;
  session.busy = busy;
  ["Login", "Enter", "Tab", "Up", "Down", "Refresh", "Complete"].forEach(name => { $(`#browserAssist${name}`).disabled = busy; });
  $("#browserAssistViewport").classList.toggle("busy", busy);
}

function assistFrame(session, frame) {
  if (browserAssistSession !== session) return;
  session.frame = frame;
  assistImage.src = frame.image;
  assistImage.hidden = false;
  $("#browserAssistPlaceholder").hidden = true;
  $("#browserAssistStatus").textContent = session.cliAuth
    ? `已连接账号代理 · ${frame.width} × ${frame.height} · 请在此页面确认 CLI 授权，完成后自动保存`
    : `已连接 · ${frame.width} × ${frame.height} · 登录并进入视频项目后保存会话`;
}

async function refreshAssistFrame(session) {
  if (browserAssistSession !== session || session.busy || session.refreshing) return;
  session.refreshing = true;
  try { assistFrame(session, await api(`/api/accounts/${session.id}/browser`)); }
  catch (error) { if (browserAssistSession === session) $("#browserAssistStatus").textContent = error.message; }
  finally { session.refreshing = false; }
}

async function openBrowserAssist(id, { cliAuth = false } = {}) {
  if (browserAssistSession) return;
  const session = { id, cliAuth, busy: false, refreshing: false, frame: null };
  browserAssistSession = session;
  $("#browserAssistComplete").hidden = cliAuth;
  $(".browser-assist-toolbar > p").textContent = cliAuth
    ? "在当前账号的代理浏览器中确认授权；无需重新登录或点击保存登录会话。"
    : "点击画面后输入文本或按键，登录并打开一个视频项目，然后保存会话。";
  $(".browser-assist-footer > span").textContent = cliAuth
    ? "授权在后台继续，完成或到期后自动关闭浏览器"
    : "关闭窗口后释放浏览器，登录进度保存在账号 Profile";
  $("#browserAssistAccount").textContent = `#${id}`;
  $("#browserAssistStatus").textContent = "正在连接账号浏览器…";
  $("#browserAssistPlaceholder").hidden = false;
  assistImage.hidden = true;
  assistDialog.showModal();
  assistBusy(session, true);
  try {
    const endpoint = cliAuth ? `/api/accounts/${id}/cli-auth/browser` : `/api/accounts/${id}/browser/open`;
    assistFrame(session, await api(endpoint, { method: "POST" }));
    if (browserAssistSession === session) session.timer = setInterval(() => refreshAssistFrame(session), 2000);
  } catch (error) { if (browserAssistSession === session) { toast(error.message, true); assistDialog.close(); } }
  finally { assistBusy(session, false); }
}

async function assistAction(action) {
  const session = browserAssistSession;
  if (!session || session.busy || !session.frame) return;
  assistBusy(session, true);
  try {
    const result = await api(`/api/accounts/${session.id}/browser/action`, { method: "POST", body: JSON.stringify(action) });
    if (result.action_result?.phase === "waiting-for-form") toast("页面仍在加载，请稍后再点击「填写并登录」");
  } catch (error) { toast(error.message, true); }
  finally { assistBusy(session, false); setTimeout(() => refreshAssistFrame(session), 500); }
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-action='browser-assist']");
  if (button) openBrowserAssist(Number(button.dataset.id));
});
assistImage.addEventListener("click", event => {
  const rect = assistImage.getBoundingClientRect();
  if (rect.width && rect.height) assistAction({ action: "click", x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height });
});
$("#browserAssistLogin").addEventListener("click", () => {
  const field = $("#browserAssistText");
  if (field.value) assistAction({ action: "type", text: field.value });
  field.value = "";
});
$("#browserAssistEnter").addEventListener("click", () => assistAction({ action: "key", key: "Enter" }));
$("#browserAssistTab").addEventListener("click", () => assistAction({ action: "key", key: "Tab" }));
$("#browserAssistUp").addEventListener("click", () => assistAction({ action: "scroll", delta_y: -500 }));
$("#browserAssistDown").addEventListener("click", () => assistAction({ action: "scroll", delta_y: 500 }));
$("#browserAssistRefresh").addEventListener("click", () => { if (browserAssistSession) refreshAssistFrame(browserAssistSession); });
$("#browserAssistComplete").addEventListener("click", async () => {
  const session = browserAssistSession;
  if (!session || session.busy || session.cliAuth) return;
  assistBusy(session, true);
  try {
    const account = await api(`/api/accounts/${session.id}/browser/complete`, { method: "POST" });
    if (browserAssistSession === session) assistDialog.close();
    toast(account.enabled ? `账号 #${session.id} 登录会话已保存` : `账号 #${session.id} 会话已保存，仍保持禁用`);
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { assistBusy(session, false); }
});

function closeBrowserAssist() {
  const session = browserAssistSession;
  browserAssistSession = null;
  if (!session) return;
  clearInterval(session.timer);
  assistImage.removeAttribute("src");
  assistImage.hidden = true;
  fetch(`/api/accounts/${session.id}/browser/close`, { method: "POST", keepalive: true }).catch(() => {});
}
function closeCanvasAuthAssist(id) {
  if (browserAssistSession?.cliAuth && browserAssistSession.id === id) assistDialog.close();
}
assistDialog.addEventListener("close", closeBrowserAssist);
window.addEventListener("pagehide", closeBrowserAssist);
