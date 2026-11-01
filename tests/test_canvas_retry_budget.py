"""Offline durable/CAS admission budgets; no Canvas CLI or supplier calls."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app.canvas_retry_budget import RetryBudgetError, budget_token, claim_attempt, new_budget


TASK = "ijm_budget_fixture"
PROOF = {"stage": "preparation", "node_run_count": 0, "generation_request_sent": False,
         "dispatch_started": False, "outcome_unknown": False, "prior_dispatch_evidence": False}


def claim(budget, owner=3, now=101, **kwargs):
    return claim_attempt(budget, task_id=TASK, account_id=owner, now=now,
                         expected_token=kwargs.pop("expected_token", budget_token(budget, task_id=TASK)),
                         proof=kwargs.pop("proof", PROOF), **kwargs)


def test_three_total_attempts_two_accounts_and_no_input_mutation():
    budget = new_budget(TASK, admitted_at=100)
    original = deepcopy(budget)
    first = claim(budget, 3, 100)
    assert budget == original and first["deadline"] == 280
    second = claim(first, 3, 102)
    third = claim(second, 7, 104)
    assert [entry["account_id"] for entry in third["attempts"]] == [3, 3, 7]
    assert [entry["attempt"] for entry in third["attempts"]] == [1, 2, 3]
    assert len({entry["claim_id"] for entry in third["attempts"]}) == 3
    with pytest.raises(RetryBudgetError, match="ATTEMPTS_EXHAUSTED"):
        claim(third, 7, 105)


def test_an_insufficient_account_can_skip_to_second_but_never_return():
    first = claim(new_budget(TASK, admitted_at=100), 3, 100)
    second = claim(first, 7, 102, excluded_account_ids={3})
    third = claim(second, 7, 104, excluded_account_ids={3})
    assert len(third["attempts"]) == 3
    with pytest.raises(RetryBudgetError, match="ACCOUNT_RETURN_BLOCKED"):
        claim(second, 3, 104)
    with pytest.raises(RetryBudgetError, match="ACCOUNT_LIMIT"):
        claim(second, 11, 104)


def test_same_account_can_only_be_tried_twice():
    second = claim(claim(new_budget(TASK, admitted_at=100), 3, 100), 3, 102)
    with pytest.raises(RetryBudgetError, match="ACCOUNT_RETRY_LIMIT"):
        claim(second, 3, 104)


@pytest.mark.parametrize("excluded", [[3], (3,), {3}, frozenset({3})])
def test_existing_exclusions_are_authoritative(excluded):
    with pytest.raises(RetryBudgetError, match="ACCOUNT_EXCLUDED"):
        claim(new_budget(TASK, admitted_at=100), excluded_account_ids=excluded)


def test_restart_roundtrip_preserves_original_deadline_and_claims():
    state = claim(new_budget(TASK, admitted_at=100), 3, 101)
    restored = json.loads(json.dumps(state))
    assert budget_token(restored, task_id=TASK) == budget_token(state, task_id=TASK)
    assert claim(restored, 3, 279.9)["deadline"] == state["deadline"] == 280
    with pytest.raises(RetryBudgetError, match="BUDGET_EXPIRED"):
        claim(restored, 3, 280)
    assert restored == state


def test_original_task_deadline_can_shorten_but_never_extend_the_window():
    early = new_budget(TASK, admitted_at=100, task_deadline=120)
    assert early["deadline"] == 120
    assert new_budget(TASK, admitted_at=100, task_deadline=9999)["deadline"] == 280
    with pytest.raises(RetryBudgetError, match="BUDGET_EXPIRED"):
        claim(early, now=120)
    with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
        new_budget(TASK, admitted_at=100, task_deadline=100)


@pytest.mark.parametrize("key,value", [
    ("generation_request_sent", True), ("generation_request_sent", 0),
    ("dispatch_started", True), ("dispatch_started", 0),
    ("outcome_unknown", True), ("outcome_unknown", None),
    ("prior_dispatch_evidence", True), ("prior_dispatch_evidence", "false"),
    ("node_run_count", 1), ("node_run_count", False), ("node_run_count", "0"),
    ("stage", "run_dispatching"), ("stage", "accepted"), ("stage", []),
])
def test_only_explicit_zero_run_no_dispatch_proof_authorizes_a_claim(key, value):
    with pytest.raises(RetryBudgetError, match="DISPATCH_EVIDENCE"):
        claim(new_budget(TASK, admitted_at=100), proof={**PROOF, key: value})


@pytest.mark.parametrize("missing", list(PROOF))
def test_absent_proof_is_not_no_send_evidence(missing):
    proof = dict(PROOF)
    del proof[missing]
    with pytest.raises(RetryBudgetError, match="DISPATCH_EVIDENCE"):
        claim(new_budget(TASK, admitted_at=100), proof=proof)


def test_extra_proof_fields_cannot_hide_an_accepted_or_paid_attempt():
    with pytest.raises(RetryBudgetError, match="DISPATCH_EVIDENCE"):
        claim(new_budget(TASK, admitted_at=100), proof={**PROOF, "accepted": True})


@pytest.mark.parametrize("owner", [None, True, False, 0, -1, 3.0, "3", 2**63])
def test_account_identity_is_strict(owner):
    with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
        claim(new_budget(TASK, admitted_at=100), owner=owner)


@pytest.mark.parametrize("value", [None, True, False, "100", -1, 0, float("nan"), float("inf"), 10**1000])
def test_admission_time_is_finite_and_typed(value):
    with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
        new_budget(TASK, admitted_at=value)


@pytest.mark.parametrize("value", ["3", {"id": 3}, [True], ["3"], [-1]])
def test_exclusion_identity_set_is_not_coerced(value):
    with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
        claim(new_budget(TASK, admitted_at=100), excluded_account_ids=value)


@pytest.mark.parametrize("value", [None, [], {}, "", True])
def test_legacy_missing_or_malformed_state_cannot_restart_the_budget(value):
    with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
        budget_token(value, task_id=TASK)


@pytest.mark.parametrize("key,value", [
    ("version", True), ("version", 2), ("task_id", "another_task"),
    ("deadline", 281), ("deadline", float("inf")), ("attempts", {}),
])
def test_durable_state_is_versioned_and_cannot_silently_expand(key, value):
    state = new_budget(TASK, admitted_at=100)
    state[key] = value
    with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
        budget_token(state, task_id=TASK)


def test_claim_chain_detects_dropped_reordered_or_modified_attempts():
    first = claim(new_budget(TASK, admitted_at=100), 3, 100)
    state = claim(first, 3, 102)
    changed = deepcopy(state)
    changed["attempts"][0]["account_id"] = 7
    dropped = deepcopy(state)
    dropped["attempts"] = dropped["attempts"][1:]
    reordered = deepcopy(state)
    reordered["attempts"].reverse()
    for value in (changed, dropped, reordered):
        with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
            budget_token(value, task_id=TASK)


def test_clock_rollback_cannot_renew_a_durable_budget():
    first = claim(new_budget(TASK, admitted_at=100), 3, 110)
    with pytest.raises(RetryBudgetError, match="CLOCK_INVALID"):
        claim(first, 3, 109)


def test_atomic_cas_integration_grants_only_one_claim_for_a_competing_ticket():
    # The scheduler holds this same lock/CAS through durable commit and only
    # invokes external preparation after commit; the helper does no I/O.
    ledger = {"budget": new_budget(TASK, admitted_at=100)}
    expected = budget_token(ledger["budget"], task_id=TASK)
    lock = threading.Lock()
    def contend(_):
        with lock:
            try:
                proposal = claim(ledger["budget"], 3, 101, expected_token=expected)
            except RetryBudgetError as exc:
                return exc.code
            ledger["budget"] = json.loads(json.dumps(proposal))
            return "COMMITTED"
    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(contend, range(30)))
    assert outcomes.count("COMMITTED") == 1
    assert outcomes.count("BUDGET_CONFLICT") == 29
    assert len(ledger["budget"]["attempts"]) == 1


def test_task_scoped_cas_cannot_copy_a_ticket_to_another_task_or_reset_state():
    state = claim(new_budget(TASK, admitted_at=100), 3, 101)
    token = budget_token(state, task_id=TASK)
    with pytest.raises(RetryBudgetError, match="BUDGET_INVALID"):
        claim_attempt(state, task_id="another_task", account_id=3, now=102,
                      expected_token=token, proof=PROOF)
    with pytest.raises(RetryBudgetError, match="BUDGET_CONFLICT"):
        claim(new_budget(TASK, admitted_at=101), now=102, expected_token=token)
