"""Use an authenticated native Chromium tab as the Dreamina Web API transport."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import websocket


BASE = "https://dreamina.capcut.com"
MWEB_API = "https://mweb-api-sg.capcut.com"
MWEB_US_API = "https://dreamina-api-us-ttp2.us.capcut.com"
COMMERCE_API = "https://commerce-api-sg.capcut.com"
COMMERCE_US_API = "https://commerce-us-ttp2.us.capcut.com"
_PROCESS_LOCK = threading.RLock()
_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
_UPLOAD_LOCKS: dict[int, threading.RLock] = {}
_TIMING_GUARD = threading.Lock()
_TIMING_LOCKS: dict[int, threading.RLock] = {}
_TIMING_CAPTURES: dict[int, tuple[str, Any, Any]] = {}

# Retain genuine SDK resource URLs arriving after a cold page's first 250
# assets. This changes timing storage only, never requests or SDK parameters.
_RESOURCE_TIMING_BUFFER_SCRIPT = """(() => {
 if(location.origin!=='https://dreamina.capcut.com')return false;
 if(typeof performance.setResourceTimingBufferSize!=='function')return false;
 performance.setResourceTimingBufferSize(5000);return true;
})()"""


class BrowserError(Exception):
    pass


class BrowserRejected(BrowserError):
    """A complete HTTP response established that the upstream rejected a call."""


class _ResourceTimingContextPending(BrowserError):
    """The selected page is replacing its normal JavaScript context."""


class _ResourceTimingHandle:
    def __init__(self, refresh: Callable[[], None]) -> None:
        self.refresh = refresh


def _timing_lock(port: int) -> threading.RLock:
    with _TIMING_GUARD:
        return _TIMING_LOCKS.setdefault(port, threading.RLock())


def close_resource_timing_capture(port: int) -> None:
    """Release only our telemetry session, without closing or navigating a tab."""
    with _timing_lock(port):
        current = _TIMING_CAPTURES.pop(port, None)
        if current:
            current[1].__exit__(None, None, None)


def ensure_resource_timing_capture(port: int, *, _target: dict[str, Any] | None = None,
                                   _initial_navigation: bool = False) -> None:
    """Keep each account's genuine SDK timing records across later documents."""
    with _timing_lock(port):
        target = _target or _page(port)
        target_id = str(target.get("id") or "")
        url = str(target.get("url") or "")
        parsed = urllib.parse.urlsplit(url)
        allowed = parsed.scheme == "https" and parsed.hostname == "dreamina.capcut.com"
        if (not target_id or target.get("type") != "page"
                or (_initial_navigation and url != "about:blank")
                or (not _initial_navigation and not allowed)):
            raise BrowserError("Dreamina resource timing target is not an owned blank or account page")
        current = _TIMING_CAPTURES.get(port)
        if current and current[0] == target_id:
            try:
                current[2].refresh()
                return
            except BrowserError:
                close_resource_timing_capture(port)
        elif current:
            close_resource_timing_capture(port)
        context = CDPBrowser(port, timeout=15).resource_timing_capture(
            _target=target, _initial_navigation=_initial_navigation)
        handle = context.__enter__()
        _TIMING_CAPTURES[port] = (target_id, context, handle)


def account_proxy_url(account: dict[str, Any]) -> str:
    value = str(account.get("proxy_url") or "").strip()
    parsed = urllib.parse.urlsplit(value)
    try:
        valid = parsed.scheme in {"socks5", "http", "https"} and bool(parsed.hostname) and bool(parsed.port)
    except ValueError:
        valid = False
    if not valid or parsed.path or parsed.query or parsed.fragment:
        raise BrowserError("account proxy must be a SOCKS5 or HTTP proxy with host and port")
    return value


def _targets(port: int) -> list[dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json", timeout=4) as response:
            return json.load(response)
    except Exception as exc:
        raise BrowserError(f"Dreamina CDP port {port} is unavailable: {type(exc).__name__}") from exc


def _page(port: int) -> dict[str, Any]:
    pages = [target for target in _targets(port) if target.get("type") == "page" and
             (urllib.parse.urlsplit(target.get("url") or "").hostname or "") == "dreamina.capcut.com"]
    pages.sort(key=lambda target: int("projectId=" in str(target.get("url") or "")), reverse=True)
    if pages:
        return pages[0]
    raise BrowserError("open and log in to Dreamina in the account browser")


def browser_alive(port: int) -> bool:
    try:
        _page(port)
        return True
    except BrowserError:
        return False


def _remove_stale_profile_locks(profile: Path) -> None:
    """Discard Chromium singleton links left by a stopped container."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        link = profile / name
        if link.is_symlink():
            link.unlink()


def start_managed_browser(account: dict[str, Any], *, chrome: str, profiles: Path) -> int:
    account_id = int(account["id"])
    port = 19300 + account_id
    profile = profiles / str(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    args = [chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run",
            "--no-default-browser-check", "--password-store=basic", "--window-size=1280,960"]
    if os.name != "nt":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    proxy_url = account_proxy_url(account)
    args.append("--proxy-server=" + proxy_url)
    # The new tab is ours and has not loaded account resources yet. Register
    # the persistent init script before its first normal website navigation.
    args.append("about:blank")
    with _PROCESS_LOCK:
        if browser_alive(port):
            ensure_resource_timing_capture(port)
            return port
        existing = _PROCESSES.get(account_id)
        if existing and existing.poll() is None:
            existing.terminate()
            try:
                existing.wait(timeout=4)
            except subprocess.TimeoutExpired:
                existing.kill()
                existing.wait(timeout=4)
        _remove_stale_profile_locks(profile)
        close_resource_timing_capture(port)
        try:
            previous_targets = {str(target.get("id")) for target in _targets(port)}
        except BrowserError:
            previous_targets = set()
        _PROCESSES[account_id] = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL,
                                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                targets = _targets(port)
            except BrowserError:
                targets = []
            fresh = next((target for target in targets if target.get("type") == "page"
                          and target.get("url") == "about:blank"
                          and str(target.get("id") or "") not in previous_targets), None)
            if fresh:
                ensure_resource_timing_capture(port, _target=fresh, _initial_navigation=True)
                return port
            time.sleep(.1)
        try:
            pages = [item for item in _targets(port) if item.get("type") == "page"]
            hosts = {urllib.parse.urlsplit(str(item.get("url") or "")).hostname for item in pages}
            redirected = sorted(host for host in hosts if host and host != "dreamina.capcut.com")
            if redirected:
                raise BrowserError(f"Dreamina browser reached {redirected[0]}; configure an account proxy")
        except BrowserError as exc:
            if "configure an account proxy" in str(exc):
                raise
    raise BrowserError("managed Dreamina Chrome did not start")


def close_browser_port(port: int) -> None:
    """Ask a local CDP browser to exit, including one orphaned by a restart."""
    close_resource_timing_capture(port)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            endpoint = json.load(response).get("webSocketDebuggerUrl")
        if endpoint:
            ws = websocket.create_connection(endpoint, timeout=2, suppress_origin=True)
            try:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            finally:
                ws.close()
    except (OSError, ValueError, KeyError, websocket.WebSocketException):
        pass


def stop_managed_browser(account_id: int) -> None:
    with _PROCESS_LOCK:
        process = _PROCESSES.pop(account_id, None)
    close_browser_port(19300 + account_id)
    if process and process.poll() is None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def clear_managed_profile(account: dict[str, Any], profiles: Path) -> None:
    """Delete only the service-owned profile for this numeric account ID."""
    account_id = int(account["id"])
    port = int(account.get("cdp_port") or 0)
    if port == 19300 + account_id and browser_alive(port):
        try:
            CDPBrowser(port).command("Browser.close")
        except BrowserError:
            pass
        for _ in range(30):
            if not browser_alive(port):
                break
            time.sleep(.1)
        if browser_alive(port):
            raise BrowserError("managed Chrome is still running; profile was not removed")
    stop_managed_browser(account_id)
    root = profiles.resolve()
    profile = (root / str(account_id)).resolve()
    if profile.parent != root:
        raise BrowserError("profile path is outside the configured profile root")
    if profile.exists():
        shutil.rmtree(profile)


class CDPBrowser:
    def __init__(self, port: int, timeout: float = 90) -> None:
        self.port = int(port)
        self.timeout = timeout

    @contextmanager
    def resource_timing_capture(self, *, _target: dict[str, Any] | None = None,
                                _initial_navigation: bool = False) -> Iterator[_ResourceTimingHandle]:
        """Keep timing-buffer injection attached across login navigations."""
        target = _target or _page(self.port)
        ws = None
        identifier = None
        sequence = 0

        def command(method: str, params: dict[str, Any] | None = None,
                    *, deadline: float | None = None) -> dict[str, Any]:
            nonlocal sequence
            sequence += 1
            request_id = sequence
            ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BrowserError("Dreamina login resource timing page was not ready")
                    ws.settimeout(min(self.timeout, remaining))
                response = json.loads(ws.recv())
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"]
                    message = str(error.get("message") or "").casefold()
                    if (method == "Runtime.evaluate" and error.get("code") == -32000
                            and any(value in message for value in (
                                "cannot find default execution context", "execution context was destroyed",
                                "cannot find context with specified id"))):
                        raise _ResourceTimingContextPending("Dreamina login resource timing page is changing")
                    raise BrowserError("Dreamina login resource timing setup failed")
                return response.get("result") or {}

        def refresh() -> None:
            try:
                ready_deadline = time.monotonic() + 30
                while True:
                    try:
                        current = command("Runtime.evaluate", {"expression": _RESOURCE_TIMING_BUFFER_SCRIPT,
                                                                "returnByValue": True}, deadline=ready_deadline)
                    except _ResourceTimingContextPending:
                        current = {}
                    if "exceptionDetails" not in current and (current.get("result") or {}).get("value") is True:
                        break
                    remaining = ready_deadline - time.monotonic()
                    if remaining <= 0:
                        raise BrowserError("Dreamina login resource timing page was not ready")
                    time.sleep(min(.2, remaining))
                ws.settimeout(self.timeout)
            except (OSError, ValueError, websocket.WebSocketException) as error:
                raise BrowserError("Dreamina login resource timing connection failed") from error

        try:
            try:
                ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=self.timeout,
                                                 suppress_origin=True)
                command("Page.enable")
                registered = command("Page.addScriptToEvaluateOnNewDocument", {"source": _RESOURCE_TIMING_BUFFER_SCRIPT})
                identifier = registered.get("identifier")
                if not isinstance(identifier, str) or not identifier:
                    raise BrowserError("Dreamina login resource timing setup failed")
                if _initial_navigation:
                    if target.get("url") != "about:blank":
                        raise BrowserError("Dreamina initial navigation requires an owned blank tab")
                    navigation = command("Page.navigate", {"url": BASE + "/ai-tool/generate"})
                    if navigation.get("errorText"):
                        raise BrowserError("Dreamina initial page navigation failed")
                # The target URL may already be Dreamina while its initial
                # about:blank context or a redirect is still being replaced.
                # Keep the init script attached and wait only on normal
                # telemetry readiness before any login action is allowed.
                refresh()
            except (OSError, ValueError, websocket.WebSocketException) as error:
                raise BrowserError("Dreamina login resource timing connection failed") from error
            yield _ResourceTimingHandle(refresh)
        finally:
            if ws is not None:
                try:
                    if identifier:
                        command("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})
                except (BrowserError, OSError, ValueError, websocket.WebSocketException):
                    pass
                finally:
                    try:
                        ws.close()
                    except (OSError, websocket.WebSocketException):
                        pass

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        target = _page(self.port)
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=self.timeout,
                                         suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                result = json.loads(ws.recv())
                if result.get("id") != 1:
                    continue
                if "error" in result:
                    raise BrowserError(f"CDP {method}: {result['error'].get('message', 'unknown error')}")
                return result.get("result") or {}
        except (OSError, websocket.WebSocketException) as exc:
            raise BrowserError(f"CDP {method} failed: {type(exc).__name__}") from exc
        finally:
            ws.close()

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.command("Runtime.evaluate", {"expression": expression,
                                                   "awaitPromise": await_promise,
                                                   "returnByValue": True})
        if "exceptionDetails" in result:
            raise BrowserError("Dreamina browser JavaScript request failed")
        value = result.get("result") or {}
        if value.get("subtype") == "error":
            raise BrowserError("Dreamina browser JavaScript request returned an error")
        return value.get("value")

    def post(self, path: str, body: dict[str, Any], *, query: dict[str, str] | None = None,
             allow_empty_ret: bool = False, native_client: bool = False) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Dreamina API path must be absolute")
        origin = COMMERCE_API if path.startswith("/commerce/") else MWEB_API
        url = origin + path
        if query and not native_client:
            url += "?" + urllib.parse.urlencode(query)
        payload = json.dumps(body, ensure_ascii=True, separators=(",", ":"))
        if native_client:
            # Image generation uses the site's complete XHR client. Its
            # container-backed interceptors attach the current account,
            # device and signature context before sending the draft.
            expression = (
                "(async()=>{let req=self.__ijm_api_require;try{"
                "const chunks=self.__LOADABLE_LOADED_CHUNKS__;"
                "if(!chunks)return JSON.stringify({transport_error:'native-client-unavailable'});"
                "if(!req){chunks.push([[900000000+Math.floor(Math.random()*90000000)],{},"
                "function(r){req=r}]);self.__ijm_api_require=req}"
                "if(!req||!req(254815)?.LL||!req(545525)?.U)"
                "return JSON.stringify({transport_error:'native-client-unavailable'});"
                "let container=self.__ijm_api_container;"
                "if(!container){const seen=new Set();"
                "for(const element of document.querySelectorAll('*')){"
                "const key=Object.keys(element).find(k=>k.startsWith('__reactFiber$'));"
                "let fiber=key&&element[key];"
                "while(fiber&&!seen.has(fiber)){seen.add(fiber);"
                "const value=fiber.memoizedProps?.value;"
                "if(value&&typeof value.createInstance==='function'&&"
                "typeof value.invokeFunction==='function'){container=value;break}"
                "fiber=fiber.return}if(container)break}"
                "self.__ijm_api_container=container}"
                "if(!container)return JSON.stringify({transport_error:'native-container-unavailable'});"
                "const client=new (req(254815).LL)().build({"
                "headers:{'Content-Type':'application/json'},withCredentials:true});"
                "req(545525).U(container,client);req(561634).vo(client);"
                "req(547986).Wh(client);req(670446).P(client);req(431316).m(client);"
                "const response=await client.post(" + json.dumps(url) + ","
                + payload + ",{params:" + json.dumps(query or {}, ensure_ascii=True) + "});"
                "return JSON.stringify({http:200,body:response})"
                "}catch(e){if(e?.response)return JSON.stringify({http:e.response.status,"
                "body:e.response.data});if(e?.ret!==undefined)"
                "return JSON.stringify({http:200,body:e});"
                "return JSON.stringify({transport_error:'native-client'})}})()"
            )
        elif path in {"/commerce/v1/benefits/user_credit", "/commerce/v1/benefits/credit_receive"} or path.startswith("/mweb/"):
            # Use the current page's verified regional SDK configuration for
            # credit and MWEB calls. Never cache context across accounts or
            # retry a request in another region. Native SDK calls are separate.
            if path.startswith("/mweb/"):
                us_url = MWEB_US_API + url[len(MWEB_API):]
                regional_url = (
                    "const requestUrl=new URL(" + json.dumps(us_url) + ");"
                    "requestUrl.searchParams.set('aid','513641');"
                    "if(requestUrl.searchParams.has('region'))"
                    "requestUrl.searchParams.set('region','US');url=requestUrl.href;"
                )
            else:
                us_url = COMMERCE_US_API + url[len(COMMERCE_API):]
                regional_url = "url=" + json.dumps(us_url) + ";"
            expression = (
                "(async()=>{let r;let url=" + json.dumps(url) + ";"
                "const headers={'content-type':'application/json'};"
                "try{const info=globalThis.__locationInfo;"
                "if(globalThis.location?.hostname==='dreamina.capcut.com'&&"
                "info?.code==='US'&&info?.domain?.commerce_domain==="
                + json.dumps(COMMERCE_US_API) + "){"
                + regional_url + "headers.appid='513641'}}catch(_){}"
                "try{r=await fetch(url,{method:'POST',credentials:'include',headers,"
                "body:JSON.stringify(" + payload + ")})}"
                "catch(e){return JSON.stringify({transport_error:'fetch'})}"
                "let body;try{body=await r.json()}"
                "catch(e){return JSON.stringify({transport_error:'json',http:r.status})}"
                "return JSON.stringify({http:r.status,body})})()"
            )
        else:
            expression = (
                "(async()=>{let r;try{r=await fetch(" + json.dumps(url) + ","
                "{method:'POST',credentials:'include',headers:{'content-type':'application/json'},"
                "body:JSON.stringify(" + payload + ")})}"
                "catch(e){return JSON.stringify({transport_error:'fetch'})}"
                "let body;try{body=await r.json()}"
                "catch(e){return JSON.stringify({transport_error:'json',http:r.status})}"
                "return JSON.stringify({http:r.status,body})})()"
            )
        value = self.evaluate(expression, await_promise=True)
        try:
            response = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise BrowserError("Dreamina returned an unreadable JSON response") from exc
        if not isinstance(response, dict):
            raise BrowserError("Dreamina returned an invalid response envelope")
        if response.get("transport_error") == "fetch":
            raise BrowserError("Dreamina browser fetch failed")
        if response.get("transport_error") == "native-client-unavailable":
            raise BrowserError("Dreamina page request client is unavailable")
        if response.get("transport_error") == "native-container-unavailable":
            raise BrowserError("Dreamina page service container is unavailable")
        if response.get("transport_error") == "native-client":
            raise BrowserError("Dreamina page request client failed")
        if response.get("transport_error") == "json":
            status = int(response.get("http") or 0)
            if status >= 400:
                raise BrowserRejected(f"Dreamina HTTP {status} returned non-JSON")
            raise BrowserError(f"Dreamina HTTP {status} returned non-JSON")
        if int(response.get("http") or 0) >= 400:
            raise BrowserRejected(f"Dreamina HTTP {response['http']}")
        data = response.get("body")
        if not isinstance(data, dict):
            raise BrowserError("Dreamina returned an invalid response envelope")
        if str(data.get("ret")) != "0" and not (allow_empty_ret and data.get("ret") == ""):
            raise BrowserRejected(f"Dreamina ret={data.get('ret')}: {str(data.get('errmsg') or 'upstream error')[:300]}")
        value = data.get("data")
        if value is None and isinstance(data.get("response"), str):
            try:
                value = json.loads(data["response"])
            except ValueError:
                pass
        return value or {}

    def screenshot(self) -> str:
        result = self.command("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        if not result.get("data"):
            raise BrowserError("Dreamina Chrome returned no screenshot")
        return "data:image/png;base64," + result["data"]

    def click(self, x: float, y: float) -> None:
        for count, kind in enumerate(("mousePressed", "mouseReleased"), 1):
            self.command("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                      "button": "left", "clickCount": 1})

    def scroll(self, delta_y: float) -> None:
        self.command("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 700, "y": 500,
                                                  "deltaX": 0, "deltaY": delta_y})

    def cookies(self) -> list[dict[str, Any]]:
        return self.command("Network.getCookies", {"urls": [BASE + "/"]}).get("cookies") or []

    def project_id(self) -> str:
        # Dreamina does not require a per-account project for generation.
        return ""
