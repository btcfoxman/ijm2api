"""Whitelist-only presentation of durable result delivery; never task control."""
from __future__ import annotations

import math
import re
from typing import Any


_PHASES = frozenset({'querying', 'generating', 'downloading', 'transferring', 'completed'})
_STAGES = frozenset({'query', 'download', 'create', 'upload', 'complete', 'presign',
                     'done', 'resume', 'checkpoint', 'validation', 'transfer'})
_OBSERVED_TIMES = ('generation_success_observed_at', 'download_completed_at',
                   'transfer_completed_at')
_TERMINAL = {'succeeded': 'completed', 'failed': 'failed', 'expired': 'expired',
             'cancelled': 'cancelled', 'canceled': 'cancelled'}
_UNKNOWN = frozenset({'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'CLI_SUBMISSION_UNCONFIRMED'})
_CODE = re.compile(r'(?:[A-Z][A-Z0-9_]{0,79}|(?:cli|service)\.[A-Za-z0-9_.-]{1,72})\Z')


def _timestamp(value: Any) -> bool:
    return type(value) in (int, float) and 0 <= value <= 253402300799 and math.isfinite(value)


def result_delivery_projection(task: dict[str, Any]) -> dict[str, Any]:
    """Project only safe delivery scalars, with canonical terminal status first.

    Missing or incompatible metadata retains the legacy API/UI presentation.
    This function neither changes status nor interprets provider error prose.
    """
    if task.get('execution_backend') != 'canvas_cli':
        return {}
    context = task.get('upstream_context')
    delivery = context.get('result_delivery') if isinstance(context, dict) else None
    if (not isinstance(delivery, dict) or type(delivery.get('version')) is not int
            or delivery['version'] != 1 or not isinstance(delivery.get('phase'), str)
            or delivery['phase'] not in _PHASES):
        return {}
    status = task.get('status')
    if not isinstance(status, str):
        return {}
    observed = {key: delivery[key] for key in _OBSERVED_TIMES
                if _timestamp(delivery.get(key))}
    if status in _TERMINAL:
        result = {'result_stage': _TERMINAL[status]}
        if observed:
            result['result_delivery_diagnostics'] = observed
        return result
    if status not in {'running', 'submitted'} or task.get('error_code') in tuple(_UNKNOWN):
        return {}
    phase = delivery['phase']
    # A delivery marker cannot turn a still-running row into completed output.
    if phase == 'completed':
        return {}
    retrying = delivery.get('retrying') is True
    result_stage = phase + '_retry' if retrying and phase in {'downloading', 'transferring'} else phase
    diagnostic: dict[str, Any] = {'phase': phase, 'retrying': retrying, **observed}
    if isinstance(delivery.get('stage'), str) and delivery['stage'] in _STAGES:
        diagnostic['stage'] = delivery['stage']
    if _timestamp(delivery.get('updated_at')):
        diagnostic['updated_at'] = delivery['updated_at']
    error = delivery.get('last_error')
    if isinstance(error, dict):
        safe: dict[str, Any] = {}
        if isinstance(error.get('code'), str) and _CODE.fullmatch(error['code']):
            safe['code'] = error['code']
        if isinstance(error.get('stage'), str) and error['stage'] in _STAGES:
            safe['stage'] = error['stage']
        if type(error.get('http_status')) is int and 100 <= error['http_status'] <= 599:
            safe['http_status'] = error['http_status']
        if type(error.get('retryable')) is bool:
            safe['retryable'] = error['retryable']
        if _timestamp(error.get('at')):
            safe['at'] = error['at']
        if safe:
            diagnostic['last_error'] = safe
    return {'result_stage': result_stage, 'result_delivery_diagnostics': diagnostic}
