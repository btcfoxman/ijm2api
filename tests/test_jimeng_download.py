"""CN history previews are not the website's Download-scene results.

Synthetic identities and URLs reproduce the captured 2026-11-02 response shape;
none of these tests contacts a provider or creates a generation.
"""

import copy
import hashlib
from unittest.mock import patch

import pytest

from app.browser import BrowserError
from app.jimeng_client import (
    JimengResultDownloadError, JimengWebClient, verified_result_download_receipts,
)


ACCOUNT = 57
USER = "570000000000001"
SUBMIT = "12345678-1234-4234-8234-123456789abc"
HISTORY = "49740000000001"
ITEM = "7700000000000000001"
VIDEO = "v02870g10004syntheticoriginalvideo"
PREVIEW = "https://v9-artist.vlabvod.com/synthetic-preview.mp4?signature=preview-secret"
DOWNLOAD = "https://v3-dreamnia.jimeng.com/synthetic-origin.mp4?signature=download-secret"


def media(url, *, width=720, height=1280, size=41560189, logo=""):
    return {"video_url": url, "width": width, "height": height, "size": size,
            "fps": 24, "duration": 0, "format": "mp4", "definition": "origin",
            "logo_type": logo, "encryption_key": "", "vid": "", "video_id": "",
            "md5": "a" * 32}


def history():
    return {SUBMIT: {"history_record_id": HISTORY, "uid": USER,
        "task": {"task_id": HISTORY, "submit_id": SUBMIT, "history_id": HISTORY, "status": 50},
        "item_list": [{"common_attr": {"id": ITEM, "local_item_id": ITEM}, "author": None,
            "video": {"video_id": VIDEO, "origin_video": None, "watermark_type": 3,
                "duration_ms": 30042, "transcoded_video": {"origin": media(
                    PREVIEW, size=41722972, logo="synthetic_preview_logo")}},
            "extra": {"credits_consume": 18}}]}}


def detail():
    item = copy.deepcopy(history()[SUBMIT]["item_list"][0])
    item["video"]["watermark_type"] = 1
    item["video"]["transcoded_video"] = {
        "360p": media(DOWNLOAD.replace("origin", "360p"), width=360, height=640, size=1303107),
        "720p": media(DOWNLOAD.replace("origin", "720p"), size=3609714),
        "origin": media(DOWNLOAD),
    }
    return {"item_list": [item]}


def client():
    with patch("app.jimeng_client.CDPBrowser"):
        return JimengWebClient({"id": ACCOUNT, "login_user_id": USER, "cdp_port": 19557})


def poll(data=None, download=None):
    instance = client()
    with patch.object(instance, "_post", side_effect=[data or history(), download or detail()]) as post:
        state = instance.poll(SUBMIT, HISTORY)
    return state, post


def test_success_resolves_the_bound_official_download_instead_of_publishing_preview():
    state, post = poll()
    assert state["status"] == "succeeded" and state["result_urls"] == [DOWNLOAD]
    assert post.call_count == 2
    assert post.call_args_list[0].args[0] == "/mweb/v1/get_history_by_ids"
    assert post.call_args_list[1].args == ("/mweb/v1/get_local_item_list", {
        "item_id_list": [ITEM], "pack_item_opt": {"scene": 1, "need_data_integrity": True},
        "is_for_video_download": True})
    assert "rwm" not in repr(post.call_args_list)
    assert verified_result_download_receipts(state, account_id=ACCOUNT, submit_id=SUBMIT, history_id=HISTORY)
    receipt = state["result_download_receipts"][0]
    assert receipt["source_url_sha256"] == hashlib.sha256(DOWNLOAD.encode()).hexdigest()
    assert receipt["preview_url_sha256"] == hashlib.sha256(PREVIEW.encode()).hexdigest()
    assert (receipt["watermark_type"], receipt["logo_type"]) == (1, "")
    assert (receipt["width"], receipt["height"], receipt["size_bytes"]) == (720, 1280, 41560189)
    assert receipt["size_source"] == "upstream_metadata"  # Not an actual CDN byte-count/hash promise.
    assert "signature=" not in repr(receipt) and "download-secret" not in repr(receipt)
    assert state["task_credit_evidence"]["amount"] == 18


@pytest.mark.parametrize("change", [
    lambda d: d["item_list"][0]["common_attr"].update(id="7700000000000000002"),
    lambda d: d["item_list"][0]["common_attr"].update(local_item_id="7700000000000000002"),
    lambda d: d["item_list"][0]["video"].update(video_id="v-synthetic-foreign"),
    lambda d: d["item_list"][0]["video"]["transcoded_video"]["origin"].update(vid="v-synthetic-foreign"),
    lambda d: d["item_list"][0].update(author={"uid": "foreign-owner"}),
    lambda d: d["item_list"].append(copy.deepcopy(d["item_list"][0])),
])
def test_wrong_item_video_or_author_cannot_be_delivered(change):
    data = detail()
    change(data)
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_BINDING_MISMATCH"
    assert not raised.value.retryable


@pytest.mark.parametrize("history_author", [None, {}, "missing"])
def test_download_additional_author_alias_does_not_require_an_alias_in_history(history_author):
    data, download = history(), detail()
    original = data[SUBMIT]["item_list"][0]
    if history_author == "missing":
        original.pop("author")
    else:
        original["author"] = history_author
    download["item_list"][0]["author"] = {"uid": USER, "sec_uid": "synthetic-current-sec-uid"}
    state, _ = poll(data=data, download=download)
    assert state["result_urls"] == [DOWNLOAD]
    assert verified_result_download_receipts(state, account_id=ACCOUNT, submit_id=SUBMIT, history_id=HISTORY)


@pytest.mark.parametrize("alias", ["sec_uid", "user_unique_id"])
def test_author_alias_is_compared_when_both_history_and_download_provide_it(alias):
    data, download = history(), detail()
    data[SUBMIT]["item_list"][0]["author"] = {alias: "synthetic-original-alias"}
    download["item_list"][0]["author"] = {"uid": USER, alias: "synthetic-foreign-alias"}
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(data=data, download=download)
    assert raised.value.code == "JIMENG_DOWNLOAD_BINDING_MISMATCH"
    assert not raised.value.retryable


@pytest.mark.parametrize("value", [None, 0, 2, 3, True, "1"])
def test_only_the_captured_plain_download_watermark_type_is_accepted(value):
    data = detail()
    data["item_list"][0]["video"]["watermark_type"] = value
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_WATERMARK_UNCONFIRMED"
    assert not raised.value.retryable


@pytest.mark.parametrize("logo", [None, "synthetic_preview_logo", False])
def test_explicitly_empty_logo_is_required(logo):
    data = detail()
    data["item_list"][0]["video"]["transcoded_video"]["origin"]["logo_type"] = logo
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_WATERMARK_UNCONFIRMED"


def test_a_relabelled_preview_is_not_accepted_as_a_download():
    data = detail()
    data["item_list"][0]["video"]["transcoded_video"]["origin"]["video_url"] = PREVIEW
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_PREVIEW_ONLY"
    assert raised.value.retryable


def test_identical_already_plain_url_still_requires_independent_download_proof():
    data = history()
    video = data[SUBMIT]["item_list"][0]["video"]
    video["watermark_type"] = 1
    video["transcoded_video"]["origin"] = media(DOWNLOAD)
    state, post = poll(data=data)
    assert state["result_urls"] == [DOWNLOAD] and post.call_count == 2
    assert verified_result_download_receipts(state, account_id=ACCOUNT, submit_id=SUBMIT)
    instance = client()
    with patch.object(instance, "_post", side_effect=[data, {"item_list": []}]) as post:
        with pytest.raises(JimengResultDownloadError):
            instance.poll(SUBMIT, HISTORY)
        assert post.call_count == 2


@pytest.mark.parametrize("url", [
    "http://v3-dreamnia.jimeng.com/result.mp4",
    "https://user:secret@v3-dreamnia.jimeng.com/result.mp4",
    "https://v3-dreamnia.jimeng.com@evil.invalid/result.mp4",
    "https://v3-dreamnia.jimeng.com.evil.invalid/result.mp4",
    "https://invalid_label.jimeng.com/result.mp4",
    "https://invalid%2flabel.jimeng.com/result.mp4",
    "https://127.0.0.1/result.mp4", "https://[::1]/result.mp4",
    "https://v3-dreamnia.jimeng.com:8443/result.mp4",
    "https://v3-dreamnia.jimeng.com/result.mp4#fragment",
    "https://v3-dreamnia.jimeng.com/result.mp4\n",
    "https://v3-dreamnia.jimeng.com\\@evil.invalid/result.mp4",
])
def test_unsafe_or_unobserved_download_hosts_are_rejected_without_exposing_urls(url):
    data = detail()
    data["item_list"][0]["video"]["transcoded_video"]["origin"]["video_url"] = url
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_URL_INVALID"
    assert not raised.value.retryable and url not in str(raised.value)


def test_official_selector_prefers_original_then_origin_then_highest_rendition():
    data = detail()
    data["item_list"][0]["video"]["origin_video"] = media(DOWNLOAD + "&source=original")
    state, _ = poll(download=data)
    assert state["result_urls"] == [DOWNLOAD + "&source=original"]
    assert state["result_download_receipts"][0]["selection"] == "origin_video"
    data["item_list"][0]["video"]["origin_video"] = None
    data["item_list"][0]["video"]["transcoded_video"].pop("origin")
    state, _ = poll(download=data)
    assert state["result_urls"] == [DOWNLOAD.replace("origin", "720p")]
    assert state["result_download_receipts"][0]["selection"] == "transcoded_video.720p"


@pytest.mark.parametrize("key,value", [("size", 0), ("height", True), ("format", "m3u8"), ("encryption_key", "opaque")])
def test_invalid_media_metadata_is_not_a_deliverable_download(key, value):
    data = detail()
    data["item_list"][0]["video"]["transcoded_video"]["origin"][key] = value
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_METADATA_INVALID"


def test_missing_download_url_is_retryable_without_falling_back_to_another_preview():
    data = detail()
    data["item_list"][0]["video"]["transcoded_video"]["origin"].pop("video_url")
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_UNAVAILABLE" and raised.value.retryable


def test_transport_failure_preserves_settlement_and_retries_only_the_original_results():
    instance = client()
    with patch.object(instance, "_post", side_effect=[history(), BrowserError(DOWNLOAD), history(), detail()]) as post:
        with pytest.raises(JimengResultDownloadError) as raised:
            instance.poll(SUBMIT, HISTORY)
        error = raised.value
        assert error.retryable and error.generation_state["task_credit_evidence"]["amount"] == 18
        assert error.generation_state["history_id"] == HISTORY
        assert "signature=" not in str(error) and "result_urls" not in error.generation_state
        assert error.__suppress_context__ and error.__cause__ is None
        state = instance.poll(SUBMIT, HISTORY)
    assert state["result_urls"] == [DOWNLOAD]
    assert [c.args[0] for c in post.call_args_list] == [
        "/mweb/v1/get_history_by_ids", "/mweb/v1/get_local_item_list",
        "/mweb/v1/get_history_by_ids", "/mweb/v1/get_local_item_list"]
    assert all(c.args[1].get("submit_ids", [SUBMIT]) == [SUBMIT] for c in post.call_args_list)


def test_completion_without_preview_can_resolve_the_official_download():
    data = history()
    data[SUBMIT]["item_list"][0]["video"]["transcoded_video"]["origin"].pop("video_url")
    state, _ = poll(data=data)
    assert state["result_urls"] == [DOWNLOAD]
    assert state["result_download_receipts"][0]["preview_url_sha256"] == ""
    assert state["task_credit_evidence"] == {"amount": 18,
        "field": "item_list[].extra.credits_consume", "submit_id": SUBMIT, "phase": "succeeded"}


def test_incomplete_generation_and_foreign_history_do_not_request_downloads():
    instance = client()
    data = history()
    data[SUBMIT]["task"]["status"] = 20
    with patch.object(instance, "_post", return_value=data) as post:
        assert instance.poll(SUBMIT, HISTORY)["status"] == "running"
        assert post.call_count == 1
    data = history()
    data[SUBMIT]["item_list"][0]["common_attr"].pop("id")
    with patch.object(instance, "_post", return_value=data) as post:
        with pytest.raises(JimengResultDownloadError):
            instance.poll(SUBMIT, HISTORY)
        assert post.call_count == 1


def test_unexpected_commercial_deductions_are_never_silently_accepted():
    data = detail()
    data["deduct_list"] = [{"amount": 1, "use_commerce_benefits_type": "synthetic-unrequested"}]
    with pytest.raises(JimengResultDownloadError) as raised:
        poll(download=data)
    assert raised.value.code == "JIMENG_DOWNLOAD_BENEFIT_UNCONFIRMED" and not raised.value.retryable


@pytest.mark.parametrize("key,value", [
    ("account_id", True), ("account_id", 58), ("submit_id", "foreign"),
    ("history_id", "foreign"), ("source", "history_preview"), ("request_scene", 0),
    ("watermark_type", 3), ("logo_type", "preview"), ("source_url_sha256", "a" * 64),
    ("is_for_video_download", False), ("size_bytes", True), ("url_host", "evil.invalid"),
])
def test_service_receipt_verification_rejects_forged_provenance(key, value):
    state, _ = poll()
    state["result_download_receipts"][0][key] = value
    assert not verified_result_download_receipts(state, account_id=ACCOUNT, submit_id=SUBMIT, history_id=HISTORY)


def test_missing_receipt_can_never_make_a_preview_or_success_publishable():
    assert not verified_result_download_receipts({"result_urls": [PREVIEW]}, account_id=ACCOUNT, submit_id=SUBMIT)
    state, _ = poll()
    state["result_urls"] = [PREVIEW]
    assert not verified_result_download_receipts(state, account_id=ACCOUNT, submit_id=SUBMIT)
