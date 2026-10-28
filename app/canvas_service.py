"""Durable Canvas execution; browser sessions are never generation dependencies."""

from __future__ import annotations

import logging
import math
import mimetypes
import tempfile
import time
from pathlib import Path

from .canvas_cli import CanvasCLIError
from .canvas_client import CanvasClient
from .account_policy import credit_rejected
from .cost_adapter import input_evidence, number
from .media import MediaError, _download
from .upload import _probe_media
from .result_transfer import ResultTransferError, store_original

logger = logging.getLogger(__name__)


def execution_mode(account: dict) -> str:
    return str(account.get("execution_mode") or "canvas_cli")


class CanvasServiceMixin:
    def _refresh_canvas_result_urls(self, task: dict) -> dict:
        if task.get('status') != 'succeeded':
            return task
        metadata = task.get('result_transfer') or {}
        receipts = metadata.get('items') or []
        if not receipts:
            return task
        from .result_transfer import expires_soon, presign
        if not any(expires_soon(item) for item in receipts):
            return task
        try:
            urls, refreshed = [], []
            for item in receipts:
                url, receipt = presign(item['bucket'], item['key'], item.get('size_bytes'), self.settings())
                urls.append(url)
                refreshed.append({**item, **receipt})
            return self.store.update_task(task['id'], {'result_urls': urls,
                'result_transfer': {**metadata, 'items': refreshed}})
        except (ResultTransferError, KeyError, TypeError):
            return task

    def canvas_client(self, account: dict) -> CanvasClient:
        return CanvasClient(self.canvas_cli, account)

    def begin_cli_authorization(self, account_id: int, account=None):
        manager = getattr(self, "canvas_auth", None)
        return manager.start(account_id) if manager else None

    def _cli_authorization_missing(self) -> bool:
        accounts = [a for a in self.store.accounts(private=True) if a.get("enabled")]
        return bool(accounts) and all(execution_mode(a) == "canvas_cli"
            and (a.get("cli_auth_status") != "ready" or not self.settings().get('canvas_cli_enabled')
                 or self.canvas_runtime.get('available') is False) for a in accounts)

    def _check_cli_account(self, account: dict) -> dict:
        account_id = int(account["id"])
        try:
            state = self.canvas_client(account).identity()
            identity = str(state.get("user_id") or "")
            expected = str(account.get("login_user_id") or account.get("cli_user_id") or "")
            if not expected or identity != expected:
                raise CanvasCLIError("CLI_IDENTITY_MISMATCH", "CLI account identity does not match the browser account")
            values = {"cli_auth_status": "ready", "cli_auth_error": "", "cli_user_id": identity}
            balance = number(state.get("available_balance"))
            if balance is not None:
                values["balance"] = balance
                values["balance_details"] = state.get("buckets") or {}
            if account.get("enabled") and account.get("watermark_ready") and account.get("agreement_ready"):
                values.update(status="active", last_error="")
            threshold = float(self.settings().get("low_balance_disable_threshold") or 0)
            if balance is not None and threshold > 0 and balance < threshold:
                values.update(enabled=False, status="disabled_low_balance", last_error="credit balance below threshold")
            return self.store.update_account(account_id, values)
        except CanvasCLIError as exc:
            # A transport failure is not proof that authorization was revoked.
            auth_error = any(word in exc.code.upper() for word in ("AUTH", "IDENTITY", "CREDENTIAL", "ACCOUNT_MISMATCH"))
            return self.store.update_account(account_id, {
                "status": "cli_auth_required" if auth_error else "network_error",
                "cli_auth_status": "expired" if auth_error else account.get("cli_auth_status", "pending"),
                "cli_auth_error": str(exc)[:300], "last_error": str(exc)[:300]})

    def _canvas_local_assets(self, normalized: dict, account: dict, directory: Path) -> dict:
        assets = {kind: [] for kind in ("images", "videos", "audios")}
        for kind in assets:
            for index, source in enumerate(normalized.get(kind) or []):
                if not str(source).startswith(("https://", "http://", "data:")):
                    raise MediaError("Canvas reference requires a media URL; browser asset IDs cannot be reused")
                contents, mime, filename = _download(str(source), kind,
                    str(account["proxy_url"]), allow_direct_fallback=False)
                suffix = Path(filename).suffix.lower()
                if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
                    suffix = {"images": ".png", "videos": ".mp4", "audios": ".mp3"}[kind]
                path = directory / f"{kind}-{index + 1}{suffix}"
                path.write_bytes(contents)
                meta = _probe_media(contents, suffix) if kind != "images" else {}
                assets[kind].append({"path": str(path), "mime": mime, **meta})
        return assets

    def _execute_canvas(self, task_id: str, account: dict, deadline: float) -> bool | str:
        """Return False only for a verified pre-run funding rejection."""
        task = self.store.task(task_id)
        account_id = int(account["id"])
        client = self.canvas_client(account)
        submission_started = False
        context = {}

        def persist(value):
            nonlocal context
            context = {**context, **value}
            self.store.update_task(task_id, {"execution_backend": "canvas_cli",
                                           "upstream_context": context})

        try:
            if time.monotonic() >= deadline:
                self._fail(task_id, 'CLI_PREPARATION_TIMEOUT', 'Task deadline passed before Canvas preparation; no generation was submitted')
                return True
            self.store.update_task(task_id, {"execution_backend": "canvas_cli"})
            state = client.identity()
            identity = str(state.get("user_id") or "")
            if not identity or identity != str(account.get("login_user_id") or account.get("cli_user_id") or ""):
                self.store.update_account(account_id, {"cli_auth_status": "error",
                    "cli_auth_error": "CLI identity does not match the signed-in account", "status": "cli_auth_required"})
                self._fail(task_id, "ACCOUNT_VERIFICATION_FAILED", "CLI identity does not match the signed-in account")
                return True
            before = number(state.get("available_balance"))
            if before is None:
                self._fail(task_id, "ACCOUNT_VERIFICATION_FAILED", "CLI did not return an authoritative available balance")
                return True
            self.store.update_account(account_id, {"balance": before})
            self.store.update_task(task_id, {"balance_before": before})
            normalized = dict(task["normalized"])
            normalized["_execution_backend"] = "canvas_cli"
            if task['model'] == 'sd-2-5-480p-x':
                normalized['resolution'] = '720p'
            with tempfile.TemporaryDirectory(prefix=f"ijm-canvas-{task_id}-") as directory:
                assets = self._canvas_local_assets(normalized, account, Path(directory))
                self.store.update_task(task_id, {"cost_evidence": input_evidence(assets), "progress": 15})
                prepared = client.prepare(normalized, task_id, assets, persist=persist)
                persist(prepared)
            if time.monotonic() >= deadline:
                self._fail(task_id, 'CLI_PREPARATION_TIMEOUT', 'Task deadline passed during Canvas preparation; no generation was submitted')
                return True
            quote = number(context.get("quoted_credits"))
            if quote is None:
                self._fail(task_id, "CLI_QUOTE_UNAVAILABLE", "Canvas could not establish the complete task quote")
                return True
            with self._lock:
                self.store.update_task(task_id, {"estimated_cost": quote,
                    "cost_evidence": {"canvas_quote": {"amount": quote, "source": "node.quote"},
                                      "execution_backend": "canvas_cli"}})
                self._credit_reservations[task_id] = (account_id, quote)
                funded, _ = self._submission_budget_status(account, task_id, quote, before)
                if not funded:
                    self.store.update_task(task_id, {"status": "queued", "account_id": None,
                        "progress": 0, "balance_before": None, "upstream_context": {},
                        "execution_backend": "canvas_cli", "error_code": "INSUFFICIENT_CREDITS",
                        "error_message": "Canvas quote exceeds available unreserved credits"})
                    return False
                if not (task['normalized'].get('_cost_evidence') or {}).get('canvas_budget_checked'):
                    pending = self._pending_credits(self.store.scheduling_rows(), task_id)
                    current_available, _ = self._available_credits({**account, 'balance': before}, pending)
                    candidates = self._funded_accounts(quote, set(), pending, 'canvas_cli')
                    candidates = [a for a in candidates if int(a['id']) != account_id
                        and int(a['priority']) == int(account['priority'])
                        and a.get('_available_credits') is not None
                        and current_available is not None and a['_available_credits'] < current_available
                        and max(self._active.get(int(a['id']), 0),
                            sum(owner == int(a['id']) for owner, _ in pending.values())) < int(a['max_concurrency'])]
                    self.store.update_task(task_id, {'cost_evidence': {'canvas_budget_checked': True}})
                    if candidates:
                        self.store.update_task(task_id, {'status': 'queued', 'account_id': None,
                            'progress': 0, 'balance_before': None, 'upstream_context': {},
                            'execution_backend': 'canvas_cli'})
                        return 'reselect'
                attempts = list(task.get("attempted_account_ids") or [])
                if account_id not in attempts:
                    attempts.append(account_id)
                # The durable record is committed before any paid run request.
                persist({"submission_phase": "submitting", "expected_user_id": identity})
                self.store.update_task(task_id, {"attempted_account_ids": attempts, "progress": 30})
            submission_started = True
            submitted = client.submit(context, int(math.ceil(quote)))
            persist({**submitted, "submission_phase": "submitted"})
            self.store.update_task(task_id, {"status": "submitted", "progress": 40})
            self._poll_canvas(task_id, client, deadline)
            return True
        except CanvasCLIError as exc:
            error = (getattr(exc, 'details', None) or {}).get('error') or {}
            rejected_credit = (credit_rejected(str(exc))
                or str(error.get('serviceCode') or error.get('code')) == '1006'
                or str(error.get('reason') or '').lower() in {'insufficient_credits', 'insufficient_balance'})
            if rejected_credit and not getattr(exc, 'outcome_unknown', False):
                return 'retry' if self._handle_credit_rejection(task_id, account_id, str(exc)) else True
            if submission_started and getattr(exc, "outcome_unknown", True):
                # A lost receipt is reconciled by original operation identity.
                self.store.update_task(task_id, {"status": "submitted", "progress": 40,
                    "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                    "error_message": "Canvas submission receipt unavailable; querying original operation"})
                self._poll_canvas(task_id, client, deadline)
            else:
                self._fail(task_id, exc.code, str(exc))
            return True
        except Exception as exc:
            code = "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN" if submission_started else (
                "MEDIA_DOWNLOAD_FAILED" if isinstance(exc, MediaError) else "CLI_PREPARATION_FAILED")
            self._fail(task_id, code, f"{type(exc).__name__}: Canvas {'submission outcome unknown' if submission_started else 'preparation failed'}")
            logger.warning("Canvas task %s failed in %s (%s)", task_id,
                           "submission" if submission_started else "preparation", type(exc).__name__)
            return True

    def _canvas_settlement(self, task: dict, client) -> dict:
        values = {}
        try:
            state = client.identity()
            if str(state.get("user_id") or "") != str((task.get("upstream_context") or {}).get("expected_user_id") or ""):
                return values
            after = number(state.get("available_balance"))
            before = number(task.get("balance_before"))
            if after is not None:
                values["balance_after"] = after
                self.store.update_account(int(task["account_id"]), {"balance": after})
            isolated = not self.store.has_account_overlap(int(task["account_id"]), task["id"],
                                                          task["created_at"], time.time())
            quote = number(task.get("estimated_cost"))
            delta = before - after if before is not None and after is not None else None
            if isolated and delta is not None and delta >= 0 and quote is not None and delta <= quote:
                values.update(actual_cost=delta, cost_evidence={"balance_isolated": True})
        except (CanvasCLIError, ValueError, KeyError, TypeError):
            pass
        return values

    def _poll_canvas(self, task_id: str, client, deadline: float) -> None:
        errors = 0
        transfer_errors = 0
        while not self._stop.is_set() and time.monotonic() < deadline:
            task = self.store.task(task_id)
            if not task or (task["status"] in {"failed", "expired", "succeeded"}
                            and task.get("error_code") != "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"):
                return
            context = task.get("upstream_context") or {}
            try:
                state = client.poll(context)
                errors = 0
            except CanvasCLIError as exc:
                errors += 1
                self.store.update_task(task_id, {"error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                    "error_message": "Canvas operation query interrupted; original submission retained"})
                self._stop.wait(min(60, 2 ** min(errors, 6)))
                continue
            status = state.get("status")
            facts = [item for item in (state.get('credit_facts') or [])
                if isinstance(item, dict) and item.get('source') == 'precharge'
                and item.get('settled') is False]
            if facts:
                self.store.update_task(task_id, {'cost_evidence': {'canvas_resource_snapshots': facts}})
            if status == "succeeded":
                urls = state.get("result_urls") or []
                metadata = {}
                if state.get('result_paths'):
                    try:
                        files = state.get('result_files') or state['result_paths']
                        urls, receipts = [], []
                        base = self.canvas_cli.account_directory(client.account).resolve()
                        for index, raw in enumerate(files):
                            path = Path(raw.get('path') if isinstance(raw, dict) else raw).resolve()
                            if not path.is_relative_to(base) or path.is_symlink():
                                raise ResultTransferError('Canvas result path does not belong to its account')
                            url, receipt = store_original(path, task_id, self.settings(), index=index,
                                content_type=(raw.get('mime') if isinstance(raw, dict) else None)
                                    or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
                            urls.append(url)
                            receipts.append(receipt)
                        metadata = {'items': receipts, 'original_bytes': True}
                    except (ResultTransferError, OSError, TypeError) as exc:
                        transfer_errors += 1
                        self.store.update_task(task_id, {'progress': 95, 'error_code': 'RESULT_TRANSFER_FAILED',
                            'error_message': 'Canvas generation complete; original result storage is being retried'})
                        if transfer_errors >= 8:
                            self.store.update_task(task_id, self._canvas_settlement(task, client))
                            self._fail(task_id, 'RESULT_TRANSFER_FAILED', 'Canvas result storage could not complete; generation was not resubmitted')
                            return
                        self._stop.wait(min(60, 2 ** min(transfer_errors, 6)))
                        continue
                if not urls:
                    self._fail(task_id, "RESULT_MISSING", "Canvas completed without downloadable result URLs")
                    return
                values = {"status": "succeeded", "progress": 100, "error_code": "", "error_message": "",
                          "result_urls": urls, 'result_transfer': metadata,
                          "completed_at": time.time(), **self._canvas_settlement(task, client)}
                if task["model"] == "sd-2-5-480p-x":
                    from .result_transfer import transfer
                    signed, metadata = transfer(urls[0], str(client.account["proxy_url"]), task_id,
                        self.settings(), aspect_ratio=task["normalized"]["aspect_ratio"])
                    values.update(result_urls=[signed], upstream_result_urls=urls, result_transfer=metadata)
                self.store.update_task(task_id, values)
                for raw in state.get('result_files') or state.get('result_paths') or []:
                    try:
                        path = Path(raw.get('path') if isinstance(raw, dict) else raw).resolve()
                        if path.is_relative_to(self.canvas_cli.account_directory(client.account).resolve()):
                            path.unlink(missing_ok=True)
                    except (OSError, TypeError):
                        pass
                return
            if status in {"failed", "cancelled", "canceled"}:
                self.store.update_task(task_id, self._canvas_settlement(task, client))
                self._fail(task_id, state.get("error_code") or "UPSTREAM_FAILED",
                    state.get("error_message") or "Canvas generation failed")
                return
            self.store.update_task(task_id, {"status": "running",
                "progress": max(45, min(95, int(number(state.get("progress")) or 45))),
                **({'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                    'error_message': state.get('error_message') or 'Canvas original submission is being reconciled'}
                   if status == 'unknown' else {})})
            self._stop.wait(max(2, int(self.settings()["poll_interval_seconds"])))
        if not self._stop.is_set():
            self.store.update_task(task_id, {"status": "expired", "progress": 100,
                "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                "error_message": "Canvas task observation timed out; original upstream operation may continue",
                "completed_at": time.time()})

    def _resume_canvas_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or not task.get("account_id"):
            return
        account = self.store.account(int(task["account_id"]), private=True)
        if not account:
            self._fail(task_id, "ACCOUNT_MISSING", "Canvas task account was removed")
            return
        self._worker_slots.acquire()
        with self._lock:
            self._active[int(account["id"])] = self._active.get(int(account["id"]), 0) + 1
        try:
            self._poll_canvas(task_id, self.canvas_client(account),
                time.monotonic() + int(self.settings()["task_timeout_seconds"]))
        except Exception as exc:
            self._fail(task_id, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", f"{type(exc).__name__}: Canvas recovery interrupted")
        finally:
            self._release_account(int(account["id"]), task_id)
            self._worker_slots.release()
