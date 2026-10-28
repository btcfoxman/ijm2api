"""Bounded, account-isolated Canvas device authorization without paid calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import math
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import websocket

from .browser import BrowserError, _targets
from .account_automation import AccountAutomation
from .auto_login import verified_login_user_id


_PUBLIC_STATES = {'queued', 'authorizing', 'manual_required', 'ready', 'failed', 'cancelled', 'expired'}
_AUTH_HOSTS = {'dreamina.capcut.com'}
_CONFIRM = r"""(() => {
 const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';};
 const text=(document.body?.innerText||'').slice(0,20000);
 if(document.querySelector('iframe[src*="captcha"],iframe[src*="recaptcha"],[class*="captcha-verify"]')||
    /verify (?:you are|your identity)|security verification|人机验证|安全验证/i.test(text))return 'manual_required';
 if(!/canvas|\bcli\b|命令行/i.test(text))return 'waiting';
 if(/purchase|checkout|subscribe|支付|购买|订阅/i.test(text))return 'manual_required';
 const labels=new Set(['authorize','allow','allow access','confirm','confirm authorization','授权','允许','确认','确认授权','同意授权','授权登录']);
 const buttons=[...document.querySelectorAll('button,[role=button]')].filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&labels.has((e.innerText||e.textContent||'').trim().toLowerCase()));
 if(buttons.length===1){buttons[0].click();return 'confirmed';}
 return 'waiting';
})()"""


class DeviceAuthorizationBrowser:
    """Control only the account's existing tab; never fill a password or CAPTCHA."""
    def __init__(self, account: dict[str, Any]):
        self.port = int(account['cdp_port'])
        self.endpoint = ''
        self.hostname = ''

    def _command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ws = websocket.create_connection(self.endpoint, timeout=8, suppress_origin=True)
        try:
            ws.send(json.dumps({'id': 1, 'method': method, 'params': params}))
            while True:
                response = json.loads(ws.recv())
                if response.get('id') == 1:
                    if 'error' in response:
                        raise BrowserError('Canvas authorization browser operation failed')
                    return response.get('result') or {}
        finally:
            ws.close()

    def open(self, uri: str) -> None:
        parsed = urlsplit(uri)
        if parsed.scheme != 'https' or parsed.hostname not in _AUTH_HOSTS or parsed.username or parsed.password:
            raise BrowserError('Canvas authorization URL is not an official supported URL')
        self.hostname = parsed.hostname
        target = next((item for item in _targets(self.port) if item.get('type') == 'page'
            and urlsplit(str(item.get('url') or '')).hostname in _AUTH_HOSTS), None)
        if not target:
            raise BrowserError('Canvas authorization account tab is unavailable')
        self.endpoint = str(target['webSocketDebuggerUrl'])
        result = self._command('Page.navigate', {'url': uri})
        if result.get('errorText'):
            raise BrowserError('Canvas authorization page navigation failed')

    def step(self) -> str:
        result = self._command('Runtime.evaluate', {'expression':
            f"location.hostname==={json.dumps(self.hostname)} ? ({_CONFIRM}) : 'manual_required'",
            'returnByValue': True})
        if 'exceptionDetails' in result:
            return 'waiting'
        value = (result.get('result') or {}).get('value')
        return value if value in {'waiting', 'confirmed', 'manual_required'} else 'waiting'


class CanvasAuthManager:
    def __init__(self, store: Any, service: Any, *, client_factory: Callable | None = None,
                 browser_factory: Callable = DeviceAuthorizationBrowser, timeout: float = 180):
        self.store, self.service = store, service
        self.client_factory = client_factory or service.canvas_client
        self.browser_factory = browser_factory
        self.timeout = min(600, max(15, float(timeout)))
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ijm-canvas-auth')
        self._guard = threading.RLock()
        self._jobs: dict[int, dict[str, Any]] = {}
        self._closed = False
        self._browser_slots = getattr(service, '_browser_aux_slots', threading.BoundedSemaphore(2))

    def busy(self, account_id: int) -> bool:
        with self._guard:
            job = self._jobs.get(int(account_id))
            return bool(job and not job['done'].is_set())

    def status(self, account_id: int) -> dict[str, Any]:
        with self._guard:
            job = self._jobs.get(int(account_id))
            if job:
                return dict(job['public'])
        account = self.store.account(int(account_id), private=True)
        if not account:
            raise KeyError(account_id)
        state = account.get('cli_auth_status') or 'required'
        return {'account_id': int(account_id), 'state': state, 'status': state,
                'message': account.get('cli_auth_error') or ''}

    def _publish(self, account_id: int, state: str, message: str = '', **fields: Any) -> None:
        if state not in _PUBLIC_STATES:
            raise ValueError('invalid Canvas authorization state')
        with self._guard:
            job = self._jobs[account_id]
            job['public'].update(state=state, status=state, message=message, **fields)
            if state in {'ready', 'failed', 'cancelled', 'expired'}:
                for key in ('verification_uri', 'verification_uri_complete', 'user_code'):
                    job['public'].pop(key, None)
        self.store.update_account(account_id, {'cli_auth_status': state, 'cli_auth_error': message})

    def start(self, account_id: int, *, force: bool = False) -> dict[str, Any]:
        account_id = int(account_id)
        # The service lock also excludes paid task/account identity changes.
        with self.service._lock, self._guard:
            if self._closed:
                raise ValueError('Canvas authorization queue is shutting down')
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            if self.busy(account_id):
                return dict(self._jobs[account_id]['public'])
            if (getattr(self.service, 'auto_login_busy', lambda _: False)(account_id)
                    or account['status'] == 'logging_in'):
                raise ValueError('account is in browser login')
            if self.service._active.get(account_id, 0) or self.store.active_account_count(account_id):
                raise ValueError('account has active tasks')
            if not account.get('proxy_url'):
                raise ValueError('Canvas authorization requires the account proxy')
            self.service.browser_runtime.assert_idle(account_id)
            if sum(not item['done'].is_set() for item in self._jobs.values()) >= 200:
                raise ValueError('Canvas authorization queue is full')
            enable_after = account.get('login_enable_after')
            if enable_after is None:
                enable_after = bool(account['enabled'])
            job = {'cancel': threading.Event(), 'done': threading.Event(), 'enable_after': bool(enable_after),
                   'assist': False, 'assist_slot': False,
                   'force': force is True,
                   'public': {'account_id': account_id, 'state': 'queued', 'status': 'queued', 'message': ''}}
            self._jobs[account_id] = job
            self.store.update_account(account_id, {'enabled': False, 'status': 'cli_auth_required',
                'cli_auth_status': 'queued', 'cli_auth_error': '', 'login_enable_after': bool(enable_after)})
            try:
                self._pool.submit(self._run, account_id)
            except RuntimeError:
                job['done'].set()
                self._publish(account_id, 'failed', 'CLI 授权未启动，请重试')
                raise ValueError('Canvas authorization queue is shutting down') from None
            return dict(job['public'])

    def cancel(self, account_id: int) -> dict[str, Any]:
        with self._guard:
            job = self._jobs.get(int(account_id))
            if job and not job['done'].is_set():
                job['cancel'].set()
        # The worker releases only its local resources. No upstream logout.
        return self.status(account_id)

    def open_assist(self, account_id: int) -> dict[str, Any]:
        """Expose the same proxied account tab through the existing admin viewer."""
        account_id = int(account_id)
        with self._guard:
            job = self._jobs.get(account_id)
            if (not job or job['done'].is_set() or job['cancel'].is_set()
                    or job['public']['state'] not in {'authorizing', 'manual_required'}):
                raise ValueError('start a fresh Canvas authorization before opening the account browser')
            uri = job['public'].get('verification_uri_complete') or job['public'].get('verification_uri')
            if not uri:
                raise ValueError('Canvas authorization browser is not ready')
            if job.get('assist'):
                try:
                    self.service.browser_view(account_id)
                    return dict(job['public'])
                except (BrowserError, KeyError):
                    job['assist'] = False
                    if job.get('assist_slot'):
                        self._browser_slots.release()
                        job['assist_slot'] = False
            shares_worker_browser = bool(job.get('browser_active'))
            if not shares_worker_browser and not self._browser_slots.acquire(blocking=False):
                raise ValueError('other account browser operations are busy; retry shortly')
            try:
                account = self.service.open_browser_view(account_id)
                # Reloading after a dispatched Authorize click can abort the
                # consent HTTP request. The active worker already opened it.
                if not shares_worker_browser:
                    self.browser_factory(account).open(str(uri))
                job['assist'] = True
                job['assist_slot'] = not shares_worker_browser
            except Exception:
                self.service.close_browser_view(account_id)
                if not shares_worker_browser:
                    self._browser_slots.release()
                raise ValueError('Canvas authorization browser could not open; check the account proxy') from None
            return dict(job['public'])

    def _close_assist(self, account_id: int) -> None:
        with self._guard:
            job = self._jobs[account_id]
            if job.get('assist'):
                try:
                    self.service.close_browser_view(account_id)
                except Exception:
                    pass
                finally:
                    job['assist'] = False
            if job.get('assist_slot'):
                self._browser_slots.release()
                job['assist_slot'] = False

    def _browser_inactive(self, account_id: int) -> None:
        with self._guard:
            self._jobs[account_id]['browser_active'] = False

    @staticmethod
    def _identity_id(identity: Any) -> str:
        value = identity.get('id') if isinstance(identity, dict) else None
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = str(value)
            if value and value != '0' and len(value) <= 256 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
                return value
        return ''

    def _accept_identity(self, account_id: int, account: dict, identity: dict) -> bool:
        if self._jobs[account_id]['cancel'].is_set() or self._closed:
            self._publish(account_id, 'cancelled', 'CLI 授权已取消')
            return False
        user_id = self._identity_id(identity)
        if not user_id or user_id != str(account['login_user_id']):
            self._publish(account_id, 'failed', 'CLI 授权身份与网页登录账号不一致，请重新授权正确账号')
            return False
        with self.service._lock, self._guard:
            if self._jobs[account_id]['cancel'].is_set() or self._closed:
                self._publish(account_id, 'cancelled', 'CLI 授权已取消')
                return False
            current = self.store.account(account_id, private=True)
            if (not current or current.get('proxy_url') != account.get('proxy_url')
                    or current.get('login_user_id') != account.get('login_user_id')
                    or current.get('execution_mode') != account.get('execution_mode')):
                self._publish(account_id, 'failed', '账号身份或代理已变更，请重新授权')
                return False
            enable_after = self._jobs[account_id]['enable_after']
            balance = identity.get('credits')
            threshold = float(self.service.settings().get('low_balance_disable_threshold') or 0)
            numeric = (isinstance(balance, (int, float)) and not isinstance(balance, bool)
                       and math.isfinite(balance) and balance >= 0)
            low = numeric and threshold > 0 and balance < threshold
            values = {'cli_user_id': user_id, 'cli_auth_status': 'ready', 'cli_auth_error': '',
                'enabled': enable_after and not low,
                'status': 'disabled_low_balance' if low else 'active' if enable_after else 'disabled'}
            if numeric:
                values['balance'] = balance
            self.store.update_account(account_id, values)
            self._publish(account_id, 'ready')
        return True

    @staticmethod
    def _expiry(value: Any) -> float | None:
        try:
            if isinstance(value, (int, float)):
                return float(value) / 1000 if value > 10**11 else float(value)
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, TypeError, OverflowError):
            pass
        return None

    def _prepare_browser_account(self, account_id: int, account: dict) -> dict | None:
        """Migrate legacy profiles using real login/settings evidence, never Generate."""
        existing_id = verified_login_user_id({'identity_verified': True,
            'identity': {'user_id': account.get('login_user_id')}})
        if existing_id and bool(account.get('watermark_ready')) and bool(account.get('agreement_ready')):
            return account
        job = self._jobs[account_id]
        cancel = job['cancel']
        self._publish(account_id, 'authorizing', '正在核验已有网页登录与生成设置')
        owns_slot = False
        deadline = time.monotonic() + self.timeout
        try:
            while not cancel.is_set() and not self._closed and time.monotonic() < deadline:
                if self._browser_slots.acquire(timeout=.25):
                    owns_slot = True
                    break
            if not owns_slot:
                self._publish(account_id, 'cancelled' if cancel.is_set() or self._closed else 'failed',
                    'CLI 授权已取消' if cancel.is_set() or self._closed else '账号浏览器暂忙，请稍后重试授权')
                return None
            with self.service.browser_runtime.lease(account) as browser_account:
                evidence = AccountAutomation(browser_account, self.service.settings(),
                    allow_initial_identity=True).ensure_ready()
                user_id = verified_login_user_id(evidence) if isinstance(evidence, dict) else ''
                if (not user_id or evidence.get('identity_verified') is not True
                        or evidence.get('watermark_ready') is not True
                        or evidence.get('agreement_ready') is not True):
                    self._publish(account_id, 'failed', '网页登录身份或生成设置尚未核实，请完成网页登录后重试授权')
                    return None
                with self.service._lock, self._guard:
                    if cancel.is_set() or self._closed:
                        self._publish(account_id, 'cancelled', 'CLI 授权已取消')
                        return None
                    current = self.store.account(account_id, private=True)
                    if (not current or current.get('proxy_url') != account.get('proxy_url')
                            or current.get('login_user_id') != account.get('login_user_id')
                            or current.get('execution_mode') != account.get('execution_mode')):
                        self._publish(account_id, 'failed', '账号身份或代理已变更，请重新授权')
                        return None
                    self.store.update_account(account_id, {'login_user_id': user_id,
                        'watermark_ready': True, 'agreement_ready': True,
                        'setup_checked_at': time.time(), 'login_stage': 'ready'})
            return self.store.account(account_id, private=True)
        except Exception as exc:
            status = str(getattr(exc, 'status', ''))
            message = ('网页登录需要人工安全验证，请完成网页登录后重试授权'
                if status in {'challenge_required', 'verification_required'}
                else '网页登录身份或生成设置核验失败，请完成网页登录后重试授权')
            self._publish(account_id, 'failed', message)
            return None
        finally:
            if owns_slot:
                self._browser_slots.release()

    def _await_authorization(self, account_id: int, account: dict, client: Any,
                             device_code: str, deadline: float, browser: Any = None) -> None:
        """A click is only dispatched; CLI completion proves consent was saved."""
        cancel = self._jobs[account_id]['cancel']
        clicked = False
        manual = browser is None
        while not cancel.is_set() and not self._closed and time.monotonic() < deadline:
            if browser is not None and not clicked and not manual:
                try:
                    state = browser.step()
                    if state == 'manual_required':
                        manual = True
                        self._publish(account_id, 'manual_required', '授权需要人工确认，请打开账号授权画面完成')
                    elif state == 'confirmed':
                        clicked = True
                except Exception:
                    manual = True
                    self._publish(account_id, 'manual_required', '自动授权未完成，请打开账号授权画面确认')
            try:
                result = client.auth_wait(device_code, timeout=5)
                if isinstance(result, dict) and str(result.get('status') or result.get('state') or '').lower() in {
                        'pending', 'authorization_pending', 'waiting', 'timeout'}:
                    cancel.wait(.5)
                    continue
                self._accept_identity(account_id, account, client.identity())
                return
            except Exception as exc:
                code = str(getattr(exc, 'code', '')).upper()
                if any(part in code for part in ('PENDING', 'TIMEOUT', 'SLOW_DOWN', 'WAITING')):
                    cancel.wait(1)
                    continue
                self._publish(account_id, 'failed', 'CLI 授权验证失败，请重新授权')
                return
        self._publish(account_id, 'cancelled' if cancel.is_set() or self._closed else 'expired',
            'CLI 授权已取消' if cancel.is_set() or self._closed else 'CLI 授权已过期，请重试')

    def _run(self, account_id: int) -> None:
        job = self._jobs[account_id]
        cancel = job['cancel']
        try:
            if cancel.is_set() or self._closed:
                self._publish(account_id, 'cancelled', 'CLI 授权已取消')
                return
            account = self.store.account(account_id, private=True)
            account = self._prepare_browser_account(account_id, account)
            if account is None:
                return
            client = self.client_factory(account)
            self._publish(account_id, 'authorizing')
            identity = None
            if not job['force']:
                try:
                    identity = client.identity()
                except Exception as exc:
                    if 'MISMATCH' in str(getattr(exc, 'code', '')).upper():
                        self._publish(account_id, 'failed', 'CLI 授权身份与网页登录账号不一致，请重新授权正确账号')
                        return
            if identity is not None:
                self._accept_identity(account_id, account, identity)
                return
            response = client.auth_start(force=True) if job['force'] else client.auth_start()
            challenge = response.get('challenge') if isinstance(response, dict) else None
            if not isinstance(challenge, dict) or not challenge.get('deviceCode'):
                # Some CLI versions finish immediately when the profile is valid.
                self._accept_identity(account_id, account, client.identity())
                return
            uri = challenge.get('verificationUriComplete') or challenge.get('verificationUri') or ''
            parsed = urlsplit(str(uri))
            if parsed.scheme != 'https' or parsed.hostname not in _AUTH_HOSTS or parsed.username or parsed.password:
                self._publish(account_id, 'failed', 'CLI 授权地址不受支持，请检查官方 CLI 版本')
                return
            self._publish(account_id, 'authorizing', verification_uri=str(challenge.get('verificationUri') or uri),
                verification_uri_complete=str(uri), user_code=str(challenge.get('userCode') or ''))
            deadline = time.monotonic() + self.timeout
            expires = self._expiry(challenge.get('expiresAt'))
            if expires is not None:
                deadline = min(deadline, time.monotonic() + max(0, expires - time.time()))
            slot = False
            while not cancel.is_set() and not self._closed and time.monotonic() < deadline:
                if self._browser_slots.acquire(timeout=.25):
                    slot = True
                    break
            with ExitStack() as resources:
                browser = None
                if slot:
                    resources.callback(self._browser_slots.release)
                    try:
                        browser_account = resources.enter_context(self.service.browser_runtime.lease(account))
                        browser = self.browser_factory(browser_account)
                        browser.open(str(uri))
                        with self._guard:
                            job['browser_active'] = True
                        resources.callback(self._browser_inactive, account_id)
                        # Close a viewer lease first, the worker browser second,
                        # and only then release its auxiliary concurrency slot.
                        resources.callback(self._close_assist, account_id)
                    except Exception:
                        browser = None
                        resources.close()
                        self._publish(account_id, 'manual_required', '自动授权未完成，请打开账号授权画面确认')
                self._await_authorization(account_id, account, client,
                    str(challenge['deviceCode']), deadline, browser=browser)
        except Exception:
            self._publish(account_id, 'failed', 'CLI 授权失败，请检查账号代理后重试')
        finally:
            self._close_assist(account_id)
            self._browser_inactive(account_id)
            job['done'].set()

    def recover_interrupted(self) -> None:
        for account in self.store.accounts(private=True):
            if account.get('cli_auth_status') in {'queued', 'authorizing', 'manual_required'}:
                self.store.update_account(account['id'], {'enabled': False, 'status': 'cli_auth_required',
                    'cli_auth_status': 'required', 'cli_auth_error': 'CLI 授权被服务重启中断，请重试'})

    def close(self) -> None:
        with self._guard:
            self._closed = True
            for job in self._jobs.values():
                job['cancel'].set()
        self._pool.shutdown(wait=False, cancel_futures=True)
