"""Admission rules shared by every IJM generation transport."""

import math


_LOW_BALANCE_IMAGE_MODELS = frozenset({
    'seedream-5.0-pro', 'seedream-5.0-pro-1.5k',
    'seedream-5.0-pro-2k', 'seedream-5.0-pro-4k',
})


def _known_balance(account: dict) -> float | None:
    value = account.get('balance')
    if isinstance(value, bool):
        return None
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result is not None and math.isfinite(result) else None


def low_balance_image_task(task: dict | None) -> bool:
    normalized = (task or {}).get('normalized') or {}
    return (normalized.get('kind') == 'image'
            and normalized.get('model') in _LOW_BALANCE_IMAGE_MODELS
            and normalized.get('upstream_model') == 'seedream_5.0_pro')


def low_balance_image_probe(account: dict, task: dict | None, threshold: float) -> bool:
    balance = _known_balance(account)
    return (balance is not None and 0 < balance < threshold and low_balance_image_task(task))


def account_accepts_task(account: dict, task: dict | None = None, *, threshold: float = 0) -> bool:
    """Preserve manual disable; low credits are usable only for Seedream Pro 5."""
    balance = _known_balance(account)
    low = balance is not None and 0 <= balance < threshold
    image = low_balance_image_task(task)
    if low and not image:
        return False
    if account.get('enabled') and account.get('status') == 'active':
        return True
    return (not account.get('enabled') and account.get('status') == 'disabled_low_balance'
            and account.get('login_enable_after') not in (False, 0)
            and balance is not None and 0 < balance < threshold and image)


def credit_rejected(message: str) -> bool:
    value = str(message).lower()
    return any(part in value for part in (
        'ret=1006', 'not enough credits left', 'no relevant benefits',
        'insufficient credits', 'insufficient balance', 'not enough credits'))
