"""Account-aware queue and durable Dreamina video task execution."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .browser import BrowserError, BrowserRejected, browser_alive, start_managed_browser, stop_managed_browser
from .browser_runtime import BrowserRuntime
from .catalog import normalize_request
from .client import DreaminaClient, DreaminaError
from .errors import public_error
from .media import MediaError
from .result_transfer import ResultTransferError, transfer, presign, expires_soon, _config
from .upload import UploadError
from .store import Store


TERMINAL = {"succeeded", "failed", "expired"}
logger = logging.getLogger(__name__)
DEFAULT_SETTINGS: dict[str, Any] = {
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


class Service:
    def __init__(self, store: Store):
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=50, thread_name_prefix="ijm-task")
        self._lock = threading.RLock()
        self._active: dict[int, int] = {}
        self._worker_slots = threading.BoundedSemaphore(50)
        self._started = False
        self._stop = threading.Event()
        self._maintenance: threading.Thread | None = None
        self.browser_runtime = BrowserRuntime(store, self.settings)
        self._browser_views: dict[int, tuple[dict[str, Any], bool, float]] = {}
        self._browser_views_lock = threading.RLock()

    def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **self.store.get_settings()}

    def public_settings(self) -> dict[str, Any]:
        values = self.settings()
        values["result_s3_center_token_configured"] = bool(values.get("result_s3_center_token"))
        values["result_s3_center_token"] = ""
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
        self._started = True
        self._maintenance = threading.Thread(target=self._maintain_accounts, daemon=True,
                                             name="ijm-account-maintenance")
        self._maintenance.start()
        for task in reversed(self.store.tasks(1000)):
            if task["status"] == "queued":
                self._pool.submit(self._run, task["id"])
            elif task["status"] in {"submitted", "running"} and task["run_id"] and task["thread_id"]:
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
                if not account["enabled"] or not int(account["cdp_port"] or 0):
                    continue
                try:
                    checked = self.check_account(int(account["id"]))
                    if checked["status"] == "login_required":
                        needs_rapid_recheck = True
                    if checked["status"] == "active" and settings["daily_checkin_enabled"]:
                        self.checkin_account(int(account["id"]))
                except (BrowserError, ValueError, KeyError):
                    needs_rapid_recheck = True
                    continue
            interval = max(30, int(settings["account_maintenance_interval_seconds"]))
            # A restarted Chrome often exposes its tab before the app can make
            # authenticated requests. Retry that state promptly instead of
            # leaving a valid account unavailable for the full interval.
            self._stop.wait(min(15, interval) if needs_rapid_recheck else interval)

    def checkin_account(self, account_id: int) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
        today = datetime.now(ZoneInfo(os.getenv("IJM_TIME_ZONE", "Asia/Taipei"))).date().isoformat()
        if account.get("last_checkin_date") == today:
            return {"already_checked_in": True, "date": today}
        account, owned = self.browser_runtime.acquire(account)
        try:
            result = DreaminaClient(account).daily_checkin()
        finally:
            self.browser_runtime.release(account, owned)
        self.store.update_account(account_id, {"last_checkin_date": today})
        return {"already_checked_in": False, "date": today,
                "received": result.get("receive_quota", 0)}

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
        estimates = [item for item in self.store.model_costs() if
                     item["model"] == ("sd-2-5" if normalized["model"] == "sd-2-5-480p-x" else normalized["model"]) and
                     item["duration"] == normalized["duration"] and
                     item["resolution"] == ("720p" if normalized["model"] == "sd-2-5-480p-x" else normalized["resolution"]) and
                     item["image_count"] == len(normalized["images"]) and
                     item["video_count"] == len(normalized["videos"]) and
                     item["audio_count"] == len(normalized["audios"])]
        if estimates:
            task = self.store.update_task(task_id, {"estimated_cost": max(item["cost"] for item in estimates)})
        self._pool.submit(self._run, task_id)
        return self.public_task(task)

    def _reserve_account(self, estimated_cost: float = 0) -> dict[str, Any] | None:
        with self._lock:
            accounts = [item for item in self.store.accounts(private=True)
                        if item["enabled"] and item["status"] == "active" and item["proxy_url"]
                        and (item["balance"] is None or float(item["balance"]) >= estimated_cost)]
            accounts.sort(key=lambda item: (-int(item["priority"]), -(float(item["balance"] or 0)), item["id"]))
            for account in accounts:
                account_id = int(account["id"])
                active = self._active.get(account_id, 0)
                if active >= int(account["max_concurrency"]):
                    continue
                self._active[account_id] = active + 1
                return account
        return None

    def _release_account(self, account_id: int) -> None:
        with self._lock:
            self._active[account_id] = max(self._active.get(account_id, 1) - 1, 0)

    def check_account(self, account_id: int, *, start_browser: bool = False) -> dict[str, Any]:
        account = self.store.account(account_id, private=True)
        if not account:
            raise KeyError(account_id)
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
        # The executor is only a waiter/dispatcher.  The configurable limit
        # applies to actual upstream work, not to the number of Python threads.
        while not self._stop.is_set():
            if not self._worker_slots.acquire(timeout=1):
                continue
            with self._lock:
                if sum(self._active.values()) < int(self.settings()["task_workers"]):
                    break
            self._worker_slots.release()
            time.sleep(1)
        else:
            return
        account = None
        deadline = time.monotonic() + int(self.settings()["task_timeout_seconds"])
        while not account and time.monotonic() < deadline and not self._stop.is_set():
            account = self._reserve_account(float(task.get("estimated_cost") or 0))
            if not account:
                time.sleep(1)
        if not account:
            self._fail(task_id, "ACCOUNT_POOL_UNAVAILABLE", "no available Dreamina account before timeout")
            self._worker_slots.release()
            return
        account_id = int(account["id"])
        owned = False
        try:
            self.store.update_task(task_id, {"account_id": account_id, "status": "preparing", "progress": 5})
            account, owned = self.browser_runtime.acquire(account)
            client = DreaminaClient(account, store=self.store)
            before = None
            try:
                before = client.account_state(max_attempts=2, timeout_seconds=6)["available_balance"]
            except (BrowserError, KeyError, TypeError, ValueError) as exc:
                # Credit lookup is only for balance accounting. The generation
                # endpoint decides whether this account can actually submit.
                logger.warning("IJM task %s account %s pre-submit credit read failed (%s); "
                               "continuing with cached balance", task_id, account_id,
                               type(exc).__name__)
            if before is not None:
                self.store.update_account(account_id, {"balance": before, "status": "active"})
            self.store.update_task(task_id, {"balance_before": before, "progress": 12})
            assets = client.upload_inputs(task["normalized"])
            self.store.update_task(task_id, {"progress": 30})
            try:
                submitted = client.submit(task["normalized"], assets)
            except BrowserRejected as exc:
                self._fail(task_id, "UPSTREAM_REJECTED", str(exc))
                return
            except BrowserError as exc:
                self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                           str(exc) + "; automatic resubmission is disabled")
                return
            self.store.update_task(task_id, {"status": "submitted", "progress": 40,
                                              "run_id": submitted["run_id"],
                                              "thread_id": submitted["thread_id"]})
            self._poll(task_id, client, deadline)
        except (BrowserError, DreaminaError, MediaError, UploadError, ValueError) as exc:
            self._fail(task_id, type(exc).__name__.upper(), str(exc))
        except Exception as exc:
            self._fail(task_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        finally:
            self.browser_runtime.release(account, owned)
            self._release_account(account_id)
            self._worker_slots.release()

    def _resume_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or not task["account_id"]:
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
            self._release_account(int(task["account_id"]))
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
                    "actual_cost": max(0, float(before) - float(after))
                        if isolated and before is not None and after is not None else None,
                    "completed_at": finished_at})
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
                # 2052 was observed with fail_msg=OutputAudioCopyright while
                # the localized fail_starling_message was only "[none]".
                if upstream_code in {"12009", "12011", "2052"}:
                    code = "CONTENT_MODERATION_FAILED"
                elif "outputaudiocopyright" in reason.lower():
                    code = "CONTENT_MODERATION_FAILED"
                elif "video editing task" in reason.lower():
                    code = "VIDEO_EDIT_CONSTRAINT"
                elif upstream_code == "99999":
                    code = "UPSTREAM_TEMPORARY"
                else:
                    code = "UPSTREAM_FAILED"
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
            "error_code": code, "error_message": message[:600], "completed_at": time.time()})

    def public_task(self, task: dict[str, Any]) -> dict[str, Any]:
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
