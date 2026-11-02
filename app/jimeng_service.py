"""One Jimeng submission followed by bounded queries of its durable identity."""

from __future__ import annotations

import time
import math

from .auto_login import verified_login_user_id
from .browser import BrowserError, BrowserRejected
from .cost_adapter import input_evidence, number
from .errors import RegionGenerationDenied, upstream_review_error
from .jimeng_client import JimengProtocolUnavailable
from .image_model_routing import plan_image_execution
from .media import MediaError, media_error_diagnostic
from .upload import UploadError


class JimengServiceMixin:
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
        try:
            account = self._submission_account(task_id, account, normalized)
            account, owned = self.browser_runtime.acquire(account)
            account = self._submission_account(task_id, account, normalized)
            client = self._web_client(account, with_store=True)
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
            assets = client.upload_inputs(normalized)
            task = self._jimeng_update(task_id, {'cost_evidence': input_evidence(assets)})
            task = self._update_cost_estimate(task)
            account = self._submission_account(task_id, account, normalized)
            client.preflight_model(normalized)
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
                self._jimeng_update(task_id, {'error_code': 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                    'error_message': 'Jimeng submission receipt was lost; querying the original submission only'},
                    {'submit_error': {'code': type(exc).__name__, 'outcome_unknown': True},
                     'automatic_resubmission_blocked': True})
            self._poll_jimeng(task_id, client, deadline)
            return True
        except RegionGenerationDenied as exc:
            self._fail(task_id, 'REGION_GENERATION_DENIED', str(exc))
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
                errors = 0
            except Exception as exc:
                errors += 1
                self._jimeng_update(task_id, context={'last_query_error': {'code': type(exc).__name__}})
                if errors >= 8:
                    break
                self._stop.wait(min(2 ** errors, max(0, deadline - time.monotonic())))
                continue
            if state.get('record_found') is True:
                with self._lock:
                    current = self.store.task(task_id) or {}
                    context, values = {}, {}
                    upstream_task_id = state.get('upstream_task_id')
                    if (isinstance(upstream_task_id, str) and upstream_task_id.strip()
                            and not (current.get('upstream_context') or {}).get('upstream_task_id')):
                        context['upstream_task_id'] = upstream_task_id
                    history_id = state.get('history_id')
                    if isinstance(history_id, str) and history_id.strip() and not current.get('thread_id'):
                        values['thread_id'] = history_id
                    if context or values:
                        task = self._jimeng_update(task_id, values, context)
            credit = state.get('task_credit_evidence')
            amount = number(credit.get('amount')) if isinstance(credit, dict) else None
            proven = (amount is not None and amount >= 0
                and credit.get('submit_id') == task['run_id'] and credit.get('phase') == state['status']
                and credit.get('field') == 'item_list[].extra.credits_consume')
            if proven:
                task = self._jimeng_update(task_id, {'cost_evidence': {'upstream_task_credits': credit},
                    'estimated_cost': amount if amount > 0 or state['status'] == 'succeeded' else task['estimated_cost']})
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
                self._jimeng_update(task_id, {'status': 'succeeded', 'progress': 100,
                    'result_urls': urls, 'upstream_result_urls': urls, 'balance_after': after,
                    'actual_cost': amount if proven else None, 'completed_at': time.time(),
                    'error_code': '', 'error_message': ''}, {'submission_phase': 'accepted'})
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
            self._fail(task_id, 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN',
                'Jimeng original submission query window ended; automatic resubmission is disabled')
