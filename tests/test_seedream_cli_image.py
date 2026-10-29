"""Seedream Pro uses the captured CLI model, never an unobserved browser draft."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import time
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_client import CanvasClient
from app.image_client import (
    DreaminaImageClient, build_image_submit_body, image_model_requires_cli,
    resolve_image_model,
)
from app.image_request import normalize_image_request
from app.service import Service
from app.store import Store


CATALOG = json.loads((Path(__file__).parent / "fixtures/seedream_5_0_pro_cli_catalog.json").read_text())


def normalized(**changes):
    request = {"model": "seedream-5.0-pro", "prompt": "A synthetic sphere", **changes}
    return normalize_image_request(request, resolve_image_model(request["model"]))


class CatalogRunner:
    """Local metadata replay only; a generation command is a test failure."""
    timeout = 10

    def __init__(self, items=None):
        self.items = deepcopy(CATALOG["items"] if items is None else items)
        self.calls = []
        self.uploaded = {}

    def profile(self, account):
        return "synthetic-seedream-profile"

    def run(self, account, args, **kwargs):
        self.calls.append(list(args))
        action = args[:2]
        if action == ["model", "list"]:
            data = {"items": self.items}
        elif action == ["canvas", "ls"]:
            data = {"items": []}
        elif action == ["canvas", "create"]:
            data = {"project": {"projectId": args[-1]}}
        elif action == ["resource", "upload"]:
            self.uploaded[args[-1]] = args[args.index("--type") + 1]
            data = {"resourceId": args[-1]}
        elif action == ["resource", "get"]:
            data = {"resourceId": args[2], "status": "success", "type": self.uploaded[args[2]]}
        elif action == ["node", "create"]:
            assert args[2] == "image" and "--run" not in args
            data = {"node": {"nodeId": args[args.index("--node-id") + 1]}}
        elif action == ["node", "quote"]:
            data = {"items": [{"nodeId": args[-1]}], "confirmable": True,
                    "totalMaxCredits": 10, "draftVersion": 1, "confirmationRequired": True}
        else:
            raise AssertionError("The replay must not confirm, generate, authorize or query real tasks")
        return {"ok": True, "data": data}


@pytest.mark.parametrize("resolution", ["1.5k", "2k", "4k"])
@pytest.mark.parametrize("ratio", ["21:9", "16:9", "3:2", "4:3", "1:1", "3:4", "2:3", "9:16"])
@pytest.mark.parametrize("reference_count", [0, 1])
def test_real_catalog_replay_keeps_exact_pro_model_and_flags(tmp_path, resolution, ratio, reference_count):
    request = normalized(resolution=resolution, ratio=ratio, n=4)
    runner = CatalogRunner()
    client = CanvasClient(runner, {"id": 1, "login_user_id": "synthetic-user"})
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
    context = client.prepare(request, "synthetic-seedream-task", {
        "images": [{"path": str(reference)}] * reference_count, "videos": [], "audios": []},
        deadline=time.monotonic() + 10)
    assert context["model"] == "seedream_5.0_pro"
    assert context["mode"] == ("i2i" if reference_count else "t2i")
    create = next(args for args in runner.calls if args[:2] == ["node", "create"])
    assert create[create.index("--model") + 1] == "seedream_5.0_pro"
    assert create[create.index("--resolution") + 1] == resolution.upper()
    assert create[create.index("--ratio") + 1] == ratio
    assert create[create.index("--count") + 1] == "4"
    assert sum(args[:2] == ["model", "list"] for args in runner.calls) == 1
    assert not any(args[:2] in (["node", "confirm"], ["node", "run"]) for args in runner.calls)


@pytest.mark.parametrize("replacement", ["seedream_5.0_lite", "dreamina_gpt_image_2_5_flare"])
def test_missing_pro_catalog_never_substitutes_another_model(replacement):
    other = deepcopy(CATALOG["items"][0])
    other.update(model=replacement, displayName=replacement, aliases=[])
    runner = CatalogRunner([other])
    with pytest.raises(CanvasCLIError) as caught:
        CanvasClient(runner, {"id": 1}).preflight_model(normalized())
    assert caught.value.code == "CLI_CAPABILITY_UNAVAILABLE"
    assert caught.value.details["error"]["reason"] == "model_not_in_catalog"
    assert runner.calls == [["model", "list", "--type", "image"]]


@pytest.mark.parametrize("body", [
    {"resolution": "1k"}, {"resolution": "3k"}, {"size": "1024x1024"},
    {"size": "1536x1024"}, {"size": "2048x2048", "resolution": "4k"},
    {"size": "2048x2048", "ratio": "16:9"},
    {"size": "1k", "resolution": "2k"}, {"size": "2k", "resolution": "4k"},
    {"size": "16:9(1k)", "resolution": "2k"},
    {"model": "seedream-5.0-pro-1k", "resolution": "2k"},
    {"model": "seedream-5.0-pro-1k"}, {"ratio": "9:21"}, {"n": 5},
    {"image_urls": [f"https://media.test/{i}.png" for i in range(11)]},
    {"image_urls": ["dreamina-image:browser-only-resource"]},
])
def test_unadvertised_seedream_parameters_fail_before_dispatch(body):
    with pytest.raises(ValueError):
        normalized(**body)


def test_seedream_defaults_aliases_and_exact_square_sizes():
    assert normalized()["resolution"] == "2k"
    for tier, size in [("1.5k", "1536x1536"), ("2k", "2048x2048"), ("4k", "4096x4096")]:
        assert normalized(size=size)["resolution"] == tier
        assert normalized(model="seedream-5.0-pro-" + tier)["resolution"] == tier
        assert resolve_image_model("seedream-5.0-pro-" + tier) == "seedream_5.0_pro"
    assert normalized(aspect_ratio="16:9")["aspect_ratio"] == "16:9"
    edit = normalized(prompt="", image_urls=[f"https://media.test/{i}.png" for i in range(10)])
    assert len(edit["images"]) == 10 and edit["prompt"] == ""
    with pytest.raises(ValueError):
        normalized(prompt="")


def test_new_model_cannot_reach_the_browser_protocol(monkeypatch):
    request = normalized()
    assert image_model_requires_cli(request["upstream_model"])
    with pytest.raises(ValueError, match="requires Canvas CLI"):
        build_image_submit_body(request, [])
    browser = DreaminaImageClient(12345)
    monkeypatch.setattr(browser.browser, "post", Mock(side_effect=AssertionError("unexpected browser request")))
    with pytest.raises(ValueError, match="requires Canvas CLI"):
        browser.submit(request, [])
    browser.browser.post.assert_not_called()


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("IJM_DATA_DIR", str(tmp_path / "module"))
    monkeypatch.setenv("IJM_API_KEY", "synthetic-api")
    monkeypatch.setenv("IJM_ADMIN_TOKEN", "synthetic-admin")
    main = importlib.import_module("app.main")
    store = Store(str(tmp_path / "tasks.db"))
    store.update_settings({"canvas_cli_enabled": True})
    service = Service(store)
    monkeypatch.setattr(service._pool, "submit", Mock())
    monkeypatch.setattr(service.browser_runtime, "acquire", Mock(side_effect=AssertionError("no Chrome")))
    monkeypatch.setattr(service.browser_runtime, "release", Mock())
    monkeypatch.setattr(service.browser_runtime, "close_all_idle", Mock())
    for key, value in {"store": store, "service": service, "API_KEY": "synthetic-api"}.items():
        monkeypatch.setattr(main, key, value)
    api = TestClient(main.app)  # No lifespan: no worker, browser or CLI starts.
    try:
        yield store, service, api
    finally:
        api.close()
        service.stop()


def owner(store, name, mode, *, priority=0, balance=100):
    value = store.upsert_account({"name": name, "execution_mode": mode,
        "proxy_url": "socks5://proxy.test:20001", "priority": priority})
    store.update_account(value["id"], {"enabled": True, "status": "active", "balance": balance,
        "login_user_id": "synthetic-user", "cli_user_id": "synthetic-user",
        "watermark_ready": True, "agreement_ready": True, "cli_auth_status": "ready"})
    return value["id"]


def test_cli_only_admission_retains_priority_and_smallest_funded_account(context):
    store, service, _ = context
    owner(store, "higher-browser", "browser", priority=100, balance=10000)
    owner(store, "larger-cli", "canvas_cli", priority=10, balance=100)
    expected = owner(store, "smallest-funded-cli", "canvas_cli", priority=10, balance=50)
    owner(store, "lower-priority-cli", "canvas_cli", priority=0, balance=25)
    result = service.create_image({"model": "seedream-5.0-pro", "prompt": "fixture"})
    task = store.task(result["id"])
    assert task["execution_backend"] == "canvas_cli" and task["account_id"] is None
    selected = service._reserve_account(20, task_id=task["id"])
    assert selected["id"] == expected
    service._release_account(expected, task["id"])


def test_browser_only_pool_is_ineligible_for_seedream_and_gpt_is_unchanged(context):
    store, service, _ = context
    browser_id = owner(store, "existing-browser", "browser", priority=100)
    new = service.create_image({"model": "seedream-5.0-pro", "prompt": "fixture"})
    assert not service._has_eligible_account(0, set(), new["id"])
    assert service._reserve_account(0, task_id=new["id"]) is None
    old = service.create_image({"model": "gpt-image-2.5-flare", "prompt": "fixture"})
    assert store.task(old["id"])["execution_backend"] == ""
    assert store.task(old["id"])["normalized"]["resolution"] == "1k"
    selected = service._reserve_account(0, task_id=old["id"])
    assert selected["id"] == browser_id
    service._release_account(browser_id, old["id"])


@pytest.mark.parametrize("rejection", ["funding_reselection", "model_not_in_catalog", "insufficient_credits"])
def test_safe_unpaid_reselection_keeps_cli_only_requirement(context, rejection):
    store, service, _ = context
    owner(store, "preferred-browser", "browser", priority=100, balance=10000)
    first = owner(store, "first-cli", "canvas_cli", priority=20, balance=50)
    second = owner(store, "second-cli", "canvas_cli", priority=10, balance=100)
    result = service.create_image({"model": "seedream-5.0-pro", "prompt": "fixture"})
    identifier = result["id"]
    assert service._reserve_account(20, task_id=identifier)["id"] == first
    store.update_task(identifier, {"account_id": first, "status": "preparing"})
    if rejection == "model_not_in_catalog":
        assert service._exclude_model_account(identifier, first, {"code": "CLI_CAPABILITY_UNAVAILABLE",
            "message": "fixture model absent", "requested_model": "seedream-5.0-pro", "kind": "image"})
        assert store.task(identifier)["attempted_account_ids"] == []
    elif rejection == "insufficient_credits":
        assert service._handle_credit_rejection(identifier, first, "fixture insufficient credits")
        assert store.task(identifier)["attempted_account_ids"] == [first]
    else:
        assert service._requeue_unaccepted_task(identifier)
    service._release_account(first, identifier)
    assert store.task(identifier)["execution_backend"] == "canvas_cli"
    selected = service._reserve_account(20, task_id=identifier, exclude_ids={first})
    assert selected["id"] == second
    service._release_account(second, identifier)


def test_backend_requirement_survives_an_unbound_queued_record(context):
    store, service, _ = context
    browser = owner(store, "only-browser", "browser", priority=100)
    result = service.create_image({"model": "seedream-5.0-pro", "prompt": "fixture"})
    store.update_task(result["id"], {"execution_backend": ""})
    service._credit_reservations["another-browser-task"] = (browser, 10)
    assert service._reserve_account(0, task_id=result["id"]) is None
    assert not service._has_eligible_account(0, set(), result["id"])
    assert not service._has_waiting_credit_account(0, set(), result["id"])
    assert service._credit_reservations["another-browser-task"] == (browser, 10)


def test_gpt_unpaid_reselection_still_allows_original_backend_policy(context):
    store, service, _ = context
    result = service.create_image({"model": "gpt-image-2.5-flare", "prompt": "fixture"})
    store.update_task(result["id"], {"status": "preparing", "execution_backend": "canvas_cli"})
    assert service._requeue_unaccepted_task(result["id"])
    assert store.task(result["id"])["execution_backend"] == ""


def test_quote_best_fit_ignores_a_smaller_browser_balance(context, monkeypatch):
    store, service, _ = context
    owner(store, "smaller-browser", "browser", priority=10, balance=50)
    cli = owner(store, "only-cli", "canvas_cli", priority=10, balance=100)
    result = service.create_image({"model": "seedream-5.0-pro", "prompt": "fixture"})
    identifier = result["id"]
    account = service._reserve_account(0, task_id=identifier)
    assert account["id"] == cli
    store.update_task(identifier, {"account_id": cli, "status": "preparing"})
    client = Mock()
    client.identity.return_value = {"user_id": "synthetic-user", "available_balance": 100}
    client.preflight_model.return_value = CATALOG["items"][0]
    def prepare(request, task_id, assets, *, persist, deadline):
        ctx = {"backend": "canvas_cli", "account_id": cli, "task_id": task_id,
            "kind": "image", "profile": "synthetic-profile", "phase": "quoted",
            "project_id": "synthetic-project", "node_id": "synthetic-node",
            "submit_id": "synthetic-submit", "quoted_credits": 10}
        persist(ctx)
        return ctx
    client.prepare.side_effect = prepare
    client.submit.side_effect = lambda ctx, ceiling, **kwargs: {
        **ctx, "phase": "accepted", "resource_ids": ["synthetic-resource"]}
    monkeypatch.setattr(service, "canvas_client", Mock(return_value=client))
    monkeypatch.setattr(service, "_poll_canvas", Mock())
    try:
        assert service._execute_canvas(identifier, account, time.monotonic() + 10) is True
        assert client.prepare.call_count == client.submit.call_count == 1
        service._poll_canvas.assert_called_once()
    finally:
        service._release_account(cli, identifier)


def test_public_responses_and_model_aliases_are_query_only_after_creation(context):
    store, service, api = context
    headers = {"Authorization": "Bearer synthetic-api"}
    response = api.post("/v1/responses", headers=headers, json={"model": "seedream-5.0-pro",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "fixture"},
            {"type": "input_image", "image_url": "https://media.test/ref.png"}]}],
        "size": "2048x2048", "background": True})
    assert response.status_code == 200
    identifier = response.json()["id"]
    assert response.json()["status"] == "in_progress"
    assert store.task(identifier)["execution_backend"] == "canvas_cli"
    assert len(store.task(identifier)["normalized"]["images"]) == 1
    assert service._pool.submit.call_count == 1
    store.update_task(identifier, {"status": "succeeded", "result_urls": ["https://media.test/result.png"]})
    result = api.get("/v1/responses/" + identifier, headers=headers)
    assert result.status_code == 200 and result.json()["status"] == "completed"
    assert result.json()["output"][0]["url"] == "https://media.test/result.png"
    assert service._pool.submit.call_count == 1
    listed = api.get("/v1/models", headers=headers)
    names = {item["id"] for item in listed.json()["data"]}
    assert {"seedream-5.0-pro", "seedream-5.0-pro-1.5k", "seedream-5.0-pro-2k", "seedream-5.0-pro-4k"} <= names
    assert "seedream-5.0-pro-1k" not in names and "seedream_5.0_lite" not in names
    assert {"gpt-image-2.5-flare", "gpt-image-2.5-flare-2k", "gpt-image-2.5-flare-4k"} <= names
    invalid = api.post("/v1/responses", headers=headers, json={"model": "seedream-5.0-pro", "prompt": "fixture", "size": "1024x1024"})
    assert invalid.status_code == 422 and invalid.headers["X-IJMAPI-Submit-Outcome"] == "not-accepted"
    assert service._pool.submit.call_count == 1


@pytest.mark.parametrize("code", ["CLI_SUBMISSION_UNCONFIRMED", "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "UPSTREAM_TIMEOUT"])
def test_response_does_not_erase_unknown_paid_identity_or_cost(context, code):
    store, service, api = context
    row = service.create_image({"model": "seedream-5.0-pro", "prompt": "fixture"})
    original = {"backend": "canvas_cli", "submit_id": "original-submit", "node_id": "original-node",
                "financial_outcome": "unconfirmed", "resource_ids": ["original-resource"]}
    store.update_task(row["id"], {"status": "failed", "error_code": code, "error_message": "fixture unresolved",
        "upstream_context": original, "actual_cost": None})
    before = store.task(row["id"])
    response = api.get("/v1/responses/" + row["id"], headers={"Authorization": "Bearer synthetic-api"})
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert response.json()["error"]["code"] == code
    after = store.task(row["id"])
    assert after["upstream_context"] == before["upstream_context"] == original
    assert after["actual_cost"] is None and service._pool.submit.call_count == 1
