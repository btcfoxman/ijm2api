"""Bounded account login jobs, separate from paid video execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path
from typing import Callable

from .account_automation import AccountAutomation
from .browser import browser_alive, start_managed_browser
from .client import DreaminaClient


AUTO_STAGES = {'queued', 'opening_browser', 'login', 'email', 'password', 'settings', 'verifying'}
SAFE_FAILURES = {
    'challenge_required': '登录需要人机验证，请使用网页登录完成后重试',
    'verification_required': '登录需要邮箱或安全验证，请使用网页登录完成后重试',
    'login_failed': '自动登录未完成，请检查账号或使用网页登录',
    'setup_required': '账号设置尚未确认，请使用网页登录检查去水印和 AI 协议',
    'login_required': '登录会话尚未验证，请重试或使用网页登录',
}


def verified_login_user_id(ready: dict) -> str:
    """Return a usable SDK identity only from an explicitly verified result."""
    identity = ready.get('identity')
    value = identity.get('user_id') if isinstance(identity, dict) else None
    if (ready.get('identity_verified') is not True or not isinstance(value, str)
            or not value or value == '0' or len(value) > 256
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)):
        return ''
    return value


class AutoLoginManager:
    def __init__(self, store, service, *, manual_active: Callable[[], dict | None] | None = None):
        self.store, self.service = store, service
        self.manual_active = manual_active or (lambda: None)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ijm-login')
        self._pending: set[int] = set()
        self._closed = False

    def busy(self, account_id: int) -> bool:
        with self.service._lock:
            return account_id in self._pending

    def recover_interrupted(self):
        for account in self.store.accounts(private=True):
            if account['status'] == 'logging_in' and account.get('login_stage') in AUTO_STAGES:
                self.store.update_account(account['id'], {'status': 'login_required', 'enabled': False,
                    'login_stage': 'interrupted', 'last_error': '自动登录被服务重启中断，请重试'})

    def start(self, account_id: int):
        with self.service._lock:
            if self._closed:
                raise ValueError('account login queue is shutting down')
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            if account_id in self._pending:
                return self.store.account(account_id)
            active = self.manual_active()
            if account['status'] in {'logging_in', 'profile_resetting'} or active and active['account_id'] == account_id:
                raise ValueError('account is already in browser login')
            if len(self._pending) >= 200:
                raise ValueError('account login queue is full')
            if not account.get('login_email') or not account.get('login_password'):
                raise ValueError('account has no stored email/password for automatic login')
            if not account.get('managed_profile'):
                raise ValueError('automatic login requires a service-managed account profile')
            if self.service._active.get(account_id, 0) or self.store.active_account_count(account_id):
                raise ValueError('account has active tasks')
            self.service.browser_runtime.assert_idle(account_id)
            previous_intent = account.get('login_enable_after')
            enable_after = bool(previous_intent) if previous_intent is not None else bool(account['enabled'] or account['status'] in {
                'pending', 'login_required', 'login_failed', 'verification_required',
                'challenge_required', 'setup_required'})
            self.store.update_account(account_id, {'status': 'logging_in', 'enabled': False,
                'login_stage': 'queued', 'last_error': '', 'watermark_ready': False,
                'agreement_ready': False, 'setup_checked_at': None, 'login_enable_after': enable_after})
            self._pending.add(account_id)
            try:
                self._pool.submit(self._run, account_id, enable_after)
            except RuntimeError:
                self._pending.discard(account_id)
                self.store.update_account(account_id, {'status': account['status'], 'enabled': account['enabled'],
                    'login_stage': account.get('login_stage', ''), 'watermark_ready': account.get('watermark_ready', False),
                    'agreement_ready': account.get('agreement_ready', False), 'setup_checked_at': account.get('setup_checked_at')})
                raise ValueError('account login queue is shutting down') from None
            return self.store.account(account_id)

    def _run(self, account_id: int, enable_after: bool):
        with self.service._lock:
            if self._closed:
                self._pending.discard(account_id)
                return
        def stage(value):
            if value in AUTO_STAGES:
                self.store.update_account(account_id, {'login_stage': value})

        try:
            stage('opening_browser')
            account = self.store.account(account_id, private=True)
            settings = self.service.settings()
            port = int(account.get('cdp_port') or 0)
            if not port or not browser_alive(port):
                port = start_managed_browser(account, chrome=str(settings['chrome_executable']),
                    profiles=Path(str(settings['chrome_user_data_root'])))
                self.store.update_account(account_id, {'cdp_port': port, 'managed_profile': True})
            account = {**account, 'cdp_port': port}
            stage('login')
            ready = AccountAutomation(account, settings).run(account['login_email'], account['login_password'], on_stage=stage)
            if any(ready.get(key) is not True for key in ('watermark_ready', 'agreement_ready', 'identity_verified')):
                self.store.update_account(account_id, {'status': 'setup_required', 'login_stage': 'setup_required',
                    'last_error': SAFE_FAILURES['setup_required'], 'enabled': False})
                return
            login_user_id = verified_login_user_id(ready)
            if not login_user_id:
                self.store.update_account(account_id, {'status': 'login_required', 'login_stage': 'login_required',
                    'last_error': SAFE_FAILURES['login_required'], 'enabled': False})
                return
            stage('verifying')
            state = DreaminaClient(account).account_state(max_attempts=2, timeout_seconds=10)
            balance = state['available_balance']
            threshold = float(settings.get('low_balance_disable_threshold') or 0)
            low = threshold > 0 and balance < threshold
            self.store.update_account(account_id, {'status': 'disabled_low_balance' if low else 'active' if enable_after else 'disabled',
                'enabled': enable_after and not low, 'login_stage': 'ready', 'watermark_ready': True,
                'agreement_ready': True, 'setup_checked_at': time.time(), 'balance': balance,
                'balance_details': state['buckets'], 'login_user_id': login_user_id,
                'last_error': '账号额度低于启用阈值' if low else ''})
        except Exception as error:
            status = getattr(error, 'status', '')
            if status not in SAFE_FAILURES:
                status = 'login_failed'
            self.store.update_account(account_id, {'status': status, 'enabled': False,
                'login_stage': status, 'last_error': SAFE_FAILURES[status]})
        finally:
            with self.service._lock:
                self._pending.discard(account_id)

    def close(self):
        with self.service._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
