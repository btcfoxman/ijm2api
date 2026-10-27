(() => {
  const dialog = document.getElementById('onlineAccountDialog');
  const form = document.getElementById('onlineAccountForm');
  const frame = document.getElementById('onlineAccountFrame');
  const message = document.getElementById('onlineAccountMessage');
  const fields = document.getElementById('onlineAccountFields');
  const finish = document.getElementById('onlineAccountFinish');
  let sessionId = '';
  let busy = false;
  let viewerReload = 0;

  async function request(path, method = 'GET', body) {
    const response = await fetch(path, {method, credentials:'same-origin',
      headers: {'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined});
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail || `HTTP ${response.status}`);
    return value;
  }
  async function cancel() {
    const id = sessionId;
    sessionId = '';
    frame.src = 'about:blank';
    if (id) await request(`/api/admin/account-login/${encodeURIComponent(id)}/cancel`, 'POST').catch(() => {});
  }
  async function reconnectViewer(id) {
    const ticket = await request(`/api/admin/account-login/${encodeURIComponent(id)}/viewer`, 'POST');
    if (sessionId !== id) return;
    const separator = ticket.url.includes('?') ? '&' : '?';
    frame.src = `${ticket.url}${separator}viewer_reload=${++viewerReload}`;
  }
  async function show(session) {
    sessionId = session.active.session_id;
    fields.hidden = true;
    finish.hidden = false;
    message.textContent = '请在远端浏览器完成登录并打开 Dreamina 生成页，再点击“完成登录并验证”。';
    await reconnectViewer(sessionId);
  }
  async function open(accountId) {
    if (busy) return;
    busy = true;
    try {
      dialog.showModal();
      fields.hidden = Boolean(accountId);
      finish.hidden = true;
      message.textContent = accountId ? '正在打开账号浏览器…' : '新账号将使用独立的持久 Chrome Profile。';
      if (accountId) await show(await request(`/api/admin/accounts/${accountId}/login/start`, 'POST'));
    } catch (error) {
      message.textContent = `打开失败：${error.message}`;
    } finally { busy = false; }
  }
  document.getElementById('onlineAddAccountButton').addEventListener('click', () => open(0));
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action="web-login"]');
    if (button) open(Number(button.dataset.id));
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    message.textContent = '正在建立浏览器 Profile…';
    try {
      const data = Object.fromEntries(new FormData(form));
      data.max_concurrency = Number(data.max_concurrency || 1);
      const account = await request('/api/admin/accounts/pending', 'POST', data);
      await show(await request(`/api/admin/accounts/${account.id}/login/start`, 'POST'));
      if (window.refresh) await window.refresh();
    } catch (error) {
      message.textContent = `创建或启动失败：${error.message}`;
    } finally { busy = false; }
  });
  finish.addEventListener('click', async () => {
    if (!sessionId || busy) return;
    busy = true;
    finish.disabled = true;
    message.textContent = '正在验证会话及积分…';
    try {
      const account = await request(`/api/admin/account-login/${encodeURIComponent(sessionId)}/finish`, 'POST');
      sessionId = '';
      frame.src = 'about:blank';
      dialog.close();
      if (window.refresh) await window.refresh();
      if (window.toast) window.toast(`账号 #${account.id} 已验证，余额 ${account.balance ?? '—'}`);
    } catch (error) {
      const failedSession = sessionId;
      if (!failedSession) return;
      let recovered = false;
      try {
        await reconnectViewer(failedSession);
        recovered = true;
      } catch (_) { /* Keep the original verification error visible. */ }
      if (sessionId === failedSession) {
        const nextStep = recovered ? '远端画面已恢复，请检查 Dreamina 生成页后重试。' : '远端画面恢复失败，请重新打开登录窗口。';
        message.textContent = `验证未完成：${error.message}。${nextStep}`;
      }
    } finally { finish.disabled = false; busy = false; }
  });
  document.getElementById('onlineAccountCancel').addEventListener('click', async () => {
    await cancel();
    dialog.close();
    if (window.refresh) await window.refresh();
  });
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); cancel().finally(() => dialog.close()); });
})();
