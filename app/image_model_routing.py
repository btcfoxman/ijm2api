"""Plan an explicit image execution model without rewriting the public request."""
from __future__ import annotations

import math
from typing import Any

from .account_policy import DEFAULT_LOW_BALANCE_IMAGE_RESCUE_THRESHOLD, low_balance_image_probe, low_balance_image_task
from .image_client import IMAGE_MODELS, image_model_resolutions, resolve_image_model


DEFAULT_LOW_BALANCE_SEEDREAM_TARGET = 'seedream-5.0-pro'
LOW_BALANCE_SEEDREAM_TARGET_OPTIONS = (
    'seedream-5.0-pro', 'seedream-5.0-lite',
    'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst',
)


def validate_rescue_threshold(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError('low-balance image rescue threshold must be a finite non-negative number')
    try:
        threshold = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('low-balance image rescue threshold must be a finite non-negative number') from None
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError('low-balance image rescue threshold must be a finite non-negative number')
    return threshold


def rescue_threshold(settings: dict) -> float:
    try:
        return validate_rescue_threshold(settings.get(
            'low_balance_image_rescue_threshold', DEFAULT_LOW_BALANCE_IMAGE_RESCUE_THRESHOLD))
    except ValueError:
        return 0.0  # Invalid persisted configuration must never enable a rescue.


def validate_target_model(value: Any) -> str:
    if not isinstance(value, str) or value not in LOW_BALANCE_SEEDREAM_TARGET_OPTIONS or value not in IMAGE_MODELS:
        raise ValueError('unsupported low-balance image target; choose a registered image model')
    return value


def plan_image_execution(task: dict, account: dict, settings: dict) -> tuple[dict, dict | None]:
    original = dict(task.get('normalized') or {})
    original.pop('_image_model_route', None)
    if not low_balance_image_task({'normalized': original}):
        return original, None
    threshold = rescue_threshold(settings)
    applies = low_balance_image_probe(account, {'normalized': original}, 0, rescue_threshold=threshold)
    target = validate_target_model(settings.get('low_balance_seedream_target_model',
                                                DEFAULT_LOW_BALANCE_SEEDREAM_TARGET)) if applies else original['model']
    upstream = resolve_image_model(target)
    if original.get('resolution') not in image_model_resolutions(upstream):
        raise ValueError('configured image execution model does not support the requested resolution')
    audit = {
        'requested_model': original['model'],
        'requested_upstream_model': original['upstream_model'],
        'effective_model': target,
        'effective_upstream_model': upstream,
        'account_id': int(account['id']),
        'rescue_threshold': threshold,
        'source': 'low_balance_image_rule' if applies else 'requested_model',
        'mapped': upstream != original['upstream_model'],
    }
    effective = {**original, 'model': target, 'upstream_model': upstream,
                 '_image_model_route': audit}
    return effective, audit


def recorded_image_route(task: dict) -> dict | None:
    """Read durable audit facts only; never re-plan an accepted operation."""
    normalized = task.get('normalized') or {}
    route = (task.get('upstream_context') or {}).get('image_model_route')
    if (not low_balance_image_task(task) or not isinstance(route, dict)
            or route.get('account_id') != task.get('account_id')
            or route.get('requested_model') != normalized.get('model')
            or route.get('requested_upstream_model') != normalized.get('upstream_model')
            or not isinstance(route.get('effective_model'), str)
            or not isinstance(route.get('effective_upstream_model'), str)):
        return None
    try:
        if resolve_image_model(route['effective_model']) != route['effective_upstream_model']:
            return None
    except ValueError:
        return None
    return {key: route[key] for key in (
        'requested_model', 'requested_upstream_model', 'effective_model',
        'effective_upstream_model', 'rescue_threshold', 'source', 'mapped') if key in route}
