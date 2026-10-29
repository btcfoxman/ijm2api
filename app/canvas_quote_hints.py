"""Short-lived scheduling quotes; never a debit, settlement or retry permission."""
from __future__ import annotations
import hashlib
import json
import re
import time

from .canvas_client import _key, _model_candidates
from .catalog import MODELS
from .cost_adapter import number

TTL_SECONDS = 3600
MAX_HINTS = 1000


def identity_epoch(account, data_dir, *, verified_user_id=None):
    """Read an existing epoch only; never create a profile or perform auth."""
    expected = str(account.get('login_user_id') or '')
    if (account.get('execution_mode') != 'canvas_cli' or account.get('cli_auth_status') != 'ready'
            or not expected or str(account.get('cli_user_id') or '') != expected
            or verified_user_id is not None and str(verified_user_id) != expected):
        return None
    try:
        marker = (data_dir / 'canvas-cli' / ('account-' + str(int(account['id']))) / 'credential-profile').read_bytes()
        if not re.fullmatch(rb'[A-Za-z0-9_.-]{1,80}\s*', marker):
            return None
    except (OSError, ValueError, KeyError):
        return None
    return hashlib.sha256(json.dumps([int(account['id']), expected, marker.decode().strip()],
        separators=(',', ':')).encode()).hexdigest()


def request_dimensions(normalized):
    if normalized.get('kind') == 'image':
        return None  # Image and browser admission retain their existing policy.
    spec = MODELS.get(normalized.get('model'))
    duration = number(normalized.get('duration'))
    if not spec or not duration:
        return None
    counts = [len(normalized.get(kind) or []) for kind in ('images', 'videos', 'audios')]
    evidence = normalized.get('_cost_evidence') or {}
    reference = number(evidence.get('reference_seconds')) if counts[1] else 0.0
    if reference is None or counts[1] and reference <= 0:
        return None
    mode = normalized.get('mode')
    if mode in {'i2v', 'i2v_first_last', 'first_last_frame'}:
        mode = 'first_last_frame'
    elif mode not in {'t2v', 'm2v'}:
        mode = 'm2v' if any(counts) else 't2v'
    return {'public_model': spec.id, 'mode': mode,
        'resolution': spec.generation_resolution if spec.id == 'sd-2-5-480p-x' else normalized.get('resolution'),
        'duration': duration, 'reference_seconds': round(reference, 6), 'counts': counts,
        'aspect_ratio': normalized.get('aspect_ratio'), 'count': normalized.get('count') or 1}


def dimension_key(dimensions):
    return hashlib.sha256(json.dumps(dimensions, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def new_hint(account, data_dir, normalized, context, verified_user_id, *, now=None):
    epoch = identity_epoch(account, data_dir, verified_user_id=verified_user_id)
    dimensions = request_dimensions(normalized)
    amount = number(context.get('quoted_credits'))
    model, mode = context.get('model'), context.get('mode')
    quote = context.get('quote') or {}
    if (epoch is None or dimensions is None or not amount
            or context.get('backend') != 'canvas_cli' or context.get('phase') != 'quoted'
            or context.get('account_id') != int(account['id'])
            or not isinstance(quote, dict) or quote.get('confirmable') is not True
            or number(quote.get('total_max_credits')) != amount
            or mode != dimensions['mode'] or not isinstance(model, str)
            or _key(model) not in {_key(value) for value in _model_candidates(normalized) if value}):
        return None
    # Called only after CanvasClient.prepare has validated node.quote, before
    # funding/reselection. No actual_cost or balance delta is considered.
    return {'account_id': int(account['id']), 'identity_epoch': epoch,
        'dimension_key': dimension_key(dimensions), 'model': model, 'mode': mode,
        'amount': amount, 'observed_at': time.time() if now is None else now,
        'source': 'node.quote'}


def matching_hint(row, account, data_dir, normalized, *, now=None):
    dimensions = request_dimensions(normalized)
    now = time.time() if now is None else now
    stamp, amount = number(row.get('observed_at')), number(row.get('amount'))
    return bool(dimensions is not None and row.get('source') == 'node.quote'
        and row.get('account_id') == int(account['id'])
        and row.get('identity_epoch') == identity_epoch(account, data_dir)
        and row.get('identity_epoch') is not None
        and row.get('dimension_key') == dimension_key(dimensions)
        and row.get('mode') == dimensions['mode']
        and isinstance(row.get('model'), str)
        and _key(row.get('model')) in {_key(value) for value in _model_candidates(normalized) if value}
        and amount is not None and amount > 0 and stamp is not None
        and now - TTL_SECONDS <= stamp <= now)
