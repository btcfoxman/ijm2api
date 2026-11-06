"""Use an authenticated native Chromium tab as the Dreamina Web API transport."""

from __future__ import annotations

import json
import logging
import math
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
JIMENG_BASE = "https://jimeng.jianying.com"
MWEB_API = "https://mweb-api-sg.capcut.com"
MWEB_US_API = "https://dreamina-api-us-ttp2.us.capcut.com"
COMMERCE_API = "https://commerce-api-sg.capcut.com"
COMMERCE_US_API = "https://commerce-us-ttp2.us.capcut.com"
COMMERCE_US_ORIGINS = (COMMERCE_US_API, "https://commerce.us.capcut.com")
_PROCESS_LOCK = threading.RLock()
# Visible browser creation and account menu transactions share one desktop.
# Keep this transport-owned lock independent of account automation imports.
_DESKTOP_UI_LOCK = threading.RLock()
_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
_UPLOAD_LOCKS: dict[int, threading.RLock] = {}
_TIMING_GUARD = threading.Lock()
_TIMING_LOCKS: dict[int, threading.RLock] = {}
_TIMING_CAPTURES: dict[int, tuple[str, Any, Any]] = {}
logger = logging.getLogger(__name__)

# Retain genuine SDK resource URLs arriving after a cold page's first 250
# assets. This changes timing storage only, never requests or SDK parameters.
_RESOURCE_TIMING_BUFFER_SCRIPT = """(() => {
 if(location.origin!=='https://dreamina.capcut.com')return false;
 if(typeof performance.setResourceTimingBufferSize!=='function')return false;
 performance.setResourceTimingBufferSize(5000);return true;
})()"""

# A boolean check over the current document's naturally completed requests.
# Signature values stay inside the website; only fixed query-key presence is
# checked. start_up and our own credit_receive never establish SDK readiness.
_DAILY_CREDIT_SDK_READY = r"""(origin=>{
 if(location.origin!=='https://dreamina.capcut.com'||
    !['interactive','complete'].includes(document.readyState))return false;
 if(typeof performance?.getEntriesByType!=='function')return false;
 return performance.getEntriesByType('resource').some(entry=>{
  if(!Number.isFinite(entry.responseEnd)||entry.responseEnd<=0)return false;
  try{const url=new URL(entry.name);
   return url.origin===origin&&!url.username&&!url.password&&!url.hash&&
    ['/commerce/v3/resource/benefit_metadata','/commerce/v3/benefits/batch_get_user_benefit'].includes(url.pathname)&&
    url.searchParams.has('X-Bogus')&&url.searchParams.has('X-Gnarly')&&url.searchParams.has('msToken');
  }catch(_){return false;}
 });
})"""


def account_browser_site(account: dict[str, Any]) -> str:
    """Select a site from the explicit transport, independently of region."""
    return "jimeng" if account.get("execution_mode") == "jimeng_web" else "dreamina"


def account_browser_options(account: dict[str, Any]) -> dict[str, str]:
    # Keep the default Dreamina calls compatible with existing CDP adapters.
    return {"site": "jimeng"} if account_browser_site(account) == "jimeng" else {}


def site_origin(site: str) -> str:
    if site == "dreamina":
        return BASE
    if site == "jimeng":
        return JIMENG_BASE
    raise ValueError("browser site must be dreamina or jimeng")


def site_start_url(site: str) -> str:
    return site_origin(site) + ("/ai-tool/home" if site == "jimeng" else "/ai-tool/generate")


def _site_page(port: int, site: str, *, deadline: float | None = None) -> dict[str, Any]:
    if deadline is not None:
        return _page(port, site=site, deadline=deadline)
    return _page(port, site=site) if site != "dreamina" else _page(port)


def _resource_timing_script(site: str) -> str:
    return _RESOURCE_TIMING_BUFFER_SCRIPT.replace(BASE, site_origin(site))


class BrowserError(Exception):
    pass


class CDPTransportError(BrowserError):
    """A CDP transport failure, not proof of login expiry or upstream dispatch.

    command_sent conservatively means send was attempted, including a partially
    written frame. It never establishes that a JavaScript business call ran.
    """
    code = "CDP_TRANSPORT_FAILED"

    def __init__(self, method: str, phase: str, command_sent: bool, exception_type: str,
                 *, retryable: bool = True) -> None:
        self.method, self.phase = method, phase
        self.command_sent, self.exception_type = command_sent, exception_type
        self.retryable = retryable
        super().__init__(f"CDP {method} failed: {exception_type}")


class BrowserRejected(BrowserError):
    """A complete HTTP response established that the upstream rejected a call."""


class BrowserTargetUnavailable(BrowserError):
    """CDP discovery cannot find the original site's page; no login verdict."""


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
                                   _initial_navigation: bool = False,
                                   site: str = "dreamina") -> None:
    """Keep each account's genuine SDK timing records across later documents."""
    with _timing_lock(port):
        origin = site_origin(site)
        target = _target or _site_page(port, site)
        target_id = str(target.get("id") or "")
        url = str(target.get("url") or "")
        parsed = urllib.parse.urlsplit(url)
        allowed = parsed.scheme == "https" and parsed.netloc == urllib.parse.urlsplit(origin).netloc
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
        options = {"site": site} if site != "dreamina" else {}
        context = CDPBrowser(port, timeout=15, **options).resource_timing_capture(
            _target=target, _initial_navigation=_initial_navigation)
        handle = context.__enter__()
        _TIMING_CAPTURES[port] = (target_id, context, handle)


def account_proxy_url(account: dict[str, Any]) -> str:
    value = str(account.get("proxy_url") or "").strip()
    if not value and account_browser_site(account) == "jimeng":
        return ""
    parsed = urllib.parse.urlsplit(value)
    try:
        valid = parsed.scheme in {"socks5", "http", "https"} and bool(parsed.hostname) and bool(parsed.port)
    except ValueError:
        valid = False
    if not valid or parsed.path or parsed.query or parsed.fragment:
        raise BrowserError("account proxy must be a SOCKS5 or HTTP proxy with host and port")
    return value


def _targets(port: int, *, timeout: float = 4) -> list[dict[str, Any]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json", timeout=timeout) as response:
            return json.load(response)
    except Exception as exc:
        raise BrowserError(f"Dreamina CDP port {port} is unavailable: {type(exc).__name__}") from exc


def _page(port: int, *, site: str = "dreamina", deadline: float | None = None) -> dict[str, Any]:
    origin = urllib.parse.urlsplit(site_origin(site))
    targets = (_targets(port) if deadline is None else
               _targets(port, timeout=max(.001, min(4, deadline - time.monotonic()))))
    pages = [target for target in targets if target.get("type") == "page" and
             (parsed := urllib.parse.urlsplit(target.get("url") or "")).scheme == "https"
             and parsed.netloc == origin.netloc]
    pages.sort(key=lambda target: int("projectId=" in str(target.get("url") or "")), reverse=True)
    if pages:
        return pages[0]
    label = "Jimeng" if site == "jimeng" else "Dreamina"
    raise BrowserTargetUnavailable(f"open and log in to {label} in the account browser")


def browser_alive(port: int, *, site: str = "dreamina") -> bool:
    try:
        _site_page(port, site)
        return True
    except BrowserError:
        return False


def _remove_stale_profile_locks(profile: Path) -> None:
    """Discard Chromium singleton links left by a stopped container."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        link = profile / name
        if link.is_symlink():
            link.unlink()


def _cleanup_failed_managed_start(account_id: int, port: int, process: subprocess.Popen[Any]) -> None:
    """Called under _PROCESS_LOCK; never close a port borrowed from another owner."""
    if _PROCESSES.get(account_id) is not process:
        return
    capture = _TIMING_CAPTURES.get(port)
    removed = False
    try:
        if process.poll() is None:
            for action in ('terminate', 'kill'):
                try:
                    getattr(process, action)()
                    process.wait(timeout=3)
                    break
                except Exception as exc:
                    logger.warning('Dreamina browser cleanup failed account_id=%s stage=%s error_type=%s',
                        account_id, action, type(exc).__name__)
        if process.poll() is not None and _PROCESSES.get(account_id) is process:
            _PROCESSES.pop(account_id, None)
            removed = True
    except Exception as exc:
        # Keep an unconfirmed child registered so later shutdown can still
        # find it. A cleanup error must not replace the original start error.
        logger.warning('Dreamina browser cleanup failed account_id=%s stage=process error_type=%s',
            account_id, type(exc).__name__)
    finally:
        try:
            if ((_PROCESSES.get(account_id) is process or removed and account_id not in _PROCESSES)
                    and _TIMING_CAPTURES.get(port) is capture):
                close_resource_timing_capture(port)
        except Exception as exc:
            logger.warning('Dreamina browser cleanup failed account_id=%s stage=telemetry error_type=%s',
                account_id, type(exc).__name__)


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
    if proxy_url:
        args.append("--proxy-server=" + proxy_url)
    options = account_browser_options(account)
    site = account_browser_site(account)
    # The new tab is ours and has not loaded account resources yet. Register
    # the persistent init script before its first normal website navigation.
    args.append("about:blank")
    with _DESKTOP_UI_LOCK, _PROCESS_LOCK:
        if browser_alive(port, **options):
            ensure_resource_timing_capture(port, **options)
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
        process = None
        ready = False
        stage = 'spawn'
        try:
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL,
                                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            _PROCESSES[account_id] = process
            stage = 'targets'
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
                    stage = 'telemetry'
                    ensure_resource_timing_capture(port, _target=fresh, _initial_navigation=True, **options)
                    ready = True
                    return port
                time.sleep(.1)
            stage = 'redirect_check'
            try:
                pages = [item for item in _targets(port) if item.get("type") == "page"]
                hosts = {urllib.parse.urlsplit(str(item.get("url") or "")).hostname for item in pages}
                expected_host = urllib.parse.urlsplit(site_origin(site)).hostname
                redirected = sorted(host for host in hosts if host and host != expected_host)
                if redirected:
                    label = "Jimeng" if site == "jimeng" else "Dreamina"
                    raise BrowserError(f"{label} browser reached {redirected[0]}; configure an account proxy")
            except BrowserError as exc:
                if "configure an account proxy" in str(exc):
                    raise
            raise BrowserError("managed Dreamina Chrome did not start")
        except Exception as exc:
            logger.warning('Dreamina browser startup failed account_id=%s stage=%s error_type=%s',
                account_id, stage, type(exc).__name__)
            if isinstance(exc, BrowserError):
                raise
            raise BrowserError('managed Dreamina Chrome startup failed') from exc
        finally:
            if process is not None and not ready:
                _cleanup_failed_managed_start(account_id, port, process)


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
    options = account_browser_options(account)
    if port == 19300 + account_id and browser_alive(port, **options):
        try:
            CDPBrowser(port, **options).command("Browser.close")
        except BrowserError:
            pass
        for _ in range(30):
            if not browser_alive(port, **options):
                break
            time.sleep(.1)
        if browser_alive(port, **options):
            raise BrowserError("managed Chrome is still running; profile was not removed")
    stop_managed_browser(account_id)
    root = profiles.resolve()
    profile = (root / str(account_id)).resolve()
    if profile.parent != root:
        raise BrowserError("profile path is outside the configured profile root")
    if profile.exists():
        shutil.rmtree(profile)


class CDPBrowser:
    def __init__(self, port: int, timeout: float = 90, *, site: str = "dreamina") -> None:
        self.port = int(port)
        self.timeout = timeout
        self.site = site
        self.base_url = site_origin(site)
        self._command_local = threading.local()

    @contextmanager
    def command_budget(self, deadline: float, *, timeout: float = 10) -> Iterator[None]:
        """Bound this thread's commands without changing shared timeout state."""
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("CDP command budget must have a deadline and positive timeout")
        previous = getattr(self._command_local, "budget", None)
        bounded = (min(deadline, previous[0]), min(timeout, previous[1])) if previous else (deadline, timeout)
        self._command_local.budget = bounded
        try:
            yield
        finally:
            if previous is None:
                del self._command_local.budget
            else:
                self._command_local.budget = previous

    @contextmanager
    def resource_timing_capture(self, *, _target: dict[str, Any] | None = None,
                                _initial_navigation: bool = False) -> Iterator[_ResourceTimingHandle]:
        """Keep timing-buffer injection attached across login navigations."""
        target = _target or _site_page(self.port, self.site)
        timing_script = _resource_timing_script(self.site)
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
                        current = command("Runtime.evaluate", {"expression": timing_script,
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
                registered = command("Page.addScriptToEvaluateOnNewDocument", {"source": timing_script})
                identifier = registered.get("identifier")
                if not isinstance(identifier, str) or not identifier:
                    raise BrowserError("Dreamina login resource timing setup failed")
                if _initial_navigation:
                    if target.get("url") != "about:blank":
                        raise BrowserError("Dreamina initial navigation requires an owned blank tab")
                    navigation = command("Page.navigate", {"url": site_start_url(self.site)})
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
        budget = getattr(self._command_local, "budget", None)
        timeout = min(self.timeout, budget[1]) if budget else self.timeout
        deadline = min(time.monotonic() + timeout, budget[0]) if budget else time.monotonic() + timeout
        ws, phase, command_sent = None, "target_lookup", False

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise CDPTransportError(method, phase, command_sent, "WebSocketTimeoutException")
            return value

        try:
            remaining()
            try:
                target = (_site_page(self.port, self.site, deadline=deadline) if budget else
                          _site_page(self.port, self.site))
            except BrowserError as exc:
                if isinstance(exc, BrowserTargetUnavailable):
                    raise CDPTransportError(method, phase, False, "TargetUnavailable") from exc
                if isinstance(exc.__cause__, (OSError, websocket.WebSocketException)):
                    raise CDPTransportError(method, phase, False, type(exc.__cause__).__name__) from exc
                raise
            phase = "connect"
            ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=remaining(),
                                             suppress_origin=True)
            phase = "send"
            ws.settimeout(remaining())
            command_sent = True
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            phase = "receive"
            while True:
                ws.settimeout(remaining())
                result = json.loads(ws.recv())
                if not isinstance(result, dict):
                    raise BrowserError(f"CDP {method} returned an invalid response")
                if result.get("id") != 1:
                    continue
                if "error" in result:
                    raise BrowserError(f"CDP {method}: {result['error'].get('message', 'unknown error')}")
                return result.get("result") or {}
        except (OSError, websocket.WebSocketException) as exc:
            retryable = (isinstance(exc, (OSError, websocket.WebSocketTimeoutException,
                                         websocket.WebSocketConnectionClosedException))
                         and not isinstance(exc, websocket.WebSocketBadStatusException))
            raise CDPTransportError(method, phase, command_sent, type(exc).__name__,
                                    retryable=retryable) from exc
        finally:
            if ws is not None:
                try:
                    ws.close()
                except (OSError, websocket.WebSocketException):
                    pass

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

    def wait_daily_credit_sdk(self, timeout_seconds: float = 20) -> None:
        """Wait only for normal page bootstrap; never make or repeat a request."""
        original_timeout = self.timeout
        deadline = time.monotonic() + min(20, timeout_seconds, original_timeout)
        expression = ("(()=>{let origin=" + json.dumps(COMMERCE_API) + ";"
            "try{const info=globalThis.__locationInfo,domain=info?.domain?.commerce_domain;"
            "if(info?.code==='US'&&" + json.dumps(COMMERCE_US_ORIGINS) + ".includes(domain))origin=domain;"
            "}catch(_){}return " + _DAILY_CREDIT_SDK_READY + "(origin)})()")
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                self.timeout = min(2, original_timeout, remaining)
                try:
                    if self.evaluate(expression) is True:
                        return
                except BrowserError:
                    pass  # A replacing document is not proof of lost login.
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(.2, remaining))
        finally:
            self.timeout = original_timeout
        raise BrowserError('Dreamina daily credit page SDK readiness timed out before request')

    def post(self, path: str, body: dict[str, Any], *, query: dict[str, str] | None = None,
             allow_empty_ret: bool = False, native_client: bool = False) -> dict[str, Any]:
        if self.site != "dreamina":
            raise BrowserError("Jimeng requests require the Jimeng web client")
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
            commerce_origin_setup = "const commerceOrigin=info?.domain?.commerce_domain;"
            us_commerce_context = json.dumps(COMMERCE_US_ORIGINS, separators=(",", ":")) + ".includes(commerceOrigin)"
            if path.startswith("/mweb/"):
                us_url = MWEB_US_API + url[len(MWEB_API):]
                regional_url = (
                    "const requestUrl=new URL(" + json.dumps(us_url) + ");"
                    "requestUrl.searchParams.set('aid','513641');"
                    "if(requestUrl.searchParams.has('region'))"
                    "requestUrl.searchParams.set('region','US');url=requestUrl.href;"
                )
            else:
                regional_url = "url=commerceOrigin+" + json.dumps(url[len(COMMERCE_API):]) + ";"
            request = ("r=await fetch(url,{method:'POST',credentials:'include',headers,"
                       "body:JSON.stringify(" + payload + ")})")
            origin_guard = response_guard = ready_guard = ''
            if path == '/commerce/v1/benefits/credit_receive':
                # The current website SDK intercepts normal page XHR, not bare
                # fetch. Verified 2026-11-02: one ordinary XHR received ret=0;
                # signatures remain entirely inside that installed website SDK.
                timeout_ms = max(1, min(15000, int(self.timeout * 1000)))
                origin_guard = ("if(globalThis.location?.origin!=='https://dreamina.capcut.com')"
                    "return JSON.stringify({transport_error:'daily-page-context'});")
                ready_guard = ("if(!" + _DAILY_CREDIT_SDK_READY + "(new URL(url).origin))"
                    "return JSON.stringify({transport_error:'daily-sdk-not-ready'});")
                response_guard = (
                    "if(r.contextError)return JSON.stringify({transport_error:'daily-response-context'});"
                    "if(!Number.isInteger(r.status)||r.status<200||r.status>=300)"
                    "return JSON.stringify({transport_error:'daily-http',http:r.status});")
                request = (
                    "r=await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();"
                    "xhr.open('POST',url,true);xhr.withCredentials=true;"
                    "xhr.timeout=" + str(timeout_ms) + ";"
                    "for(const [key,value]of Object.entries(headers))xhr.setRequestHeader(key,value);"
                    "xhr.onload=()=>{try{const actual=new URL(xhr.responseURL),expected=new URL(url);"
                    "if(actual.origin!==expected.origin||actual.pathname!==expected.pathname||"
                    "actual.username||actual.password||actual.hash){resolve({contextError:true});return;}"
                    "resolve({status:xhr.status,json:async()=>JSON.parse(xhr.responseText)});"
                    "}catch(_){resolve({contextError:true});}};"
                    "xhr.onerror=xhr.ontimeout=xhr.onabort=()=>reject(new Error('daily-xhr-failed'));"
                    "xhr.send(JSON.stringify(" + payload + "))})")
            expression = (
                "(async()=>{" + origin_guard + "let r;let url=" + json.dumps(url) + ";"
                "const headers={'content-type':'application/json'};"
                "try{const info=globalThis.__locationInfo;" + commerce_origin_setup +
                "if(globalThis.location?.hostname==='dreamina.capcut.com'&&"
                "info?.code==='US'&&" + us_commerce_context + "){"
                + regional_url + "headers.appid='513641'}}catch(_){}"
                + ready_guard +
                "try{" + request + "}"
                "catch(e){return JSON.stringify({transport_error:'fetch'})}"
                + response_guard +
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
        if response.get('transport_error') in {'daily-page-context', 'daily-response-context'}:
            raise BrowserError('Dreamina daily credit request context could not be verified')
        if response.get('transport_error') == 'daily-sdk-not-ready':
            raise BrowserError('Dreamina daily credit page SDK is not ready before request')
        if response.get('transport_error') == 'daily-http':
            status = response.get('http')
            if isinstance(status, int) and status >= 400:
                raise BrowserRejected(f'Dreamina HTTP {status}')
            raise BrowserError('Dreamina daily credit request returned an unexpected HTTP status')
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
        return self.command("Network.getCookies", {"urls": [self.base_url + "/"]}).get("cookies") or []

    def project_id(self) -> str:
        # Dreamina does not require a per-account project for generation.
        return ""
