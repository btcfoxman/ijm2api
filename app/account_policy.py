"""Admission rules shared by every IJM generation transport."""


def credit_rejected(message: str) -> bool:
    value = str(message).lower()
    return any(part in value for part in (
        'ret=1006', 'not enough credits left', 'no relevant benefits',
        'insufficient credits', 'insufficient balance', 'not enough credits'))
