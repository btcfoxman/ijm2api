"""One account can claim one workspace creation until its original result binds."""

from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest

from app.store import Store


def marker():
    return "jimeng-create-pending:" + str(uuid.uuid4())


def test_workspace_creation_claim_is_atomic_and_unknown_result_cannot_reclaim(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    account = store.upsert_account({"name": "CN", "execution_mode": "jimeng_web"})
    candidates = [marker() for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda value: store.claim_jimeng_workspace_creation(account["id"], value), candidates))
    assert results.count(True) == 1
    pending = candidates[results.index(True)]
    assert store.account(account["id"])["project_id"] == pending
    assert not store.claim_jimeng_workspace_creation(account["id"], marker())
    assert not store.bind_jimeng_workspace(account["id"], marker(), "12345678901234")
    assert store.account(account["id"])["project_id"] == pending
    assert store.bind_jimeng_workspace(account["id"], pending, "12345678901234")
    assert store.account(account["id"])["project_id"] == "12345678901234"
    assert not store.bind_jimeng_workspace(account["id"], pending, "12345678901235")
    assert not store.claim_jimeng_workspace_creation(account["id"], marker())


def test_workspace_cas_cannot_modify_international_or_existing_project_bindings(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    for mode in ("canvas_cli", "browser", "jimeng_web"):
        account = store.upsert_account({"name": mode, "execution_mode": mode,
                                        "project_id": "existing" if mode == "jimeng_web" else ""})
        before = store.account(account["id"])
        assert not store.claim_jimeng_workspace_creation(account["id"], marker())
        assert not store.bind_jimeng_workspace(account["id"], marker(), "12345678901234")
        assert store.account(account["id"]) == before


@pytest.mark.parametrize("invalid", [None, "", "pending", "jimeng-create-pending:not-a-uuid",
    "jimeng-create-pending:" + "A" * 32])
def test_invalid_workspace_creation_markers_fail_before_update(tmp_path, invalid):
    store = Store(str(tmp_path / "test.db"))
    account = store.upsert_account({"name": "CN", "execution_mode": "jimeng_web"})
    with pytest.raises(ValueError):
        store.claim_jimeng_workspace_creation(account["id"], invalid)
    assert store.account(account["id"])["project_id"] == ""


@pytest.mark.parametrize("invalid", [True, None, "0", "-1", "1.0", "Canvas-UUID", "1" * 21, "１２３"])
def test_workspace_binding_requires_observed_positive_ascii_decimal_id(tmp_path, invalid):
    store = Store(str(tmp_path / "test.db"))
    account = store.upsert_account({"name": "CN", "execution_mode": "jimeng_web"})
    pending = marker()
    assert store.claim_jimeng_workspace_creation(account["id"], pending)
    with pytest.raises(ValueError):
        store.bind_jimeng_workspace(account["id"], pending, invalid)
    assert store.account(account["id"])["project_id"] == pending
