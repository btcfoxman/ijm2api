"""Region denial precedes any browser/CLI/material or paid generation work."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.image_client import resolve_image_model
from app.image_request import normalize_image_request
from app.region_policy import DEFAULT_REGION_GENERATION_POLICY
from test_canvas_execution import context, owner, task
from test_mixed_execution_admission import mixed


def policy_for(region, models):
    policy = deepcopy(DEFAULT_REGION_GENERATION_POLICY)
    policy[region] = {"enabled": True, "models": models}
    return {"region_generation_policy": policy}


@pytest.mark.parametrize("kind", ["video", "image"])
def test_every_enabled_region_denied_rejects_before_task_persistence_or_dispatch(context, monkeypatch, kind):
    store, service, _ = context
    account = owner(store)
    store.update_account(account["id"], {"region": "cn"})
    disabled = owner(store, "disabled-us")
    store.update_account(disabled["id"], {"region": "us", "enabled": False})
    create_task = Mock(wraps=store.create_task)
    monkeypatch.setattr(store, "create_task", create_task)
    request = {"model": "gpt-image-2.5-flare" if kind == "image" else "sd-2-0-mini", "prompt": "Synthetic"}
    with pytest.raises(ValueError, match="region"):
        (service.create_image if kind == "image" else service.create)(request)
    create_task.assert_not_called()
    service._pool.submit.assert_not_called()
    service.browser_runtime.acquire.assert_not_called()
    assert store.tasks() == []


@pytest.mark.parametrize("kind", ["video", "image"])
def test_no_configured_accounts_preserves_existing_queue_admission_contract(context, kind):
    store, service, _ = context
    request = {"model": "gpt-image-2.5-flare" if kind == "image" else "sd-2-0-mini", "prompt": "Synthetic"}
    response = (service.create_image if kind == "image" else service.create)(request)
    assert store.task(response["id"])["status"] == "queued"
    service._pool.submit.assert_called_once()
    service.browser_runtime.acquire.assert_not_called()


@pytest.mark.parametrize("kind", ["video", "image"])
@pytest.mark.parametrize("mode", ["canvas_cli", "browser"])
def test_scheduler_skips_denied_high_priority_mainland_and_uses_allowed_us(mixed, kind, mode):
    denied = mixed.add("denied-cn", mode=mode, priority=9, kind=kind)
    allowed = mixed.add("allowed-us", mode=mode, priority=1, kind=kind)
    mixed.store.update_account(denied["id"], {"region": "cn"})
    mixed.store.update_account(allowed["id"], {"region": "us"})
    mixed.store.update_settings(policy_for("cn", {"sd-2-0-mini": {"min_duration": 5, "max_duration": 10}}))
    identifier = mixed.create(kind)
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result["status"] == "succeeded", result["error_message"]
    assert result["account_id"] == allowed["id"]
    mixed.clients[denied["id"]].identity.assert_not_called()
    mixed.clients[denied["id"]].prepare.assert_not_called()
    mixed.browser[denied["id"]].account_state.assert_not_called()
    mixed.browser[denied["id"]].upload_inputs.assert_not_called()
    mixed.browser[denied["id"]].submit.assert_not_called()


def test_only_explicit_jimeng_transport_can_schedule_without_an_account_proxy(context):
    store, service, _ = context
    international = owner(store, "international-cn", mode="browser", priority=9)
    direct = owner(store, "direct-jimeng", mode="jimeng_web")
    for account in (international, direct):
        store.update_account(account["id"], {"region": "cn", "proxy_url": ""})
    store.update_settings(policy_for("cn", {"sd-2-0-mini": {"min_duration": 4, "max_duration": 15}}))
    row = task(store)
    reserved = service._reserve_account(task_id=row["id"])
    try:
        assert reserved and reserved["id"] == direct["id"]
        assert reserved["execution_mode"] == "jimeng_web"
    finally:
        if reserved:
            service._release_account(reserved["id"], row["id"])


@pytest.mark.parametrize("kind", ["video", "image"])
@pytest.mark.parametrize("mode", ["canvas_cli", "browser"])
def test_policy_change_after_reservation_rejects_before_identity_browser_or_upload(mixed, monkeypatch, kind, mode):
    account = mixed.add("changed-policy", mode=mode, kind=kind)
    identifier = mixed.create(kind)
    reserve = mixed.service._reserve_account

    def reserve_then_restrict(*args, **kwargs):
        selected = reserve(*args, **kwargs)
        if selected:
            mixed.store.update_settings(policy_for("other", {}))
        return selected

    monkeypatch.setattr(mixed.service, "_reserve_account", reserve_then_restrict)
    mixed.service._run(identifier)
    result = mixed.store.task(identifier)
    assert result["status"] == "failed"
    assert result["error_code"] == "REGION_GENERATION_DENIED", result["error_message"]
    assert result["attempted_account_ids"] == []
    mixed.clients[account["id"]].identity.assert_not_called()
    mixed.clients[account["id"]].preflight_model.assert_not_called()
    mixed.clients[account["id"]].prepare.assert_not_called()
    mixed.clients[account["id"]].submit.assert_not_called()
    mixed.browser[account["id"]].account_state.assert_not_called()
    mixed.browser[account["id"]].upload_inputs.assert_not_called()
    mixed.browser[account["id"]].submit.assert_not_called()
    mixed.service.browser_runtime.acquire.assert_not_called()


def test_low_balance_effective_image_alias_cannot_escape_the_region_allowlist(context, monkeypatch):
    store, service, _ = context
    account = owner(store, balance=20)
    store.update_settings({"low_balance_seedream_target_model": "seedream-5.0-lite",
                           **policy_for("other", {"seedream-5.0-pro": {}})})
    request = {"model": "seedream-5.0-pro", "prompt": "Synthetic", "resolution": "2k"}
    normalized = normalize_image_request(request, resolve_image_model(request["model"]))
    assert service._region_denial(account, normalized)
    create_task = Mock(wraps=store.create_task)
    monkeypatch.setattr(store, "create_task", create_task)
    with pytest.raises(ValueError, match="region"):
        service.create_image(request)
    create_task.assert_not_called()
    service._pool.submit.assert_not_called()
    store.update_settings(policy_for("other", {"seedream-5.0-pro": {}, "seedream-5.0-lite": {}}))
    assert service._region_denial(account, normalized) is None
    response = service.create_image(request)
    assert store.task(response["id"])["status"] == "queued"
