"""Account-aware queue and durable Dreamina video task execution."""

from __future__ import annotations

from .cost_adapter import input_evidence, number, settle_failed_balance

import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .browser import BrowserError, BrowserRejected, CDPBrowser, browser_alive
from .account_automation import AccountAutomation, AutomationError
from .browser_runtime import BrowserRuntime
from .canvas_cli import CanvasCLI, CanvasCLIError
from .canvas_service import ADMISSION_AUDIT_FIELDS, CanvasServiceMixin, execution_mode
from .account_policy import credit_rejected as _credit_rejected
from .catalog import MODELS, normalize_request
from .client import DreaminaClient, DreaminaError, InputAudioDurationError, SubmissionPreflightError
from .image_client import DreaminaImageClient, resolve_image_model
from .image_request import normalize_image_request
from .errors import public_error, upstream_review_error
from .media import MediaError
from .result_transfer import ResultTransferError, transfer, presign, expires_soon, _config
from .upload import UploadError
from .store import Store


TERMINAL = {"succeeded", "failed", "expired"}
MAX_CREDIT_ACCOUNT_ATTEMPTS = 2
HISTORY_COST_WINDOW_SECONDS = 7 * 24 * 60 * 60
HISTORY_COST_SAMPLES = 5
logger = logging.getLogger(__name__)


class AccountMaintenanceBusy(BrowserError):
    """A daily credit claim must wait until all generation accounting is idle."""


def _credential_setup_ready(account: dict[str, Any]) -> bool:
    return not account.get('login_email') or bool(account.get('watermark_ready') and account.get('agreement_ready'))


DEFAULT_SETTINGS: dict[str, Any] = {
    "canvas_cli_enabled": False,
    "task_workers": 10,
    "task_queue_capacity": 100,
    "poll_interval_seconds": 8,
    "task_timeout_seconds": 1800,
    "account_maintenance_interval_seconds": 300,
    "daily_checkin_enabled": False,
    "chrome_executable": os.getenv("IJM_CHROME_EXECUTABLE", "/usr/bin/chromium" if os.name != "nt" else "chrome.exe"),
    "chrome_user_data_root": os.getenv("IJM_CHROME_USER_DATA_ROOT", "/app/data/chrome-profiles"),
    "low_balance_disable_threshold": 0,
    "allow_video_reference_inputs": True,
    "excess_media_policy": "reject",
    "prompt_media_reference_cleanup_enabled": True,
    "strict_prompt_media_references": False,
    "enable_sd_2_5_480p_x": False,
    "result_s3_backend": "s3_center_direct",
    "result_s3_center_api_base": os.getenv("IJM_S3_CENTER_API_BASE", ""),
    "result_s3_center_token": os.getenv("IJM_S3_CENTER_TOKEN", ""),
    "result_s3_bucket": os.getenv("IJM_S3_CENTER_BUCKET", "assets"),
    "result_s3_key_prefix": os.getenv("IJM_S3_CENTER_KEY_PREFIX", "aicontent/ai-sdijm"),
}


class Service(CanvasServiceMixin):
    def __init__(self, store: Store):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=50, thread_name_prefix="ijm-task")
        self._lock = threading.RLock()
        self._active: dict[int, int] = {}
        self._aux_account_owners: dict[int, object] = {}
        self._credit_reservations: dict[str, tuple[int, float]] = {}
        self._worker_slots = threading.BoundedSemaphore(50)
        self._started = False
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self.browser_runtime = BrowserRuntime(store, self.settings)
        self.canvas_cli = CanvasCLI(store.path.parent)
        self.canvas_runtime: dict[str, Any] = {'available': None, 'status': 'not_checked'}
        self._browser_aux_slots = threading.BoundedSemaphore(2)
        self._maintenance_failures: dict[int, tuple[int, float]] = {}
        self._browser_views: dict[int, tuple[dict[str, Any], bool, float]] = {}
        self._browser_views_lock = threading.RLock()

    def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **self.store.get_settings()}

    def public_settings(self) -> dict[str, Any]:
        values = self.settings()
        values["result_s3_center_token_configured"] = bool(values.get("result_s3_center_token"))
        values["result_s3_center_token"] = ""
        values['canvas_cli_runtime'] = dict(self.canvas_runtime)
        return values

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS)
        filtered = {key: value for key, value in values.items() if key in allowed}
        if "result_s3_center_token" in filtered and not str(filtered["result_s3_center_token"] or "").strip():
            filtered.pop("result_s3_center_token")
        for key in ("task_workers", "task_queue_capacity", "poll_interval_seconds", "task_timeout_seconds"):
            if key in filtered and not 1 <= int(filtered[key]) <= (3600 if key != "task_queue_capacity" else 10000):
                raise ValueError(f"invalid {key}")
        candidate = {**self.settings(), **filtered}
        if candidate["enable_sd_2_5_480p_x"]:
            try:
                _config(candidate)
            except ResultTransferError as exc:
                raise ValueError(str(exc)) from exc
        self.store.update_settings(filtered)
        return self.public_settings()

    def start(self) -> None:
        try:
            self.canvas_runtime = self.canvas_cli.verify_runtime()
        except CanvasCLIError as exc:
            self.canvas_runtime = {'available': False, 'error_code': exc.code, 'message': str(exc)}
            logger.error('Canvas runtime unavailable (%s); browser backend remains available', exc.code)
        self._started = True
        self._maintenance = threading.Thread(target=self._maintain_accounts, daemon=True,
                                             name="ijm-account-maintenance")
        self._maintenance.start()
        for task in reversed(self.store.tasks(1000)):
            if (task.get('execution_backend') == 'canvas_cli'
                    and (task.get('upstream_context') or {}).get('reconciliation_paused') is True):
                context = task['upstream_context']
                if (task.get('error_code') != 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
                        or context.get('manual_termination') or context.get('reconciliation_closed')
                        or context.get('reconciliation_pause_reason') in {'result_pending', 'result_transfer_failed'}):
                    continue
                # Migrate old unconfirmed submissions into bounded observation.
                # Keep the original operation and deadline; never submit it again.
                task = self.store.update_task(task['id'], {'upstream_context': {
                    **context, 'reconciliation_paused': False}}, preserve_terminal=True)
            if task["status"] == "queued":
                self._pool.submit(self._run, task["id"])
            elif task.get("execution_backend") == "canvas_cli" and task.get("upstream_context"):
                phase = task["upstream_context"].get("submission_phase")
                if phase in {"submitting", "submitted", "run_dispatching", "accepted"} and (task["status"] not in TERMINAL
                        or task.get("error_code") == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"):
                    self._pool.submit(self._resume_canvas_poll, task["id"])
                elif task['status'] not in TERMINAL and (phase == 'confirming' or task["status"] == "preparing"):
                    self._fail(task["id"], "CLI_PREPARATION_INTERRUPTED",
                               "service restarted before Canvas paid submission")
            elif task["status"] in {"submitted", "running"} and task["run_id"] and task["thread_id"]:
                self._pool.submit(self._resume_poll, task["id"])
            elif task["status"] in {"submitted", "running"} and task["run_id"] and task["normalized"].get("kind") == "image":
                self._pool.submit(self._resume_poll, task["id"])
            elif task["status"] == "preparing":
                self.store.update_task(task["id"], {"status": "failed", "progress": 100,
                    "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                    "error_message": "service restarted during preparation; automatic resubmission is disabled",
                    "completed_at": time.time()})

    def stop(self) -> None:
        self._stop.set()
        if self._maintenance and self._maintenance.is_alive():
            self._maintenance.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=False)
        for account_id in list(self._browser_views):
            self.close_browser_view(account_id)
        self.browser_runtime.close_all_idle()
        self.canvas_cli.close()

    def open_browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id in self._browser_views:
                account, owned, _ = self._browser_views[account_id]
                self._browser_views[account_id] = (account, owned, time.monotonic())
                return account
            account = self.store.account(account_id, private=True)
            if not account:
                raise KeyError(account_id)
            current, owned = self.browser_runtime.acquire(account)
            self._browser_views[account_id] = (current, owned, time.monotonic())
            return current

    def browser_view(self, account_id: int) -> dict[str, Any]:
        with self._browser_views_lock:
            if account_id not in self._browser_views:
                raise BrowserError("account browser view is closed")
            account, owned, _ = self._browser_views[account_id]
            self._browser_views[account_id] = (account, owned, time.monotonic())
            return account

    def close_browser_view(self, account_id: int) -> None:
        with self._browser_views_lock:
            entry = self._browser_views.pop(account_id, None)
        if entry:
            self.browser_runtime.release(entry[0], entry[1])

    def _maintain_accounts(self) -> None:
        while not self._stop.is_set():
            with self._browser_views_lock:
                expired = [account_id for account_id, (_, _, touched) in self._browser_views.items()
                           if time.monotonic() - touched > 20 * 60]
            for account_id in expired:
                self.close_browser_view(account_id)
            settings = self.settings()
            needs_rapid_recheck = False
            for account in self.store.accounts(private=True):
                if self._stop.is_set():
                    break
                account_id = int(account["id"])
                if not account["enabled"] or account["status"] == "logging_in":
                    continue
                failed, retry_at = self._maintenance_failures.get(account_id, (0, 0))
                if time.monotonic() < retry_at:
                    continue
                stage = 'check_account'
                try:
                    # CLI accounts without credentials wait for authorization;
                    # routine maintenance must not open Chrome for them.
                    if execution_mode(account) == "canvas_cli" and account.get("cli_auth_status") != "ready":
                        checked = account
                    elif execution_mode(account) == "browser":
                        # Web quota checks share the short-lived auxiliary budget.
                        with self._browser_aux_slots:
                            checked = self.check_account(account_id)
                    else:
                        checked = self.check_account(account_id)
                    if checked["status"] == "login_required":
                        needs_rapid_recheck = True
                    if settings["daily_checkin_enabled"] and checked['status'] in {'active', 'cli_auth_required'}:
                        stage = 'daily_checkin'
                        with self._browser_aux_slots:
                            self.checkin_account(account_id)
                    self._maintenance_failures.pop(account_id, None)
                except AccountMaintenanceBusy:
                    # Occupancy is temporary and does not mean the account or
                    # its proxy failed. Revisit it during the next normal pass.
                    continue
                except (BrowserError, CanvasCLIError, ValueError, KeyError) as exc:
                    failed += 1
                    retry_seconds = min(1800, 60 * 2 ** min(failed - 1, 5))
                    self._maintenance_failures[account_id] = (failed,
                        time.monotonic() + retry_seconds)
                    logger.warning('IJM account maintenance failed account_id=%s stage=%s error_type=%s',
                        account_id, stage, type(exc).__name__, extra={
                        'account_id': account_id, 'stage': stage,
                        'error_type': type(exc).__name__})
                    continue
            interval = max(30, int(settings["account_maintenance_interval_seconds"]))
            # A restarted Chrome often exposes its tab before the app can make
            # authenticated requests. Retry that state promptly instead of
            # leaving a valid account unavailable for the full interval.
            self._stop.wait(interval)

    def checkin_account(self, account_id: int) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        today = datetime.now(ZoneInfo(os.getenv("IJM_TIME_ZONE", "Asia/Taipei"))).date().isoformat()
        if account.get("last_checkin_date") == today:
            return {"already_checked_in": True, "date": today}
        with self._exclusive_aux_account(account_id):
            account = self.store.account(account_id, private=True)
            if account.get('last_checkin_date') == today:
                return {"already_checked_in": True, "date": today}
            account, owned = self.browser_runtime.acquire(account)
            try:
                self._wait_checkin_identity(account)
                result = DreaminaClient(account).daily_checkin()
            finally:
                self.browser_runtime.release(account, owned)
            self.store.update_account(account_id, {"last_checkin_date": today})
        return {"already_checked_in": False, "date": today,
                "received": result.get("receive_quota", 0)}

    @contextmanager
    def _exclusive_aux_account(self, account_id: int):
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        # BrowserRuntime's lock can span Chrome startup and shutdown. Inspect
        # it before taking the global generation admission lock.
        if account.get('status') == 'logging_in' or self.browser_runtime.busy(account_id):
            raise AccountMaintenanceBusy('Account browser is busy; daily check-in is deferred')
        owner = object()
        with self._lock:
            current = self.store.account(account_id, private=True)
            if not current:
                raise KeyError(account_id)
            if (current.get('status') == 'logging_in' or self._active.get(account_id, 0) or self.store.active_account_count(account_id)
                    or account_id in self._aux_account_owners):
                raise AccountMaintenanceBusy('Account is busy; daily check-in is deferred until its tasks finish')
            self._aux_account_owners[account_id] = owner
        try:
            yield
        finally:
            with self._lock:
                if self._aux_account_owners.get(account_id) is owner:
                    self._aux_account_owners.pop(account_id, None)

    def _wait_checkin_identity(self, account: dict[str, Any]) -> None:
        """Wait only for genuine read-only SDK readiness before one daily claim."""
        expected = str(account.get('login_user_id') or '')
        if not expected:
            raise BrowserError('Dreamina check-in requires a verified signed-in account')
        deadline = time.monotonic() + 30
        browser = CDPBrowser(int(account.get('cdp_port') or 0), timeout=10)
        automation = AccountAutomation(account, self.settings(), browser=browser)
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError('Dreamina check-in identity readiness timed out')
            browser.timeout = min(10, remaining)
            try:
                identity = automation.read_identity()
            except AutomationError as exc:
                if (exc.status != 'login_required'
                        or str(exc) != 'Dreamina identity read could not complete'):
                    raise
                self._stop.wait(min(.5, max(0, deadline - time.monotonic())))
                continue
            if time.monotonic() >= deadline:
                raise BrowserError('Dreamina check-in identity readiness timed out')
            if self._stop.is_set():
                raise BrowserError('Dreamina check-in was cancelled before claiming credits')
            if not isinstance(identity, dict) or str(identity.get('user_id') or '') != expected:
                raise BrowserError('Dreamina check-in identity does not match this account')
            return
        raise BrowserError('Dreamina check-in was cancelled before claiming credits')

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        normalized = normalize_request(request, strict_media_references=bool(
            settings["strict_prompt_media_references"]))
        if normalized["model"] == "sd-2-5-480p-x" and not settings["enable_sd_2_5_480p_x"]:
            raise ValueError("sd-2-5-480p-x is disabled in service settings")
        if not bool(settings["allow_video_reference_inputs"]) and normalized["videos"]:
            raise ValueError("video reference inputs are disabled by service settings")
        if self.store.active_count() >= int(settings["task_workers"]) + int(settings["task_queue_capacity"]):
            raise RuntimeError("IJMAPI queue is full")
        task_id = "ijm_" + uuid.uuid4().hex[:16]
        task = self.store.create_task(task_id, request, normalized)
        configured_modes = {execution_mode(a) for a in self.store.accounts(private=True) if a['enabled']}
        try:
            estimates = [item for item in self.store.model_costs() if
                         configured_modes == {'browser'} and item.get('execution_backend', 'browser') == 'browser' and
                         item["model"] == ("sd-2-5" if normalized["model"] == "sd-2-5-480p-x" else normalized["model"]) and
                         item["duration"] == normalized["duration"] and
                         item["resolution"] == ("720p" if normalized["model"] == "sd-2-5-480p-x" else normalized["resolution"]) and
                         item["image_count"] == len(normalized["images"]) and
                         item["video_count"] == len(normalized["videos"]) and
                         item["audio_count"] == len(normalized["audios"])]
        except Exception as exc:
            logger.warning('IJM legacy cost estimate unavailable (%s)', type(exc).__name__)
            estimates = []
        if estimates:
            task = self.store.update_task(task_id, {"estimated_cost": max(item["cost"] for item in estimates)})
        task = self._update_cost_estimate(task)
        self._pool.submit(self._run, task_id)
        return self.public_task(task)

    def retry_unsubmitted_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            if self._stop.is_set():
                raise ValueError('task scheduling is stopped')
            settings = self.settings()
            if self.store.active_count() >= int(settings['task_workers']) + int(settings['task_queue_capacity']):
                raise ValueError('IJMAPI queue is full')
            task = self.store.requeue_unsubmitted_task(task_id)
            try:
                self._pool.submit(self._run, task_id)
            except RuntimeError:
                self.store.update_task(task_id, {'status': 'failed', 'progress': 0,
                    'error_code': 'ACCOUNT_POOL_UNAVAILABLE',
                    'error_message': 'task dispatch unavailable before submission',
                    'completed_at': time.time()})
                raise ValueError('task dispatch unavailable') from None
            return self.public_task(task)

    def create_image(self, request: dict[str, Any]) -> dict[str, Any]:
        model = str(request.get("model") or "").strip()
        normalized = normalize_image_request(request, resolve_image_model(model))
        settings = self.settings()
        if self.store.active_count() >= int(settings["task_workers"]) + int(settings["task_queue_capacity"]):
            raise RuntimeError("IJMAPI queue is full")
        task_id = "ijm_img_" + uuid.uuid4().hex[:16]
        task = self.store.create_task(task_id, request, normalized)
        self._pool.submit(self._run_image, task_id)
        return self.image_response(task)

    @staticmethod
    def _cost_dimensions(normalized: dict[str, Any]) -> tuple | None:
        if normalized.get('kind') == 'image':
            return None
        spec = MODELS.get(normalized.get('model'))
        duration = number(normalized.get('duration'))
        if not spec or not duration:
            return None
        evidence = normalized.get('_cost_evidence')
        evidence = evidence if isinstance(evidence, dict) else {}
        reference = number(evidence.get('reference_seconds')) if normalized.get('videos') else 0.0
        if reference is None or normalized.get('videos') and reference <= 0:
            return None  # Uploaded metadata must establish reference-video seconds.
        resolution = spec.generation_resolution if spec.id == 'sd-2-5-480p-x' else normalized.get('resolution')
        return (spec.upstream_model, duration, resolution, round(reference, 6))

    def _cost_estimate(self, normalized: dict[str, Any], fallback: Any, rows: dict) -> tuple[float, dict]:
        legacy = number(fallback) or 0.0
        metadata = {'source': 'legacy_estimate' if legacy else 'unknown', 'sample_count': 0}
        dimensions = self._cost_dimensions(normalized)
        if dimensions is None:
            return legacy, metadata
        counts = tuple(len(normalized.get(kind) or []) for kind in ('images', 'videos', 'audios'))
        exact, compatible, covering = [], [], []
        now = time.time()
        backend = (normalized.get('_cost_evidence') or {}).get('execution_backend', 'browser')
        for row in rows['history']:
            if (row.get('execution_backend') or 'browser') != backend:
                continue
            cost, before, after = (number(row.get(key)) for key in ('actual_cost', 'balance_before', 'balance_after'))
            stamp = number(row.get('completed_at')) or number(row.get('updated_at')) or 0
            if (row.get('status') != 'succeeded' or not cost
                    or not now - HISTORY_COST_WINDOW_SECONDS <= stamp <= now + 60):
                continue
            try:
                request = json.loads(row['normalized_json'])
            except (ValueError, TypeError):
                continue
            if not isinstance(request, dict):
                continue
            evidence = request.get('_cost_evidence')
            if not isinstance(evidence, dict):
                continue
            official = evidence.get('upstream_task_credits')
            official = official if isinstance(official, dict) else {}
            task_scoped = (official.get('field') == 'item_list[].extra.credits_consume'
                           and bool(row.get('run_id')) and official.get('submit_id') == row.get('run_id')
                           and official.get('phase') == 'succeeded'
                           and number(official.get('amount')) == cost)
            isolated = (evidence.get('balance_isolated') is True and before is not None and after is not None
                        and after <= before and math.isclose(before - after, cost, abs_tol=1e-6, rel_tol=1e-6))
            if not task_scoped and not isolated:
                continue
            previous = self._cost_dimensions(request)
            if previous != dimensions:
                if (previous is not None and previous[0] == dimensions[0] and previous[2] == dimensions[2]
                        and previous[1] >= dimensions[1] and previous[3] >= dimensions[3]
                        and len(covering) < HISTORY_COST_SAMPLES):
                    covering.append((cost, row['id']))
                continue
            if len(compatible) < HISTORY_COST_SAMPLES:
                compatible.append(cost)
            if counts == tuple(len(request.get(kind) or []) for kind in ('images', 'videos', 'audios')):
                if len(exact) < HISTORY_COST_SAMPLES:
                    exact.append(cost)
        samples = exact or compatible
        if samples:
            return max(samples), {'source': 'recent_actual_exact' if exact else 'recent_actual_dimensions',
                                  'sample_count': len(samples), 'window_days': 7}
        if covering:
            return max(cost for cost, _ in covering), {'source': 'recent_actual_covering_dimensions',
                'sample_count': len(covering), 'window_days': 7, 'sample_ids': [identifier for _, identifier in covering]}
        return legacy, metadata

    def _update_cost_estimate(self, task: dict) -> dict:
        with self._lock:
            if task.get('execution_backend') == '':
                modes = {execution_mode(a) for a in self.store.accounts(private=True) if a['enabled']}
                if len(modes) != 1:
                    return self.store.update_task(task['id'], {'estimated_cost': 0,
                        'cost_evidence': {'scheduling_estimate': {'source': 'unknown', 'sample_count': 0}}})
                backend = next(iter(modes))
                task = self.store.update_task(task['id'], {'cost_evidence': {'execution_backend': backend}})
            try:
                cost, metadata = self._cost_estimate(task['normalized'], task.get('estimated_cost'), self.store.scheduling_rows())
            except Exception as exc:
                logger.warning('IJM recent cost estimate unavailable (%s); keeping prior estimate', type(exc).__name__)
                return task
            updated = self.store.update_task(task['id'], {'estimated_cost': cost,
                'cost_evidence': {'scheduling_estimate': metadata}})
            if task['id'] in self._credit_reservations:
                account_id, _ = self._credit_reservations[task['id']]
                self._credit_reservations[task['id']] = (account_id, cost)
            return updated

    def _pending_credits(self, rows: dict, exclude_task_id: str | None = None) -> dict[str, tuple[int, float]]:
        pending = {row['id']: (int(row['account_id']), number(row.get('estimated_cost')) or 0.0)
                   for row in rows['inflight'] if row['id'] != exclude_task_id}
        for task_id, (account_id, amount) in self._credit_reservations.items():
            if task_id != exclude_task_id:
                pending[task_id] = (account_id, max(amount, pending.get(task_id, (account_id, 0.0))[1]))
        return pending

    @staticmethod
    def _available_credits(account: dict, pending: dict) -> tuple[float | None, bool]:
        amounts = [amount for account_id, amount in pending.values() if account_id == int(account['id'])]
        balance = number(account.get('balance'))
        uncertain = any(amount <= 0 for amount in amounts) or balance is None and bool(amounts)
        return (None if balance is None else balance - sum(amounts), uncertain)

    def _funded_accounts(self, estimated_cost: float, excluded: set[int], pending: dict,
                         backend: str | None = None, *, include_aux_busy: bool = False) -> list[dict]:
        accounts = []
        for item in self.store.accounts(private=True):
            if (int(item['id']) in excluded or not item['enabled'] or item['status'] != 'active'
                    or not item['proxy_url'] or not _credential_setup_ready(item)):
                continue
            if not include_aux_busy and int(item['id']) in self._aux_account_owners:
                continue
            if execution_mode(item) == 'canvas_cli' and item.get('cli_auth_status') != 'ready':
                continue
            if execution_mode(item) == 'canvas_cli' and (not self.settings()['canvas_cli_enabled']
                    or self.canvas_runtime.get('available') is False):
                continue
            if backend and execution_mode(item) != backend:
                continue
            available, unknown_hold = self._available_credits(item, pending)
            if unknown_hold or available is not None and (available < estimated_cost or available <= 0):
                continue
            accounts.append({**item, '_available_credits': available})
        return accounts

    def _reserve_account(self, estimated_cost: float = 0, *,
                         exclude_ids: set[int] | None = None,
                         prefer_balance: bool = False, task_id: str | None = None) -> dict[str, Any] | None:
        excluded = exclude_ids or set()
        with self._lock:
            if sum(self._active.values()) >= int(self.settings()["task_workers"]):
                return None
            pending = self._pending_credits(self.store.scheduling_rows(), task_id)
            task = (self.store.task(task_id) or {}) if task_id else {}
            backend = task.get('execution_backend')
            excluded = excluded | self._model_excluded_accounts(task)
            accounts = self._funded_accounts(estimated_cost, excluded, pending, backend)
            # Unknown cost is not a free task: probe a larger balance until a
            # measured budget exists. Explicit priority always remains first.
            accounts.sort(key=lambda item: (-int(item['priority']), item['_available_credits'] is None,
                (item['_available_credits'] or 0) * (1 if estimated_cost > 0 else -1), item['id']))
            for account in accounts:
                account_id = int(account["id"])
                active = self._active.get(account_id, 0)
                held = sum(owner == account_id for owner, _ in pending.values())
                if max(active, held) >= int(account["max_concurrency"]):
                    continue
                self._active[account_id] = active + 1
                self._credit_reservations[task_id or 'slot-' + uuid.uuid4().hex] = (account_id, estimated_cost)
                return account
        return None

    def _has_eligible_account(self, estimated_cost: float, excluded: set[int], task_id: str | None = None) -> bool:
        with self._lock:
            pending = self._pending_credits(self.store.scheduling_rows(), task_id)
            task = (self.store.task(task_id) or {}) if task_id else {}
            return bool(self._funded_accounts(estimated_cost, excluded | self._model_excluded_accounts(task),
                pending, task.get('execution_backend'), include_aux_busy=True))

    def _release_account(self, account_id: int, task_id: str | None = None) -> None:
        with self._lock:
            self._active[account_id] = max(self._active.get(account_id, 1) - 1, 0)
            key = task_id or next((key for key, (owner, _) in self._credit_reservations.items() if owner == account_id), None)
            if key:
                self._credit_reservations.pop(key, None)

    def _handle_credit_rejection(self, task_id: str, account_id: int, reason: str) -> bool:
        """One admission-rejection policy for browser, Canvas, image and video."""
        return self._handle_admission_rejection(task_id, account_id, 'INSUFFICIENT_CREDITS', reason)

    @staticmethod
    def _unaccepted_admission(task: dict, *, rejected: bool = False) -> bool:
        """A typed rejection may clear a dispatched request, never an acceptance."""
        context = task.get('upstream_context') or {}
        if (CanvasServiceMixin._canvas_task_terminated(task)
                or task.get('status') in {'submitted', 'running', 'submission_unknown'}
                or task.get('error_code') == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
                or task.get('run_id') or task.get('thread_id')
                or task.get('result_urls') or task.get('upstream_result_urls')
                or (task.get('normalized') or {}).get('_execution')
                or context.get('phase') in {'accepted', 'unknown'}
                or context.get('submission_phase') in {'accepted', 'submitted', 'unknown'}
                or context.get('resource_ids')
                or context.get('financial_outcome') in {'unknown', 'unconfirmed'}
                or context.get('reconciliation_paused') or context.get('reconciliation_closed')
                or context.get('automatic_resubmission_blocked')
                or (context.get('submit_error') or {}).get('outcome_unknown') is True):
            return False
        return rejected or context.get('submission_phase') not in {'submitting', 'run_dispatching'}

    def _requeue_unaccepted_task(self, task_id: str, *, rejected: bool = False,
                                 **changes) -> bool:
        """Release only a proven-unaccepted binding; keep its admission audit."""
        with self._lock:
            task = self.store.task(task_id) or {}
            if not self._unaccepted_admission(task, rejected=rejected):
                return False
            audit = {key: value for key, value in (task.get('upstream_context') or {}).items()
                     if key in ADMISSION_AUDIT_FIELDS}
            saved = self.store.update_task(task_id, {'status': 'queued', 'progress': 0,
                'account_id': None, 'balance_before': None, 'execution_backend': '',
                'upstream_context': audit,
                'cost_evidence': {'execution_backend': '', 'canvas_quote': None},
                **changes}, preserve_terminal=True)
            return saved.get('status') == 'queued' and saved.get('execution_backend') == ''

    @staticmethod
    def _admission_history(task: dict) -> list:
        context = task.get('upstream_context') or {}
        return list(context.get('preflight_failures') or []) + list(context.get('model_eligibility_exclusions') or [])

    @staticmethod
    def _model_excluded_accounts(task: dict) -> set[int]:
        kind = 'image' if (task.get('normalized') or {}).get('kind') == 'image' else 'video'
        return {entry['account_id'] for entry in (task.get('upstream_context') or {}).get('model_eligibility_exclusions') or []
            if isinstance(entry, dict) and isinstance(entry.get('account_id'), int)
            and not isinstance(entry['account_id'], bool)
            and entry.get('requested_model') == task.get('model') and entry.get('kind') == kind}

    def _exclude_model_account(self, task_id: str, account_id: int, diagnostic: dict) -> bool:
        """A request-local eligibility skip never consumes a credit attempt."""
        with self._lock:
            task = self.store.task(task_id) or {}
            if (not self._unaccepted_admission(task)
                    or any(key not in ADMISSION_AUDIT_FIELDS for key in task.get('upstream_context') or {})):
                return False
            context = dict(task.get('upstream_context') or {})
            history = list(context.get('model_eligibility_exclusions') or [])
            history.append({'account_id': account_id, **diagnostic})
            context['model_eligibility_exclusions'] = history
            self.store.update_task(task_id, {'upstream_context': context}, preserve_terminal=True)
            return self._requeue_unaccepted_task(task_id, error_code=diagnostic['code'],
                error_message=diagnostic['message'])

    def _admission_deadline(self, task_id: str, deadline: float) -> float:
        """Read-only account preflights retain their original wall budget across modes."""
        task = self.store.task(task_id) or {}
        previous = [value for entry in self._admission_history(task)
                    if isinstance(entry, dict) and (value := number(entry.get('task_deadline'))) is not None]
        if previous:
            return min(deadline, time.monotonic() + max(0, min(previous) - time.time()))
        return deadline

    def _admission_expired(self, task_id: str, deadline: float) -> bool:
        if time.monotonic() < self._admission_deadline(task_id, deadline):
            return False
        if not self._admission_history(self.store.task(task_id) or {}):
            return False
        self._fail(task_id, 'CLI_PREPARATION_TIMEOUT',
                   'Original task admission deadline passed; no generation was submitted')
        return True

    def _handle_admission_rejection(self, task_id: str, account_id: int, code: str,
                                    reason: str, *, preflight_error: dict | None = None) -> bool:
        """A proven rejection before paid work shares one two-account budget."""
        task = self.store.task(task_id) or {}
        if not self._unaccepted_admission(task, rejected=True):
            return False
        attempted = list(task.get('attempted_account_ids') or [])
        if account_id not in attempted:
            attempted.append(account_id)
        context = task.get('upstream_context') or {}
        changes = {'attempted_account_ids': attempted}
        if preflight_error:
            failures = list(context.get('preflight_failures') or [])
            failures.append({'account_id': account_id, **preflight_error})
            changes['upstream_context'] = {**context, 'preflight_failures': failures}
        task = self.store.update_task(task_id, changes, preserve_terminal=True)
        if (not task or (task.get('upstream_context') or {}).get('manual_termination')
                or task.get('status') in TERMINAL and task.get('error_code') != 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'):
            return False
        self.store.update_account(account_id, {'last_error': reason[:300]})
        if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
            self._fail(task_id, code, reason)
            return False
        return self._requeue_unaccepted_task(task_id, rejected=True,
            error_code=code, error_message=reason)

    def _submission_funded(self, account: dict, task_id: str, estimated_cost: float, balance: Any) -> bool:
        with self._lock:
            pending = self._pending_credits(self.store.scheduling_rows(), task_id)
            current = self.store.account(int(account['id']), private=True) or {}
            snapshots = [value for raw in (balance, current.get('balance')) if (value := number(raw)) is not None]
            available, unknown_hold = self._available_credits({**account, 'balance': min(snapshots) if snapshots else None}, pending)
            return not unknown_hold and available is not None and available > 0 and available >= estimated_cost

    def _submission_budget_status(self, account: dict, task_id: str, estimated_cost: float, balance: Any) -> tuple[bool, bool]:
        with self._lock:
            funded = self._submission_funded(account, task_id, estimated_cost, balance)
            pending = self._pending_credits(self.store.scheduling_rows(), task_id)
            return funded, any(owner == int(account['id']) for owner, _ in pending.values())

    def _has_waiting_credit_account(self, estimated_cost: float, excluded: set[int], task_id: str) -> bool:
        with self._lock:
            pending = self._pending_credits(self.store.scheduling_rows(), task_id)
            task = self.store.task(task_id) or {}
            return any(int(account['id']) in self._aux_account_owners
                       or any(owner == int(account['id']) for owner, _ in pending.values())
                       for account in self._funded_accounts(estimated_cost,
                           excluded | self._model_excluded_accounts(task), {}, task.get('execution_backend'),
                           include_aux_busy=True))

    def check_account(self, account_id: int, *, start_browser: bool = False) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        if not start_browser and execution_mode(account) == 'canvas_cli':
            if account.get('cli_auth_status') != 'ready':
                return self.store.update_account(account_id, {'status': 'cli_auth_required',
                    'last_error': 'CLI authorization is required before accepting tasks'})
            return self._check_cli_account(account)
        account, owned = self.browser_runtime.acquire(account, force_managed=start_browser)
        try:
            return self._check_account_live(account)
        finally:
            self.browser_runtime.release(account, owned)

    def _check_account_live(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = int(account["id"])
        port = int(account["cdp_port"] or 0)
        if not browser_alive(port):
            return self.store.update_account(account_id, {"status": "login_required",
                                                         "last_error": "Dreamina account browser is unavailable"})
        # Chrome exposes a CDP page before Dreamina's JavaScript has finished
        # loading. After a service restart, one early fetch failure should not
        # put an otherwise valid account into login_required for an entire
        # maintenance interval. Complete HTTP rejections still fail promptly.
        for attempt in range(3):
            try:
                state = DreaminaClient(account).account_state()
                break
            except BrowserRejected as exc:
                return self.store.update_account(account_id, {"status": "login_required", "last_error": str(exc)[:300]})
            except BrowserError as exc:
                if attempt == 2:
                    return self.store.update_account(account_id, {"status": "login_required", "last_error": str(exc)[:300]})
                time.sleep(1)
        balance = state["available_balance"]
        project_id = DreaminaClient(account).browser.project_id()
        threshold = float(self.settings()["low_balance_disable_threshold"] or 0)
        values: dict[str, Any] = {"balance": balance, "balance_details": state["buckets"],
                                  "status": "active", "last_error": ""}
        if not _credential_setup_ready(account):
            values.update(status='setup_required', enabled=False,
                          last_error='账号登录及生成设置尚未完成，请使用自动登录或网页登录完成验证')
        if project_id:
            values["project_id"] = project_id
        if threshold > 0 and balance < threshold:
            values.update({"enabled": False, "status": "disabled_low_balance",
                           "last_error": f"credit balance below {threshold}"})
        return self.store.update_account(account_id, values)

    def _run(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or task["status"] != "queued":
            return
        if self._cli_authorization_missing():
            self._fail(task_id, 'ACCOUNT_POOL_UNAVAILABLE', 'CLI authorization is required for the enabled accounts')
            return
        if task["normalized"].get("kind") == "image":
            self._run_image(task_id)
            return
        # The executor is only a waiter/dispatcher.  The configurable limit
        # applies to actual upstream work, not to the number of Python threads.
        deadline = self._admission_deadline(task_id,
            time.monotonic() + int(self.settings()['task_timeout_seconds']))
        while not self._stop.is_set():
            if self._admission_expired(task_id, deadline):
                return
            if not self._worker_slots.acquire(timeout=1):
                continue
            with self._lock:
                if sum(self._active.values()) < int(self.settings()["task_workers"]):
                    break
            self._worker_slots.release()
            time.sleep(1)
        else:
            return
        deadline = self._admission_deadline(task_id,
            time.monotonic() + int(self.settings()['task_timeout_seconds']))
        estimated_cost = float(task.get("estimated_cost") or 0)
        attempted = list(task["attempted_account_ids"])
        funding_excluded: set[int] = set()
        credit_reason = task.get("error_message") or "Dreamina account has insufficient credits"
        admission_code = (task.get('error_code') if self._admission_history(task)
                          else None) or 'INSUFFICIENT_CREDITS'
        try:
            task = self._update_cost_estimate(self.store.task(task_id) or task)
            estimated_cost = float(task.get('estimated_cost') or 0)
            while not self._stop.is_set() and time.monotonic() < deadline:
                if len(attempted) >= MAX_CREDIT_ACCOUNT_ATTEMPTS:
                    self._fail(task_id, admission_code, credit_reason)
                    return
                account = None
                while not self._stop.is_set() and time.monotonic() < deadline:
                    excluded = set(attempted) | funding_excluded
                    account = self._reserve_account(estimated_cost, exclude_ids=excluded,
                                                    prefer_balance=bool(attempted), task_id=task_id)
                    if account or ((attempted or funding_excluded or self._model_excluded_accounts(self.store.task(task_id) or {}))
                            and not self._has_eligible_account(
                            estimated_cost, excluded, task_id) and not self._has_waiting_credit_account(
                                estimated_cost, excluded, task_id)):
                        break
                    self._stop.wait(1)
                if not account:
                    rejected = attempted or self._model_excluded_accounts(self.store.task(task_id) or {})
                    code = admission_code if rejected else "ACCOUNT_POOL_UNAVAILABLE"
                    message = credit_reason if rejected else "no available Dreamina account before timeout"
                    self._fail(task_id, code, message)
                    return
                account_id = int(account["id"])
                owned = False
                try:
                    deadline = self._admission_deadline(task_id, deadline)
                    if self._admission_expired(task_id, deadline):
                        return
                    backend = execution_mode(account)
                    self.store.update_task(task_id, {"account_id": account_id,
                        "status": "preparing", "progress": 5, "balance_before": None,
                        "execution_backend": backend, "cost_evidence": {'execution_backend': backend},
                        "error_code": "", "error_message": ""})
                    if backend == 'canvas_cli':
                        result = self._execute_canvas(task_id, account, deadline)
                        if result is True:
                            return
                        if result in {'retry', 'retry_preflight', 'skip_model'}:
                            retry = self.store.task(task_id) or {}
                            attempted = list(retry.get('attempted_account_ids') or [])
                            credit_reason = retry.get('error_message') or credit_reason
                            admission_code = retry.get('error_code') or 'INSUFFICIENT_CREDITS'
                            if result == 'retry':
                                deadline = self._admission_deadline(task_id,
                                    time.monotonic() + int(self.settings()['task_timeout_seconds']))
                        elif result != 'reselect':
                            funding_excluded.add(account_id)
                        estimated_cost = float((self.store.task(task_id) or {}).get('estimated_cost') or 0)
                        continue
                    account, owned = self.browser_runtime.acquire(account)
                    client = DreaminaClient(account, store=self.store)
                    before = None
                    try:
                        before = number(client.account_state(max_attempts=2, timeout_seconds=6)["available_balance"])
                    except (BrowserError, KeyError, TypeError, ValueError) as exc:
                        logger.warning("IJM task %s account %s pre-submit credit read failed (%s); "
                                       "continuing with cached balance", task_id, account_id,
                                       type(exc).__name__)
                    if before is not None:
                        self.store.update_account(account_id, {"balance": before, "status": "active"})
                    funded, temporary_hold = self._submission_budget_status(account, task_id, estimated_cost,
                        before if before is not None else account.get('balance'))
                    if not funded:
                        if not temporary_hold:
                            funding_excluded.add(account_id)
                        if not self._requeue_unaccepted_task(task_id):
                            return
                        continue
                    self.store.update_task(task_id, {"balance_before": before, "progress": 12})
                    assets = client.upload_inputs(task["normalized"])
                    task = self.store.update_task(task_id, {"cost_evidence": input_evidence(assets)})
                    task = self._update_cost_estimate(task)
                    estimated_cost = float(task.get('estimated_cost') or 0)
                    funded, temporary_hold = self._submission_budget_status(account, task_id, estimated_cost,
                        before if before is not None else account.get('balance'))
                    if not funded:
                        if not temporary_hold:
                            funding_excluded.add(account_id)
                        if not self._requeue_unaccepted_task(task_id):
                            return
                        continue
                    if self._admission_expired(task_id, deadline):
                        return
                    self.store.update_task(task_id, {"progress": 30})
                    attempted.append(account_id)
                    self.store.update_task(task_id, {"attempted_account_ids": attempted})
                    try:
                        submitted = client.submit(task["normalized"], assets)
                    except SubmissionPreflightError as exc:
                        self._fail(task_id, "ACCOUNT_VERIFICATION_FAILED", str(exc))
                        return
                    except InputAudioDurationError as exc:
                        self._fail(task_id, "INPUT_AUDIO_DURATION_UNSUPPORTED", str(exc))
                        return
                    except BrowserRejected as exc:
                        if not _credit_rejected(str(exc)):
                            self._fail(task_id, "UPSTREAM_REJECTED", str(exc))
                            return
                        credit_reason = str(exc)
                        if not self._handle_credit_rejection(task_id, account_id, credit_reason):
                            return
                        deadline = self._admission_deadline(task_id,
                            time.monotonic() + int(self.settings()['task_timeout_seconds']))
                        continue
                    except BrowserError as exc:
                        self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                                   str(exc) + "; automatic resubmission is disabled")
                        return
                    self.store.update_task(task_id, {"status": "submitted", "progress": 40,
                                                      "run_id": submitted["run_id"],
                                                      "thread_id": submitted["thread_id"]})
                    self._poll(task_id, client, deadline)
                    return
                except (BrowserError, DreaminaError, MediaError, UploadError, ValueError) as exc:
                    self._fail(task_id, type(exc).__name__.upper(), str(exc))
                    return
                except Exception as exc:
                    self._fail(task_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
                    return
                finally:
                    self.browser_runtime.release(account, owned)
                    self._release_account(account_id, task_id)
            if not self._stop.is_set():
                rejected = attempted or self._model_excluded_accounts(self.store.task(task_id) or {})
                self._fail(task_id, admission_code if rejected else "ACCOUNT_POOL_UNAVAILABLE",
                           credit_reason if rejected else "no available Dreamina account before timeout")
        except Exception as exc:
            self._fail(task_id, 'INTERNAL_ERROR', f'{type(exc).__name__}: task scheduling could not complete')
        finally:
            self._worker_slots.release()

    def _run_image(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or task["status"] != "queued":
            return
        if self._cli_authorization_missing():
            self._fail(task_id, 'ACCOUNT_POOL_UNAVAILABLE', 'CLI authorization is required for the enabled accounts')
            return
        deadline = self._admission_deadline(task_id,
            time.monotonic() + int(self.settings()['task_timeout_seconds']))
        while not self._stop.is_set():
            if self._admission_expired(task_id, deadline):
                return
            if not self._worker_slots.acquire(timeout=1):
                continue
            with self._lock:
                if sum(self._active.values()) < int(self.settings()["task_workers"]):
                    break
            self._worker_slots.release()
            time.sleep(1)
        else:
            return
        attempted: list[int] = list(task["attempted_account_ids"])
        funding_excluded: set[int] = set()
        estimated_cost = 0.0
        preflight_failure = ((task.get('error_code'), task.get('error_message'))
            if self._admission_history(task) and task.get('error_code') else None)
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                excluded = set(attempted) | funding_excluded
                account = self._reserve_account(estimated_cost, exclude_ids=excluded,
                                                prefer_balance=bool(attempted), task_id=task_id)
                if not account:
                    if not self._has_eligible_account(estimated_cost, excluded, task_id):
                        self._fail(task_id, *(preflight_failure or ("ACCOUNT_POOL_UNAVAILABLE",
                                   "no Dreamina image account has available credits")))
                        return
                    time.sleep(1)
                    continue
                account_id = int(account["id"])
                owned = False
                try:
                    deadline = self._admission_deadline(task_id, deadline)
                    if self._admission_expired(task_id, deadline):
                        return
                    backend = execution_mode(account)
                    self.store.update_task(task_id, {"account_id": account_id,
                        "status": "preparing", "progress": 5, "error_code": "", "error_message": "",
                        "execution_backend": backend, 'cost_evidence': {'execution_backend': backend}})
                    if backend == 'canvas_cli':
                        result = self._execute_canvas(task_id, account, deadline)
                        if result is True:
                            return
                        if result in {'retry', 'retry_preflight', 'skip_model'}:
                            retry = self.store.task(task_id) or {}
                            attempted = list(retry.get('attempted_account_ids') or [])
                            preflight_failure = ((retry.get('error_code'), retry.get('error_message'))
                                if result in {'retry_preflight', 'skip_model'} else None)
                        elif result != 'reselect':
                            funding_excluded.add(account_id)
                        estimated_cost = float((self.store.task(task_id) or {}).get('estimated_cost') or 0)
                        continue
                    account, owned = self.browser_runtime.acquire(account)
                    video_client = DreaminaClient(account, store=self.store)
                    balance = None
                    try:
                        balance = number(video_client.account_state(max_attempts=2, timeout_seconds=6)['available_balance'])
                    except (BrowserError, KeyError, TypeError, ValueError):
                        pass
                    if balance is not None:
                        self.store.update_account(account_id, {'balance': balance})
                    if not self._submission_funded(account, task_id, estimated_cost, balance if balance is not None else account.get('balance')):
                        funding_excluded.add(account_id)
                        if not self._requeue_unaccepted_task(task_id):
                            return
                        continue
                    assets = video_client.upload_inputs(task["normalized"])["images"]
                    if self._admission_expired(task_id, deadline):
                        return
                    self.store.update_task(task_id, {"progress": 30})
                    attempted.append(account_id)
                    self.store.update_task(task_id, {"attempted_account_ids": attempted})
                    try:
                        submitted = DreaminaImageClient(int(account["cdp_port"])).submit(
                            task["normalized"], assets)
                    except BrowserRejected as exc:
                        if _credit_rejected(str(exc)):
                            if self._handle_credit_rejection(task_id, account_id, str(exc)):
                                continue
                            return
                        self._fail(task_id, "INSUFFICIENT_CREDITS" if _credit_rejected(str(exc))
                                   else "UPSTREAM_REJECTED", str(exc))
                        return
                    except BrowserError as exc:
                        self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                                   str(exc) + "; automatic resubmission is disabled")
                        return
                    self.store.update_task(task_id, {"status": "submitted", "progress": 40,
                        "run_id": submitted["run_id"], "thread_id": submitted["thread_id"]})
                    self._poll_image(task_id, DreaminaImageClient(int(account["cdp_port"])), deadline)
                    return
                except (BrowserError, DreaminaError, MediaError, UploadError, ValueError) as exc:
                    self._fail(task_id, type(exc).__name__.upper(), str(exc))
                    return
                except Exception as exc:
                    self._fail(task_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
                    return
                finally:
                    self.browser_runtime.release(account, owned)
                    self._release_account(account_id, task_id)
            if not self._stop.is_set():
                self._fail(task_id, *(preflight_failure or ("UPSTREAM_TIMEOUT", "Dreamina image task timed out")))
        finally:
            self._worker_slots.release()

    def _resume_image_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or not task["account_id"]:
            return
        account = self.store.account(int(task["account_id"]), private=True)
        if not account:
            self._fail(task_id, "ACCOUNT_MISSING", "task account was removed")
            return
        self._worker_slots.acquire()
        with self._lock:
            self._active[int(account["id"])] = self._active.get(int(account["id"]), 0) + 1
        owned = False
        try:
            account, owned = self.browser_runtime.acquire(account)
            self._poll_image(task_id, DreaminaImageClient(int(account["cdp_port"])),
                             time.monotonic() + int(self.settings()["task_timeout_seconds"]))
        except Exception as exc:
            self._fail(task_id, type(exc).__name__.upper(), str(exc))
        finally:
            self.browser_runtime.release(account, owned)
            self._release_account(int(account["id"]), task_id)
            self._worker_slots.release()

    def _poll_image(self, task_id: str, client: DreaminaImageClient, deadline: float) -> None:
        errors = 0
        while time.monotonic() < deadline and not self._stop.is_set():
            task = self.store.task(task_id)
            if not task or task["status"] in TERMINAL:
                return
            try:
                state = client.poll(task["run_id"])
                errors = 0
            except BrowserError:
                errors += 1
                if errors >= 8:
                    raise
                time.sleep(min(60, 2 ** errors))
                continue
            if state["status"] == "succeeded":
                self.store.update_task(task_id, {"status": "succeeded", "progress": 100,
                    "result_urls": state["result_urls"], "completed_at": time.time()})
                return
            if state["status"] == "failed":
                self._fail(task_id, "UPSTREAM_FAILED", state["error"])
                return
            self.store.update_task(task_id, {"status": "running",
                                             "progress": max(45, task["progress"])})
            time.sleep(max(2, int(self.settings()["poll_interval_seconds"])))
        if not self._stop.is_set():
            self.store.update_task(task_id, {"status": "expired", "progress": 100,
                "error_code": "UPSTREAM_TIMEOUT",
                "error_message": "Dreamina image task timed out; upstream run may continue",
                "completed_at": time.time()})

    def _resume_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or not task["account_id"]:
            return
        if task.get('execution_backend') == 'canvas_cli':
            self._resume_canvas_poll(task_id)
            return
        if task["normalized"].get("kind") == "image":
            self._resume_image_poll(task_id)
            return
        account = self.store.account(task["account_id"], private=True)
        if not account:
            self._fail(task_id, "ACCOUNT_MISSING", "task account was removed")
            return
        self._worker_slots.acquire()
        with self._lock:
            self._active[int(task["account_id"])] = self._active.get(int(task["account_id"]), 0) + 1
        owned = False
        try:
            account, owned = self.browser_runtime.acquire(account)
            self._poll(task_id, DreaminaClient(account),
                       time.monotonic() + int(self.settings()["task_timeout_seconds"]))
        except Exception as exc:
            self._fail(task_id, type(exc).__name__.upper(), str(exc))
        finally:
            self.browser_runtime.release(account, owned)
            self._release_account(int(task["account_id"]), task_id)
            self._worker_slots.release()

    def _poll(self, task_id: str, client: DreaminaClient, deadline: float) -> None:
        errors = 0
        missing_failure_detail = 0
        while time.monotonic() < deadline and not self._stop.is_set():
            task = self.store.task(task_id)
            if not task or task["status"] in TERMINAL:
                return
            try:
                state = client.poll(task["run_id"], task["thread_id"])
                errors = 0
            except (BrowserError, DreaminaError):
                errors += 1
                if errors >= 8:
                    raise
                time.sleep(min(60, 2 ** errors))
                continue
            credit = state.get("task_credit_evidence")
            credit = credit if isinstance(credit, dict) else {}
            reported = number(credit.get("amount"))
            if (reported is not None and reported >= 0
                    and credit.get("field") == "item_list[].extra.credits_consume"
                    and credit.get("submit_id") == task["run_id"]
                    and credit.get("phase") == state["status"]):
                with self._lock:
                    task = self.store.update_task(task_id, {"estimated_cost": reported if reported > 0 or state["status"] == "succeeded" else task.get("estimated_cost", 0),
                        "cost_evidence": {"upstream_task_credits": credit}})
                    if task_id in self._credit_reservations:
                        owner, _ = self._credit_reservations[task_id]
                        self._credit_reservations[task_id] = (owner, float(task["estimated_cost"]))
            else:
                reported = None
            if state["status"] == "succeeded":
                if not state["result_urls"]:
                    errors += 1
                    if errors < 4:
                        time.sleep(3)
                        continue
                    self._fail(task_id, "RESULT_MISSING", "Dreamina completed without a video URL")
                    return
                result_urls = state["result_urls"]
                transfer_metadata = {}
                if task["model"] == "sd-2-5-480p-x":
                    self.store.update_task(task_id, {"status": "running", "progress": 90,
                                                     "upstream_result_urls": result_urls})
                    try:
                        signed_url, transfer_metadata = transfer(
                            result_urls[0], str(client.account["proxy_url"]), task_id,
                            self.settings(), aspect_ratio=task["normalized"]["aspect_ratio"])
                        result_urls = [signed_url]
                    except ResultTransferError as exc:
                        self._fail(task_id, "RESULT_TRANSFER_FAILED", str(exc))
                        return
                after = None
                try:
                    after = client.account_state(max_attempts=2, timeout_seconds=6)["available_balance"]
                except (BrowserError, KeyError, TypeError, ValueError) as exc:
                    logger.warning("IJM task %s post-submit credit read failed (%s); "
                                   "result remains available", task_id, type(exc).__name__)
                if after is not None:
                    self.store.update_account(int(task["account_id"]), {"balance": after})
                before = task.get("balance_before")
                finished_at = time.time()
                isolated = not self.store.has_account_overlap(
                    int(task["account_id"]), task_id, task["created_at"], finished_at)
                self.store.update_task(task_id, {"status": "succeeded", "progress": 100,
                    "result_urls": result_urls, "result_transfer": transfer_metadata,
                    "balance_after": after,
                    "actual_cost": reported if reported is not None else (
                        max(0, float(before) - float(after))
                        if isolated and before is not None and after is not None else None),
                    "completed_at": finished_at,
                    "cost_evidence": {"balance_isolated": isolated and before is not None and after is not None}})
                return
            if state["status"] == "failed":
                # History may first report a terminal status and only add the
                # localized failure reason a few seconds later.
                if not state["error"] and missing_failure_detail < 3:
                    missing_failure_detail += 1
                    time.sleep(2)
                    continue
                reason = state["error"] or "Dreamina video generation failed"
                upstream_code = state.get("error_code") or ""
                reason_lower = reason.lower()
                # 2052 was observed with fail_msg=OutputAudioCopyright while
                # the localized fail_starling_message was only "[none]".
                if "audio clip is too long or too short" in reason_lower:
                    code = "INPUT_AUDIO_DURATION_UNSUPPORTED"
                elif "image you uploaded" in reason_lower and "community guidelines" in reason_lower:
                    code = "INPUT_IMAGE_CONTENT_VIOLATION"
                elif "video may contain third-party content" in reason_lower:
                    code = "OUTPUT_VIDEO_COPYRIGHT"
                elif upstream_code in {"12009", "12011", "2052"}:
                    code = "CONTENT_MODERATION_FAILED"
                elif "outputaudiocopyright" in reason_lower:
                    code = "CONTENT_MODERATION_FAILED"
                elif "video editing task" in reason_lower:
                    code = "VIDEO_EDIT_CONSTRAINT"
                elif upstream_code == "99999":
                    code = "UPSTREAM_TEMPORARY"
                elif review_code := upstream_review_error(upstream_code, reason):
                    code = review_code
                else:
                    code = "UPSTREAM_FAILED"
                settle_failed_balance(self.store, task, client)
                self._fail(task_id, code, reason)
                return
            self.store.update_task(task_id, {"status": "running", "progress": max(45, task["progress"])})
            time.sleep(max(2, int(self.settings()["poll_interval_seconds"])))
        if not self._stop.is_set():
            self.store.update_task(task_id, {"status": "expired", "progress": 100,
                "error_code": "UPSTREAM_TIMEOUT", "error_message": "Dreamina task timed out; upstream run may continue",
                "completed_at": time.time()})

    def _fail(self, task_id: str, code: str, message: str) -> None:
        self.store.update_task(task_id, {"status": "failed", "progress": 100,
            "error_code": code, "error_message": message[:600], "completed_at": time.time()}, preserve_terminal=True)

    def image_response(self, task: dict[str, Any]) -> dict[str, Any]:
        task = self._refresh_canvas_result_urls(task)
        status = {"succeeded": "completed", "failed": "failed",
                  "expired": "failed"}.get(task["status"], "in_progress")
        result: dict[str, Any] = {
            "id": task["id"], "object": "response", "created_at": int(task["created_at"]),
            "status": status, "model": task["model"],
            "output": [{"id": f"ig_{task['id']}_{index}",
                        "type": "image_generation_call", "status": "completed", "url": url}
                       for index, url in enumerate(task["result_urls"])],
            "metadata": {"task_id": task["id"], "account_id": task.get("account_id")},
            "error": None,
        }
        if status == "failed":
            result["error"] = public_error(task["error_code"], task["error_message"])
        return result

    def public_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task = self._refresh_canvas_result_urls(task)
        if task["normalized"].get("kind") == "image":
            result = self.image_response(task)
            result["progress"] = task["progress"]
            result["data"] = [{"url": url} for url in task["result_urls"]]
            return result
        if (task["status"] == "succeeded" and task["model"] == "sd-2-5-480p-x"
                and task["result_urls"] and expires_soon(task.get("result_transfer") or {})):
            metadata = task["result_transfer"]
            try:
                url, refreshed = presign(metadata["bucket"], metadata["key"], metadata["size_bytes"],
                                         self.settings())
                task = self.store.update_task(task["id"], {"result_urls": [url],
                    "result_transfer": {**metadata, **refreshed}})
            except (ResultTransferError, KeyError):
                # Keep the last successful URL; a temporary presign outage
                # must not silently replace it with the 720p upstream result.
                pass
        result = {"id": task["id"], "object": "video", "created_at": task["created_at"],
                  "status": task["status"], "model": task["model"], "progress": task["progress"],
                  "data": [{"url": url} for url in task["result_urls"]],
                  "account_id": task.get("account_id")}
        if task["status"] in {"failed", "expired"}:
            result["error"] = public_error(task["error_code"], task["error_message"])
        return result
