"""Keep actionable media failure facts without persisting HTTP secrets."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from urllib3.exceptions import MaxRetryError

from app.media import MediaError, _download, media_error_diagnostic
from test_canvas_execution import context, fake_client, owner, task
from test_canvas_media_deadline import response_fixture


SOURCE = "https://caller.example/reference.png?X-Amz-Signature=source-signature-secret&token=source-token-secret"
PROXY = "socks5://proxy-user-secret:proxy-password-secret@proxy.test:20001"
SECRETS = (SOURCE, "source-signature-secret", "source-token-secret", "proxy-user-secret",
           "proxy-password-secret", "request-authorization-secret", "request-body-secret")


def request():
    return requests.Request("GET", SOURCE, headers={"Authorization": "Bearer request-authorization-secret"},
                            data="request-body-secret").prepare()


def response(status):
    value = requests.Response()
    value.status_code = status
    value.url = SOURCE
    value.request = request()
    value.raw = SimpleNamespace(close=lambda: None, release_conn=lambda: None)
    return value


def assert_no_secrets(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    for secret in SECRETS:
        assert secret not in text
    for field in ("traceback", "request", "response", "args"):
        assert field not in value if isinstance(value, dict) else True


@pytest.fixture
def session(monkeypatch):
    value = Mock()
    monkeypatch.setattr("app.media.requests.Session", lambda: value)
    monkeypatch.setattr("app.media.time.sleep", lambda _: None)
    return value


@pytest.mark.parametrize("status,attempts", [(403, 1), (503, 2)])
def test_http_status_and_attempts_preserve_existing_retry_policy_without_http_secrets(session, status, attempts):
    session.get.side_effect = lambda *args, **kwargs: response(status)
    with pytest.raises(MediaError) as found:
        _download(SOURCE, "images", PROXY, allow_direct_fallback=False)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic == {"category": "http", "http_status": status, "attempt_count": attempts,
        "retry_count": attempts - 1, "route": "account_proxy", "media_kind": "images",
        "exception_types": ["MediaError", "HTTPError"]}
    assert session.get.call_count == attempts
    assert session.close.call_count == attempts
    for call in session.get.call_args_list:
        assert call.kwargs["proxies"] == {"http": PROXY.replace("socks5://", "socks5h://"),
                                          "https": PROXY.replace("socks5://", "socks5h://")}
        assert call.kwargs["allow_redirects"] is True
        assert call.kwargs["timeout"] == (10, 45)
    assert session.trust_env is False
    assert_no_secrets(diagnostic)


@pytest.mark.parametrize("error_type,category", [(requests.ReadTimeout, "timeout"),
    (requests.exceptions.ProxyError, "proxy"), (requests.exceptions.SSLError, "tls"),
    (requests.ConnectionError, "connection"), (requests.exceptions.ChunkedEncodingError, "transport")])
def test_real_requests_transport_errors_keep_only_class_chain(session, error_type, category):
    def fail(*args, **kwargs):
        cause = MaxRetryError(None, SOURCE, reason=Exception(PROXY))
        error = error_type(f"raw transport error: {SOURCE} {PROXY}", request=request())
        raise error from cause
    session.get.side_effect = fail
    with pytest.raises(MediaError) as found:
        _download(SOURCE, "images", PROXY, allow_direct_fallback=False)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic["category"] == category
    assert diagnostic["attempt_count"] == 2 and diagnostic["retry_count"] == 1
    assert diagnostic["exception_types"][:3] == ["MediaError", error_type.__name__, "MaxRetryError"]
    assert "http_status" not in diagnostic
    assert session.get.call_count == 2
    assert_no_secrets(diagnostic)


def test_retry_deadline_keeps_first_attempt_facts_without_opening_second_session(monkeypatch, session):
    clock = {"now": 0.0}
    def fail(*args, **kwargs):
        clock["now"] = 1
        raise requests.ReadTimeout(SOURCE + PROXY, request=request())
    session.get.side_effect = fail
    monkeypatch.setattr("app.media.time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=Mock()))
    with pytest.raises(MediaError, match="deadline exceeded") as found:
        _download(SOURCE, "images", PROXY, allow_direct_fallback=False, deadline=1)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic["category"] == "deadline"
    assert diagnostic["attempt_count"] == 1 and diagnostic["retry_count"] == 0
    assert diagnostic["route"] == "account_proxy"
    assert clock["now"] == 1 and session.get.call_count == session.close.call_count == 1
    assert session.get.call_args.kwargs["timeout"].total == 1
    assert_no_secrets(diagnostic)


def test_retry_transport_error_does_not_retain_previous_http_status(session):
    session.get.side_effect = [response(503), requests.ReadTimeout(SOURCE + PROXY, request=request())]
    with pytest.raises(MediaError) as found:
        _download(SOURCE, "images", PROXY, allow_direct_fallback=False)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic["category"] == "timeout" and diagnostic["attempt_count"] == 2
    assert "http_status" not in diagnostic
    assert_no_secrets(diagnostic)


@pytest.mark.parametrize("host,attempts,proxy_routes", [
    ("caller.example", 3, [True, True, False]),
    ("media.juxingdonghua.com", 4, [False, True, True, False])])
def test_existing_optional_public_direct_routes_remain_unchanged(session, host, attempts, proxy_routes):
    source = SOURCE.replace("caller.example", host)
    session.get.side_effect = requests.ReadTimeout(SOURCE + PROXY, request=request())
    with pytest.raises(MediaError) as found:
        _download(source, "images", PROXY)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic["category"] == "timeout" and diagnostic["attempt_count"] == attempts
    assert diagnostic["retry_count"] == attempts - 1 and diagnostic["route"] == "direct_media"
    assert [bool(call.kwargs["proxies"]) for call in session.get.call_args_list] == proxy_routes
    assert_no_secrets(diagnostic)


def test_diagnostic_refilters_mutated_fields_and_bounds_cyclic_type_chain():
    error = MediaError(SOURCE + PROXY)
    cause = type("CredentialSensitiveType", (Exception,), {})(SOURCE + PROXY)
    error.__cause__ = cause
    cause.__cause__ = error
    error.diagnostic = {"category": {}, "route": PROXY, "http_status": SOURCE, "attempt_count": True,
                        "media_kind": SOURCE, "media_index": -1, "url": SOURCE, "request": request()}
    assert media_error_diagnostic(error) == {"category": "unknown", "exception_types": ["MediaError", "Exception"]}
    assert_no_secrets(media_error_diagnostic(error))


def test_invalid_data_url_retains_safe_category_without_any_http_attempt(session):
    with pytest.raises(MediaError) as found:
        _download("data:image/png;base64,!", "images", PROXY)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic["category"] == "invalid_source" and diagnostic["attempt_count"] == 0
    assert diagnostic["media_kind"] == "images"
    session.get.assert_not_called()


@pytest.mark.parametrize("source,proxy,category", [
    ("data:image/png;base64,", PROXY, "empty"),
    ("data:text/plain;base64,eA==", PROXY, "content_type"),
    ("https://user:password@caller.example/image.png", PROXY, "invalid_source"),
    (SOURCE, "", "proxy_required")])
def test_validation_categories_require_no_http_attempt(session, source, proxy, category):
    with pytest.raises(MediaError) as found:
        _download(source, "images", proxy)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic["category"] == category and diagnostic["attempt_count"] == 0
    assert "http_status" not in diagnostic and "route" not in diagnostic
    assert_no_secrets(diagnostic)
    session.get.assert_not_called()


def test_stream_deadline_keeps_actual_attempt_and_http_facts_without_renewal(monkeypatch, session):
    clock = {"now": 0.0}
    value = response_fixture()
    def drip(*args, **kwargs):
        clock["now"] += 1
        return b"x"
    value.raw.read1.side_effect = drip
    session.get.return_value = value
    monkeypatch.setattr("app.media.time", SimpleNamespace(monotonic=lambda: clock["now"]))
    with pytest.raises(MediaError, match="deadline exceeded") as found:
        _download(SOURCE, "images", PROXY, allow_direct_fallback=False, deadline=3)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic == {"category": "deadline", "http_status": 200, "attempt_count": 1,
        "retry_count": 0, "route": "account_proxy", "media_kind": "images", "exception_types": ["MediaError"]}
    assert clock["now"] == 3 and session.get.call_count == session.close.call_count == 1
    assert_no_secrets(diagnostic)


def test_bounded_stream_encoding_error_keeps_category_and_does_not_retry(session):
    value = response_fixture()
    value.headers["Content-Encoding"] = "br"
    session.get.return_value = value
    with pytest.raises(MediaError) as found:
        _download(SOURCE, "images", PROXY, allow_direct_fallback=False, deadline=10**12)
    diagnostic = media_error_diagnostic(found.value)
    assert diagnostic["category"] == "unsupported_encoding"
    assert diagnostic["http_status"] == 200 and diagnostic["attempt_count"] == 1
    assert session.get.call_count == 1
    assert_no_secrets(diagnostic)


def test_third_reference_failure_is_durable_safe_and_never_reaches_canvas_prepare(context, monkeypatch, caplog):
    store, service, _ = context
    account = owner(store)
    store.update_account(account["id"], {"proxy_url": PROXY})
    account = store.account(account["id"], private=True)
    row = task(store, image_urls=[SOURCE + f"&index={index}" for index in range(3)])
    client = fake_client(store, service, account)
    session = Mock()
    session.get.side_effect = lambda *args, **kwargs: response(503)
    monkeypatch.setattr("app.media.requests.Session", lambda: session)
    monkeypatch.setattr("app.media.time.sleep", lambda _: None)
    seen = []
    def download(source, kind, proxy_url, **kwargs):
        seen.append((kind, proxy_url, kwargs))
        if len(seen) < 3:
            return b"fixture image", "image/png", "fixture.png"
        return _download(source, kind, proxy_url, **kwargs)
    with patch.object(service, "canvas_client", return_value=client), patch("app.canvas_service._download", download):
        service._run(row["id"])
    result = store.task(row["id"])
    diagnostic = result["upstream_context"]["media_download_error"]
    assert result["status"] == "failed" and result["error_code"] == "MEDIA_DOWNLOAD_FAILED"
    assert result["error_message"] == "MediaError: Canvas preparation failed"
    assert diagnostic == {"category": "http", "http_status": 503, "attempt_count": 2, "retry_count": 1,
        "route": "account_proxy", "media_kind": "images", "media_index": 3,
        "exception_types": ["MediaError", "HTTPError"]}
    assert len(seen) == 3 and all(item[1] == PROXY and item[2]["allow_direct_fallback"] is False for item in seen)
    assert result["actual_cost"] is None and not result["attempted_account_ids"]
    assert not service._credit_reservations and not sum(service._active.values())
    assert service._worker_slots._value == 50
    client.prepare.assert_not_called()
    client.submit.assert_not_called()
    assert_no_secrets(result["upstream_context"])
    assert_no_secrets(caplog.text)
    public = service.public_task(result)
    assert public["error"]["code"] == "MEDIA_DOWNLOAD_FAILED"
    assert public["error"]["message"] == "素材下载失败，请检查素材链接后重试~"
    assert "media_download_error" not in public
    assert_no_secrets(public)
