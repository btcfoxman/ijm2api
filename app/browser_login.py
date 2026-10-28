"""Private headed Chromium and short-lived VNC lease for Dreamina account login."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .browser import BrowserError, BrowserRejected, browser_alive, close_resource_timing_capture, ensure_resource_timing_capture
from .client import DreaminaClient as UpstreamClient
from .account_automation import AccountAutomation
from .auto_login import verified_login_user_id

REQUIRES_PROJECT = False


class LoginError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def _verification_failure(exc: Exception, stage: str) -> str:
    """Explain a failed admin check without returning browser or response data."""
    known_browser_errors = {
        "account proxy must be a SOCKS5 or HTTP proxy with host and port":
            "account proxy configuration is invalid",
        "open and log in to Dreamina in the account browser":
            "open the Dreamina generation page and finish login",
        "Dreamina browser fetch failed":
            "Dreamina credit request failed; check browser region and proxy connectivity",
        "Dreamina browser JavaScript request failed":
            "Dreamina page is not ready; wait for the generation page to load",
        "Dreamina browser JavaScript request returned an error":
            "Dreamina page is not ready; wait for the generation page to load",
        "Dreamina returned an unreadable JSON response":
            "Dreamina credit service returned an unreadable response",
        "Dreamina returned an invalid response envelope":
            "Dreamina credit service returned an unreadable response",
        "Dreamina HTTP 200 returned non-JSON":
            "Dreamina credit service returned an unreadable response",
    }
    if isinstance(exc, BrowserRejected):
        reason = "Dreamina credit service rejected verification; check the signed-in account"
    elif isinstance(exc, BrowserError):
        reason = known_browser_errors.get(str(exc), "browser connection or page request failed")
    else:
        reason = "account verification could not complete"
    return f"browser login verification failed ({stage}): {reason}"


def browser_proxy(value: str) -> str:
    if not value.strip():
        return ""
    parsed = urlsplit(value)
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise LoginError("browser proxy port is invalid", 422) from exc
    if parsed.scheme not in {"http", "https", "socks5"} or not host or not port:
        raise LoginError("browser proxy must be an HTTP or SOCKS5 host:port URL", 422)
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise LoginError("browser login does not support proxy credentials or URL paths", 422)
    return f"{parsed.scheme}://{host}:{port}"


class LoginDesktop:
    def __init__(self, profile: Path, proxy: str) -> None:
        self.profile = profile
        self.proxy = proxy
        self.folder: tempfile.TemporaryDirectory | None = None
        self.processes: list[asyncio.subprocess.Process] = []
        self.display: str | None = None
        self.vnc_port: int | None = None
        self.cdp_port: int | None = None
        self.password: str | None = None

    @staticmethod
    def available() -> bool:
        return os.name == "posix" and all(shutil.which(command) for command in
            ("Xvfb", "x11vnc", "fluxbox", "chromium")) and Path("/usr/share/novnc/core/rfb.js").is_file()

    async def _spawn(self, *args: str, **kwargs) -> asyncio.subprocess.Process:
        task = asyncio.create_task(asyncio.create_subprocess_exec(
            *args, stderr=asyncio.subprocess.DEVNULL, **kwargs))
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            self.processes.append(await task)
            raise
        self.processes.append(process)
        return process

    @staticmethod
    def _port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def _clear_stale_profile_locks(self, proc_root: Path = Path("/proc")) -> None:
        """Release Chromium's symlink locks left by a replaced container."""
        profile_arg = f"--user-data-dir={self.profile}".encode()
        for process in proc_root.iterdir():
            if not process.name.isdigit():
                continue
            try:
                command = (process / "cmdline").read_bytes()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if profile_arg in command.split(b"\0"):
                raise LoginError("this browser profile is already in use", 409)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = self.profile / name
            if path.is_symlink():
                path.unlink()

    async def start(self) -> None:
        if not self.available():
            raise LoginError("headed browser components are unavailable in the IJM2API image", 503)
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile.chmod(0o700)
        self._clear_stale_profile_locks()
        self.folder = tempfile.TemporaryDirectory(prefix="ijm2api-login-")
        self.password = secrets.token_urlsafe(6)  # RFB protocol reads eight characters.
        password_file = Path(self.folder.name) / "vnc-password"
        password_file.write_text(self.password + "\n", encoding="ascii")
        password_file.chmod(0o600)
        xvfb = await self._spawn("Xvfb", "-displayfd", "1", "-screen", "0", "1920x900x24",
                                 "-nolisten", "tcp", "-ac", stdout=asyncio.subprocess.PIPE)
        number = (await asyncio.wait_for(xvfb.stdout.readline(), 10)).decode().strip()
        if not number.isdigit() or xvfb.returncode is not None:
            raise LoginError("login desktop did not start", 503)
        self.display = ":" + number
        # Debian Chromium's crashpad needs a writable HOME/XDG tree. The
        # service account intentionally has no system home directory.
        home = self.profile.with_name(self.profile.name + "-home")
        config_home, cache_home = home / ".config", home / ".cache"
        for directory in (home, config_home, cache_home):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        env = {**os.environ, "DISPLAY": self.display, "HOME": str(home),
               "XDG_CONFIG_HOME": str(config_home), "XDG_CACHE_HOME": str(cache_home)}
        await self._spawn("fluxbox", env=env, stdout=asyncio.subprocess.DEVNULL)
        self.vnc_port = self._port()
        await self._spawn("x11vnc", "-display", self.display, "-rfbport", str(self.vnc_port),
                          "-listen", "127.0.0.1", "-localhost", "-no6", "-forever", "-shared",
                          "-passwdfile", str(password_file), "-norc", "-noremote", "-nocmds",
                          "-novncconnect", "-quiet", stdout=asyncio.subprocess.DEVNULL,
                          env=env)
        for _ in range(50):
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self.vnc_port), 1)
                banner = await asyncio.wait_for(reader.readexactly(12), 1)
                if banner.startswith(b"RFB "):
                    break
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
                pass
            finally:
                if writer:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), 1)
                    except (OSError, asyncio.TimeoutError):
                        pass
            await asyncio.sleep(.1)
        else:
            raise LoginError("remote login screen did not start", 503)
        self.cdp_port = self._port()
        args = ["chromium", f"--user-data-dir={self.profile}", "--no-sandbox",
                "--disable-dev-shm-usage", "--password-store=basic", "--no-first-run",
                "--no-default-browser-check", "--start-maximized", "--window-size=1920,900",
                "--window-position=0,0", "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={self.cdp_port}"]
        if self.proxy:
            args.append("--proxy-server=" + self.proxy)
        args.append("https://dreamina.capcut.com/ai-tool/generate")
        browser = await self._spawn(*args, stdout=asyncio.subprocess.DEVNULL, env=env)
        for _ in range(100):
            if browser.returncode is not None:
                break
            try:
                response = await asyncio.to_thread(requests.get,
                    f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=1,
                    proxies={"http": "", "https": ""})
                if response.ok:
                    return
            except requests.RequestException:
                pass
            await asyncio.sleep(.1)
        raise LoginError("login browser did not start", 503)

    def alive(self) -> bool:
        return bool(self.processes) and all(process.returncode is None for process in self.processes)

    async def close(self) -> None:
        if self.cdp_port:
            try:
                await asyncio.to_thread(close_resource_timing_capture, self.cdp_port)
            except Exception:
                # Telemetry cleanup must never prevent the existing owned
                # desktop/process cleanup from completing.
                pass
        for process in reversed(self.processes):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            else:
                await process.wait()
        self.processes.clear()
        if self.folder:
            self.folder.cleanup()
            self.folder = None
        self.password = None
        self.vnc_port = None
        self.cdp_port = None


class BrowserLoginManager:
    LEASE_SECONDS = 20 * 60

    def __init__(self, store, service) -> None:
        self.store = store
        self.service = service
        self.active: dict | None = None
        self.desktops: dict[int, LoginDesktop] = {}
        self.lock = asyncio.Lock()
        self.maintenance: asyncio.Task | None = None
        self.closed = False

    def start_maintenance(self) -> None:
        self.maintenance = asyncio.create_task(self._maintain(), name="account-login-lease")

    def status(self) -> dict:
        active = self.active
        return {"available": LoginDesktop.available(),
                "active": ({"account_id": active["account_id"], "session_id": active["id"],
                            "phase": active["phase"],
                            "remaining_seconds": max(0, int(active["deadline"] - time.monotonic()))}
                           if active else None)}

    def viewer(self, ticket: str | None) -> dict | None:
        active = self.active
        if (not active or active["phase"] != "login" or not ticket
                or not secrets.compare_digest(active["ticket"], ticket)
                or time.monotonic() >= active["deadline"] or not active["desktop"].alive()):
            return None
        return active

    def _owned(self, session_id: str) -> dict:
        active = self.active
        if not active or not secrets.compare_digest(active["id"], session_id):
            raise LoginError("login window has ended", 404)
        if time.monotonic() >= active["deadline"]:
            raise LoginError("login window has expired", 409)
        return active

    async def start(self, account_id: int) -> dict:
        async with self.lock:
            if self.closed or not LoginDesktop.available():
                raise LoginError("headed browser components are unavailable", 503)
            if self.active:
                raise LoginError("another account is using the login browser")
            account = self.store.account(account_id, private=True)
            if not account:
                raise LoginError("account not found", 404)
            if getattr(self.service, 'auto_login_busy', lambda _: False)(account_id):
                raise LoginError('account is in automatic login')
            proxy = browser_proxy(str(account["proxy_url"]))
            desktop = self.desktops.get(account_id)
            if desktop and desktop.proxy != proxy:
                raise LoginError("account proxy changed; close the old browser before login")
            if desktop and not desktop.alive():
                await desktop.close()
                self.desktops.pop(account_id, None)
                desktop = None
            with self.service._lock:
                if getattr(self.service, 'auto_login_busy', lambda _: False)(account_id):
                    raise LoginError('account is in automatic login')
                if self.service._active.get(account_id, 0) or self.store.active_account_count(account_id):
                    raise LoginError("account has active tasks")
                try:
                    if desktop:
                        self.service.browser_runtime.assert_idle(account_id)
                    else:
                        self.service.browser_runtime.close_idle(account_id)
                except Exception as exc:
                    raise LoginError("account browser is in use") from exc
                self.store.update_account(account_id, {"enabled": False, "status": "logging_in",
                    "login_stage": "manual_login", "watermark_ready": False, "agreement_ready": False,
                    "setup_checked_at": None})
            root = Path(str(self.service.settings()["chrome_user_data_root"]))
            desktop = desktop or LoginDesktop(root / str(account_id), proxy)
            active = {"id": secrets.token_urlsafe(18), "ticket": secrets.token_urlsafe(32),
                      "account_id": account_id, "desktop": desktop,
                      "previous": account, "new_desktop": account_id not in self.desktops,
                      "enable_after": (bool(account['login_enable_after'])
                                       if account.get('login_enable_after') is not None
                                       else bool(account["enabled"] or account["status"] in
                                           {"pending", "login_required", "logging_in"})),
                      "deadline": time.monotonic() + self.LEASE_SECONDS, "phase": "starting"}
            self.active = active
            try:
                if active["new_desktop"]:
                    await desktop.start()
                    self.desktops[account_id] = desktop
                target_deadline = time.monotonic() + 30
                while not await asyncio.to_thread(browser_alive, desktop.cdp_port):
                    if time.monotonic() >= target_deadline:
                        raise BrowserError("Dreamina login page target did not become ready")
                    await asyncio.sleep(.1)
                await asyncio.to_thread(ensure_resource_timing_capture, desktop.cdp_port)
                active["phase"] = "login"
                return self.status()
            except BaseException:
                if active["new_desktop"]:
                    await desktop.close()
                    self.desktops.pop(account_id, None)
                self.active = None
                self.store.update_account(account_id, {"enabled": bool(account["enabled"]),
                                                       "status": account["status"]})
                raise

    async def finish(self, session_id: str) -> dict:
        async with self.lock:
            active = self._owned(session_id)
            active["phase"] = "verifying"
            account_id = active["account_id"]
            stage = "account lookup"
            try:
                account = self.store.account(account_id, private=True)
                if not account:
                    raise LoginError("account no longer exists", 404)
                temporary = {**account, "cdp_port": active["desktop"].cdp_port}
                stage = "browser setup"
                client = UpstreamClient(temporary)
                stage = "credit read"
                state = await asyncio.to_thread(client.account_state)
                stage = 'generation preferences'
                ready = await asyncio.to_thread(AccountAutomation(temporary, self.service.settings(),
                    allow_initial_identity=True).ensure_ready)
                if ready.get('watermark_ready') is not True or ready.get('agreement_ready') is not True:
                    raise LoginError('generation preferences are not confirmed; check watermark and AI agreement settings')
                login_user_id = verified_login_user_id(ready)
                if not login_user_id:
                    raise LoginError('signed-in account identity is not confirmed; complete login before retrying')
                stage = "page check"
                project_id = client.browser.project_id()
                if REQUIRES_PROJECT and not (project_id or account["project_id"]):
                    raise LoginError("open the Dreamina generation page before finishing login")
                stage = "account update"
                values = {"cdp_port": active["desktop"].cdp_port,
                          "managed_profile": True, "enabled": active["enable_after"],
                          "status": "active" if active["enable_after"] else "disabled",
                          "balance": state["available_balance"],
                          "balance_details": state["buckets"], "last_error": "", "login_stage": "ready",
                          "watermark_ready": True, "agreement_ready": True, "setup_checked_at": time.time(),
                          "login_user_id": login_user_id}
                if project_id:
                    values["project_id"] = project_id
                threshold = float(self.service.settings()["low_balance_disable_threshold"] or 0)
                if threshold > 0 and float(state["available_balance"]) < threshold:
                    values.update({"enabled": False, "status": "disabled_low_balance",
                                   "last_error": f"credit balance below {threshold}"})
                with self.service._lock:
                    self.store.clear_media_cache(account_id)
                    result = self.store.update_account(account_id, values)
                    self.active = None  # Viewer ticket expires; Chrome stays online.
                return result
            except asyncio.CancelledError:
                active["phase"] = "login"
                raise
            except Exception as exc:
                if self.active is active:
                    active["phase"] = "login"
                if isinstance(exc, LoginError):
                    raise
                raise LoginError(_verification_failure(exc, stage), 409) from exc

    async def cancel(self, session_id: str) -> None:
        async with self.lock:
            active = self._owned(session_id)
            self._end_viewer(active)

    def _end_viewer(self, active: dict) -> None:
        previous = active["previous"]
        desktop = active["desktop"]
        with self.service._lock:
            self.active = None
            self.store.update_account(active["account_id"],
                {"enabled": bool(previous["enabled"]), "status": previous["status"],
                 "cdp_port": desktop.cdp_port if desktop.alive() else 19300 + active["account_id"],
                 "managed_profile": True})

    async def close_account(self, account_id: int) -> None:
        async with self.lock:
            if getattr(self.service, 'auto_login_busy', lambda _: False)(account_id):
                raise LoginError('account is in automatic login')
            if self.active and self.active["account_id"] == account_id:
                raise LoginError("finish or cancel online login first")
            with self.service._lock:
                if getattr(self.service, 'auto_login_busy', lambda _: False)(account_id):
                    raise LoginError('account is in automatic login')
                if self.service._active.get(account_id, 0) or self.store.active_account_count(account_id):
                    raise LoginError("account has active tasks")
                try:
                    self.service.browser_runtime.assert_idle(account_id)
                except Exception as exc:
                    raise LoginError("account browser is in use") from exc
                self.store.update_account(account_id, {"enabled": False, "status": "profile_resetting"})
            desktop = self.desktops.pop(account_id, None)
            if desktop:
                await desktop.close()

    async def _maintain(self) -> None:
        while not self.closed:
            await asyncio.sleep(3)
            async with self.lock:
                active = self.active
                if active and (time.monotonic() >= active["deadline"] or not active["desktop"].alive()):
                    self._end_viewer(active)

    async def close(self) -> None:
        self.closed = True
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        async with self.lock:
            if self.active:
                self._end_viewer(self.active)
            for account_id, desktop in self.desktops.items():
                old_port = desktop.cdp_port
                await desktop.close()
                account = self.store.account(account_id, private=True)
                if account and old_port and int(account["cdp_port"] or 0) == old_port:
                    self.store.update_account(account_id, {"cdp_port": 19300 + account_id})
            self.desktops.clear()
