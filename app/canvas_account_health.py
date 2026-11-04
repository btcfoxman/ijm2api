"""Derive short future-admission cooldowns from original task audit facts.

This module is pure: no clocks, files, account updates or task mutations. A
cooldown is scheduling evidence only, never evidence of a refund or permission
to resubmit the original paid operation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


_PROFILE = re.compile(r'[A-Za-z0-9_.-]{1,80}\Z')
_MODEL = re.compile(r'[A-Za-z0-9_.+/-]{1,160}\Z')
_UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z')
_FLAGS = {'accepted', 'output_resources', 'receipt', 'identity_conflict', 'ambiguous_partial'}
_TRANSPORT_REASONS = {'', 'network_timeout', 'network_error', 'dns_resolution_failed',
    'transport_error', 'transport_failure', 'service_unavailable'}
_PREPARATION_PHASES = {'identity', 'model_selection', 'preparing', 'project', 'uploading', 'saving', 'quoting', 'quoted'}
_CLOSE_REASONS = {'missing_output_resource_budget_exhausted', 'operation_submission_absent'}


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def model_scope(normalized: Any) -> tuple[str, str] | None:
    """Strict effective canonical model; do not merge merely similar names."""
    if not isinstance(normalized, dict):
        return None
    kind = normalized.get('kind') or 'video'
    model = normalized.get('upstream_model')
    if kind not in {'image', 'video'} or not isinstance(model, str) or not _MODEL.fullmatch(model):
        return None
    return kind, model


def _evidence(context: dict) -> dict | None:
    value = context.get('submission_evidence')
    if (not isinstance(value, dict) or set(value) != {'version', *_FLAGS}
            or type(value.get('version')) is not int or value['version'] != 1
            or any(type(value.get(key)) is not bool for key in _FLAGS)):
        return None
    return value


def _has_outputs(context: dict) -> bool:
    ids, count = context.get('resource_ids'), context.get('output_count')
    return (isinstance(ids, list) and bool(ids) and type(count) is int and 1 <= count <= 4
        and len(ids) == count and all(isinstance(value, str) and _UUID.fullmatch(value) for value in ids)
        and len(set(ids)) == count)


def _positive_receipt(context: dict, *, delivered: bool = False) -> bool:
    evidence = _evidence(context)
    return (context.get('phase') == 'accepted'
        and context.get('submission_phase') in {'accepted', 'submitted'}
        and (delivered or not context.get('submit_error')) and _has_outputs(context)
        and evidence is not None and evidence['accepted'] is True
        and evidence['output_resources'] is True and evidence['identity_conflict'] is False)


def _system_error(error: Any, *, upload_unknown: bool = False) -> bool:
    if not isinstance(error, dict):
        return False
    code = error.get('code')
    vod_unknown = (upload_unknown and code == 'cli.upload_state_unknown'
        and error.get('class') == 'human_intervention' and error.get('reason') == 'vod.data_timeout'
        and error.get('serviceCode') in (None, ''))
    if (not isinstance(code, str)
            or any(word in ' '.join(str(error.get(key) or '').lower() for key in ('code', 'reason', 'class'))
                   for word in ('auth', 'identity', 'mismatch', 'permission', 'forbidden', 'policy',
                                'moderation', 'copyright', 'insufficient', 'unsupported', 'invalid_input'))):
        return False
    actions = (None, '', 'retry', 'contact_support', 'none', 'resume')
    if vod_unknown:
        actions += ('human', 'human_intervention')
    if (error.get('class') in {'business_failure', 'permanent_failure', 'invalid_input'}
            or error.get('requiredAction') not in actions):
        return False
    if code in {'cli.invalid_service_response', 'CLI_INVALID_RESPONSE'}:
        return (error.get('class') in (None, '', 'internal_failure')
            and error.get('serviceCode') in (None, '')
            and error.get('requiredAction') in (None, '', 'retry', 'contact_support')
            and str(error.get('reason') or '') in _TRANSPORT_REASONS)
    if code == 'cli.transport_failed':
        return (error.get('class') == 'retryable_failure' and error.get('retryable') is True
            and error.get('requiredAction') == 'retry' and error.get('serviceCode') in (None, '')
            and str(error.get('reason') or '') in _TRANSPORT_REASONS)
    if code == 'service.1':
        return (error.get('class') == 'retryable_failure' and error.get('retryable') is True
            and error.get('requiredAction') == 'retry' and error.get('serviceCode') in (1, '1')
            and str(error.get('reason') or '') in {'', 'service_unavailable'})
    if code == 'CLI_COMMAND_TIMEOUT':
        return (error.get('class') in (None, '', 'retryable_failure', 'internal_failure')
            and error.get('serviceCode') in (None, '')
            and str(error.get('reason') or '') in _TRANSPORT_REASONS)
    return vod_unknown


def _original_binding(row: dict, context: dict, account: dict, profile: str) -> bool:
    uid = str(account.get('login_user_id') or account.get('cli_user_id') or '')
    binding = (context.get('upload_checkpoint') or {}).get('binding')
    if (type(account.get('id')) is not int or account['id'] < 1
            or account.get('execution_mode') != 'canvas_cli' or not uid
            or any(str(account.get(key) or '') not in {'', uid} for key in ('login_user_id', 'cli_user_id'))
            or not isinstance(profile, str) or not _PROFILE.fullmatch(profile)
            or row.get('execution_backend') != 'canvas_cli' or context.get('backend') != 'canvas_cli'
            or type(row.get('account_id')) is not int or type(context.get('account_id')) is not int
            or row.get('account_id') != account['id'] or context.get('account_id') != account['id']
            or context.get('task_id') != row.get('id') or context.get('profile') != profile
            or not isinstance(context.get('project_id'), str) or not _UUID.fullmatch(context['project_id'])
            or context.get('expected_user_id') not in (None, '', uid)
            or not isinstance(binding, dict) or binding.get('task_id') != row.get('id')
            or type(binding.get('account_id')) is not int
            or binding.get('account_id') != account['id'] or binding.get('project_id') != context.get('project_id')
            or binding.get('profile_sha256') != _digest(profile)
            or binding.get('user_id_sha256') != _digest(uid)
            or binding.get('proxy_sha256') != _digest(str(account.get('proxy_url') or ''))):
        return False
    return True


def _row_scope(row: dict, context: dict) -> tuple[str, str] | None:
    normalized = row.get('normalized')
    scope = model_scope(normalized)
    route = context.get('image_model_route')
    if route is None:
        return scope
    if (scope is None or scope[0] != 'image' or not isinstance(route, dict)
            or route.get('account_id') != row.get('account_id')
            or route.get('requested_model') != normalized.get('model')
            or route.get('requested_upstream_model') != normalized.get('upstream_model')
            or not isinstance(route.get('effective_upstream_model'), str)
            or not _MODEL.fullmatch(route['effective_upstream_model'])):
        return None
    return 'image', route['effective_upstream_model']


def _event(row: dict, context: dict, now: float) -> tuple[str, float] | None:
    if 'manual_termination' in context or context.get('previous_submissions'):
        return None
    if row.get('status') in {'succeeded', 'running', 'submitted'} and _positive_receipt(
            context, delivered=row.get('status') == 'succeeded'):
        # Completion/download of an older concurrent run cannot prove that a
        # subsequent submission failure has recovered. Use the actual accepted
        # operation's dispatch time; creation is the conservative legacy bound.
        stamp = _number(context.get('dispatch_started_at')) or _number(row.get('created_at'))
        return ('success', stamp) if stamp is not None and 0 < stamp <= now else None
    if row.get('status') not in {'failed', 'expired'}:
        return None
    evidence = _evidence(context)
    if context.get('submission_evidence') is not None and evidence is None:
        return None
    if (context.get('phase') in {'accepted', 'submitted', 'running'} or context.get('resource_ids')
            or evidence is not None and any(evidence[key] for key in ('accepted', 'output_resources', 'receipt', 'identity_conflict'))):
        return None
    # The old summary bit was also set for a partial reply containing only
    # original IDs. Strong typed negative flags plus the failed original run
    # and closed bounded lookup below may inform future health. This does not
    # resolve the paid outcome or permit that original run to be repeated.
    legacy_partial = context.get('submission_receipt_evidence') is True
    if legacy_partial and evidence is None:
        return None
    error = context.get('submit_error')
    if (isinstance(error, dict) and _system_error(error)
            and context.get('submission_phase') == 'run_dispatching'
            and error.get('phase') == 'run_dispatching' and error.get('dispatch_started') is True
            and error.get('outcome_unknown') is True
            and context.get('reconciliation_closed') is True
            and context.get('reconciliation_close_reason') in _CLOSE_REASONS
            and context.get('financial_outcome') == 'unconfirmed'):
        commands = (context.get('preparation_timing') or {}).get('commands')
        run = commands.get('node run') if isinstance(commands, dict) else None
        if (not isinstance(run, dict) or type(run.get('count')) is not int or run['count'] != 1
                or type(run.get('failed')) is not int or run['failed'] != 1):
            return None
        recovery = context.get('missing_resource_recovery')
        count = context.get('consecutive_submission_absent')
        exhausted = (context.get('reconciliation_close_reason') == 'operation_submission_absent'
            and type(count) is int and count >= 3)
        if isinstance(recovery, dict):
            attempts, maximum = recovery.get('attempts'), recovery.get('max_attempts')
            start = _number(recovery.get('started_at'))
            end = _number(recovery.get('deadline'))
            exhausted = exhausted or (type(recovery.get('version')) is int and recovery['version'] == 1
                and type(attempts) is int and attempts >= 1 and type(maximum) is int and 1 <= maximum <= 10
                and start is not None and end is not None and 0 < start < end <= start + 300
                and (attempts >= maximum or end <= now))
        stamp = _number(context.get('reconciliation_closed_at')) or _number(row.get('completed_at'))
        if exhausted and stamp is not None and 0 < stamp <= now:
            return 'dispatch_failure', stamp
        return None
    error = context.get('preparation_error')
    if (legacy_partial or context.get('phase') not in _PREPARATION_PHASES or not _system_error(error, upload_unknown=True)
            or error.get('phase') not in _PREPARATION_PHASES or error.get('outcome_unknown') is not False
            or context.get('submission_phase') in {'run_dispatching', 'submitted', 'accepted', 'unknown'}):
        return None
    commands = (context.get('preparation_timing') or {}).get('commands')
    if (not isinstance(commands, dict)
            or any(not isinstance(commands.get(name, {}), dict)
                   or type(commands.get(name, {}).get('count', 0)) is not int
                   or commands.get(name, {}).get('count', 0) != 0
                   for name in ('node confirm', 'node run'))):
        return None
    stamp = _number(error.get('at')) or _number(row.get('completed_at'))
    return ('preparation_failure', stamp) if stamp is not None and 0 < stamp <= now else None


def account_cooldowns(rows: list[dict], accounts: list[dict], normalized: dict, *, profiles: dict[int, str],
                      now: float, cooldown_seconds: int = 1800,
                      effective_requests: dict[int, dict] | None = None) -> dict[int, dict]:
    """Return future-admission exclusions; absent or conflicting proof is ignored."""
    if _number(now) is None or now <= 0 or type(cooldown_seconds) is not int or not 0 <= cooldown_seconds <= 86400:
        raise ValueError('invalid Canvas account-health cooldown parameters')
    if not cooldown_seconds:
        return {}
    result = {}
    for account in accounts:
        identifier = account.get('id')
        profile = profiles.get(identifier)
        effective = (effective_requests or {}).get(identifier, normalized)
        scope = model_scope(effective)
        if type(identifier) is not int or scope is None or not isinstance(profile, str):
            continue
        events = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
                continue
            context = row.get('upstream_context')
            if not isinstance(context, dict) or _row_scope(row, context) != scope:
                continue
            try:
                if not _original_binding(row, context, account, profile):
                    continue
                event = _event(row, context, now)
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            if event is None or not now - cooldown_seconds <= event[1] <= now:
                continue
            previous = events.get(row['id'])
            if previous is None or event[1] > previous[1]:
                events[row['id']] = event
        latest_success = max((stamp for kind, stamp in events.values() if kind == 'success'), default=0)
        # A success must be strictly later than a failure. Task-ID sort order
        # cannot turn simultaneous coarse-clock events into recovery proof.
        failures = sorted(((stamp, task_id, kind) for task_id, (kind, stamp) in events.items()
                           if kind != 'success' and stamp >= latest_success), reverse=True)
        severe = any(kind == 'dispatch_failure' for _, _, kind in failures)
        if not failures or not severe and len(failures) < 2:
            continue
        until = failures[0][0] + cooldown_seconds
        if until <= now:
            continue
        result[identifier] = {'blocked_until': until,
            'reason': 'original_dispatch_system_failure' if severe else 'repeated_preparation_system_failure',
            'tail_failure_ids': [task_id for _, task_id, _ in failures[:8]],
            'model': scope[1], 'kind': scope[0], 'backend': 'canvas_cli'}
    return result
