"""One Jimeng submission followed by bounded queries of its durable identity."""

from __future__ import annotations

import time
import math
import re
import hashlib
from contextlib import contextmanager, nullcontext
from urllib.parse import urlsplit

from .auto_login import verified_login_user_id
from .browser import BrowserError, BrowserRejected, CDPTransportError
from .cost_adapter import input_evidence, number
from .errors import RegionGenerationDenied, upstream_review_error
from .jimeng_client import (JimengPageNotReady, JimengProtocolUnavailable,
                            JimengResultDownloadError, verified_result_download_receipts)
from .image_model_routing import plan_image_execution
from .media import MediaError, media_error_diagnostic
from .upload import UploadError
from .result_transfer import ResultTransferError


_JIMENG_DOWNLOAD_ATTEMPTS = 3
_JIMENG_DOWNLOAD_RECEIPT_FIELDS = (
    'version', 'kind', 'account_id', 'submit_id', 'history_id', 'item_id', 'video_id',
    'source', 'request_scene', 'is_for_video_download', 'watermark_type', 'logo_type',
    'source_url_sha256', 'preview_url_sha256', 'url_host', 'selection', 'width', 'height',
    'size_bytes', 'size_source', 'history_watermark_type', 'history_logo_type',
)
_JIMENG_DOWNLOAD_MESSAGE = 'Jimeng original output download could not be verified'
_JIMENG_STORAGE_ATTEMPTS = 3
_JIMENG_STORAGE_MESSAGE = 'Jimeng original output could not be saved and verified'
_JIMENG_STORAGE_FIELDS = ('bucket', 'key', 'size_bytes', 'sha256', 'content_type',
    'original_bytes', 'upload_id', 'expires_at', 'source_url_sha256',
    'public_download_verified', 'public_download_verified_at', 'width', 'height',
    'duration_seconds', 'source_route', 'public_verification')


def _transfer_jimeng_original(*args, **kwargs):
    from .result_transfer import transfer_jimeng_original
    return transfer_jimeng_original(*args, **kwargs)


def _jimeng_stored_output_valid(url, metadata, source_hash):
    try:
        parsed = urlsplit(url)
        valid_url = (parsed.scheme == 'https' and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.fragment and parsed.port in (None, 443))
    except (TypeError, ValueError):
        return False
    return bool(valid_url and isinstance(metadata, dict)
        and metadata.get('original_bytes') is True
        and metadata.get('public_download_verified') is True
        and type(metadata.get('public_download_verified_at')) in (int, float)
        and math.isfinite(metadata['public_download_verified_at'])
        and metadata['public_download_verified_at'] > 0
        and metadata.get('content_type') == 'video/mp4'
        and type(metadata.get('size_bytes')) is int and metadata['size_bytes'] > 0
        and isinstance(metadata.get('sha256'), str)
        and re.fullmatch(r'[0-9a-f]{64}', metadata['sha256'])
        and metadata.get('source_url_sha256') == source_hash
        and isinstance(metadata.get('bucket'), str) and metadata['bucket']
        and isinstance(metadata.get('key'), str) and metadata['key'])


class JimengServiceMixin:
    @staticmethod
    @contextmanager
    def _jimeng_verification_reads(client, budget):
        """Share thirty seconds of live verification across preparation phases.

        Uploading or preparing a quote does not consume this read-only budget.
        Nested client retries can only shorten the browser's outer deadline.
        """
        started = time.monotonic()
        remaining = budget[0]
        if remaining <= 0:
            raise CDPTransportError('Runtime.evaluate', 'target_lookup', False, 'TimeoutError')
        browser = getattr(client, 'browser', None)
        factory = getattr(type(browser), 'command_budget', None)
        scope = factory(browser, started + remaining, timeout=10) if factory else nullcontext()
        try:
            with scope:
                yield
        finally:
            budget[0] = max(0, remaining - max(0, time.monotonic() - started))

    @staticmethod
    def _jimeng_cdp_diagnostic(exc):
        diagnostic = {'code': 'CDP_TRANSPORT_FAILED', 'retryable': exc.retryable is True,
                      'command_sent': exc.command_sent is True}
        for key in ('method', 'phase', 'exception_type'):
            value = getattr(exc, key, '')
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value):
                diagnostic[key] = value
        return diagnostic

    def _jimeng_cdp_pre_dispatch_failure(self, task_id, account, exc, *, stage='account_verification'):
        """The service has not entered its only generation call for this task."""
        with self._lock:
            task = self.store.task(task_id) or {}
            context = dict(task.get('upstream_context') or {})
            if (not self._jimeng_open(task) or task.get('status') in {'succeeded', 'failed', 'expired', 'cancelled'}
                    or task.get('execution_backend') != 'jimeng_web'
                    or task.get('account_id') != int(account['id'])
                    or task.get('run_id') or task.get('thread_id')
                    or task.get('result_urls') or task.get('upstream_result_urls')
                    or task.get('actual_cost') is not None or context.get('upstream_task_id')
                    or context.get('submission_phase') in {'dispatching', 'accepted', 'submitted', 'unknown'}
                    or (context.get('submit_error') or {}).get('outcome_unknown') is True):
                return False
            facts = {'generation_request_sent': False, 'financial_outcome': 'not_charged_this_invocation',
                     'automatic_resubmission': False, 'stage': stage,
                     'reason': 'cdp_transport_failed'}
            context.update(submission_phase='not_dispatched', pre_dispatch_failure=facts,
                           cdp_transport_failure=self._jimeng_cdp_diagnostic(exc))
            saved = self.store.update_task(task_id, {'status': 'failed', 'progress': 100,
                'error_code': 'JIMENG_PROTOCOL_UNAVAILABLE', 'error_message': str(exc)[:600],
                'completed_at': time.time(), 'upstream_context': context}, preserve_terminal=True,
                expected_updated_at=task['updated_at'])
            if not self._jimeng_public_pre_dispatch_facts(saved):
                return False
            self._account_cdp_transport_failure(account, exc)
            return True

    @staticmethod
    def _jimeng_public_pre_dispatch_facts(task: dict) -> dict:
        context = task.get('upstream_context') or {}
        facts = context.get('pre_dispatch_failure')
        if (task.get('execution_backend') != 'jimeng_web'
                or task.get('status') != 'failed' or task.get('error_code') != 'JIMENG_PROTOCOL_UNAVAILABLE'
                or task.get('run_id') or task.get('thread_id') or task.get('result_urls')
                or task.get('upstream_result_urls') or context.get('upstream_task_id')
                or task.get('actual_cost') is not None
                or context.get('submission_phase') != 'not_dispatched'
                or not isinstance(facts, dict) or facts.get('generation_request_sent') is not False
                or facts.get('financial_outcome') != 'not_charged_this_invocation'
                or facts.get('automatic_resubmission') is not False):
            return {}
        return {key: facts[key] for key in ('generation_request_sent', 'financial_outcome',
                                            'automatic_resubmission')}

    def _jimeng_not_dispatched(self, task_id: str, exc: JimengPageNotReady, *, submit_id: str = '') -> bool:
        """Only the native pre-call gate can disprove a persisted dispatch intent."""
        if exc.did_dispatch is not False:
            return False
        with self._lock:
            task = self.store.task(task_id) or {}
            context = dict(task.get('upstream_context') or {})
            if (not self._jimeng_open(task)
                    or task.get('run_id') and task['run_id'] != submit_id
                    or task.get('thread_id') or task.get('result_urls') or task.get('upstream_result_urls')
                    or context.get('upstream_task_id')
                    or task.get('actual_cost') is not None
                    or context.get('submission_phase') in {'accepted', 'submitted', 'unknown'}
                    or (context.get('submit_error') or {}).get('outcome_unknown') is True):
                return False
            facts = {'generation_request_sent': False, 'financial_outcome': 'not_charged_this_invocation',
                     'automatic_resubmission': False, 'stage': exc.stage, 'reason': exc.reason}
            context.update(submission_phase='not_dispatched', pre_dispatch_failure=facts)
            saved = self.store.update_task(task_id, {'status': 'failed', 'progress': 100, 'run_id': '', 'thread_id': '',
                'error_code': 'JIMENG_PROTOCOL_UNAVAILABLE', 'error_message': str(exc)[:600],
                'completed_at': time.time(), 'upstream_context': context}, preserve_terminal=True,
                expected_updated_at=task['updated_at'])
            if not self._jimeng_public_pre_dispatch_facts(saved):
                return False
            account_id = task.get('account_id')
            account = self.store.account(account_id, private=True) if account_id else None
            if account and account.get('status') == 'active':
                self.store.update_account(account_id, {'status': 'network_error', 'last_error': str(exc)[:300]})
            return True

    @staticmethod
    def _jimeng_open(task: dict | None) -> bool:
        return bool(task and 'manual_termination' not in (task.get('upstream_context') or {})
            and (task.get('status') not in {'succeeded', 'failed', 'expired', 'cancelled'}
                 or task.get('error_code') == 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'))

    def _jimeng_update(self, task_id: str, values: dict | None = None,
                       context: dict | None = None) -> dict:
        with self._lock:
            task = self.store.task(task_id) or {}
            if not self._jimeng_open(task):
                return task
            changes = dict(values or {})
            if context is not None:
                changes['upstream_context'] = {**(task.get('upstream_context') or {}), **context}
            return self.store.update_task(task_id, changes, preserve_terminal=True)

    @staticmethod
    def _jimeng_identity(client, expected: str = '') -> str:
        identity = client.login_identity()
        actual = verified_login_user_id({'identity_verified': isinstance(identity, dict)
            and identity.get('authenticated') is True, 'identity': identity})
        if not actual or expected and actual != expected:
            raise BrowserError('Jimeng signed-in identity does not match this account')
        return actual

    @staticmethod
    def _jimeng_quote_context(prepared, account_id: int, normalized: dict) -> dict:
        """Only a bound native credit quote can authorize a zero-cost write."""
        context = prepared.receipt_context(account_id, phase='prepared')
        quote = context.get('quoted_credits')
        bound = context.get('quote_bound')
        if (isinstance(quote, bool) or not isinstance(quote, (int, float))
                or not math.isfinite(quote) or quote < 0
                or context.get('quote_source') != 'native_get_need_credits'
                or not isinstance(bound, dict)):
            raise JimengProtocolUnavailable('Jimeng current native credit quote is unavailable')
        model = (prepared.body.get('extend') or {}).get('root_model')
        expected_materials = sorted(kind for key, kind in (
            ('images', 'image'), ('videos', 'video'), ('audios', 'audio')) for _ in normalized.get(key) or [])
        materials = bound.get('material_types')
        if (not isinstance(model, str) or not model or bound.get('model_req_key') != model
                or bound.get('resolution') != normalized.get('resolution')
                or type(bound.get('duration_seconds')) is not int
                or bound['duration_seconds'] != normalized.get('duration')
                or type(bound.get('batch_number')) is not int or bound['batch_number'] != 1
                or not isinstance(materials, list) or any(not isinstance(item, str) for item in materials)
                or sorted(materials) != expected_materials
                or number(bound.get('input_video_duration_seconds')) is None):
            raise JimengProtocolUnavailable('Jimeng credit quote does not match the prepared request')
        return context

    def _execute_jimeng(self, task_id: str, account: dict, deadline: float) -> bool:
        """Keep the selected account bound through quoting and one dispatch."""
        task = self.store.task(task_id)
        if not self._jimeng_open(task):
            return True
        if task.get('run_id'):
            # Existing operations belong exclusively to the recovery path;
            # capability or regional changes cannot turn one into a new write.
            return True
        account = self._submission_account(task_id, account)
        client = self._web_client(account, with_store=True)
        normalized = dict(task['normalized'])
        if normalized.get('kind') == 'image':
            normalized, _ = plan_image_execution(task, account, self.settings())
        # Capability checks are pure. Unsupported Jimeng combinations cannot
        # open Chrome, upload media, or fall back to an international client.
        try:
            client.preflight_model(normalized)
        except (JimengProtocolUnavailable, ValueError) as exc:
            self._fail(task_id, 'JIMENG_PROTOCOL_UNAVAILABLE', str(exc))
            return True
        owned = False
        dispatched = False
        verification_budget = [30.0]
        preparation_stage = 'account_verification'
        try:
            account = self._submission_account(task_id, account, normalized)
            account, owned = self.browser_runtime.acquire(account)
            account = self._submission_account(task_id, account, normalized)
            client = self._web_client(account, with_store=True)
            with self._jimeng_verification_reads(client, verification_budget):
                identity = self._jimeng_identity(client, str(account.get('login_user_id') or ''))
                if not account.get('login_user_id'):
                    self.store.update_account(int(account['id']), {'login_user_id': identity})
                client.account['login_user_id'] = identity
                account = self._submission_account(task_id, account, normalized)
                state = client.account_state(max_attempts=2, timeout_seconds=6)
            before = number(state.get('available_balance'))
            if before is None or before < 0:
                raise BrowserError('Jimeng available balance could not be verified')
            self.store.update_account(int(account['id']), {'balance': before})
            account = self._submission_account(task_id, account)
            if normalized.get('kind') == 'image':
                normalized, route = plan_image_execution(task, account, self.settings())
                if route is not None:
                    self._jimeng_update(task_id, context={'image_model_route': route})
            account = self._submission_account(task_id, account, normalized)
            client.preflight_model(normalized)
            self._jimeng_update(task_id, {'balance_before': before, 'progress': 12})
            account = self._submission_account(task_id, account, normalized)
            preparation_stage = 'media_upload'
            assets = client.upload_inputs(normalized)
            task = self._jimeng_update(task_id, {'cost_evidence': input_evidence(assets)})
            task = self._update_cost_estimate(task)
            account = self._submission_account(task_id, account, normalized)
            client.preflight_model(normalized)
            preparation_stage = 'quote_preparation'
            prepared = client.prepare_submit(normalized, assets)
            if prepared.kind != ('image' if normalized.get('kind') == 'image' else 'video'):
                raise BrowserError('Jimeng prepared submission kind does not match the task')
            current = self.store.task(task_id)
            if not self._jimeng_open(current) or current.get('run_id'):
                return True
            quote_context = self._jimeng_quote_context(prepared, int(account['id']), normalized)
            quoted_credits = float(quote_context['quoted_credits'])
            # Preparation may upload media and refresh commercial state. Read
            # credits again; historical estimates and commerce amount are not
            # the price of this account's newly prepared request.
            preparation_stage = 'account_verification'
            with self._jimeng_verification_reads(client, verification_budget):
                self._jimeng_identity(client, identity)
                latest_state = client.account_state(max_attempts=2, timeout_seconds=6)
            latest_balance = number(latest_state.get('available_balance'))
            if latest_balance is None:
                raise BrowserError('Jimeng final available balance could not be verified')
            if self._stop.is_set() or time.monotonic() >= deadline:
                self._fail(task_id, 'JIMENG_PROTOCOL_UNAVAILABLE', 'task stopped before Jimeng generation dispatch')
                return True
            with self._lock:
                account = self._submission_account(task_id, account, normalized)
                current = self.store.task(task_id) or {}
                if (not self._jimeng_open(current) or current.get('run_id')
                        or current.get('account_id') != int(account['id'])):
                    return True
                reservation = self._credit_reservations.get(task_id)
                if reservation is None or reservation[0] != int(account['id']):
                    self._fail(task_id, 'ACCOUNT_VERIFICATION_FAILED',
                        'Jimeng reservation ownership changed before generation was submitted')
                    return True
                current_balance = number(account.get('balance'))
                budget_balance = min(latest_balance, current_balance) if current_balance is not None else latest_balance
                self.store.update_account(int(account['id']), {'balance': budget_balance})
                quote_evidence = {'amount': quoted_credits, 'source': 'native_get_need_credits',
                    'field': 'getNeedCredits.credits', 'account_id': int(account['id']),
                    'submit_id': prepared.submit_id, 'bound': quote_context['quote_bound'],
                    'observed_at': time.time()}
                self._jimeng_update(task_id, {'estimated_cost': quoted_credits,
                    'cost_evidence': {'jimeng_quote': quote_evidence}}, quote_context)
                self._credit_reservations[task_id] = (int(account['id']), quoted_credits)
                if not self._submission_funded(account, task_id, quoted_credits, budget_balance,
                                               allow_zero_cost=quoted_credits == 0):
                    self._fail(task_id, 'JIMENG_CREDITS_INSUFFICIENT',
                        'Jimeng current quote exceeds available unreserved credits; no generation was submitted')
                    return True
                attempts = list(current.get('attempted_account_ids') or [])
                if int(account['id']) not in attempts:
                    attempts.append(int(account['id']))
                context = {**quote_context, 'submission_phase': 'dispatching',
                    'task_id': task_id, 'expected_user_id': identity,
                    'observation_deadline': time.time() + max(0, deadline - time.monotonic())}
                # Commit the original UUID before entering the only generating
                # call. An interrupted write may only query this account and ID.
                saved = self._jimeng_update(task_id, {'status': 'submitted', 'progress': 30,
                    'run_id': prepared.submit_id, 'thread_id': '', 'attempted_account_ids': attempts}, context)
                if (not self._jimeng_open(saved) or saved.get('run_id') != prepared.submit_id
                        or (saved.get('upstream_context') or {}).get('submission_phase') != 'dispatching'):
                    return True
                dispatched = True
            try:
                receipt = client.submit_prepared(prepared)
                if receipt.get('run_id') != prepared.submit_id:
                    raise BrowserError('Jimeng receipt does not match the original submission')
                self._jimeng_update(task_id, {'status': 'submitted', 'progress': 40,
                    'thread_id': str(receipt.get('thread_id') or '')},
                    {'submission_phase': 'submitted', 'upstream_task_id': str(receipt.get('upstream_task_id') or '')})
            except BrowserRejected:
                self._jimeng_update(task_id, context={'submission_phase': 'rejected'})
                self._fail(task_id, 'JIMENG_UPSTREAM_REJECTED', 'Jimeng rejected the generation request')
                return True
            except Exception as exc:
                if isinstance(exc, JimengPageNotReady) and self._jimeng_not_dispatched(
                        task_id, exc, submit_id=prepared.submit_id):
                    return True
                self._jimeng_update(task_id, {'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                    'error_message': 'Jimeng submission receipt was lost; querying the original submission only'},
                    {'submit_error': {'code': type(exc).__name__, 'outcome_unknown': True},
                     'automatic_resubmission_blocked': True})
            self._poll_jimeng(task_id, client, deadline)
            return True
        except RegionGenerationDenied as exc:
            self._fail(task_id, 'REGION_GENERATION_DENIED', str(exc))
            return True
        except JimengPageNotReady as exc:
            if not dispatched and self._jimeng_not_dispatched(task_id, exc):
                return True
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', str(exc))
            return True
        except (MediaError, UploadError) as exc:
            if dispatched:
                code = 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
            elif isinstance(exc, UploadError):
                code = 'MEDIA_UPLOAD_FAILED'
            else:
                category = media_error_diagnostic(exc).get('category')
                code = {'content_type': 'MEDIA_FORMAT_UNSUPPORTED',
                        'too_large': 'MEDIA_LIMIT_EXCEEDED'}.get(category, 'MEDIA_DOWNLOAD_FAILED')
            self._fail(task_id, code, str(exc))
            return True
        except JimengProtocolUnavailable as exc:
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN' if dispatched else 'JIMENG_PROTOCOL_UNAVAILABLE', str(exc))
            return True
        except CDPTransportError as exc:
            if not dispatched:
                # A concurrent terminal state or accepted binding is authoritative;
                # a rejected CAS must not relabel that operation as unsubmitted.
                self._jimeng_cdp_pre_dispatch_failure(task_id, account, exc, stage=preparation_stage)
                return True
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', str(exc))
            return True
        except (BrowserError, ValueError, KeyError, TypeError) as exc:
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN' if dispatched else 'ACCOUNT_VERIFICATION_FAILED', str(exc))
            return True
        except Exception:
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN' if dispatched else 'JIMENG_PROTOCOL_UNAVAILABLE',
                'Jimeng original submission requires query recovery' if dispatched else 'Jimeng preparation could not complete')
            return True
        finally:
            self.browser_runtime.release(account, owned)

    def _resume_jimeng_poll(self, task_id: str) -> None:
        task = self.store.task(task_id) or {}
        if not self._jimeng_open(task):
            return
        context = task.get('upstream_context') or {}
        if (context.get('backend') != 'jimeng_web' or context.get('site') != 'jimeng'
                or context.get('account_id') != task.get('account_id')
                or context.get('submit_id') != task.get('run_id') or not task.get('run_id')
                or context.get('task_id') != task_id or not context.get('expected_user_id')):
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'Jimeng original submission ownership is unavailable')
            return
        account = self.store.account(int(task['account_id']), private=True)
        if not account or account.get('execution_mode') != 'jimeng_web':
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'Jimeng original account transport is unavailable')
            return
        remaining = (number(context.get('observation_deadline')) or 0) - time.time()
        if remaining <= 0:
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'Jimeng original submission query deadline passed')
            return
        self._worker_slots.acquire()
        account_id = int(account['id'])
        with self._lock:
            self._active[account_id] = self._active.get(account_id, 0) + 1
        owned = False
        try:
            account, owned = self.browser_runtime.acquire(account)
            client = self._web_client(account, with_store=True)
            self._jimeng_identity(client, str(context['expected_user_id']))
            remaining = max(0, (number(context.get('observation_deadline')) or 0) - time.time())
            self._poll_jimeng(task_id, client, time.monotonic() + remaining)
        except Exception as exc:
            self._jimeng_update(task_id, context={'last_query_error': {'code': type(exc).__name__}})
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'Jimeng original submission could not be queried')
        finally:
            self.browser_runtime.release(account, owned)
            self._release_account(account_id, task_id)
            self._worker_slots.release()

    def _jimeng_poll_evidence(self, task_id: str, state: dict) -> tuple[dict, float | None, bool]:
        """Keep accepted identity and task charges even if result retrieval fails."""
        with self._lock:
            task = self.store.task(task_id) or {}
            if not self._jimeng_open(task):
                return task, None, False
            if state.get('record_found') is True:
                context, values = {}, {}
                upstream_task_id = state.get('upstream_task_id')
                if (isinstance(upstream_task_id, str) and upstream_task_id.strip()
                        and not (task.get('upstream_context') or {}).get('upstream_task_id')):
                    context['upstream_task_id'] = upstream_task_id
                history_id = state.get('history_id')
                if isinstance(history_id, str) and history_id.strip() and not task.get('thread_id'):
                    values['thread_id'] = history_id
                if context or values:
                    task = self._jimeng_update(task_id, values, context)
            credit = state.get('task_credit_evidence')
            amount = number(credit.get('amount')) if isinstance(credit, dict) else None
            proven = (amount is not None and amount >= 0
                and credit.get('submit_id') == task.get('run_id') and credit.get('phase') == state.get('status')
                and credit.get('field') == 'item_list[].extra.credits_consume')
            if proven:
                task = self._jimeng_update(task_id, {'cost_evidence': {'upstream_task_credits': credit},
                    'estimated_cost': amount if amount > 0 or state['status'] == 'succeeded' else task['estimated_cost']})
            return task, amount, bool(proven)

    def _jimeng_download_failure(self, task_id: str, exc: JimengResultDownloadError,
                                 deadline: float) -> bool:
        """Retry only the bound result read; never dispatch or select an account."""
        evidence = getattr(exc, 'generation_state', None)
        if isinstance(evidence, dict) and evidence.get('status') == 'succeeded':
            task, amount, proven = self._jimeng_poll_evidence(task_id, evidence)
        else:
            task, amount, proven = self.store.task(task_id) or {}, None, False
        if not self._jimeng_open(task) or self._stop.is_set():
            return False
        previous = (task.get('upstream_context') or {}).get('result_download') or {}
        previous = previous if isinstance(previous, dict) else {}
        attempts = previous.get('attempts', 0)
        attempts = attempts if type(attempts) is int and attempts >= 0 else 0
        attempts += 1
        reason = getattr(exc, 'code', '')
        if not isinstance(reason, str) or not re.fullmatch(r'JIMENG_DOWNLOAD_[A-Z_]{1,64}', reason):
            reason = 'JIMENG_DOWNLOAD_UNAVAILABLE'
        retryable = getattr(exc, 'retryable', False) is True
        retry = retryable and attempts < _JIMENG_DOWNLOAD_ATTEMPTS and time.monotonic() < deadline
        facts = {'phase': 'retrying' if retry else 'failed', 'attempts': attempts,
                 'reason_code': reason, 'retryable': retryable, 'automatic_resubmission': False,
                 'generation_completed': (isinstance(evidence, dict) and evidence.get('status') == 'succeeded')
                     or previous.get('generation_completed') is True}
        values = {'status': 'running' if retry else 'failed',
                  'progress': max(95, task.get('progress') or 0) if retry else 100,
                  'error_code': '' if retry else 'RESULT_TRANSFER_FAILED',
                  'error_message': '' if retry else _JIMENG_DOWNLOAD_MESSAGE}
        if not retry:
            values['completed_at'] = time.time()
        if proven:
            values['actual_cost'] = amount
        saved = self._jimeng_update(task_id, values,
            {'submission_phase': 'accepted', 'result_download': facts})
        if not retry or not self._jimeng_open(saved):
            return False
        self._stop.wait(min(attempts, max(0, deadline - time.monotonic())))
        return True

    def _jimeng_store_outputs(self, task_id: str, client, state: dict, deadline: float):
        """Publish only original bytes verified through the configured S3-center.

        Storage retries keep the same generated assets, account and upload sessions.
        The official CDN address remains private upstream evidence, never a fallback.
        """
        task = self.store.task(task_id) or {}
        account_id, submit_id = task.get('account_id'), task.get('run_id')
        receipts = [{key: receipt[key] for key in _JIMENG_DOWNLOAD_RECEIPT_FIELDS if key in receipt}
                    for receipt in state['result_download_receipts']]
        sources = list(state['result_urls'])
        prior = (task.get('upstream_context') or {}).get('result_storage') or {}
        storage = dict(prior) if isinstance(prior, dict) else {}
        for key in ('bindings', 'checkpoints', 'completed'):
            storage[key] = dict(storage.get(key) or {}) if isinstance(storage.get(key), dict) else {}
        attempts = storage.get('attempts', 0)
        attempts = attempts if type(attempts) is int and attempts >= 0 else 0
        bindings = {receipt['item_id']: {key: receipt[key] for key in (
            'account_id', 'submit_id', 'history_id', 'item_id', 'video_id')}
            | {'index': index} for index, receipt in enumerate(receipts)}

        if storage['bindings'] and storage['bindings'] != bindings:
            self._jimeng_update(task_id, context={'result_storage': {
                **storage, 'phase': 'failed', 'reason_code': 'JIMENG_STORAGE_BINDING_INVALID'}})
            self._fail(task_id, 'RESULT_TRANSFER_FAILED', _JIMENG_STORAGE_MESSAGE)
            return None

        def current_open():
            current = self.store.task(task_id) or {}
            return (not self._stop.is_set() and time.monotonic() < deadline
                and self._jimeng_open(current) and current.get('account_id') == account_id
                and current.get('run_id') == submit_id)

        def persist(phase, *, values=None):
            with self._lock:
                if not current_open():
                    current = self.store.task(task_id) or {}
                    if (not self._stop.is_set() and self._jimeng_open(current)
                            and current.get('account_id') == account_id and current.get('run_id') == submit_id
                            and time.monotonic() >= deadline):
                        storage.update(phase='failed', deadline_exhausted=True,
                            generation_completed=True, automatic_resubmission=False)
                        self._jimeng_update(task_id, {'status': 'failed', 'progress': 100,
                            'error_code': 'RESULT_TRANSFER_FAILED', 'error_message': _JIMENG_STORAGE_MESSAGE,
                            'completed_at': time.time()}, {'result_storage': storage})
                    return False
                storage.update(phase=phase, generation_completed=True, automatic_resubmission=False)
                self._jimeng_update(task_id, values, {'result_storage': storage})
                return True

        # A resumed transfer may use a renewed CDN signature, but completed S3
        # objects must retain their own original receipt and asset binding.
        previous_sources = task.get('upstream_result_urls') or []
        for index, receipt in enumerate(receipts):
            item_id, binding = receipt['item_id'], bindings[receipt['item_id']]
            old_binding = storage['bindings'].get(item_id)
            if old_binding is not None and old_binding != binding:
                self._jimeng_update(task_id, context={'result_storage': {
                    **storage, 'phase': 'failed', 'reason_code': 'JIMENG_STORAGE_BINDING_INVALID'}})
                self._fail(task_id, 'RESULT_TRANSFER_FAILED', _JIMENG_STORAGE_MESSAGE)
                return None
            saved = storage['completed'].get(item_id)
            if saved:
                old_receipt = saved.get('receipt') if isinstance(saved, dict) else None
                old_source = previous_sources[index] if index < len(previous_sources) else ''
                old_hash = hashlib.sha256(old_source.encode()).hexdigest() if isinstance(old_source, str) else ''
                if (not isinstance(old_receipt, dict) or saved.get('binding') != binding
                        or any(old_receipt.get(key) != value for key, value in binding.items() if key != 'index')
                        or old_receipt.get('source_url_sha256') != old_hash
                        or not verified_result_download_receipts({'result_urls': [old_source],
                            'result_download_receipts': [old_receipt]}, account_id=account_id,
                            submit_id=submit_id, history_id=binding['history_id'])
                        or not _jimeng_stored_output_valid(saved.get('url'), saved.get('metadata'), old_hash)):
                    self._jimeng_update(task_id, context={'result_storage': {
                        **storage, 'phase': 'failed', 'reason_code': 'JIMENG_STORAGE_CACHE_INVALID'}})
                    self._fail(task_id, 'RESULT_TRANSFER_FAILED', _JIMENG_STORAGE_MESSAGE)
                    return None
                sources[index], receipts[index] = old_source, {
                    key: old_receipt[key] for key in _JIMENG_DOWNLOAD_RECEIPT_FIELDS if key in old_receipt}
        storage['bindings'] = bindings
        download = (task.get('upstream_context') or {}).get('result_download') or {}
        download = {'phase': 'verified', 'attempts': download.get('attempts', 0),
            'generation_completed': True, 'automatic_resubmission': False, 'receipts': receipts}
        if not current_open():
            persist('transferring')
            return None
        self._jimeng_update(task_id, {'status': 'running', 'progress': max(95, task.get('progress') or 0),
            'upstream_result_urls': sources}, {'submission_phase': 'accepted', 'result_download': download})

        while current_open() and attempts < _JIMENG_STORAGE_ATTEMPTS:
            try:
                for index, receipt in enumerate(receipts):
                    item_id, binding = receipt['item_id'], bindings[receipt['item_id']]
                    if item_id in storage['completed']:
                        continue
                    if not persist('transferring'):
                        return None

                    def checkpoint(value, rid=item_id):
                        storage['checkpoints'][rid] = value
                        return persist('transferring')

                    def phase(value):
                        # Only the local phase enum is persisted, never URLs or
                        # remote error prose which might include signed links.
                        stage = value if isinstance(value, str) and re.fullmatch(
                            r'[a-z_]{1,40}', value) else 'transfer'
                        if storage.get('stage') == stage and current_open():
                            return True  # Download chunks still check cancellation; no repeated SQLite writes.
                        storage['stage'] = stage
                        return persist('downloading' if stage in {'download', 'jimeng_download', 'jimeng_probe'}
                                       else 'transferring')

                    signed, metadata = _transfer_jimeng_original(sources[index], task_id,
                        self.settings(), index=index,
                        proxy_url=str(getattr(client, 'account', {}).get('proxy_url') or ''),
                        deadline=deadline, checkpoint=storage['checkpoints'].get(item_id),
                        persist_checkpoint=checkpoint, on_phase=phase)
                    if not current_open():
                        persist('transferring')
                        return None
                    source_hash = hashlib.sha256(sources[index].encode()).hexdigest()
                    if not _jimeng_stored_output_valid(signed, metadata, source_hash):
                        raise ResultTransferError('Original storage receipt is invalid',
                            code='JIMENG_STORAGE_RECEIPT_INVALID', retryable=False)
                    storage['completed'][item_id] = {'url': signed, 'binding': binding, 'receipt': receipt,
                        'metadata': {key: metadata[key] for key in _JIMENG_STORAGE_FIELDS if key in metadata}}
                    if not persist('transferring'):
                        return None
                items = [storage['completed'][receipt['item_id']] for receipt in receipts]
                if not persist('verified'):
                    return None
                return ([item['url'] for item in items],
                    {'items': [item['metadata'] for item in items], 'original_bytes': True,
                     'source': 'jimeng_web_download'}, sources, download)
            except Exception as exc:
                if not current_open():
                    break
                attempts += 1
                code = getattr(exc, 'code', None)
                code = code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code) else 'JIMENG_STORAGE_FAILED'
                retryable = getattr(exc, 'retryable', None) is True or isinstance(exc, OSError)
                retry = retryable and attempts < _JIMENG_STORAGE_ATTEMPTS
                storage.update(attempts=attempts, reason_code=code, retryable=retryable)
                if not persist('retrying' if retry else 'failed'):
                    return None
                if not retry:
                    self._fail(task_id, 'RESULT_TRANSFER_FAILED', _JIMENG_STORAGE_MESSAGE)
                    return None
                self._stop.wait(min(attempts, max(0, deadline - time.monotonic())))
        if not self._stop.is_set() and self._jimeng_open(self.store.task(task_id)):
            storage.update(deadline_exhausted=time.monotonic() >= deadline, phase='failed')
            self._jimeng_update(task_id, context={'result_storage': storage})
            self._fail(task_id, 'RESULT_TRANSFER_FAILED', _JIMENG_STORAGE_MESSAGE)
        return None

    def _poll_jimeng(self, task_id: str, client, deadline: float) -> None:
        errors = 0
        while not self._stop.is_set() and time.monotonic() < deadline:
            task = self.store.task(task_id)
            if not self._jimeng_open(task):
                return
            try:
                state = client.poll(task['run_id'], task.get('thread_id') or '')
                if not isinstance(state, dict) or state.get('status') not in {'running', 'succeeded', 'failed'}:
                    raise BrowserError('Jimeng returned an invalid task state')
                download = (task.get('upstream_context') or {}).get('result_download') or {}
                if (state['status'] == 'running' and isinstance(download, dict)
                        and (download.get('generation_completed') is True or download.get('phase') == 'retrying')):
                    # A missing/stale history record cannot undo confirmed
                    # completion or return result retrieval to generation polling.
                    raise JimengResultDownloadError('JIMENG_DOWNLOAD_UNAVAILABLE')
                if (state['status'] == 'succeeded' and task.get('normalized', {}).get('kind') != 'image'
                        and not verified_result_download_receipts(state,
                            account_id=task['account_id'], submit_id=task['run_id'],
                            history_id=task.get('thread_id') or '')):
                    missing = JimengResultDownloadError('JIMENG_DOWNLOAD_METADATA_INVALID', retryable=False)
                    missing.generation_state = {key: state[key] for key in (
                        'status', 'state', 'record_found', 'upstream_task_id', 'history_id', 'task_credit_evidence')
                        if key in state}
                    raise missing
                errors = 0
            except JimengResultDownloadError as exc:
                if not self._jimeng_download_failure(task_id, exc, deadline):
                    return
                continue
            except Exception as exc:
                # Once generation has completed, later history/transport errors
                # share the small result-read budget instead of the 8-query backoff.
                current = self.store.task(task_id) or {}
                download = (current.get('upstream_context') or {}).get('result_download') or {}
                if isinstance(download, dict) and (download.get('phase') == 'retrying'
                        or download.get('generation_completed') is True):
                    transient = JimengResultDownloadError('JIMENG_DOWNLOAD_UNAVAILABLE')
                    if not self._jimeng_download_failure(task_id, transient, deadline):
                        return
                    continue
                errors += 1
                self._jimeng_update(task_id, context={'last_query_error': {'code': type(exc).__name__}})
                if errors >= 8:
                    break
                self._stop.wait(min(2 ** errors, max(0, deadline - time.monotonic())))
                continue
            task, amount, proven = self._jimeng_poll_evidence(task_id, state)
            if not self._jimeng_open(task):
                return
            if state['status'] == 'succeeded':
                urls = state.get('result_urls')
                if not isinstance(urls, list) or not urls or any(not isinstance(url, str) or not url for url in urls):
                    self._fail(task_id, 'JIMENG_RESULT_MISSING', 'Jimeng completed without a result URL')
                    return
                after = None
                try:
                    after = number(client.account_state(max_attempts=2, timeout_seconds=6).get('available_balance'))
                    if after is not None:
                        self.store.update_account(int(task['account_id']), {'balance': after})
                except Exception:
                    pass  # A credit read never invalidates a verified output.
                if self._stop.is_set() or not self._jimeng_open(self.store.task(task_id)):
                    return
                receipts = state.get('result_download_receipts') or []
                download = (task.get('upstream_context') or {}).get('result_download') or {}
                download = {'phase': 'verified', 'attempts': download.get('attempts', 0),
                    'generation_completed': True, 'automatic_resubmission': False,
                    'receipts': [{key: receipt[key] for key in _JIMENG_DOWNLOAD_RECEIPT_FIELDS if key in receipt}
                                 for receipt in receipts]}
                upstream_urls, transfer = urls, {}
                if task.get('normalized', {}).get('kind') != 'image':
                    task = self._jimeng_update(task_id, {
                        'actual_cost': amount if proven else task.get('actual_cost')})
                    stored = self._jimeng_store_outputs(task_id, client, state, deadline)
                    if stored is None:
                        return
                    urls, transfer, upstream_urls, download = stored
                if self._stop.is_set():
                    return
                self._jimeng_update(task_id, {'status': 'succeeded', 'progress': 100,
                    'result_urls': urls, 'upstream_result_urls': upstream_urls, 'result_transfer': transfer,
                    'balance_after': after,
                    'actual_cost': amount if proven else task.get('actual_cost'), 'completed_at': time.time(),
                    'error_code': '', 'error_message': ''},
                    {'submission_phase': 'accepted', 'result_download': download})
                return
            if state['status'] == 'failed':
                upstream_code = str(state.get('error_code') or '')
                review = upstream_review_error(upstream_code, '')
                moderation = (state.get('state') in {10, 40}
                    or upstream_code in {'12009', '12011', '2052', '20115'}
                    or review in {'UPSTREAM_REVIEW_FAILED', 'UPSTREAM_IMAGE_REVIEW_FAILED'})
                self._jimeng_update(task_id, {'actual_cost': amount if proven else None},
                    {'submission_phase': 'accepted', 'upstream_failure_code': upstream_code,
                     'upstream_failure_state': state.get('state')})
                self._fail(task_id, 'CONTENT_MODERATION_FAILED' if moderation else 'JIMENG_UPSTREAM_FAILED',
                    str(state.get('error') or 'Jimeng generation failed'))
                return
            self._jimeng_update(task_id, {'status': 'running', 'progress': max(45, task.get('progress') or 0)},
                {'submission_phase': 'accepted'} if state.get('record_found') is True else None)
            self._stop.wait(min(max(2, int(self.settings()['poll_interval_seconds'])),
                                max(0, deadline - time.monotonic())))
        if not self._stop.is_set():
            current = self.store.task(task_id) or {}
            download = (current.get('upstream_context') or {}).get('result_download') or {}
            storage = (current.get('upstream_context') or {}).get('result_storage') or {}
            if isinstance(storage, dict) and storage.get('generation_completed') is True:
                self._jimeng_update(task_id, context={'result_storage': {
                    **storage, 'phase': 'failed', 'deadline_exhausted': True}})
                self._fail(task_id, 'RESULT_TRANSFER_FAILED', _JIMENG_STORAGE_MESSAGE)
                return
            if isinstance(download, dict) and download.get('phase') == 'retrying':
                self._jimeng_update(task_id, context={'result_download': {
                    **download, 'phase': 'failed', 'deadline_exhausted': True}})
                self._fail(task_id, 'RESULT_TRANSFER_FAILED', _JIMENG_DOWNLOAD_MESSAGE)
                return
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                'Jimeng original submission query window ended; automatic resubmission is disabled')
