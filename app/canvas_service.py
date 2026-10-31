"""Durable Canvas execution; browser sessions are never generation dependencies."""

from __future__ import annotations

import logging
import math
import mimetypes
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .canvas_cli import TRANSPORT_DIAGNOSTIC_COMMANDS, CanvasCLIError, sanitize
from .canvas_client import (CanvasClient, identity_network_error, preparation_retryable_failure,
                            public_image_source_url, submission_evidence_blocks_fallback)
from .canvas_timing import PreparationTiming, archived_attempts, INITIAL_ADMISSION_UNSET
from .canvas_quote_hints import new_hint
from .account_policy import credit_rejected
from .image_model_routing import plan_image_execution, recorded_image_route
from .cost_adapter import input_evidence, number
from .media import MediaError, _DIRECT_FIRST_MEDIA_HOSTS, _download, media_error_diagnostic
from .upload import _probe_media
from .result_transfer import ResultTransferError, store_original

logger = logging.getLogger(__name__)
ADMISSION_AUDIT_FIELDS = frozenset({'previous_submissions', 'recovery_resolution',
    'preflight_failures', 'model_eligibility_exclusions', 'preparation_admission_at',
    'preparation_attempts', 'preparation_attempt_count'})


def execution_mode(account: dict) -> str:
    return str(account.get("execution_mode") or "canvas_cli")


class CanvasServiceMixin:
    def _initial_preparation_admission(self, task):
        context = task.get('upstream_context') or {}
        if 'preparation_admission_at' in context:
            return number(context['preparation_admission_at'])
        if task.get('attempted_account_ids') or self._admission_history(task) or context.get('previous_submissions'):
            return None  # A legacy/recovered attempt has no reliable first binding timestamp.
        return INITIAL_ADMISSION_UNSET

    def _remember_preparation_admission(self, task_id, admitted_at):
        """Optional audit shared by browser and CLI; no intent/receipt writes."""
        try:
            current = self.store.task(task_id) or {}
            if 'preparation_admission_at' not in (current.get('upstream_context') or {}):
                self._canvas_context(task_id, {'preparation_admission_at': admitted_at})
        except Exception as exc:
            logger.warning('Canvas preparation audit unavailable (%s)', type(exc).__name__)

    @staticmethod
    def _canvas_task_terminated(task: dict | None) -> bool:
        if not task:
            return True
        context = task.get('upstream_context') or {}
        return ('manual_termination' in context or task.get('status') in {
            'failed', 'expired', 'succeeded', 'cancelled'}
            and task.get('error_code') != 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN')

    def _update_canvas_task(self, task_id: str, values: dict) -> dict:
        return self.store.update_task(task_id, values, preserve_terminal=True)

    def _fail_canvas_task(self, task_id: str, code: str, message: str) -> dict:
        return self._update_canvas_task(task_id, {'status': 'failed', 'progress': 100,
            'error_code': code, 'error_message': message[:600], 'completed_at': time.time()})

    def _canvas_context(self, task_id: str, changes: dict | None = None,
                        remove: tuple[str, ...] = ()) -> dict:
        with self._lock:
            task = self.store.task(task_id) or {}
            if self._canvas_task_terminated(task):
                return dict(task.get('upstream_context') or {})
            context = dict(task.get('upstream_context') or {})
            context.update(changes or {})
            for key in remove:
                context.pop(key, None)
            result = self._update_canvas_task(task_id, {'upstream_context': context})
            return dict(result.get('upstream_context') or {})

    @staticmethod
    def _canvas_dispatch_evidence(task: dict) -> bool:
        """Any prior/uncertain dispatch evidence vetoes a new no-send proof."""
        context = task.get('upstream_context') or {}
        error = context.get('submit_error') or {}
        if (task.get('run_id') or task.get('thread_id')
                or task.get('result_urls') or task.get('upstream_result_urls')
                or context.get('resource_ids') or context.get('dispatch_started_at') or context.get('accepted')
                or context.get('dispatch_started') is True or error.get('dispatch_started') is True
                or context.get('operation_ref') or context.get('operationRef') or context.get('operation_id')
                or context.get('phase') in {'accepted', 'unknown', 'submission_unconfirmed'}
                or context.get('submission_phase') not in {None, '', 'confirming'}
                or context.get('financial_outcome') in {'unknown', 'unconfirmed'}
                or context.get('reconciliation_closed') or context.get('reconciliation_paused')
                or context.get('automatic_resubmission_blocked')
                or context.get('previous_submissions')
                or error.get('outcome_unknown') is True):
            return True
        timings = [context.get('preparation_timing')]
        timings.extend(item.get('timing') for item in context.get('preparation_attempts') or []
                       if isinstance(item, dict))
        for timing in timings:
            if not isinstance(timing, dict):
                continue
            commands = timing.get('commands') or {}
            run = commands.get('node run')
            if run is not None and (not isinstance(run, dict) or run.get('count') != 0):
                return True
            current = timing.get('current_command') or {}
            if current.get('command_pair') == ['node', 'run']:
                return True
        return False

    @staticmethod
    def _canvas_pre_dispatch_facts(task: dict, exc: CanvasCLIError, submission_started: bool) -> dict:
        """Positive call-stage evidence, not an inference from missing outputs."""
        if (exc.outcome_unknown or task.get('status') not in {'queued', 'preparing'}
                or CanvasServiceMixin._canvas_dispatch_evidence(task)):
            return {}
        if submission_started and (exc.details.get('dispatch_started') is not False
                or exc.details.get('phase') != 'confirming'):
            return {}
        return {'generation_request_sent': False,
                'financial_outcome': 'not_charged_this_invocation',
                'automatic_resubmission': False,
                'stage': 'confirming' if submission_started else 'preparation'}

    @staticmethod
    def _canvas_public_pre_dispatch_facts(task: dict) -> dict:
        """Expose only the new bounded proof; preserve all stored financial data."""
        context = task.get('upstream_context') or {}
        facts = context.get('pre_dispatch_failure')
        if (task.get('status') not in {'failed', 'expired'}
                or task.get('error_code') != 'CLI_PREPARATION_TRANSIENT_FAILED'
                or not isinstance(facts, dict) or facts.get('generation_request_sent') is not False
                or facts.get('financial_outcome') != 'not_charged_this_invocation'
                or facts.get('automatic_resubmission') is not False
                or facts.get('stage') not in {'preparation', 'confirming'}
                or CanvasServiceMixin._canvas_dispatch_evidence(task)):
            return {}
        return {key: facts[key] for key in ('generation_request_sent', 'financial_outcome',
                                            'automatic_resubmission')}

    @staticmethod
    def _canvas_retryable_submission_failure(task: dict) -> dict:
        """Caller may fail over after bounded absence; this is not no-send proof."""
        context = task.get('upstream_context') or {}
        if not isinstance(context, dict):
            return {}
        original = context.get('submit_error') or {}
        query = context.get('last_query_error') or {}
        count = context.get('consecutive_submission_absent')
        if (task.get('execution_backend') != 'canvas_cli'
                or task.get('status') not in {'failed', 'expired'}
                or task.get('error_code') not in {'CLI_SUBMISSION_UNCONFIRMED', 'CLI_SUBMISSION_RETRYABLE'}
                or context.get('reconciliation_closed') is not True
                or context.get('reconciliation_close_reason') != 'operation_submission_absent'
                or type(count) is not int or count < 3
                or not isinstance(query, dict) or query.get('code') != 'cli.operation_submission_absent'
                or not isinstance(original, dict) or original.get('phase') != 'run_dispatching'
                or original.get('dispatch_started') is not True or original.get('outcome_unknown') is not True
                or context.get('submission_phase') != 'run_dispatching'
                or context.get('financial_outcome') != 'unconfirmed'
                or type(task.get('account_id')) is not int or task['account_id'] < 1
                or context.get('account_id') != task.get('account_id')
                or any(not isinstance(context.get(key), str) or not context[key]
                       for key in ('profile', 'project_id', 'node_id', 'submit_id'))):
            return {}
        # Only the observed service/decode families are eligible. In particular,
        # an arbitrary unknown, auth error or policy rejection is not a retry.
        error = CanvasCLIError(str(original.get('code') or ''), '', details={'error': original})
        if not preparation_retryable_failure(error):
            return {}
        normalized = task.get('normalized') or {}
        if not isinstance(normalized, dict):
            return {}
        normalized_context = normalized.get('_execution') or {}
        if not isinstance(normalized_context, dict):
            return {}
        for value in (context, normalized_context):
            if (value.get('accepted') or submission_evidence_blocks_fallback(value)
                    or value.get('resource_ids') or value.get('resourceIds')
                    or isinstance(value.get('resources'), list) and bool(value['resources'])
                    or value.get('phase') in {'accepted', 'submitted', 'running', 'succeeded', 'completed'}
                    or value.get('submission_phase') in {'accepted', 'submitted', 'running'}
                    or any(value.get(key) for key in ('run_data', 'run_receipt', 'submission', 'receipt',
                        'operation_ref', 'operationRef', 'operation_id', 'last_operation', 'result_urls', 'result_files'))):
                return {}
        if (task.get('run_id') or task.get('thread_id') or task.get('result_urls')
                or task.get('upstream_result_urls') or context.get('previous_submissions')
                or context.get('manual_termination')):
            return {}
        if context.get('task_id') not in (None, task.get('id')):
            return {}
        for key in ('account_id', 'profile', 'project_id', 'node_id', 'submit_id', 'expected_user_id'):
            if key in normalized_context and normalized_context[key] != context.get(key):
                return {}
        timings = [context.get('preparation_timing')]
        for attempt in context.get('preparation_attempts') or []:
            if not isinstance(attempt, dict) or attempt.get('reason') == 'accepted':
                return {}
            timings.append(attempt.get('timing'))
        for timing in timings:
            if isinstance(timing, dict):
                commands = timing.get('commands') or {}
                if not isinstance(commands, dict):
                    return {}
                run = commands.get('node run')
                if run is not None and (not isinstance(run, dict) or type(run.get('count')) is not int or run['count'] > 1):
                    return {}
        return {'may_have_been_accepted': True, 'financial_outcome': 'unconfirmed',
                'automatic_resubmission': False, 'fallback_allowed': True}

    @staticmethod
    def _canvas_diagnostic(error, phase: str) -> dict:
        """Keep useful scalar evidence, never a raw CLI envelope or credential."""
        details = getattr(error, 'details', None) or {}
        details = details if isinstance(details, dict) else {}
        source = details.get('error')
        source = source if isinstance(source, dict) else {}
        values = {key: source[key] for key in ('code', 'message', 'class', 'reason', 'serviceCode',
            'traceId', 'exit_code', 'dispatch_started', 'outcome_unknown', 'requiredAction', 'retryable') if key in source}
        values.update(code=getattr(error, 'code', type(error).__name__), message=str(error), phase=phase)
        exit_code = getattr(error, 'exit_code', None)
        if exit_code is not None:
            values['exit_code'] = exit_code
        if hasattr(error, 'outcome_unknown'):
            values['outcome_unknown'] = error.outcome_unknown
        for key in ('dispatch_started', 'outcome_unknown'):
            if key in details:
                values[key] = details[key]
        diagnostic = {key: sanitize(value)[:600] if isinstance(value, str) else value
            for key, value in values.items() if isinstance(value, (str, int, float, bool)) or value is None}
        diagnostic['at'] = time.time()
        transport = details.get('transport_diagnostic')
        if isinstance(transport, dict):
            safe_transport = {}
            pair = transport.get('command_pair')
            if (isinstance(pair, (list, tuple)) and len(pair) == 2
                    and all(isinstance(value, str) for value in pair)
                    and tuple(pair) in TRANSPORT_DIAGNOSTIC_COMMANDS):
                safe_transport['command_pair'] = list(pair)
            for name in ('stdout', 'stderr'):
                length = transport.get(name + '_bytes')
                digest = transport.get(name + '_sha256')
                if isinstance(length, int) and not isinstance(length, bool) and length >= 0:
                    safe_transport[name + '_bytes'] = length
                if (isinstance(digest, str) and len(digest) == 64
                        and all(char in '0123456789abcdef' for char in digest)):
                    safe_transport[name + '_sha256'] = digest
            if safe_transport:
                diagnostic['transport_diagnostic'] = safe_transport
        return diagnostic

    def _canvas_observation_deadline(self, task_id: str) -> float:
        task = self.store.task(task_id) or {}
        context = task.get('upstream_context') or {}
        deadline = number(context.get('observation_deadline'))
        if deadline is None:
            # Legacy records have no reliable submission timestamp. A restart
            # must not grant another full observation window to an empty run.
            resolution = context.get('recovery_resolution') or {}
            started = number(resolution.get('attempt_started_at')) if isinstance(resolution, dict) else None
            started = number(task.get('created_at')) if started is None else started
            started = time.time() if started is None else started
            deadline = started + int(self.settings()['task_timeout_seconds'])
            self._canvas_context(task_id, {'observation_started_at': started,
                                          'observation_deadline': deadline})
        return deadline

    def _canvas_pending_message(self, task_id: str, fallback: str) -> str:
        context = (self.store.task(task_id) or {}).get('upstream_context') or {}
        original = context.get('submit_error') or {}
        original = original if isinstance(original, dict) else {}
        return str(original.get('message') or fallback)[:600]

    @staticmethod
    def _canvas_closed_diagnostic(context: dict) -> dict:
        """Public-safe enums only; never copy prose, request IDs or raw bodies."""
        source = context.get('submit_error')
        source = source if isinstance(source, dict) else {}
        diagnostic = {}
        for key in ('code', 'phase', 'class', 'reason', 'requiredAction'):
            value = source.get(key)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value):
                diagnostic[key] = value
        exit_code = source.get('exit_code')
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and -255 <= exit_code <= 255:
            diagnostic['exit_code'] = exit_code
        for key in ('dispatch_started', 'outcome_unknown', 'retryable'):
            if isinstance(source.get(key), bool):
                diagnostic[key] = source[key]
        return diagnostic

    def _park_canvas_task(self, task_id: str, reason: str, message: str | None = None) -> dict:
        task = self.store.task(task_id) or {}
        if self._canvas_task_terminated(task):
            return task
        context = task.get('upstream_context') or {}
        self._canvas_context(task_id, {'reconciliation_paused': True,
            'reconciliation_pause_reason': reason,
            'reconciliation_paused_at': context.get('reconciliation_paused_at') or time.time()})
        return self._update_canvas_task(task_id, {'status': 'submitted',
            'progress': min(40, int(task.get('progress') or 40)), 'completed_at': None,
            'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
            'error_message': message or self._canvas_pending_message(task_id,
                'Canvas submission requires verification; original operation and credit hold retained')})

    def _close_unconfirmed_canvas_task(self, task_id: str, reason: str) -> dict:
        task = self.store.task(task_id) or {}
        if self._canvas_task_terminated(task):
            return task
        original = self._canvas_closed_diagnostic(task.get('upstream_context') or {})
        message = 'Canvas submission could not be confirmed within the bounded lookup window; task failed without resubmission'
        if original:
            labels = [('code', 'code'), ('phase', 'phase'), ('exit_code', 'exit'),
                      ('class', 'class'), ('reason', 'reason'), ('requiredAction', 'action')]
            summary = '; '.join(f'{label}={original[key]}' for key, label in labels if key in original)
            if summary:
                message += f' (original CLI: {summary})'
        context = self._canvas_context(task_id, {'reconciliation_closed': True,
            'reconciliation_close_reason': reason, 'reconciliation_closed_at': time.time(),
            'reconciliation_paused': False, 'financial_outcome': 'unconfirmed',
            'closed_submission_diagnostic': original},
            remove=('reconciliation_pause_reason', 'reconciliation_paused_at'))
        candidate = {**task, 'status': 'failed', 'error_code': 'CLI_SUBMISSION_UNCONFIRMED',
                     'upstream_context': context}
        retryable = self._canvas_retryable_submission_failure(candidate)
        return self._update_canvas_task(task_id, {'status': 'failed', 'progress': 100,
            'completed_at': time.time(), 'error_code': 'CLI_SUBMISSION_RETRYABLE' if retryable else 'CLI_SUBMISSION_UNCONFIRMED',
            'error_message': message[:600],
            'upstream_context': context})

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

    @staticmethod
    def _canvas_direct_media_allowed(source: str) -> bool:
        """Reuse only the browser's existing fixed public-material policy."""
        try:
            parsed = urlsplit(source)
            return (parsed.scheme == "https" and parsed.hostname in _DIRECT_FIRST_MEDIA_HOSTS
                    and parsed.username is None and parsed.password is None and parsed.port in {None, 443})
        except (TypeError, ValueError, AttributeError):
            return False

    def _canvas_local_assets(self, normalized: dict, account: dict, directory: Path, *,
                             deadline: float | None = None) -> dict:
        assets = {kind: [] for kind in ("images", "videos", "audios")}
        for kind in assets:
            for index, source in enumerate(normalized.get(kind) or []):
                if kind == 'images' and str(source).startswith(('https://', 'http://')):
                    # The pinned CLI imports public image URLs server-side. Its
                    # request still uses the selected account proxy transport.
                    assets[kind].append({'source_url': public_image_source_url(source)})
                    continue
                try:
                    if not str(source).startswith(("https://", "http://", "data:")):
                        raise MediaError("Canvas reference requires a media URL; browser asset IDs cannot be reused",
                                         category="invalid_source", attempt_count=0)
                    contents, mime, filename = _download(str(source), kind,
                        str(account["proxy_url"]),
                        allow_direct_fallback=self._canvas_direct_media_allowed(str(source)), deadline=deadline)
                except MediaError as exc:
                    exc.diagnostic.update(media_kind=kind, media_index=index + 1)
                    raise
                suffix = Path(filename).suffix.lower()
                if not suffix or len(suffix) > 12 or not suffix[1:].isalnum():
                    suffix = {"images": ".png", "videos": ".mp4", "audios": ".mp3"}[kind]
                path = directory / f"{kind}-{index + 1}{suffix}"
                path.write_bytes(contents)
                meta = _probe_media(contents, suffix) if kind != "images" else {}
                assets[kind].append({"path": str(path), "mime": mime, **meta})
        return assets

    @staticmethod
    def _canvas_identity_network_retry(task: dict, exc: CanvasCLIError, deadline: float) -> bool:
        """Only a read-only identity failure can safely change this account."""
        if time.monotonic() >= deadline or exc.outcome_unknown or task.get('run_id') or task.get('thread_id'):
            return False
        if (task.get('normalized') or {}).get('_execution'):
            return False
        context = task.get('upstream_context') or {}
        if any(key not in ADMISSION_AUDIT_FIELDS for key in context):
            return False
        return identity_network_error(exc)

    def _canvas_model_ineligible(self, task: dict, exc: CanvasCLIError, deadline: float) -> bool:
        """Only a valid account catalog with no requested model is ineligible."""
        route = recorded_image_route(task)
        allowed = ADMISSION_AUDIT_FIELDS | ({'image_model_route'} if route else set())
        if (time.monotonic() >= deadline or exc.outcome_unknown
                or exc.code != 'CLI_CAPABILITY_UNAVAILABLE'
                or not self._unaccepted_admission(task)
                or any(key not in allowed for key in task.get('upstream_context') or {})):
            return False
        error = (getattr(exc, 'details', None) or {}).get('error') or {}
        return (isinstance(error, dict) and error.get('scope') == 'account' and error.get('reason') == 'model_not_in_catalog'
            and error.get('requested_model') == (route['effective_model'] if route else task.get('model'))
            and error.get('kind') == ('image' if task['normalized'].get('kind') == 'image' else 'video'))

    def _execute_canvas(self, task_id: str, account: dict, deadline: float, *,
                        initial_admission_at=INITIAL_ADMISSION_UNSET) -> bool | str:
        """Return False only for a verified pre-run funding rejection."""
        task = self.store.task(task_id)
        if self._canvas_task_terminated(task):
            return True
        prior_dispatch_evidence = self._canvas_dispatch_evidence(task)
        account_id = int(account["id"])
        client = self.canvas_client(account)
        submission_started = False
        identity_pending = True
        model_pending = False
        context = {key: value for key, value in (task.get('upstream_context') or {}).items()
                   if key in ADMISSION_AUDIT_FIELDS}
        resolution = context.get('recovery_resolution') or {}
        attempt_started = number(resolution.get('attempt_started_at')) if isinstance(resolution, dict) else None
        if attempt_started is not None:
            deadline = min(deadline, time.monotonic() + max(0,
                attempt_started + int(self.settings()['task_timeout_seconds']) - time.time()))
        preflight_deadlines = [value for entry in self._admission_history(task)
            if isinstance(entry, dict) and (value := number(entry.get('task_deadline'))) is not None]
        if preflight_deadlines:
            deadline = min(deadline, time.monotonic() + max(0, min(preflight_deadlines) - time.time()))

        def timing_changed(snapshot):
            # Admission failures remain inside their existing audit entries.
            # Never put a new top-level key on a task requeued to another account.
            current = self.store.task(task_id) or {}
            if (context.get('backend') == 'canvas_cli' and current.get('account_id') == account_id
                    and current.get('status') in {'preparing', 'submitted', 'running'}
                    and (current.get('upstream_context') or {}).get('backend') == 'canvas_cli'
                    and not self._canvas_task_terminated(current)):
                self._canvas_context(task_id, {'preparation_timing': snapshot})

        if initial_admission_at is INITIAL_ADMISSION_UNSET:
            initial_admission_at = self._initial_preparation_admission(task)
        timing = PreparationTiming(task_id, queued_at=task.get('created_at'),
            first_admission_at=context.get('preparation_admission_at', initial_admission_at), on_change=timing_changed)
        context.setdefault('preparation_admission_at', timing.first_admission_at)

        def persist_timing_audit(value):
            try:
                self._canvas_context(task_id, value)
            except Exception as exc:
                logger.warning('Canvas preparation audit unavailable (%s)', type(exc).__name__)

        persist_timing_audit({'preparation_admission_at': context['preparation_admission_at']})
        attempt_archived = False

        def finish_attempt(reason):
            nonlocal attempt_archived
            timing.finish()
            if not attempt_archived:
                try:
                    current = self.store.task(task_id) or {}
                    if not self._canvas_task_terminated(current):
                        audit = archived_attempts(current.get('upstream_context') or {}, timing.snapshot(), account_id, reason)
                        context.update(audit)
                        persist_timing_audit(audit)
                except Exception as exc:
                    logger.warning('Canvas preparation audit unavailable (%s)', type(exc).__name__)
                finally:
                    attempt_archived = True

        if isinstance(client, CanvasClient):
            client.preparation_timing = timing

        def fail_preparation(code, message):
            finish_attempt('preparation_failed')
            self._canvas_context(task_id, {'preparation_timing': timing.snapshot()})
            return self._fail_canvas_task(task_id, code, message)

        def persist(value):
            nonlocal context
            context = {**context, **value}
            # A submit receipt may contain an older context copy from before
            # finish_attempt archived the current attempt. Keep the durable audit.
            durable = (self.store.task(task_id) or {}).get('upstream_context') or {}
            for key in ('preparation_admission_at', 'preparation_attempts', 'preparation_attempt_count'):
                if key in durable:
                    context[key] = durable[key]
            context['preparation_timing'] = timing.snapshot()
            old_error = ((self.store.task(task_id) or {}).get('upstream_context') or {}).get('submit_error')
            if old_error:
                context['submit_error'] = old_error
            saved = self._update_canvas_task(task_id, {"execution_backend": "canvas_cli",
                                             "upstream_context": context})
            if self._canvas_task_terminated(saved):
                raise CanvasCLIError('CLI_TASK_TERMINATED',
                    'Task was terminated; no further Canvas generation may be dispatched')

        try:
            if time.monotonic() >= deadline:
                fail_preparation('CLI_PREPARATION_TIMEOUT', 'Task deadline passed before Canvas preparation; no generation was submitted')
                return True
            self._update_canvas_task(task_id, {"execution_backend": "canvas_cli"})
            with timing.phase('identity'):
                state = client.identity(deadline=deadline)
            identity_pending = False
            identity = str(state.get("user_id") or "")
            if not identity or identity != str(account.get("login_user_id") or account.get("cli_user_id") or ""):
                self.store.update_account(account_id, {"cli_auth_status": "error",
                    "cli_auth_error": "CLI identity does not match the signed-in account", "status": "cli_auth_required"})
                fail_preparation("ACCOUNT_VERIFICATION_FAILED", "CLI identity does not match the signed-in account")
                return True
            fresh_before = number(state.get("available_balance"))
            budget_balance = fresh_before if fresh_before is not None else number(account.get('balance'))
            if budget_balance is None:
                fail_preparation("ACCOUNT_VERIFICATION_FAILED", "CLI account has neither a fresh nor a cached available balance for its budget")
                return True
            if fresh_before is not None:
                self.store.update_account(account_id, {"balance": fresh_before})
            # Cached credits can admit a quoted task, but cannot prove spend.
            self._update_canvas_task(task_id, {"balance_before": fresh_before})
            normalized = dict(task["normalized"])
            normalized["_execution_backend"] = "canvas_cli"
            if normalized.get('kind') == 'image':
                try:
                    normalized, image_route = plan_image_execution(task,
                        {**account, 'balance': budget_balance}, self.settings())
                except ValueError as exc:
                    raise CanvasCLIError('CLI_CAPABILITY_UNAVAILABLE', str(exc)) from None
                normalized['_execution_backend'] = 'canvas_cli'
                if image_route is not None:
                    context['image_model_route'] = image_route
                    self._canvas_context(task_id, {'image_model_route': image_route})
                    if self._canvas_task_terminated(self.store.task(task_id)):
                        return True
            if task['model'] == 'sd-2-5-480p-x':
                normalized['resolution'] = '720p'
            model_pending = True
            with timing.phase('model_preflight'):
                client.preflight_model(normalized, deadline=deadline)
            model_pending = False
            with tempfile.TemporaryDirectory(prefix=f"ijm-canvas-{task_id}-") as directory:
                with timing.phase('material_prepare'):
                    assets = self._canvas_local_assets(normalized, account, Path(directory), deadline=deadline)
                self._update_canvas_task(task_id, {"cost_evidence": input_evidence(assets), "progress": 15})
                prepared = client.prepare(normalized, task_id, assets, persist=persist, deadline=deadline)
                persist(prepared)
            if time.monotonic() >= deadline:
                fail_preparation('CLI_PREPARATION_TIMEOUT', 'Task deadline passed during Canvas preparation; no generation was submitted')
                return True
            quote = number(context.get("quoted_credits"))
            if quote is None:
                fail_preparation("CLI_QUOTE_UNAVAILABLE", "Canvas could not establish the complete task quote")
                return True
            try:
                hint = new_hint(account, self.canvas_cli.data_dir,
                    (self.store.task(task_id) or task)['normalized'], context, identity)
                if hint is not None:
                    self.store.save_canvas_quote_hint(hint)
            except Exception as exc:
                logger.warning('Canvas scheduling quote cache unavailable (%s)', type(exc).__name__)
            ceiling = number(normalized.get('_canvas_credit_ceiling'))
            if ceiling is not None and quote > ceiling:
                fail_preparation('CLI_CREDIT_CEILING_EXCEEDED', 'Canvas quote exceeds the explicitly authorized recovery credit ceiling')
                return True
            with self._lock:
                self._update_canvas_task(task_id, {"estimated_cost": quote,
                    "cost_evidence": {"canvas_quote": {"amount": quote, "source": "node.quote"},
                                      "execution_backend": "canvas_cli"}})
                self._credit_reservations[task_id] = (account_id, quote)
            credit_wait = timing.begin_phase('credit_wait')
            while not self._stop.is_set():
                if self._canvas_task_terminated(self.store.task(task_id)):
                    return True
                deadline = self._admission_deadline(task_id, deadline)
                if time.monotonic() >= deadline:
                    fail_preparation('CLI_PREPARATION_TIMEOUT',
                        'Task deadline passed while waiting for Canvas credits; no generation was submitted')
                    return True
                with self._lock:
                    funded, temporary_hold = self._submission_budget_status(account, task_id, quote, budget_balance)
                    current_balance = (self.store.account(account_id, private=True) or {}).get('balance')
                    raw_balances = [value for raw in (budget_balance, current_balance)
                                    if (value := number(raw)) is not None]
                    raw_funded = bool(raw_balances) and min(raw_balances) >= quote
                    if not funded and (not temporary_hold or quote <= 0 or not raw_funded):
                        finish_attempt('insufficient_credits')
                        return False if self._requeue_unaccepted_task(task_id,
                            error_code='INSUFFICIENT_CREDITS',
                            error_message='Canvas quote exceeds available unreserved credits') else True
                    if funded:
                        if not (task['normalized'].get('_cost_evidence') or {}).get('canvas_budget_checked'):
                            pending = self._pending_credits(self.store.scheduling_rows(), task_id)
                            current_available, _ = self._available_credits({**account, 'balance': budget_balance}, pending)
                            candidates = self._funded_accounts(quote, set(task.get('attempted_account_ids') or [])
                                | self._model_excluded_accounts(task), pending,
                                self._required_execution_backend(task) or None, task=self.store.task(task_id))
                            candidates = [a for a in candidates if int(a['id']) != account_id
                                and int(a['priority']) == int(account['priority'])
                                and a.get('_available_credits') is not None
                                and current_available is not None and a['_available_credits'] < current_available
                                and max(self._active.get(int(a['id']), 0),
                                    sum(owner == int(a['id']) for owner, _ in pending.values())) < int(a['max_concurrency'])]
                            self._update_canvas_task(task_id, {'cost_evidence': {'canvas_budget_checked': True}})
                            if candidates:
                                finish_attempt('best_fit_reselect')
                                return 'reselect' if self._requeue_unaccepted_task(task_id) else True
                        attempts = list(task.get("attempted_account_ids") or [])
                        if account_id not in attempts:
                            attempts.append(account_id)
                        # The durable record is committed before any paid run request.
                        began_at = time.time() if attempt_started is None else attempt_started
                        observation_deadline = began_at + int(self.settings()['task_timeout_seconds'])
                        if preflight_deadlines:
                            observation_deadline = min(observation_deadline, min(preflight_deadlines))
                        persist({"submission_phase": "confirming", "expected_user_id": identity,
                            'observation_started_at': began_at,
                            'observation_deadline': observation_deadline})
                        self._update_canvas_task(task_id, {"attempted_account_ids": attempts, "progress": 30})
                        break
                # Our own positive quote is durable. Another admitted task may
                # still have an unknown hold; let it quote/finish without this
                # task losing its uploads or holding the scheduler lock.
                self._stop.wait(min(1, max(0, deadline - time.monotonic())))
            else:
                return True
            timing.end_phase(credit_wait)
            submission_started = True
            with timing.phase('confirmation_submit'), timing.defer_notifications():
                submitted = client.submit(context, int(math.ceil(quote)), persist=persist, deadline=deadline)
            finish_attempt('accepted')
            persist({**submitted, "submission_phase": "submitted"})
            self._update_canvas_task(task_id, {"status": "submitted", "progress": 40})
            self._poll_canvas(task_id, client, deadline)
            return True
        except CanvasCLIError as exc:
            finish_attempt('command_failed')
            current_task = self.store.task(task_id)
            if self._canvas_task_terminated(current_task):
                return True
            if model_pending and self._canvas_model_ineligible(current_task, exc, deadline):
                diagnostic = self._canvas_diagnostic(exc, 'model_selection')
                diagnostic['preparation_timing'] = timing.snapshot()
                diagnostic.update(dispatch_started=False, requested_model=task['model'],
                    kind='image' if task['normalized'].get('kind') == 'image' else 'video',
                    task_deadline=time.time() + max(0, deadline - time.monotonic()))
                skipped = self._exclude_model_account(task_id, account_id, diagnostic)
                return 'skip_model' if skipped else True
            if identity_pending and self._canvas_identity_network_retry(current_task, exc, deadline):
                diagnostic = self._canvas_diagnostic(exc, 'identity')
                diagnostic['preparation_timing'] = timing.snapshot()
                diagnostic.update(dispatch_started=False,
                    task_deadline=time.time() + max(0, deadline - time.monotonic()))
                retry = self._handle_admission_rejection(task_id, account_id, exc.code,
                    str(diagnostic.get('message') or exc), preflight_error=diagnostic)
                return 'retry_preflight' if retry else True
            if submission_started:
                current = (self.store.task(task_id) or {}).get('upstream_context') or {}
                if not current.get('submit_error'):
                    self._canvas_context(task_id, {'submit_error': self._canvas_diagnostic(exc,
                        str(current.get('submission_phase') or 'submitting'))})
            error = (getattr(exc, 'details', None) or {}).get('error') or {}
            rejected_credit = (credit_rejected(str(exc))
                or str(error.get('serviceCode') or error.get('code')) == '1006'
                or str(error.get('reason') or '').lower() in {'insufficient_credits', 'insufficient_balance'})
            if rejected_credit and not getattr(exc, 'outcome_unknown', False):
                return 'retry' if self._handle_credit_rejection(task_id, account_id, str(exc)) else True
            if submission_started and getattr(exc, "outcome_unknown", True):
                # A lost receipt is reconciled by original operation identity.
                self._update_canvas_task(task_id, {"status": "submitted", "progress": 40,
                    "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                    "error_message": self._canvas_pending_message(task_id,
                        "Canvas submission receipt unavailable; querying original operation")})
                self._poll_canvas(task_id, client, deadline)
            else:
                if not submission_started:
                    phase = str((current_task.get('upstream_context') or {}).get('phase')
                        or ('identity' if identity_pending else 'model_selection' if model_pending else 'preparing'))
                    self._canvas_context(task_id, {'preparation_error': {**self._canvas_diagnostic(exc, phase),
                        'preparation_timing': timing.snapshot()}, 'preparation_timing': timing.snapshot()})
                expired_preparation = not submission_started and time.monotonic() >= deadline
                facts = (self._canvas_pre_dispatch_facts(self.store.task(task_id) or {}, exc, submission_started)
                         if preparation_retryable_failure(exc) and not prior_dispatch_evidence else {})
                if facts:
                    self._canvas_context(task_id, {'pre_dispatch_failure': facts})
                self._fail_canvas_task(task_id, 'CLI_PREPARATION_TRANSIENT_FAILED' if facts else
                    'CLI_PREPARATION_TIMEOUT' if expired_preparation else exc.code,
                    'Task deadline passed during Canvas preparation; no generation was submitted'
                    if expired_preparation else str(exc))
            return True
        except Exception as exc:
            finish_attempt('preparation_failed')
            if self._canvas_task_terminated(self.store.task(task_id)):
                return True
            code = "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN" if submission_started else (
                'CLI_PREPARATION_TIMEOUT' if time.monotonic() >= deadline else
                "MEDIA_DOWNLOAD_FAILED" if isinstance(exc, MediaError) else "CLI_PREPARATION_FAILED")
            if submission_started:
                current = (self.store.task(task_id) or {}).get('upstream_context') or {}
                if not current.get('submit_error'):
                    self._canvas_context(task_id, {'submit_error': self._canvas_diagnostic(exc,
                        str(current.get('submission_phase') or 'submitting'))})
                self._close_unconfirmed_canvas_task(task_id, 'submission_interrupted')
            else:
                if isinstance(exc, MediaError):
                    diagnostic = media_error_diagnostic(exc)
                    self._canvas_context(task_id, {"media_download_error": diagnostic})
                    logger.warning("Canvas media download failed task=%s account=%s diagnostic=%s",
                                   task_id, account_id, diagnostic)
                fail_preparation(code, f"{type(exc).__name__}: Canvas preparation failed")
            logger.warning("Canvas task %s failed in %s (%s)", task_id,
                           "submission" if submission_started else "preparation", type(exc).__name__)
            return True
        finally:
            finish_attempt('stopped')

    def _canvas_settlement(self, task: dict, client, *, deadline: float | None = None) -> dict:
        values = {}
        try:
            state = client.identity(deadline=deadline)
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

    def _poll_canvas(self, task_id: str, client, deadline: float, *,
                     one_shot: bool = False) -> None:
        errors = 0
        transfer_errors = 0
        result_storage_pending = False
        task = self.store.task(task_id)
        if self._canvas_task_terminated(task):
            return
        prior = task.get('upstream_context') or {}
        if prior.get('reconciliation_paused') and not one_shot:
            if prior.get('reconciliation_pause_reason') in {'result_pending', 'result_transfer_failed'}:
                return
            self._canvas_context(task_id, {'reconciliation_paused': False})
        absolute_deadline = self._canvas_observation_deadline(task_id)
        if one_shot:
            # An explicit read of an old result has its own bounded request
            # budget; it never changes the original observation deadline.
            query_deadline = time.monotonic() + float(getattr(self.canvas_cli, 'timeout', 120))
        else:
            query_deadline = min(deadline, time.monotonic() + max(0, absolute_deadline - time.time()))

        def wait(delay):
            self._stop.wait(min(delay, max(0, query_deadline - time.monotonic())))

        queried = False
        while not self._stop.is_set() and (one_shot and not queried or not one_shot
                and time.monotonic() < query_deadline and time.time() < absolute_deadline):
            task = self.store.task(task_id)
            if self._canvas_task_terminated(task):
                return
            context = task.get("upstream_context") or {}
            if context.get('reconciliation_paused') and not one_shot:
                return
            queried = True
            try:
                state = client.poll(context, deadline=query_deadline)
                if self._canvas_task_terminated(self.store.task(task_id)):
                    return
                errors = 0
            except CanvasCLIError as exc:
                if self._canvas_task_terminated(self.store.task(task_id)):
                    return
                errors += 1
                absent = str(exc.code).lower() == 'cli.operation_submission_absent'
                count = int(context.get('consecutive_submission_absent') or 0) + 1 if absent else 0
                self._canvas_context(task_id, {'last_query_error': self._canvas_diagnostic(exc, 'query'),
                    'consecutive_submission_absent': count})
                self._update_canvas_task(task_id, {"error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN",
                    "error_message": self._canvas_pending_message(task_id,
                        "Canvas operation query interrupted; original submission retained")})
                if count >= 3 or one_shot:
                    self._close_unconfirmed_canvas_task(task_id, 'operation_submission_absent' if count >= 3
                        else context.get('reconciliation_pause_reason') or 'manual_verification_unresolved')
                    return
                wait(min(60, 2 ** min(errors, 6)))
                continue
            status = state.get("status")
            if status == 'unknown':
                absent = str(state.get('error_code') or '').lower() == 'cli.operation_submission_absent'
                count = int(context.get('consecutive_submission_absent') or 0) + 1 if absent else 0
                diagnostic = CanvasCLIError(state.get('error_code') or 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                    state.get('error_message') or 'Canvas original submission could not be matched')
                self._canvas_context(task_id, {'last_query_error': self._canvas_diagnostic(diagnostic, 'query'),
                    'consecutive_submission_absent': count})
                self._update_canvas_task(task_id, {'status': 'submitted', 'progress': min(40, task['progress']),
                    'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                    'error_message': self._canvas_pending_message(task_id, str(diagnostic))})
                if count >= 3 or one_shot:
                    self._close_unconfirmed_canvas_task(task_id, 'operation_submission_absent' if count >= 3
                        else context.get('reconciliation_pause_reason') or 'manual_verification_unresolved')
                    return
                wait(max(2, int(self.settings()['poll_interval_seconds'])))
                continue
            # A positive operation match clears only obsolete query errors.
            # Preserve the original submission diagnostic as durable evidence.
            self._canvas_context(task_id, {'consecutive_submission_absent': 0,
                'reconciliation_paused': False, 'submission_phase': 'accepted', 'phase': 'accepted'}, remove=('last_query_error',
                'reconciliation_pause_reason', 'reconciliation_paused_at'))
            facts = [item for item in (state.get('credit_facts') or [])
                if isinstance(item, dict) and item.get('source') == 'precharge'
                and item.get('settled') is False]
            if facts:
                self._update_canvas_task(task_id, {'cost_evidence': {'canvas_resource_snapshots': facts}})
            if status == "succeeded":
                resource_ids = [item.get('resource_id') for item in state.get('result_files') or []
                    if isinstance(item, dict) and item.get('resource_id')]
                if resource_ids:
                    self._canvas_context(task_id, {'resource_ids': resource_ids})
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
                        result_storage_pending = True
                        self._update_canvas_task(task_id, {'progress': 95, 'error_code': 'RESULT_TRANSFER_FAILED',
                            'error_message': 'Canvas generation complete; original result storage is being retried'})
                        if transfer_errors >= 8:
                            self._update_canvas_task(task_id, self._canvas_settlement(task, client, deadline=query_deadline))
                            self._park_canvas_task(task_id, 'result_transfer_failed',
                                'Canvas generation completed; original result storage remains unavailable; generation was not resubmitted')
                            return
                        if one_shot:
                            self._park_canvas_task(task_id, 'result_transfer_failed',
                                'Canvas generation completed; original result storage remains unavailable; retry result storage only')
                            return
                        wait(min(60, 2 ** min(transfer_errors, 6)))
                        continue
                if not urls:
                    pending = CanvasCLIError('CLI_RESULT_PENDING',
                        'Canvas reported completion before the final output became available', outcome_unknown=True)
                    self._canvas_context(task_id, {'last_query_error': self._canvas_diagnostic(pending, 'query')})
                    self._update_canvas_task(task_id, {'status': 'submitted', 'progress': min(40, task['progress']),
                        'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                        'error_message': str(pending)})
                    self._park_canvas_task(task_id, 'result_pending',
                        'Canvas generation completed; the final output has not been retrieved; retry the original result only')
                    return
                values = {"status": "succeeded", "progress": 100, "error_code": "", "error_message": "",
                          "result_urls": urls, 'result_transfer': metadata,
                          "completed_at": time.time(), **self._canvas_settlement(task, client, deadline=query_deadline)}
                if task["model"] == "sd-2-5-480p-x":
                    from .result_transfer import transfer
                    signed, metadata = transfer(urls[0], str(client.account["proxy_url"]), task_id,
                        self.settings(), aspect_ratio=task["normalized"]["aspect_ratio"])
                    values.update(result_urls=[signed], upstream_result_urls=urls, result_transfer=metadata)
                saved = self._update_canvas_task(task_id, values)
                if saved.get('status') != 'succeeded':
                    return
                for raw in state.get('result_files') or state.get('result_paths') or []:
                    try:
                        path = Path(raw.get('path') if isinstance(raw, dict) else raw).resolve()
                        if path.is_relative_to(self.canvas_cli.account_directory(client.account).resolve()):
                            path.unlink(missing_ok=True)
                    except (OSError, TypeError):
                        pass
                return
            if status in {"failed", "cancelled", "canceled"}:
                self._update_canvas_task(task_id, self._canvas_settlement(task, client, deadline=query_deadline))
                self._fail_canvas_task(task_id, state.get("error_code") or "UPSTREAM_FAILED",
                    state.get("error_message") or "Canvas generation failed")
                return
            self._update_canvas_task(task_id, {"status": "running", 'error_code': '', 'error_message': '',
                'completed_at': None,
                "progress": max(45, min(95, int(number(state.get("progress")) or 45))),
                })
            if one_shot:
                return
            wait(max(2, int(self.settings()["poll_interval_seconds"])))
        if not self._stop.is_set() and not one_shot:
            if result_storage_pending:
                self._park_canvas_task(task_id, 'result_transfer_failed',
                    'Canvas generation completed; the result storage observation window expired; retry original result storage only')
            else:
                self._close_unconfirmed_canvas_task(task_id, 'observation_deadline_reached')

    def _resume_canvas_poll(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if self._canvas_task_terminated(task) or not task.get('account_id'):
            return
        context = task.get('upstream_context') or {}
        if context.get('reconciliation_paused') and context.get('reconciliation_pause_reason') in {
                'result_pending', 'result_transfer_failed'}:
            return
        account = self.store.account(int(task["account_id"]), private=True)
        if not account:
            self._fail_canvas_task(task_id, "ACCOUNT_MISSING", "Canvas task account was removed")
            return
        self._worker_slots.acquire()
        with self._lock:
            self._active[int(account["id"])] = self._active.get(int(account["id"]), 0) + 1
        try:
            self._poll_canvas(task_id, self.canvas_client(account),
                time.monotonic() + int(self.settings()["task_timeout_seconds"]))
        except Exception as exc:
            self._canvas_context(task_id, {'last_query_error': self._canvas_diagnostic(exc, 'query')})
            self._close_unconfirmed_canvas_task(task_id, 'recovery_interrupted')
        finally:
            self._release_account(int(account["id"]), task_id)
            self._worker_slots.release()

    def reconcile_canvas_task(self, task_id: str) -> dict:
        """Query the retained operation once; never prepare, confirm or run it."""
        with self._lock:
            task = self.store.task(task_id)
            if not task or task.get('execution_backend') != 'canvas_cli' or not task.get('account_id'):
                raise ValueError('task has no Canvas operation to verify')
            context = task.get('upstream_context') or {}
            if not all(context.get(key) for key in ('project_id', 'node_id', 'submit_id')):
                raise ValueError('task has no retained Canvas operation identifiers')
            if task['status'] in {'failed', 'expired', 'succeeded'} and task.get('error_code') != 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN':
                raise ValueError('task no longer requires Canvas verification')
            account_id = int(task['account_id'])
            account = self.store.account(account_id, private=True)
            if not account:
                raise ValueError('task account is unavailable')
            if self._active.get(account_id, 0) or not self._worker_slots.acquire(blocking=False):
                raise RuntimeError('task account is busy')
            self._active[account_id] = self._active.get(account_id, 0) + 1
        try:
            self._poll_canvas(task_id, self.canvas_client(account), time.monotonic(), one_shot=True)
        except Exception as exc:
            self._canvas_context(task_id, {'last_query_error': self._canvas_diagnostic(exc, 'query')})
            self._close_unconfirmed_canvas_task(task_id, 'manual_verification_interrupted')
        finally:
            with self._lock:
                self._active[account_id] = max(0, self._active.get(account_id, 1) - 1)
            self._worker_slots.release()
        return self.store.task(task_id) or {}
