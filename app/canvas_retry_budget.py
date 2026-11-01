"""Durable, task-local admission budget. This module performs no I/O.

The caller must read the latest task and commit the returned budget atomically
under its admission lock/CAS, before starting the next external operation.
An old expected_token never authorizes a second claim. A restart must load the
existing budget; invalid or expired state must not be replaced with a new one.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

MAX_ATTEMPTS = 3
MAX_ACCOUNTS = 2
MAX_ATTEMPTS_PER_ACCOUNT = 2
MAX_SECONDS = 180
STAGES = frozenset({"preparation", "identity", "model_preflight", "material_prepare",
                    "project", "reference_upload", "draft_save", "quote", "confirming"})
PROOF_FIELDS = frozenset({"stage", "node_run_count", "generation_request_sent",
                          "dispatch_started", "outcome_unknown", "prior_dispatch_evidence"})


class RetryBudgetError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise RetryBudgetError(code)


def _timestamp(value) -> float:
    _require(type(value) in (int, float), "BUDGET_INVALID")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise RetryBudgetError("BUDGET_INVALID") from None
    _require(math.isfinite(result) and result > 0, "BUDGET_INVALID")
    return result


def _task_id(value) -> None:
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value) is not None,
             "BUDGET_INVALID")


def _account_id(value) -> None:
    _require(type(value) is int and 1 <= value <= 2**63 - 1, "BUDGET_INVALID")


def _proof(value) -> None:
    _require(type(value) is dict and set(value) == PROOF_FIELDS, "DISPATCH_EVIDENCE")
    _require(type(value.get("stage")) is str and value["stage"] in STAGES, "DISPATCH_EVIDENCE")
    _require(type(value.get("node_run_count")) is int and value["node_run_count"] == 0,
             "DISPATCH_EVIDENCE")
    _require(all(value.get(key) is False for key in PROOF_FIELDS - {"stage", "node_run_count"}),
             "DISPATCH_EVIDENCE")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _claim_id(previous, account_id, at, proof) -> str:
    return _digest({"previous": _digest(previous), "account_id": account_id, "at": at, "proof": proof})


def _validate(budget, task_id: str) -> None:
    _task_id(task_id)
    _require(type(budget) is dict and set(budget) == {"version", "task_id", "started_at", "deadline", "attempts"},
             "BUDGET_INVALID")
    _require(type(budget["version"]) is int and budget["version"] == 1 and budget["task_id"] == task_id,
             "BUDGET_INVALID")
    started, deadline = _timestamp(budget["started_at"]), _timestamp(budget["deadline"])
    _require(started < deadline <= started + MAX_SECONDS, "BUDGET_INVALID")
    attempts = budget["attempts"]
    _require(type(attempts) is list and len(attempts) <= MAX_ATTEMPTS, "BUDGET_INVALID")
    previous = {key: copy.deepcopy(value) for key, value in budget.items() if key != "attempts"}
    previous["attempts"] = []
    owners, last_at, last_owner = [], started, None
    for index, entry in enumerate(attempts):
        _require(type(entry) is dict and set(entry) == {"attempt", "account_id", "at", "proof", "claim_id"},
                 "BUDGET_INVALID")
        _require(type(entry["attempt"]) is int and entry["attempt"] == index + 1, "BUDGET_INVALID")
        owner = entry["account_id"]
        _account_id(owner)
        at = _timestamp(entry["at"])
        _require(last_at <= at < deadline, "BUDGET_INVALID")
        _proof(entry["proof"])
        _require(owner == last_owner or owner not in owners, "BUDGET_INVALID")
        owners.append(owner)
        _require(len(set(owners)) <= MAX_ACCOUNTS and owners.count(owner) <= MAX_ATTEMPTS_PER_ACCOUNT,
                 "BUDGET_INVALID")
        _require(entry["claim_id"] == _claim_id(previous, owner, entry["at"], entry["proof"]),
                 "BUDGET_INVALID")
        previous["attempts"].append(copy.deepcopy(entry))
        last_at, last_owner = at, owner


def new_budget(task_id: str, *, admitted_at, task_deadline=None) -> dict:
    """Create once at the first real admission, never at retry or restart."""
    _task_id(task_id)
    started = _timestamp(admitted_at)
    deadline = min(started + MAX_SECONDS, _timestamp(task_deadline)) if task_deadline is not None else started + MAX_SECONDS
    result = {"version": 1, "task_id": task_id, "started_at": started, "deadline": deadline, "attempts": []}
    _validate(result, task_id)
    return result


def budget_token(budget: dict, *, task_id: str) -> str:
    _validate(budget, task_id)
    return _digest(budget)


def claim_attempt(budget: dict, *, task_id: str, account_id: int, now,
                  expected_token: str, excluded_account_ids=(), proof: dict) -> dict:
    """Return one CAS proposal; persist it before executing this attempt.

    The exclusion set comes from the task's existing admission/model policy.
    Balance, concurrency, and atomic credit reservation remain in the existing
    scheduler; this helper cannot grant or charge account credits.
    """
    _validate(budget, task_id)
    _require(isinstance(expected_token, str) and re.fullmatch(r"[a-f0-9]{64}", expected_token) is not None,
             "BUDGET_CONFLICT")
    _require(_digest(budget) == expected_token, "BUDGET_CONFLICT")
    _proof(proof)
    _account_id(account_id)
    _require(type(excluded_account_ids) in (list, tuple, set, frozenset), "BUDGET_INVALID")
    for identifier in excluded_account_ids:
        _account_id(identifier)
    _require(account_id not in excluded_account_ids, "ACCOUNT_EXCLUDED")
    at = _timestamp(now)
    attempts = budget["attempts"]
    _require(at >= budget["started_at"] and (not attempts or at >= attempts[-1]["at"]), "CLOCK_INVALID")
    _require(at < budget["deadline"], "BUDGET_EXPIRED")
    _require(len(attempts) < MAX_ATTEMPTS, "ATTEMPTS_EXHAUSTED")
    owners = [entry["account_id"] for entry in attempts]
    _require(account_id == (owners[-1] if owners else None) or account_id not in owners, "ACCOUNT_RETURN_BLOCKED")
    _require(len(set(owners) | {account_id}) <= MAX_ACCOUNTS, "ACCOUNT_LIMIT")
    _require(owners.count(account_id) < MAX_ATTEMPTS_PER_ACCOUNT, "ACCOUNT_RETRY_LIMIT")
    result = copy.deepcopy(budget)
    result["attempts"].append({"attempt": len(attempts) + 1, "account_id": account_id, "at": at,
                               "proof": copy.deepcopy(proof),
                               "claim_id": _claim_id(budget, account_id, at, proof)})
    return result
