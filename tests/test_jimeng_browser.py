"""Jimeng accounts stay on their explicit site and never enter Dreamina login."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from app import browser, browser_login, browser_runtime
from app.browser_login_api import register_browser_login
from app.jimeng_client import JimengProtocolUnavailable


def target(identity, url):
    return {"id": identity, "type": "page", "url": url,
            "webSocketDebuggerUrl": "ws://127.0.0.1/synthetic/" + identity}


def test_site_selection_uses_backend_independently_of_region(monkeypatch):
    pages = [target("international", browser.BASE + "/ai-tool/home"),
             target("mainland", browser.JIMENG_BASE + "/ai-tool/home")]
    monkeypatch.setattr(browser, "_targets", lambda _: pages)
    assert browser._page(19001)["id"] == "international"
    assert browser._page(19001, site="jimeng")["id"] == "mainland"
    assert browser.account_browser_options({"execution_mode": "browser", "region": "cn"}) == {}
    assert browser.account_browser_options({"execution_mode": "jimeng_web", "region": "us"}) == {"site": "jimeng"}


@pytest.mark.parametrize("url", [
    "https://jimeng.jianying.com.evil.test/ai-tool/home",
    "https://jimeng.jianying.com:8443/ai-tool/home",
    "https://user@jimeng.jianying.com/ai-tool/home",
    "http://jimeng.jianying.com/ai-tool/home",
    browser.BASE + "/ai-tool/home",
])
def test_jimeng_target_rejects_other_origins(monkeypatch, url):
    monkeypatch.setattr(browser, "_targets", lambda _: [target("foreign", url)])
    assert browser.browser_alive(19001, site="jimeng") is False
    with pytest.raises(browser.BrowserError, match="Jimeng"):
        browser._page(19001, site="jimeng")


def test_jimeng_cookies_and_post_do_not_use_international_origins(monkeypatch):
    page = browser.CDPBrowser(19001, site="jimeng")
    command = Mock(return_value={"cookies": []})
    evaluate = Mock(side_effect=AssertionError("must not invoke an international request"))
    monkeypatch.setattr(page, "command", command)
    monkeypatch.setattr(page, "evaluate", evaluate)
    assert page.cookies() == []
    command.assert_called_once_with("Network.getCookies", {"urls": [browser.JIMENG_BASE + "/"]})
    with pytest.raises(browser.BrowserError, match="Jimeng web client"):
        page.post("/mweb/v1/aigc_draft/generate", {})
    evaluate.assert_not_called()
    with pytest.raises(ValueError, match="browser site"):
        browser.CDPBrowser(19001, site="unknown")


@pytest.mark.parametrize("mode", ["browser", "canvas_cli", None])
def test_mainland_region_does_not_relax_international_proxy_requirement(mode):
    with pytest.raises(browser.BrowserError, match="account proxy"):
        browser.account_proxy_url({"execution_mode": mode, "region": "cn", "proxy_url": ""})


def test_only_jimeng_backend_can_omit_proxy():
    assert browser.account_proxy_url({"execution_mode": "jimeng_web", "region": "other"}) == ""
    with pytest.raises(browser.BrowserError, match="account proxy"):
        browser.account_proxy_url({"execution_mode": "jimeng_web", "proxy_url": "http://invalid/path"})


class TimingPeer:
    def __init__(self):
        self.calls, self.pending = [], []
        self.closed = False

    def settimeout(self, _):
        pass

    def send(self, raw):
        request = json.loads(raw)
        self.calls.append(request)
        result = {}
        if request["method"] == "Page.addScriptToEvaluateOnNewDocument":
            result = {"identifier": "synthetic-timing"}
        elif request["method"] == "Runtime.evaluate":
            result = {"result": {"value": True}}
        self.pending.append(json.dumps({"id": request["id"], "result": result}))

    def recv(self):
        return self.pending.pop(0)

    def close(self):
        self.closed = True


def test_cold_jimeng_page_registers_its_own_origin_buffer_before_navigation(monkeypatch):
    peer = TimingPeer()
    monkeypatch.setattr(browser.websocket, "create_connection", Mock(return_value=peer))
    page = browser.CDPBrowser(19001, site="jimeng")
    with page.resource_timing_capture(_target=target("owned", "about:blank"), _initial_navigation=True):
        registered = next(call for call in peer.calls if call["method"] == "Page.addScriptToEvaluateOnNewDocument")
        source = registered["params"]["source"]
        assert "location.origin!=='https://jimeng.jianying.com'" in source
        assert "dreamina.capcut.com" not in source
        navigation = next(call for call in peer.calls if call["method"] == "Page.navigate")
        assert navigation["params"]["url"] == browser.JIMENG_BASE + "/ai-tool/home"
        assert peer.calls.index(registered) < peer.calls.index(navigation)
    assert peer.closed


def test_jimeng_timing_capture_rejects_international_page_before_connecting(monkeypatch):
    connect = Mock(side_effect=AssertionError("foreign target must never be attached"))
    monkeypatch.setattr(browser.websocket, "create_connection", connect)
    with pytest.raises(browser.BrowserError):
        browser.ensure_resource_timing_capture(19001, site="jimeng",
            _target=target("foreign", browser.BASE + "/ai-tool/home"))
    connect.assert_not_called()


def test_managed_jimeng_start_omits_proxy_and_preserves_site(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "_PROCESSES", {})
    monkeypatch.setattr(browser, "browser_alive", Mock(return_value=False))
    monkeypatch.setattr(browser, "_targets", Mock(side_effect=[[], [target("fresh", "about:blank")]]))
    timing = Mock()
    monkeypatch.setattr(browser, "ensure_resource_timing_capture", timing)
    monkeypatch.setattr(browser, "close_resource_timing_capture", Mock())
    process = Mock()
    popen = Mock(return_value=process)
    monkeypatch.setattr(browser.subprocess, "Popen", popen)
    account = {"id": 8, "execution_mode": "jimeng_web", "proxy_url": ""}
    assert browser.start_managed_browser(account, chrome="synthetic-chrome", profiles=tmp_path) == 19308
    assert not any(arg.startswith("--proxy-server") for arg in popen.call_args.args[0])
    timing.assert_called_once_with(19308, _target=target("fresh", "about:blank"),
        _initial_navigation=True, site="jimeng")


@pytest.mark.parametrize("managed", [False, True])
def test_runtime_attaches_only_to_account_site(monkeypatch, tmp_path, managed):
    account = {"id": 8, "cdp_port": 19001, "execution_mode": "jimeng_web", "managed_profile": managed,
               "status": "active"}
    store = SimpleNamespace(account=lambda *_, **__: dict(account))
    runtime = browser_runtime.BrowserRuntime(store, lambda: {})
    alive, capture = Mock(return_value=True), Mock()
    monkeypatch.setattr(browser_runtime, "browser_alive", alive)
    monkeypatch.setattr(browser_runtime, "ensure_resource_timing_capture", capture)
    returned, owned = runtime.acquire(account)
    assert returned == account and owned is managed
    alive.assert_called_once_with(19001, site="jimeng")
    capture.assert_called_once_with(19001, site="jimeng")


class FakeStore:
    def __init__(self, mode="jimeng_web"):
        self.row = {"id": 8, "name": "synthetic-account", "execution_mode": mode, "region": "cn",
                    "proxy_url": "", "enabled": False, "status": "pending", "cdp_port": 0,
                    "project_id": "", "login_enable_after": None}
        self.clear_media_cache = Mock()

    def account(self, *_, **__):
        return dict(self.row)

    def update_account(self, _identifier, changes):
        self.row.update(changes)
        return dict(self.row)

    def active_account_count(self, _):
        return 0


class FakeDesktop:
    def __init__(self, profile, proxy, *, site="dreamina"):
        self.profile, self.proxy, self.site = profile, proxy, site
        self.cdp_port, self.closed = 19001, False

    @staticmethod
    def available():
        return True

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    def alive(self):
        return not self.closed


def login_context(monkeypatch, tmp_path, *, mode="jimeng_web"):
    store = FakeStore(mode)
    service = SimpleNamespace(_lock=threading.RLock(), _active={}, browser_runtime=Mock(),
        settings=lambda: {"chrome_user_data_root": str(tmp_path), "low_balance_disable_threshold": 0},
        begin_cli_authorization=Mock())
    manager = browser_login.BrowserLoginManager(store, service)
    alive, capture = Mock(return_value=True), Mock()
    monkeypatch.setattr(browser_login, "LoginDesktop", FakeDesktop)
    monkeypatch.setattr(browser_login, "browser_alive", alive)
    monkeypatch.setattr(browser_login, "ensure_resource_timing_capture", capture)
    automation, international = Mock(), Mock()
    monkeypatch.setattr(browser_login, "AccountAutomation", automation)
    monkeypatch.setattr(browser_login, "UpstreamClient", international)
    return SimpleNamespace(store=store, service=service, manager=manager, alive=alive,
        capture=capture, automation=automation, international=international)


def test_jimeng_login_start_uses_mainland_site_and_direct_connection(monkeypatch, tmp_path):
    context = login_context(monkeypatch, tmp_path)

    async def run():
        try:
            await context.manager.start(8)
            desktop = context.manager.active["desktop"]
            assert desktop.proxy == "" and desktop.site == "jimeng"
            context.alive.assert_called_once_with(19001, site="jimeng")
            context.capture.assert_called_once_with(19001, site="jimeng")
            context.automation.assert_not_called()
            context.international.assert_not_called()
        finally:
            await context.manager.close()

    asyncio.run(run())


def test_international_login_still_requires_proxy_even_for_cn_region(monkeypatch, tmp_path):
    context = login_context(monkeypatch, tmp_path, mode="browser")

    async def run():
        with pytest.raises(browser_login.LoginError, match="account proxy is required"):
            await context.manager.start(8)
        assert context.manager.active is None
        context.alive.assert_not_called()

    asyncio.run(run())


def test_jimeng_finish_confirms_identity_and_credits_without_cli_or_dreamina(monkeypatch, tmp_path):
    context = login_context(monkeypatch, tmp_path)
    client = Mock()
    client.login_identity.return_value = {"authenticated": True, "user_id": "synthetic-jimeng-user"}
    client.account_state.return_value = {"available_balance": 24, "buckets": {"gift_credit": 24}}
    client.browser.project_id.return_value = ""
    monkeypatch.setattr("app.jimeng_client.JimengWebClient", Mock(return_value=client))

    async def run():
        try:
            await context.manager.start(8)
            result = await context.manager.finish(context.manager.active["id"])
            assert result["status"] == "active" and result["balance"] == 24
            assert result["login_user_id"] == "synthetic-jimeng-user"
            assert result["watermark_ready"] is False and result["agreement_ready"] is False
            assert context.manager.active is None
            context.automation.assert_not_called()
            context.international.assert_not_called()
            context.service.begin_cli_authorization.assert_not_called()
        finally:
            await context.manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("identity", [
    {"authenticated": False, "user_id": "unverified"},
    {"authenticated": True, "user_id": ""},
    {"authenticated": True, "user_id": "0"},
    {"authenticated": True, "user_id": "invalid identity"},
])
def test_jimeng_finish_does_not_activate_unverified_identity(monkeypatch, tmp_path, identity):
    context = login_context(monkeypatch, tmp_path)
    client = Mock()
    client.login_identity.return_value = identity
    monkeypatch.setattr("app.jimeng_client.JimengWebClient", Mock(return_value=client))

    async def run():
        try:
            await context.manager.start(8)
            with pytest.raises(browser_login.LoginError, match="identity is not confirmed"):
                await context.manager.finish(context.manager.active["id"])
            assert context.store.row["enabled"] is False
            assert context.manager.active["phase"] == "login"
            client.account_state.assert_not_called()
            context.international.assert_not_called()
        finally:
            await context.manager.close()

    asyncio.run(run())


def test_unavailable_jimeng_verifier_keeps_login_open_without_international_fallback(monkeypatch, tmp_path):
    context = login_context(monkeypatch, tmp_path)
    client = Mock()
    client.login_identity.side_effect = JimengProtocolUnavailable("synthetic unavailable contract")
    monkeypatch.setattr("app.jimeng_client.JimengWebClient", Mock(return_value=client))

    async def run():
        try:
            await context.manager.start(8)
            with pytest.raises(browser_login.LoginError, match="account identity"):
                await context.manager.finish(context.manager.active["id"])
            assert context.manager.active["phase"] == "login"
            assert context.store.row["enabled"] is False
            context.automation.assert_not_called()
            context.international.assert_not_called()
        finally:
            await context.manager.close()

    asyncio.run(run())


def test_pending_login_endpoint_preserves_explicit_backend_and_region():
    app = FastAPI()
    store = SimpleNamespace(create_pending_account=Mock(return_value={"id": 8}))
    register_browser_login(app, Mock(), store, lambda: None, "synthetic-admin")
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/admin/accounts/pending")
    response = endpoint({"name": "mainland", "execution_mode": "jimeng_web", "region": "us"})
    assert json.loads(response.body) == {"id": 8}
    store.create_pending_account.assert_called_once_with("mainland", "", 1, execution_mode="jimeng_web", region="us")
