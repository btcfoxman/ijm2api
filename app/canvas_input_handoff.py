"""One unpaid input-preparation handoff; no I/O and no paid retry authority.

The caller must have joined every upload worker. Persist the proposal with CAS
before releasing the original owner. Old inputs are audit only, never deleted
or passed to the replacement account. The existing retry ledger is unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

from .canvas_cli import CanvasCLIError
from .canvas_client import _node_id, _stable, preparation_retryable_failure, submission_evidence_blocks_fallback
from .canvas_retry_budget import budget_token

HANDOFF_KEY = 'input_preparation_handoffs'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_BINDING = {'task_id', 'account_id', 'profile_sha256', 'user_id_sha256', 'proxy_sha256',
            'project_id', 'request_sha256', 'media_sha256'}
_GUARD = {'version', 'task_id', 'source_account_id', 'request_sha256', 'media_sha256', 'handoff_sha256'}
_KEEP = {'preparation_admission_at', 'preparation_attempts', 'preparation_attempt_count',
         'preparation_failures', 'preparation_retry_budget', 'pre_dispatch_failure',
         'preflight_failures', 'model_eligibility_exclusions'}
_RECORD = {'version', 'ordinal', 'at', 'account_id', 'checkpoint', 'operation_binding',
           'budget_before', 'diagnostic', 'proof', 'sha256'}
_VETO = {'manual_termination', 'run_data', 'run_receipt', 'receipt', 'operation_receipt',
         'output_binding', 'result_observation', 'result_delivery', 'recovery_resolution',
         'previous_submissions', 'operation_ref', 'operationRef', 'operation_id', 'operationId',
         'last_operation', 'last_operation_status', 'dispatch_started_at', 'confirmation_received_at',
         'confirmation_expires_at', 'reconciliation_closed', 'reconciliation_paused',
         'automatic_resubmission_blocked', 'accepted', 'dispatch_started', 'submission_receipt_evidence'}
_PREPAID = {('auth', 'account'), ('model', 'list'), ('canvas', 'ls'), ('canvas', 'create'),
            ('resource', 'upload'), ('resource', 'get')}
_TRANSIENT_CODES = {'CLI_COMMAND_TIMEOUT', 'CLI_TASK_DEADLINE_EXCEEDED', 'CLI_INVALID_RESPONSE',
    'cli.invalid_service_response', 'cli.transport_failed', 'service.1', 'cli.upload_state_unknown',
    'cli.upload_credential_unavailable'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _require(value):
    if not value:
        raise ValueError('Canvas input preparation handoff is not valid')


def _no_generation(task):
    context = task['upstream_context']
    _require('manual_termination' not in context
        and not any(key in context for key in ('operation_receipt', 'output_binding', 'run_data',
            'run_receipt', 'receipt', 'result_observation', 'result_delivery', 'previous_submissions',
            'submission_evidence', 'submit_error', 'operation', 'operations', 'submission', 'submissions',
            'partialData', 'generation_id', 'generationId'))
        and not any(context.get(key) for key in _VETO)
        and not context.get('resource_ids') and not context.get('resourceIds')
        and context.get('submission_phase') in (None, '')
        and context.get('financial_outcome') not in {'unknown', 'unconfirmed', 'charged'})
    def contradiction(value):
        if isinstance(value, list):
            return any(contradiction(item) for item in value)
        if not isinstance(value, dict):
            return False
        return (value.get('phase') in {'run_dispatching', 'submitting', 'confirming', 'submitted', 'accepted', 'unknown'}
            or value.get('dispatch_started') is True or value.get('outcome_unknown') is True
            or value.get('generation_request_sent') is True or value.get('accepted') is True
            or value.get('financial_outcome') in {'unknown', 'unconfirmed', 'charged'}
            or value.get('financialOutcome') in {'unknown', 'unconfirmed', 'charged'}
            or any(str(value.get(key) or '').lower() in {'accepted', 'submitted', 'running',
                'processing', 'succeeded', 'completed', 'success'} for key in ('state', 'submissionState'))
            or value.get('command_pair') in (['node', 'run'], ['node', 'confirm'])
            or any(name in value and (not isinstance(value[name], dict)
                or type(value[name].get('count')) is not int or value[name]['count'] != 0)
                for name in ('node run', 'node confirm'))
            or any(value.get(key) for key in ('receipt', 'run_receipt', 'operation_receipt', 'output_binding',
                'operationRef', 'operation_ref', 'resource_ids', 'resourceIds', 'submission_receipt_evidence',
                'submission', 'submissions', 'operation', 'operations', 'partialData', 'resources'))
            or any(contradiction(item) for key, item in value.items()
                   if key not in {'resource_facts', 'quote_facts'}))
    for key in ('preparation_failures', 'preflight_failures'):
        failures = context.get(key, [])
        _require(isinstance(failures, list) and not contradiction(failures))
        for failure in failures:
            error = failure.get('error') if isinstance(failure, dict) else None
            _require(isinstance(failure, dict))
            if error is not None:
                _require(isinstance(error, dict))
                text = ' '.join(str(error.get(k) or '').lower() for k in ('code', 'class', 'reason', 'requiredAction'))
                _require(not any(word in text for word in ('identity', 'authentication', 'authorization',
                    'mismatch', 'policy', 'material_requirements', 'invalid_input', 'fix_input', 'login', 'authenticate')))


def _failed_command(entry):
    code = entry.get('error_code');facts = entry.get('failure') or {}
    pair = tuple(entry['command_pair'])
    _require(isinstance(facts, dict) and facts.get('outcome_unknown') is not True
             and facts.get('dispatch_started') is not True)
    if code == 'service.6':
        _require(pair == ('resource', 'get') and facts.get('class') == 'invalid_input'
            and facts.get('serviceCode') == 6 and facts.get('requiredAction') == 'none'
            and facts.get('retryable') is False)
        return
    _require(code in _TRANSIENT_CODES)
    _require(facts.get('class') in (None, '', 'retryable_failure', 'internal_failure')
        or code == 'cli.upload_state_unknown' and pair == ('resource', 'upload')
            and facts.get('class') == 'human_intervention')
    _require(facts.get('requiredAction') in (None, '', 'none', 'retry', 'contact_support')
        or code == 'cli.upload_state_unknown' and facts.get('requiredAction') == 'human_intervention')
    _require(facts.get('reason') in (None, '', 'network_error', 'network_timeout', 'dns_resolution_failed',
        'service_unavailable', 'transport_error', 'transport_failure', 'connection_error'))


def _finished_zero_paid(timing, task_id):
    _require(isinstance(timing, dict) and timing.get('task_id') == task_id
        and timing.get('finished') is True and timing.get('current_command') is None
        and timing.get('current_phase') is None and type(timing.get('dropped_command_samples')) is int
        and timing['dropped_command_samples'] == 0)
    commands, recent = timing.get('commands'), timing.get('recent_commands')
    _require(isinstance(commands, dict) and isinstance(recent, list)
        and type(timing.get('command_count')) is int and timing['command_count'] == len(recent))
    counts, failed = {}, {}
    for entry in recent:
        pair = entry.get('command_pair') if isinstance(entry, dict) else None
        _require(isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)
            and tuple(pair) in _PREPAID
            and entry.get('outcome') in {'success', 'failed'})
        name = ' '.join(pair)
        counts[name] = counts.get(name, 0) + 1
        failed[name] = failed.get(name, 0) + int(entry['outcome'] == 'failed')
        if entry['outcome'] == 'failed':
            _failed_command(entry)
        if isinstance(entry.get('resource_facts'), dict):
            _require(entry['resource_facts'].get('source') != 'generated')
            _require(entry['resource_facts'].get('resource_id_matches') is not False)
    _require(set(commands) == set(counts))
    for name, value in commands.items():
        _require(isinstance(value, dict) and type(value.get('count')) is int
            and type(value.get('failed')) is int and value['count'] == counts[name]
            and value['failed'] == failed[name])
    _require(any(e.get('command_pair') == ['auth', 'account'] and e.get('outcome') == 'success' for e in recent))


def _checkpoint(task, account, profile):
    context = task['upstream_context'];normalized = task['normalized']
    checkpoint = context.get('upload_checkpoint')
    _require(isinstance(checkpoint, dict) and set(checkpoint) <= {
        'version', 'backend', 'binding', 'refs', 'pending_refs', 'first_failure', 'file_unknown_retries'}
        and type(checkpoint.get('version')) is int and checkpoint['version'] == 1
        and checkpoint.get('backend') == 'canvas_cli')
    binding = checkpoint.get('binding')
    _require(isinstance(binding, dict) and set(binding) == _BINDING
        and binding['task_id'] == task['id'] == context.get('task_id')
        and type(binding['account_id']) is int and binding['account_id'] == task['account_id'] == account['id'] == context.get('account_id')
        and isinstance(profile, str) and bool(profile) and profile == context.get('profile')
        and isinstance(binding['project_id'], str) and bool(binding['project_id'])
        and binding['project_id'] == context.get('project_id')
        and context.get('node_id') == _node_id(f"{account['id']}:{task['id']}")
        and context.get('update_id') == _stable(account['id'], task['id'], 'update')
        and context.get('submit_id') == _stable(account['id'], task['id'], 'submit'))
    _require(all(isinstance(binding[key], str) and _HASH.fullmatch(binding[key]) for key in _BINDING if key.endswith('_sha256')))
    uid = str(account.get('login_user_id') or account.get('cli_user_id') or '')
    _require(bool(uid) and account.get('execution_mode') == 'canvas_cli'
        and all(str(account[key]) == uid for key in ('login_user_id', 'cli_user_id') if account.get(key))
        and (context.get('expected_user_id') is None or str(context['expected_user_id']) == uid)
        and binding['profile_sha256'] == digest(profile)
        and binding['user_id_sha256'] == digest(uid)
        and binding['proxy_sha256'] == digest(str(account.get('proxy_url') or ''))
        and binding['request_sha256'] == digest({k: v for k, v in normalized.items() if not k.startswith('_')}))
    resources = context.get('resources');pending = checkpoint.get('pending_refs');verified = checkpoint.get('refs')
    _require(isinstance(resources, dict) and set(resources) == {'images', 'videos', 'audios'}
        and isinstance(pending, list) and bool(pending) and isinstance(verified, list))
    binding_hash = digest(binding)
    for group, ids in resources.items():
        _require(isinstance(normalized.get(group), list) and isinstance(ids, list)
            and len(ids) == len(normalized[group]) and len(ids) <= 30)
        _require(all(identifier == _stable(account['id'], task['id'], group, index, binding_hash)
            for index, identifier in enumerate(ids)))
    seen = set()
    for ref in [*verified, *pending]:
        _require(isinstance(ref, dict))
        group, index = ref.get('group'), ref.get('index')
        _require(group in resources and type(index) is int and 0 <= index < len(resources[group])
            and (group, index) not in seen and ref.get('resource_id') == resources[group][index]
            and ref.get('source_kind') in {'url', 'file'} and isinstance(ref.get('source_sha256'), str)
            and _HASH.fullmatch(ref['source_sha256']))
        if ref.get('source_kind') == 'url':
            _require(group == 'images' and ref['source_sha256'] == hashlib.sha256(str(normalized[group][index]).encode()).hexdigest())
        if ref in verified:
            _require(set(ref) == {'group', 'index', 'resource_id', 'source_kind', 'source_sha256', 'verified'}
                and ref.get('verified') is True)
        else:
            _require(set(ref) == {'group', 'index', 'resource_id', 'source_kind', 'source_sha256', 'observe_only', 'registration'}
                and ref.get('observe_only') is True and ref.get('registration') in {'acknowledged', 'unacknowledged', 'unknown'})
        seen.add((group, index))
    return checkpoint


def _project_read_recovery(task):
    """A resumed input checkpoint failed before any new remote write.

    Prior input uploads remain part of the complete audit. This is an unpaid
    input handoff, never a claim that the whole task performed only reads.
    """
    context = task['upstream_context']
    attempts = context.get('preparation_attempts')
    _require(isinstance(attempts, list) and len(attempts) == 2)
    timing = attempts[-1].get('timing') or {}
    commands = timing.get('commands')
    _require(isinstance(commands, dict) and set(commands) <= {'auth account', 'model list', 'canvas ls'}
        and isinstance(commands.get('canvas ls'), dict) and commands['canvas ls'].get('failed', 0) > 0
        and any((entry.get('timing') or {}).get('commands', {}).get('resource upload', {}).get('count', 0) > 0
                for entry in attempts[:-1]))
    recent = timing.get('recent_commands')
    _require(isinstance(recent, list) and recent and recent[-1].get('command_pair') == ['canvas', 'ls']
        and recent[-1].get('outcome') == 'failed')


def propose_input_handoff(task, account, profile, diagnostic, *, now, workers_joined):
    """Return a deterministic CAS proposal, or None when any proof is absent."""
    try:
        return _propose(task, account, profile, diagnostic, now=now, workers_joined=workers_joined)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def _propose(task, account, profile, diagnostic, *, now, workers_joined):
    _require(workers_joined is True and type(now) in (int, float) and math.isfinite(now))
    context = task.get('upstream_context');normalized = task.get('normalized')
    _require(task.get('status') == 'preparing' and task.get('execution_backend') == 'canvas_cli'
        and task.get('actual_cost') is None and task.get('error_code') != 'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'
        and not any(task.get(k) for k in ('run_id', 'thread_id', 'result_urls', 'upstream_result_urls', 'result_transfer'))
        and isinstance(context, dict) and isinstance(normalized, dict) and normalized.get('kind', 'video') == 'video'
        and '_execution' not in normalized and HANDOFF_KEY not in context and 'manual_termination' not in context
        and not any(key in context for key in ('operation_receipt', 'output_binding', 'run_data',
            'run_receipt', 'receipt', 'result_observation', 'result_delivery', 'previous_submissions'))
        and context.get('phase') in {'uploading', 'preparing'} and context.get('submission_phase') in (None, '')
        and not context.get('resource_ids') and not any(context.get(k) for k in _VETO)
        and context.get('financial_outcome') not in {'unknown', 'unconfirmed', 'charged'}
        and not submission_evidence_blocks_fallback(context))
    _no_generation(task)
    _require(isinstance(diagnostic, dict) and diagnostic.get('phase') == context['phase']
        and diagnostic.get('outcome_unknown') is False and diagnostic.get('dispatch_started') is not True)
    code = str(diagnostic.get('code') or '')
    unknown_input = code == 'cli.upload_state_unknown'
    pending_input = code == 'MEDIA_UPLOAD_PENDING'
    _require(not any(word in code.lower() for word in ('identity', 'auth', 'mismatch', 'policy', 'material', 'invalid_input')))
    _require(not any(word in str(diagnostic.get('class') or '').lower() for word in
        (('auth', 'invalid_input', 'business') if unknown_input else ('auth', 'invalid_input', 'business', 'human'))))
    failure = CanvasCLIError(code, '', details={'error': diagnostic}, outcome_unknown=False)
    if pending_input:
        from .canvas_service import CanvasServiceMixin
        _require(context['phase'] == 'uploading'
            and all(diagnostic.get(key) in (None, '') for key in
                    ('class', 'reason', 'requiredAction', 'serviceCode', 'exit_code'))
            and bool(CanvasServiceMixin._canvas_pending_reference_retry(task, failure)))
    elif unknown_input:
        from .canvas_service import CanvasServiceMixin
        _require(bool(CanvasServiceMixin._canvas_input_only_failure_facts_checked(task)))
    else:
        _require(preparation_retryable_failure(failure))
    budget = context.get('preparation_retry_budget')
    token = budget_token(budget, task_id=task['id'])
    _require(budget['version'] == 2 and len(budget['attempts']) in ({1, 2} if unknown_input else {2})
        and all(entry['account_id'] == account['id'] for entry in budget['attempts'])
        and budget['attempts'][-1]['at'] <= now < budget['deadline'] <= budget['task_deadline'])
    attempts = context.get('preparation_attempts')
    _require(isinstance(attempts, list) and len(attempts) == len(budget['attempts'])
        and type(context.get('preparation_attempt_count')) is int and context['preparation_attempt_count'] == len(attempts)
        and context.get('preparation_timing') == attempts[-1].get('timing'))
    for index, entry in enumerate(attempts):
        _require(isinstance(entry, dict) and set(entry) == {'ordinal', 'account_id', 'reason', 'timing'}
            and entry['ordinal'] == index + 1 and entry['account_id'] == account['id']
            and entry['reason'] == 'command_failed')
        _finished_zero_paid(entry['timing'], task['id'])
    if context['phase'] == 'preparing':
        _project_read_recovery(task)
    checkpoint = _checkpoint(task, account, profile)
    attempted = task.get('attempted_account_ids')
    _require(isinstance(attempted, list) and all(type(v) is int and v == account['id'] for v in attempted))
    attempted = list(dict.fromkeys([*attempted, account['id']]))
    record = {'version': 1, 'ordinal': 1, 'at': now, 'account_id': account['id'],
        'checkpoint': copy.deepcopy(checkpoint), 'budget_before': copy.deepcopy(budget),
        'operation_binding': {key: context.get(key) for key in ('task_id', 'account_id', 'project_id', 'node_id', 'update_id', 'submit_id')},
        'diagnostic': {key: diagnostic[key] for key in ('code', 'class', 'reason', 'phase', 'exit_code', 'outcome_unknown', 'dispatch_started', 'serviceCode', 'requiredAction', 'retryable') if key in diagnostic},
        'proof': {'workers_joined': True, 'node_run_count': 0, 'node_confirm_count': 0,
                  'finished_audit_count': len(attempts)}}
    record['sha256'] = digest(record)
    updated = {key: copy.deepcopy(value) for key, value in context.items() if key in _KEEP}
    updated[HANDOFF_KEY] = [record]
    return {'context': updated, 'attempted_account_ids': attempted, 'budget_token': token,
            'handoff_sha256': record['sha256']}


def input_handoff_conflict_can_close(task):
    """A fresh transaction may close a still-owned, finished unpaid conflict."""
    try:
        context = task['upstream_context']
        _require(task.get('status') == 'preparing' and task.get('execution_backend') == 'canvas_cli'
            and task.get('actual_cost') is None and HANDOFF_KEY not in context
            and context.get('phase') in {'uploading', 'preparing'}
            and not any(task.get(k) for k in ('run_id', 'thread_id', 'result_urls', 'upstream_result_urls', 'result_transfer')))
        _no_generation(task)
        attempts = context.get('preparation_attempts')
        _require(isinstance(attempts, list) and 1 <= len(attempts) <= 2
            and type(context.get('preparation_attempt_count')) is int
            and context['preparation_attempt_count'] == len(attempts)
            and context.get('preparation_timing') == attempts[-1].get('timing'))
        budget = context.get('preparation_retry_budget')
        budget_token(budget, task_id=task['id'])
        _require(budget['version'] == 2 and len(budget['attempts']) == len(attempts)
            and all(entry['account_id'] == task['account_id'] for entry in budget['attempts']))
        for index, attempt in enumerate(attempts):
            _require(isinstance(attempt, dict) and type(attempt.get('ordinal')) is int
                and attempt['ordinal'] == index + 1 and attempt.get('account_id') == task.get('account_id')
                and attempt.get('reason') == 'command_failed')
            _finished_zero_paid(attempt.get('timing'), task['id'])
        if context['phase'] == 'preparing':
            _project_read_recovery(task)
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def handoff_input_guard(task):
    """Return the exact original source pin; malformed stored history is fatal."""
    context = task.get('upstream_context') or {}
    if HANDOFF_KEY not in context:
        return None
    _require(task.get('status') in {'queued', 'preparing'})
    _no_generation(task)
    entries = context[HANDOFF_KEY]
    _require(isinstance(entries, list) and len(entries) == 1)
    record = entries[0]
    _require(isinstance(record, dict) and set(record) == _RECORD
        and type(record['version']) is int and record['version'] == 1
        and type(record['ordinal']) is int and record['ordinal'] == 1
        and record['sha256'] == digest({k: v for k, v in record.items() if k != 'sha256'}))
    old, current = record['budget_before'], context.get('preparation_retry_budget')
    budget_token(old, task_id=task['id']);budget_token(current, task_id=task['id'])
    original_count = len(old['attempts'])
    _require(old['version'] == 2 and original_count in {1, 2}
        and all(entry['account_id'] == record['account_id'] for entry in old['attempts'])
        and type(record['at']) in (int, float) and old['started_at'] <= record['at'] < old['deadline']
        and record['proof'] == {'workers_joined': True, 'node_run_count': 0,
                               'node_confirm_count': 0, 'finished_audit_count': original_count}
        and {k: v for k, v in old.items() if k != 'attempts'} == {k: v for k, v in current.items() if k != 'attempts'}
        and original_count <= len(current['attempts']) <= 3 and current['attempts'][:original_count] == old['attempts'])
    later = current['attempts'][original_count:]
    if later:
        _require(len({entry['account_id'] for entry in later}) == 1
            and later[-1]['account_id'] != record['account_id'])
    _require(task.get('account_id') is None or task['account_id'] != record['account_id'])
    if task.get('status') == 'preparing':
        _require(bool(later) and later[-1]['account_id'] == task.get('account_id'))
    binding = record['checkpoint'].get('binding')
    _require(isinstance(binding, dict) and set(binding) == _BINDING and binding['task_id'] == task['id']
        and binding['account_id'] == record['account_id']
        and binding['request_sha256'] == digest({k: v for k, v in task['normalized'].items() if not k.startswith('_')}))
    _require(all(isinstance(binding[k], str) and _HASH.fullmatch(binding[k])
                 for k in _BINDING if k.endswith('_sha256'))
        and record['operation_binding'].get('task_id') == task['id']
        and record['operation_binding'].get('account_id') == record['account_id']
        and record['operation_binding'].get('project_id') == binding['project_id'])
    return {'version': 1, 'task_id': task['id'], 'source_account_id': record['account_id'],
            'request_sha256': binding['request_sha256'], 'media_sha256': binding['media_sha256'],
            'handoff_sha256': record['sha256']}


def verify_input_guard(guard, binding):
    """A new account must reproduce original request/file hashes before writes.

URL inputs are pinned to their locator string, as in the original upload plan;
FILE inputs include the actual byte SHA. This is not a URL-content guarantee.
"""
    _require(isinstance(guard, dict) and set(guard) == _GUARD and type(guard['version']) is int
        and guard['version'] == 1 and guard['task_id'] == binding['task_id']
        and type(guard['source_account_id']) is int and guard['source_account_id'] > 0
        and guard['source_account_id'] != binding['account_id']
        and all(isinstance(guard[k], str) and _HASH.fullmatch(guard[k])
                for k in ('request_sha256', 'media_sha256', 'handoff_sha256'))
        and guard['request_sha256'] == binding['request_sha256']
        and guard['media_sha256'] == binding['media_sha256'])
