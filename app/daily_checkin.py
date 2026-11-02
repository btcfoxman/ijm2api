"""Independent, bounded daily benefit maintenance; no generation operations."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from .browser import BrowserError, BrowserRejected

logger = logging.getLogger(__name__)
MAX_CLAIM_ATTEMPTS = 3
BURST_COOLDOWN = 3600
MAX_QUOTA_CHECKS = 3
QUOTA_INTERVAL = 3600


class AccountMaintenanceBusy(BrowserError):
    """Temporary occupancy defers benefits without changing account health."""


def claim_timezone():
    value = os.getenv('IJM_TIME_ZONE', 'Asia/Taipei')
    ZoneInfo(value)
    return value


def claim_day():
    return datetime.now(ZoneInfo(claim_timezone())).date().isoformat()


def identity_binding(account):
    keys = ('id', 'execution_mode', 'login_user_id', 'login_email', 'proxy_url',
            'managed_profile', 'setup_checked_at')
    values = {key: account.get(key) for key in keys}
    values['managed_profile'] = bool(account.get('managed_profile'))
    port = int(account.get('cdp_port') or 0)
    managed = values['managed_profile'] or port in {0, 19300 + int(account.get('id') or 0)}
    values['external_cdp_port'] = None if managed else port
    return hashlib.sha256(json.dumps(values,
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def read_state(account, day=None):
    day = day or claim_day()
    raw = account.get('checkin_state_json', '{}')
    try:
        state = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        state = {}
    binding = identity_binding(account)
    if not isinstance(state, dict) or state.get('binding') != binding or state.get('day') != day:
        state = {'day': day, 'binding': binding, 'state': 'pending', 'attempts': 0, 'quota_checks': 0}
        if account.get('last_checkin_date') == day:
            state['state'] = 'confirmed'  # Preserve existing same-day receipts during migration.
    return dict(state)


def public_state(account):
    state = read_state(account)
    return {**{key: state[key] for key in ('day', 'state', 'received', 'confirmed_at',
        'quota_status', 'quota_checked_at', 'next_retry_at', 'last_error_type') if key in state},
        'confirmed_today': account.get('last_checkin_date') == state['day']
            and state.get('state') == 'confirmed'}


def decode_details(account):
    value = account.get('balance_details')
    if not isinstance(value, dict):
        try:
            value = json.loads(account.get('balance_details_json') or '{}')
        except (TypeError, ValueError):
            value = {}
    return dict(value) if isinstance(value, dict) else {}


def credit_read_is_stale(account, started):
    meta = decode_details(account).get('_meta', {})
    state = read_state(account)
    values = (meta.get('observed_at'), meta.get('missing_at'), state.get('confirmed_at'), state.get('last_attempt_at'))
    return any(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value > started for value in values)


def cli_credit_details(account, buckets, *, now=None):
    """An absent breakdown never invalidates the independently read aggregate."""
    now = time.time() if now is None else now
    rows = buckets.get('details') if isinstance(buckets, dict) else None
    if isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows):
        return {'details': rows, '_meta': {'source': 'canvas_cli', 'observed_at': now, 'stale': False}}
    previous = decode_details(account)
    meta = dict(previous.get('_meta') or {})
    meta.update(stale=True, latest_source='canvas_cli', missing_at=now)
    previous['_meta'] = meta
    return previous


def website_credit_values(state):
    """Reject empty/malformed web buckets rather than manufacturing a zero."""
    if not isinstance(state, dict) or not isinstance(state.get('buckets'), dict):
        raise BrowserError('Daily credit refresh did not provide verified quota buckets')
    buckets = state['buckets']
    known = {}
    for key in ('vip_credit', 'gift_credit', 'purchase_credit'):
        value = buckets.get(key)
        if isinstance(value, bool):
            raise BrowserError('Daily credit refresh returned invalid quota buckets')
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise BrowserError('Daily credit refresh returned incomplete quota buckets') from None
        if not math.isfinite(number) or number < 0:
            raise BrowserError('Daily credit refresh returned invalid quota buckets')
        known[key] = number
    balance = sum(known.values())
    provided = state.get('available_balance')
    if (isinstance(provided, bool) or not isinstance(provided, (int, float))
            or not math.isfinite(provided) or provided < 0 or abs(provided-balance) > 1e-7):
        raise BrowserError('Daily credit refresh returned inconsistent quota totals')
    known['_meta'] = {'source': 'dreamina_web', 'observed_at': time.time(), 'stale': False}
    return {'balance': balance, 'balance_details': known}


def receipt_quota(result):
    if not isinstance(result, dict) or 'receive_quota' not in result or isinstance(result['receive_quota'], bool):
        raise BrowserError('Daily credit claim returned an unverified receipt')
    try:
        value = float(result['receive_quota'])
    except (TypeError, ValueError):
        raise BrowserError('Daily credit claim returned an unverified receipt') from None
    if not math.isfinite(value) or value < 0:
        raise BrowserError('Daily credit claim returned an invalid receipt')
    return value


def automatic_eligible(account, settings):
    return bool(settings.get('daily_checkin_enabled') and account.get('enabled')
        and account.get('execution_mode', 'canvas_cli') != 'jimeng_web'
        and account.get('login_user_id')
        and account.get('status') not in {'logging_in', 'profile_resetting', 'login_required',
            'login_failed', 'challenge_required', 'verification_required', 'suspended'})


class DailyClaimScheduler:
    """At most two idle accounts acquire exclusive auxiliary leases at a time."""
    def __init__(self, service):
        self.service = service
        self._wake = threading.Event()
        self._guard = threading.Lock()
        self._inflight = set()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ijm-daily')
        self._thread = None
        self.failures = {}

    def start(self):
        self._thread = threading.Thread(target=self._loop, name='ijm-daily-dispatch', daemon=True)
        self._thread.start()

    def wake(self):
        self._wake.set()

    def reserved(self, identifier):
        with self._guard:
            return identifier in self._inflight

    def close(self):
        self.wake()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self):
        while not self.service._stop.is_set():
            try:
                self.dispatch()
            except Exception as exc:
                logger.warning('Daily credit dispatcher recovered error_type=%s', type(exc).__name__)
            self._wake.wait(30)
            self._wake.clear()

    def dispatch(self):
        settings = self.service.settings()
        if not settings.get('daily_checkin_enabled') or self.service._stop.is_set():
            return
        for account in self.service.store.accounts(private=True):
            if not automatic_eligible(account, settings):
                continue
            identifier = int(account['id'])
            state = read_state(account)
            try:
                previous = json.loads(account.get('checkin_state_json') or '{}')
            except (TypeError, ValueError):
                previous = {}
            if not isinstance(previous, dict) or previous.get('binding') != state['binding'] or previous.get('day') != state['day']:
                self.service.store.save_checkin_state(identifier, state, binding=state['binding'])
            now = time.time()
            confirmed = account.get('last_checkin_date') == state['day']
            if confirmed:
                missing = decode_details(account).get('_meta', {}).get('stale') is True
                if (state.get('quota_checks', 0) >= MAX_QUOTA_CHECKS
                        or not missing and state.get('quota_status') == 'fresh'
                        or now < state.get('quota_checked_at', 0) + QUOTA_INTERVAL):
                    continue
                mode = 'quota'
            else:
                if now < state.get('next_retry_at', 0):
                    continue
                mode = 'claim'
            if getattr(self.service, 'auto_login_busy', lambda _: False)(identifier):
                continue
            auth = getattr(self.service, 'canvas_auth', None)
            if auth and auth.busy(identifier):
                continue
            # User policy: keep admitting work on busy accounts. Only naturally
            # idle accounts may acquire a short daily-maintenance reservation.
            if (self.service._active.get(identifier, 0) or self.service.store.active_account_count(identifier)
                    or self.service.browser_runtime.busy(identifier)):
                continue
            with self._guard:
                if identifier in self._inflight:
                    continue
                if len(self._inflight) >= 2:
                    continue
                self._inflight.add(identifier)
            try:
                self._pool.submit(self._run_one, identifier, mode)
            except RuntimeError:
                with self._guard:
                    self._inflight.discard(identifier)
                raise

    def _run_one(self, identifier, mode='claim'):
        try:
            with self.service._browser_aux_slots:
                self.service.checkin_account(identifier, _automatic=True, _refresh_only=mode == 'quota')
            self.failures.pop(identifier, None)
        except AccountMaintenanceBusy:
            pass  # Busy is not an auth/network failure and has no failure backoff.
        except Exception as exc:
            self.failures[identifier] = type(exc).__name__
            logger.warning('Daily credit maintenance failed account_id=%s error_type=%s', identifier, type(exc).__name__)
        finally:
            with self._guard:
                self._inflight.discard(identifier)
            self.wake()


def run_checkin(service, identifier, client_factory, *, automatic=False, refresh_only=False):
    account = service.store.account(identifier, private=True)
    if not account:
        raise KeyError(identifier)
    if account.get('execution_mode') == 'jimeng_web':
        from .jimeng_client import JimengProtocolUnavailable
        raise JimengProtocolUnavailable('Jimeng daily credit claim contract is unavailable')
    today = claim_day()
    if account.get('last_checkin_date') == today and not refresh_only:
        return {'already_checked_in': True, 'date': today}
    with service._exclusive_aux_account(identifier):
        account = service.store.account(identifier, private=True)
        if automatic and not automatic_eligible(account, service.settings()):
            raise AccountMaintenanceBusy('Daily credit maintenance is disabled or account is unavailable')
        state = read_state(account, today)
        confirmed = account.get('last_checkin_date') == today
        if confirmed and not refresh_only:
            return {'already_checked_in': True, 'date': today}
        binding = identity_binding(account)
        if automatic and not confirmed and time.time() < state.get('next_retry_at', 0):
            raise AccountMaintenanceBusy('Daily credit retry limit reached for this account today')
        if automatic and not confirmed and state.get('attempts', 0) >= MAX_CLAIM_ATTEMPTS:
            state['total_attempts'] = state.get('total_attempts', state['attempts'])
            state['attempts'] = 0
            state['bursts'] = state.get('bursts', 0) + 1
        state.update(state='refreshing' if confirmed or refresh_only else 'claiming', last_attempt_at=time.time())
        quota_started = confirmed or refresh_only
        if quota_started:
            state['quota_checks'] = state.get('quota_checks', 0) + 1
            state['quota_checked_at'] = time.time()
        if not confirmed and not refresh_only:
            state['total_attempts'] = state.get('total_attempts', state.get('attempts', 0)) + 1
            state['attempts'] = state.get('attempts', 0) + 1
        if not service.store.save_checkin_state(identifier, state, binding=binding):
            raise AccountMaintenanceBusy('Account identity changed before daily credit maintenance')

        def roll_day(current):
            nonlocal today, state, confirmed
            day = claim_day()
            if day == today:
                return
            today = day
            state = read_state(current, today)
            confirmed = current.get('last_checkin_date') == today
            if not confirmed and not refresh_only:
                state['total_attempts'] = state.get('total_attempts', state.get('attempts', 0)) + 1
                state['attempts'] = state.get('attempts', 0) + 1
            state.update(state='refreshing' if confirmed or refresh_only else 'claiming',
                         last_attempt_at=time.time())
            if quota_started:
                state['quota_checks'] = state.get('quota_checks', 0) + 1
                state['quota_checked_at'] = time.time()
            if not service.store.save_checkin_state(identifier, state, binding=binding):
                raise AccountMaintenanceBusy('Account changed at the daily credit boundary')

        owned = False
        acquired = False
        try:
            account, owned = service.browser_runtime.acquire(account)
            acquired = True
            if not account or not account.get('login_user_id'):
                raise BrowserError('Dreamina check-in requires a verified signed-in account')
            if not account or int(account.get('id') or 0) != identifier or identity_binding(account) != binding:
                raise AccountMaintenanceBusy('Browser lease does not match the daily credit account binding')
            service._wait_checkin_identity(account)
            current = service.store.account(identifier, private=True)
            if not current or identity_binding(current) != binding:
                raise AccountMaintenanceBusy('Account identity changed during daily credit readiness')
            roll_day(current)
            client = client_factory(account)
            if not confirmed and not refresh_only:
                if state.get('total_attempts', state['attempts']) > 1:
                    try:
                        values = website_credit_values(client.account_state(max_attempts=1, timeout_seconds=6))
                    except BrowserError:
                        values = None
                    # An unavailable quota is informative; an observed identity
                    # mismatch must stop the claim, never be swallowed as a read failure.
                    service._verify_checkin_identity(account)
                    if values is not None:
                        service.store.save_checkin_state(identifier, state, binding=binding, values=values)
                current = service.store.account(identifier, private=True)
                if (not current or service._stop.is_set() or identity_binding(current) != binding
                        or automatic and not automatic_eligible(current, service.settings())):
                    raise AccountMaintenanceBusy('Account changed or daily credit maintenance stopped before claim')
                roll_day(current)
                if not confirmed:
                    received = receipt_quota(service._daily_receipt(account, client, today))
                    service._verify_checkin_identity(account)
                    state.update(state='confirmed', received=received, confirmed_at=time.time(), next_retry_at=0)
                    if not service.store.save_checkin_state(identifier, state, binding=binding,
                            values={'last_checkin_date': today}):
                        raise AccountMaintenanceBusy('Account identity changed after the daily credit receipt')
                    confirmed = True
            if not quota_started:
                state['quota_checks'] = state.get('quota_checks', 0) + 1
                state['quota_checked_at'] = time.time()
            try:
                values = website_credit_values(client.account_state(max_attempts=2, timeout_seconds=6))
                service._verify_checkin_identity(account)
                state.update(quota_status='fresh', state='confirmed' if confirmed else 'pending')
                if not service.store.save_checkin_state(identifier, state, binding=binding, values=values):
                    raise AccountMaintenanceBusy('Account identity changed during daily credit refresh')
                refreshed = True
            except BrowserError as exc:
                state.update(quota_status='failed', state='confirmed' if confirmed else 'pending',
                             last_error_type=type(exc).__name__)
                current = service.store.account(identifier, private=True) or account
                stale = decode_details(current)
                stale['_meta'] = {**(stale.get('_meta') or {}), 'stale': True,
                                  'latest_source': 'dreamina_web', 'missing_at': time.time()}
                service.store.save_checkin_state(identifier, state, binding=binding,
                                                values={'balance_details': stale})
                refreshed = False
            return {'already_checked_in': False, 'date': today, 'received': state.get('received', 0),
                    'quota_refreshed': refreshed}
        except Exception as exc:
            if not confirmed:
                restricted = isinstance(exc, BrowserRejected) or getattr(exc, 'status', '') in {
                    'challenge_required', 'verification_required', 'login_required', 'setup_required', 'agreement_required'}
                delay = BURST_COOLDOWN if restricted or state.get('attempts', 0) >= MAX_CLAIM_ATTEMPTS else (60, 300)[
                    min(1, max(0, state.get('attempts', 1)-1))]
                state.update(state='retry', last_error_type=type(exc).__name__,
                    retry_class='rejected' if restricted else 'transport', next_retry_at=time.time() + delay)
                service.store.save_checkin_state(identifier, state, binding=binding)
            else:
                state.update(state='confirmed', quota_status='failed', last_error_type=type(exc).__name__)
                service.store.save_checkin_state(identifier, state, binding=binding)
            raise
        finally:
            if acquired:
                service.browser_runtime.release(account, owned)
