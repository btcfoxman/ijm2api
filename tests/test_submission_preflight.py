"""Keep account checks before generation distinct from an unknown paid outcome."""

from unittest.mock import Mock, patch

import pytest

from app.account_automation import AutomationError
from app.browser import BrowserError, BrowserRejected
from app.catalog import normalize_request
from app.client import DreaminaClient, SubmissionPreflightError
from app.errors import public_error
from app.service import Service
from app.store import Store


READY = {"identity_verified": True, "watermark_ready": True, "agreement_ready": True}
ASSETS = {"images": [], "videos": [], "audios": []}


@pytest.fixture
def execution(tmp_path):
    store = Store(str(tmp_path / "tasks.db"))
    service = Service(store)
    accounts = []
    for index in (1, 2):
        account = store.upsert_account({"execution_mode": "browser", "name": f"synthetic-{index}",
            "cdp_port": 19300 + index, "proxy_url": "socks5://proxy.invalid:20002"})
        accounts.append(store.update_account(account["id"], {"status": "active", "balance": index * 100}))
    request = {"model": "sd-2-5", "prompt": "A synthetic landscape", "duration": 4}
    store.create_task("synthetic-preflight", request, normalize_request(request), execution_backend='browser')
    client = DreaminaClient(accounts[0])
    client.browser = Mock()
    with patch.object(client, "account_state", return_value={"available_balance": 100}), \
         patch.object(client, "upload_inputs", return_value=ASSETS), \
         patch.object(service.browser_runtime, "acquire", side_effect=lambda account: (account, False)), \
         patch.object(service.browser_runtime, "release"), \
         patch.object(service, "_poll") as poll, \
         patch("app.service.DreaminaClient", return_value=client) as factory:
        yield store, service, client, poll, factory
    service.stop()


def assert_failed_once(execution, code):
    store, service, client, poll, factory = execution
    service._run("synthetic-preflight")
    task = store.task("synthetic-preflight")
    assert task["status"] == "failed"
    assert task["error_code"] == code
    assert not task["run_id"] and not task["thread_id"]
    factory.assert_called_once()
    poll.assert_not_called()
    return task


@pytest.mark.parametrize("failure", [
    AutomationError("login_required", "Dreamina identity read could not complete"),
    AutomationError("setup_required", "Dreamina account settings could not be verified"),
    AutomationError("challenge_required", "Dreamina login requires a human security challenge"),
    BrowserError("Dreamina CDP connection was unavailable during verification"),
    BrowserRejected("Dreamina HTTP 401 during settings verification"),
])
def test_readiness_failure_never_sends_generation_or_reports_unknown(execution, failure):
    with patch("app.account_automation.AccountAutomation.ensure_ready", side_effect=failure):
        task = assert_failed_once(execution, "ACCOUNT_VERIFICATION_FAILED")
    execution[2].browser.post.assert_not_called()
    assert task["error_message"] == str(failure)
    assert "automatic resubmission" not in task["error_message"]
    assert public_error(task["error_code"], task["error_message"]) == {
        "code": "ACCOUNT_VERIFICATION_FAILED", "message": "生成服务校验失败，请稍后重试~"}


@pytest.mark.parametrize("ready", [None, [], {},
    {"watermark_ready": True, "agreement_ready": True}, {**READY, "identity_verified": False},
    {**READY, "watermark_ready": False}, {**READY, "agreement_ready": False},
    {**READY, "watermark_ready": "true"}, {**READY, "agreement_ready": 1},
])
def test_incomplete_settings_never_pass_preflight(execution, ready):
    with patch("app.account_automation.AccountAutomation.ensure_ready", return_value=ready):
        assert_failed_once(execution, "ACCOUNT_VERIFICATION_FAILED")
    execution[2].browser.post.assert_not_called()


def test_preflight_error_preserves_cause_before_generation(execution):
    client = execution[2]
    failure = AutomationError("login_required", "Synthetic identity verification failed")
    with patch("app.account_automation.AccountAutomation.ensure_ready", side_effect=failure):
        with pytest.raises(SubmissionPreflightError) as raised:
            client.submit(normalize_request({"model": "sd-2-5", "prompt": "A synthetic scene", "duration": 4}), ASSETS)
    assert raised.value.__cause__ is failure
    client.browser.post.assert_not_called()


def test_generation_transport_failure_remains_unknown_without_retry(execution):
    client = execution[2]
    client.browser.post.side_effect = BrowserError("Connection closed after generation request")
    with patch("app.account_automation.AccountAutomation.ensure_ready", return_value=READY):
        task = assert_failed_once(execution, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN")
    client.browser.post.assert_called_once()
    assert client.browser.post.call_args.args[0] == "/mweb/v1/aigc_draft/generate"
    assert "automatic resubmission is disabled" in task["error_message"]


@pytest.mark.parametrize("response", [None, [], {}, {"aigc_data": "incomplete"},
    {"aigc_data": {}}, {"aigc_data": {"task": []}},
    {"aigc_data": {"task": {"task_id": "accepted-without-submit-id"}}},
    {"aigc_data": {"task": {"submit_id": "accepted-without-task-id"}}},
])
def test_missing_generation_receipt_remains_unknown_without_retry(execution, response):
    client = execution[2]
    client.browser.post.return_value = response
    with patch("app.account_automation.AccountAutomation.ensure_ready", return_value=READY):
        assert_failed_once(execution, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN")
    client.browser.post.assert_called_once()


def test_definitive_generation_rejection_is_not_account_preflight(execution):
    client = execution[2]
    client.browser.post.side_effect = BrowserRejected("Dreamina ret=2001: Synthetic request rejected")
    with patch("app.account_automation.AccountAutomation.ensure_ready", return_value=READY):
        assert_failed_once(execution, "UPSTREAM_REJECTED")
    client.browser.post.assert_called_once()


def test_successful_receipt_is_bound_and_polled_once(execution):
    store, service, client, poll, factory = execution
    client.browser.post.return_value = {"aigc_data": {"history_record_id": "synthetic-history",
        "task": {"task_id": "synthetic-task", "submit_id": "synthetic-run"}}}
    with patch("app.account_automation.AccountAutomation.ensure_ready", return_value=READY):
        service._run("synthetic-preflight")
    task = store.task("synthetic-preflight")
    assert task["status"] == "submitted"
    assert task["run_id"] == "synthetic-run" and task["thread_id"] == "synthetic-history"
    client.browser.post.assert_called_once()
    factory.assert_called_once()
    poll.assert_called_once()


def test_exact_legacy_preflight_signature_changes_only_public_projection(execution):
    store, service, client, _, _ = execution
    raw = "Dreamina identity read could not complete; automatic resubmission is disabled"
    task = store.update_task("synthetic-preflight", {"status": "failed",
        "error_code": "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "error_message": raw})
    result = service.public_task(task)
    assert result["error"] == {"code": "ACCOUNT_VERIFICATION_FAILED", "message": "生成服务校验失败，请稍后重试~"}
    assert store.task("synthetic-preflight") == task
    assert task["error_code"] == "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN" and task["error_message"] == raw
    client.browser.post.assert_not_called()


@pytest.mark.parametrize("code,message", [
    ("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Connection closed after generation request; automatic resubmission is disabled"),
    ("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Dreamina identity request timed out; automatic resubmission is disabled"),
    ("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Dreamina identity read could not complete"),
    ("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "prefix Dreamina identity read could not complete; automatic resubmission is disabled"),
    ("UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "Dreamina identity read could not complete; automatic resubmission is disabled suffix"),
    ("UPSTREAM_TIMEOUT", "Dreamina identity read could not complete; automatic resubmission is disabled"),
])
def test_other_unknown_outcomes_keep_no_resubmission_guidance(code, message):
    assert public_error(code, message) == {"code": code,
        "message": "提交结果未确认，请核对上游任务，暂勿重复提交~"}
