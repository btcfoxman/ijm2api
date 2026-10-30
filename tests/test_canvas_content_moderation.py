"""Caller wording changes must preserve original failures and credit evidence."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.canvas_client import _failure_code
from app.errors import public_error
from app.service import Service
from test_canvas_operation_lifecycle import operation_reply


EXPECTED = "检测到内容可能违规，请修改提示词或参考素材后重试~"


@pytest.mark.parametrize("message", [
    "", "The video may contain third-party content",
    "Canvas generation failed (20115)",
])
def test_observed_20115_has_stable_possible_content_wording(message):
    error = public_error("20115", message)
    assert error == {"code": "CONTENT_MODERATION_FAILED", "message": EXPECTED}
    assert _failure_code(20115, message) == error["code"]
    # Downstream normalization may see the stored original text again.
    assert public_error(error["code"], message) == error
    assert public_error(error["code"], error["message"]) == error


@pytest.mark.parametrize("code,message", [
    ("20114", "Canvas generation failed (20114)"),
    ("20115", "Canvas generation failed (20115)"),
    ("99999", "Canvas generation failed (99999)"),
    ("UPSTREAM_REVIEW_FAILED", "Canvas generation failed (2059)"),
    ("UPSTREAM_REVIEW_SERVICE_ERROR", "Canvas generation failed (1064)"),
    ("UPSTREAM_FAILED", "Canvas generation failed (failed)"),
    ("unknown", "Canvas generation failed (unknown)"),
    ("", "Canvas generation failed"),
    ("UPSTREAM_FAILED", " cAnVaS gEnErAtIoN fAiLeD (other) "),
])
def test_generic_terminal_fallback_has_user_requested_possible_wording(code, message):
    error = public_error(code, message)
    assert error["message"] == EXPECTED
    assert public_error(error["code"], error["message"]) == error


@pytest.mark.parametrize("code", [
    "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "UPSTREAM_TIMEOUT",
    "cli.invalid_service_response", "CLI_SUBMISSION_UNCONFIRMED",
])
def test_unknown_submission_is_not_reclassified_by_generic_terminal_text(code):
    result = public_error(code, "Canvas generation failed (20115)")
    assert result["code"] == code.upper()
    assert result["message"] != EXPECTED


@pytest.mark.parametrize("message", [
    "prefix Canvas generation failed (20114)",
    "Canvas generation failed (20114) extra",
    "Canvas generation failed (nested(reason))",
    "Canvas generation failed (line\nbreak)",
    "Canvas generation failed (" + "x" * 81 + ")",
])
def test_unrelated_or_malformed_text_does_not_match_the_terminal_signature(message):
    assert public_error("UPSTREAM_FAILED", message)["message"] != EXPECTED


def test_explicit_service_errors_and_old_specific_categories_are_preserved():
    assert _failure_code("1064", "ReviewRpc") == "UPSTREAM_REVIEW_SERVICE_ERROR"
    assert public_error("1064", "ReviewRpc")["message"] == "上游审核服务异常，请稍后重试~"
    assert _failure_code("99999", "provider is busy") == "UPSTREAM_TEMPORARY"
    assert _failure_code("20114", "provider failed") != "CONTENT_MODERATION_FAILED"
    old = public_error("OUTPUT_VIDEO_COPYRIGHT", "The video may contain third-party content")
    assert old == {"code": "OUTPUT_VIDEO_COPYRIGHT",
                   "message": "生成的视频内容违规，请修改描述后重试，积分已返还~"}


@pytest.mark.parametrize("code", ["20114", "20115", "unknown"])
def test_public_video_projection_does_not_rewrite_error_or_financial_records(code):
    task = {"id": "ijm_" + "a" * 16, "status": "failed", "progress": 100,
        "model": "sd-2-0-mini", "normalized": {"kind": "video"}, "created_at": 123,
        "account_id": 1, "result_urls": [], "error_code": code,
        "error_message": f"Canvas generation failed ({code})",
        "estimated_cost": 40, "reserved_cost": 40, "actual_cost": None,
        "financial_outcome": "unconfirmed", "balance_before": 100,
        "upstream_context": {"upstream_error_code": code},
        "cost_evidence": {"canvas_resource_snapshots": [{"amount": 20, "settled": False}]}}
    before = deepcopy(task)
    service = object.__new__(Service)
    service._refresh_canvas_result_urls = Mock(side_effect=lambda row: row)
    response = service.public_task(task)
    assert response["status"] == "failed" and response["error"]["message"] == EXPECTED
    assert public_error(response["error"]["code"], task["error_message"]) == response["error"]
    assert task == before


@pytest.mark.parametrize("upstream_code", [20114, 20115, 99999])
def test_original_terminal_poll_keeps_raw_failure_and_precharge_facts(tmp_path, upstream_code):
    runner, client, context, _ = operation_reply(tmp_path, state="failed", resource_state="failed")
    context.update(estimated_cost=40, reserved_cost=40, actual_cost=None,
                   financial_outcome="unconfirmed")
    before = deepcopy(context)
    runner.resource_error_code = upstream_code
    result = client.poll(context)
    assert result["status"] == "failed" and result["progress"] == 100
    assert result["upstream_error_code"] == str(upstream_code)
    assert result["error_message"] == f"Canvas generation failed ({upstream_code})"
    if upstream_code == 20115:
        assert result["error_code"] == "CONTENT_MODERATION_FAILED"
    else:
        # Existing typed storage categories stay distinct from presentation.
        assert result["error_code"] == ("20114" if upstream_code == 20114 else "UPSTREAM_TEMPORARY")
    assert public_error(result["error_code"], result["error_message"])["message"] == EXPECTED
    assert result["credit_facts"] == [{"source": "precharge", "amount": 20,
        "field": "resource.creditsAmount", "resource_id": context["resource_ids"][0],
        "submit_id": context["submit_id"], "settled": False}]
    assert context == before
    assert not any(args[:2] in (["node", "run"], ["node", "confirm"], ["resource", "upload"])
                   for args in runner.calls)
