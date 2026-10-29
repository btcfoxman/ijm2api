"""Only existing fixed public-media hosts reuse the browser preparation route."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from app.canvas_client import CanvasClient
from app.canvas_service import CanvasServiceMixin
from app.media import MediaError, media_error_diagnostic
from test_canvas_execution import context, owner, task
from test_canvas_transport import FakeRunner


SOURCE = "https://media.juxingdonghua.com/fixture.png?signature=caller-signature"
PROXY = "socks5://fixture-user:fixture-password@proxy.test:20019"
ACCOUNT = {"id": 19, "proxy_url": PROXY}
IMAGE = b"\x89PNG\r\n\x1a\nfixture-image"


@pytest.mark.parametrize("source", [SOURCE, SOURCE.replace("media.juxingdonghua.com", "MEDIA.JUXINGDONGHUA.COM"),
    "https://media.juxingdonghua.com:443/fixture.png?signature=SIGNED%2FValue&token=x@y"])
def test_exact_existing_public_host_accepts_case_insensitive_hostname_and_signed_query(source):
    assert CanvasServiceMixin._canvas_direct_media_allowed(source)


@pytest.mark.parametrize("source", [
    "https://media.juxingdonghua.com.evil.example/fixture.png",
    "https://evil-media.juxingdonghua.com/fixture.png",
    "https://media.juxingdonghua.com./fixture.png",
    "https://dreamina.capcut.com/fixture.png",
    "https://commerce.us.capcut.com/fixture.png",
    "https://caller.example/fixture.png",
    "http://media.juxingdonghua.com/fixture.png",
    "https://user@media.juxingdonghua.com/fixture.png",
    "https://user:password@media.juxingdonghua.com/fixture.png",
    "https://@media.juxingdonghua.com/fixture.png",
    "https://media.juxingdonghua.com:8443/fixture.png",
    "https://media.juxingdonghua.com:bad/fixture.png",
    "https://media.juxingdonghua.com:99999/fixture.png",
    "https://[malformed/fixture.png", "data:image/png;base64,eA==", None, {}])
def test_other_hosts_schemes_credentials_ports_and_parse_failures_cannot_enable_direct(source):
    assert not CanvasServiceMixin._canvas_direct_media_allowed(source)


def response():
    value = Mock()
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    value.headers = {"Content-Type": "image/png"}
    value.status_code = 200
    raw = SimpleNamespace(closed=False, _connection=SimpleNamespace(sock=Mock()))
    def read1(*args, **kwargs):
        raw.closed = True
        return IMAGE
    raw.read1 = Mock(side_effect=read1)
    value.raw = raw
    return value


@pytest.fixture
def sessions(monkeypatch):
    actions, opened = [], []
    def create():
        value = Mock()
        action = actions.pop(0)
        if isinstance(action, Exception):
            value.get.side_effect = action
        elif isinstance(action, Mock):
            value.get.return_value = action
        elif callable(action):
            value.get.side_effect = action
        else:
            value.get.return_value = action
        opened.append(value)
        return value
    monkeypatch.setattr("app.media.requests.Session", create)
    monkeypatch.setattr("app.media.time.sleep", lambda _: None)
    monkeypatch.setattr("app.media._DIRECT_MEDIA_UNTIL", {})
    return actions, opened


def assert_original_proxy(value):
    assert value.get.call_args.kwargs["proxies"] == {
        "http": PROXY.replace("socks5://", "socks5h://"),
        "https": PROXY.replace("socks5://", "socks5h://")}


def test_real_download_uses_direct_first_then_original_account_proxy(tmp_path, sessions):
    actions, opened = sessions
    actions.extend([requests.ReadTimeout("fixture interruption"), response()])
    assets = CanvasServiceMixin()._canvas_local_assets({"images": [SOURCE]}, ACCOUNT, tmp_path, deadline=10**12)
    assert len(opened) == 2
    assert opened[0].get.call_args.kwargs["proxies"] == {}
    assert opened[0].get.call_args.kwargs["allow_redirects"] is False
    assert_original_proxy(opened[1])
    assert opened[1].get.call_args.kwargs["allow_redirects"] is True
    assert all(value.trust_env is False for value in opened)
    assert all(value.close.call_count == 1 for value in opened)
    for value in opened:
        kwargs = value.get.call_args.kwargs
        assert kwargs["headers"] == {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
        assert "cookies" not in kwargs and "auth" not in kwargs
    assert Path(assets["images"][0]["path"]).read_bytes() == IMAGE
    assert ACCOUNT == {"id": 19, "proxy_url": PROXY}


@pytest.mark.parametrize("source", ["https://caller.example/fixture.png", "https://dreamina.capcut.com/fixture.png"])
def test_other_public_and_official_hosts_remain_proxy_only(tmp_path, sessions, monkeypatch, source):
    actions, opened = sessions
    actions.extend([requests.ReadTimeout("fixture interruption"), requests.ReadTimeout("fixture interruption")])
    monkeypatch.setattr("app.media._DIRECT_MEDIA_UNTIL", {"caller.example": 10**15})
    with pytest.raises(MediaError) as found:
        CanvasServiceMixin()._canvas_local_assets({"images": [source]}, ACCOUNT, tmp_path, deadline=10**12)
    assert len(opened) == 2
    for value in opened:
        assert_original_proxy(value)
    assert media_error_diagnostic(found.value)["route"] == "account_proxy"
    assert media_error_diagnostic(found.value)["attempt_count"] == 2


def test_direct_retry_consumes_original_shared_download_deadline(tmp_path, sessions, monkeypatch):
    actions, opened = sessions
    clock = {"now": 0.0}
    def interrupted(*args, **kwargs):
        clock["now"] = 4
        raise requests.ReadTimeout("fixture interruption")
    actions.extend([interrupted, response()])
    monkeypatch.setattr("app.media.time", SimpleNamespace(monotonic=lambda: clock["now"],
        sleep=lambda delay: clock.update(now=clock["now"] + delay)))
    CanvasServiceMixin()._canvas_local_assets({"images": [SOURCE]}, ACCOUNT, tmp_path, deadline=10)
    assert [value.get.call_args.kwargs["timeout"].total for value in opened] == [10, 5.5]
    assert clock["now"] == 4.5
    assert_original_proxy(opened[1])


def test_direct_failure_at_original_deadline_never_opens_proxy_attempt(tmp_path, sessions, monkeypatch):
    actions, opened = sessions
    clock = {"now": 0.0}
    def interrupted(*args, **kwargs):
        clock["now"] = 3
        raise requests.ReadTimeout("fixture interruption")
    actions.append(interrupted)
    monkeypatch.setattr("app.media.time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=Mock()))
    with pytest.raises(MediaError) as found:
        CanvasServiceMixin()._canvas_local_assets({"images": [SOURCE]}, ACCOUNT, tmp_path, deadline=3)
    assert len(opened) == 1 and clock["now"] == 3
    assert media_error_diagnostic(found.value)["category"] == "deadline"
    assert media_error_diagnostic(found.value)["route"] == "direct_media"
    assert media_error_diagnostic(found.value)["attempt_count"] == 1


def test_public_material_route_does_not_change_account_for_any_cli_rpc(context, tmp_path, sessions):
    store, service, _ = context
    account = owner(store)
    store.update_account(account["id"], {"proxy_url": PROXY, "login_user_id": "42", "cli_user_id": "42"})
    account = store.account(account["id"], private=True)
    row = task(store, image_urls=[SOURCE])
    actions, opened = sessions
    actions.append(response())
    class AccountRunner(FakeRunner):
        def __init__(self):
            super().__init__(service.canvas_cli.account_directory(account))
            self.accounts = []
            self.persisted.append({"fixture": True})
        def run(self, selected, args, **kwargs):
            self.accounts.append(dict(selected))
            return super().run(selected, args, **kwargs)
    runner = AccountRunner()
    client = CanvasClient(runner, account)
    with patch.object(service, "canvas_client", return_value=client) as factory:
        service._run(row["id"])
    result = store.task(row["id"])
    assert result["status"] == "succeeded", (result["error_code"], result["error_message"],
        result["upstream_context"].get("submit_error"), result["upstream_context"].get("last_query_error"))
    factory.assert_called_once()
    selected = factory.call_args.args[0]
    assert all(selected[key] == account[key] for key in account)
    assert runner.accounts and all(selected == account for selected in runner.accounts)
    assert sum(args[:2] == ["node", "run"] for args in runner.calls) == 1
    assert store.account(account["id"], private=True)["proxy_url"] == PROXY
    assert opened[0].get.call_args.kwargs["proxies"] == {}
    service.browser_runtime.acquire.assert_not_called()
