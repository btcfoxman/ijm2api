"""Bounded, account-isolated transport for the pinned official Canvas CLI.

No credentials are placed on a command line. Each invocation is a short-lived
process; both credential profiles and local recovery records belong to one IJM
account. The inherited service proxy/credential environment is never reused.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PINNED_VERSION = "1.2.0"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_SECRET_KEY = re.compile(r"token|password|secret|authorization|cookie|devicecode|privatekey", re.I)
_PROFILE = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")


def sanitize(value: Any) -> Any:
    """Make error diagnostics safe for the existing public task/error fields."""
    if isinstance(value, dict):
        return {str(k): "[redacted]" if _SECRET_KEY.search(str(k)) else sanitize(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)(https?|socks5h?)://[^\s/@]+:[^\s/@]+@", r"\1://[redacted]@", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", value)
    value = re.sub(r"(?i)((?:access[_-]?token|refresh[_-]?token|password|secret|device[_-]?code|credit[_-]?token)\s*[=:]\s*)[^\s,;&\"']+", r"\1[redacted]", value)
    value = re.sub(r"(https?://[^\s?#]+)\?[^\s]+", r"\1?[redacted]", value)
    return value[:3000]


def _has_accepted(envelope: dict[str, Any]) -> bool:
    # Preserve any partial acceptance. A query/recovery error mentioning a
    # balance must not turn a paid/uncertain submission into an account retry.
    for body in (envelope.get("data"), envelope.get("partialData")):
        if not isinstance(body, dict):
            continue
        for item in body.get("items", []):
            if not isinstance(item, dict):
                continue
            if item.get("resources") or str(item.get("state", "")).lower() in {
                    "accepted", "running", "in_progress", "completed", "success", "unknown"}:
                return True
    return False


def _credit_rejection(envelope: dict[str, Any], error: dict[str, Any]) -> bool:
    if (error.get("requiredAction") == "resume" or error.get("class") == "operation_incomplete"
            or _has_accepted(envelope)):
        return False
    facts = {str(error.get(name, "")).lower() for name in ("code", "reason", "serviceCode")}
    return bool(facts & {"insufficient_credits", "insufficient_credit", "credit_insufficient",
                         "insufficient_balance", "no_relevant_benefits", "1006"})


class CanvasCLIError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None,
                 exit_code: int | None = None, outcome_unknown: bool = False) -> None:
        self.code = code
        self.message = str(sanitize(message))
        self.details = sanitize(details or {})
        self.exit_code = exit_code
        self.outcome_unknown = outcome_unknown
        super().__init__(self.message)


def _account_id(account: dict[str, Any]) -> int:
    value = account.get("id")
    if isinstance(value, bool):
        raise CanvasCLIError("CLI_ACCOUNT_INVALID", "Canvas requires a persistent account ID")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise CanvasCLIError("CLI_ACCOUNT_INVALID", "Canvas requires a persistent account ID") from None
    if result < 1:
        raise CanvasCLIError("CLI_ACCOUNT_INVALID", "Canvas requires a positive account ID")
    return result


def account_profile(account: dict[str, Any]) -> str:
    profile = str(account.get("cli_profile") or f"ijm-account-{_account_id(account)}")
    if not _PROFILE.fullmatch(profile) or profile in {".", "..", "default"}:
        raise CanvasCLIError("CLI_PROFILE_INVALID", "Canvas account profile is invalid")
    return profile


def account_proxy(account: dict[str, Any]) -> str:
    value = str(account.get("proxy_url") or "").strip()
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https", "socks5", "socks5h"}
                 and parsed.hostname and parsed.port and not parsed.path
                 and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid or any(c in value for c in "\r\n\x00"):
        raise CanvasCLIError("CLI_PROXY_REQUIRED", "Canvas requires the account's valid HTTP or SOCKS5 proxy")
    # Go's proxy transport understands socks5h; destination DNS stays at the
    # account proxy rather than the service's ambient DNS route.
    return value.replace("socks5://", "socks5h://", 1)


class CanvasCLI:
    def __init__(self, data_dir: str | Path, binary: str | None = None, *,
                 timeout: float = 120, max_output_bytes: int = MAX_OUTPUT_BYTES) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.binary = binary or os.getenv("IJM_CANVAS_CLI_BINARY", "dreamina-canvas")
        self.timeout = max(1, float(timeout))
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self._guard = threading.Lock()
        self._locks: dict[int, threading.RLock] = {}
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._closed = threading.Event()

    def account_directory(self, account: dict[str, Any]) -> Path:
        return self.data_dir / "canvas-cli" / f"account-{_account_id(account)}"

    def verify_runtime(self) -> dict[str, Any]:
        """Inspect local version/schema once; no account auth or network calls.

        A reserved probe profile deliberately has a dead loopback proxy, so a
        changed binary cannot silently borrow the service's default egress.
        Service startup can catch this error and keep explicit browser mode up.
        """
        probe = {"id": 2147483647, "proxy_url": "http://127.0.0.1:9"}
        version = self.run(probe, ["version"], timeout=10)["data"]
        if (version.get("version") != PINNED_VERSION or version.get("distribution") != "global"
                or version.get("edition") != "public"):
            raise CanvasCLIError("CLI_RUNTIME_INCOMPATIBLE", "Canvas CLI must be the pinned global public 1.2.0 release")
        schema = self.run(probe, ["schema"], timeout=15)["data"]
        if schema.get("distribution") != "global" or str(schema.get("schemaVersion")) != "1":
            raise CanvasCLIError("CLI_RUNTIME_INCOMPATIBLE", "Canvas CLI schema is incompatible")
        commands: dict[str, set[str]] = {}
        def visit(node: dict[str, Any], prefix: str = "") -> None:
            name = (prefix + " " + str(node.get("name") or "")).strip()
            commands[name] = {str(x.get("name")) for x in (node.get("flags") or [])}
            for child in node.get("subcommands") or []:
                visit(child, name)
        for command in schema.get("subcommands", []):
            visit(command)
        required = {
            "auth account": set(), "auth status": set(), "auth login": {"force"},
            "auth wait": {"device-code", "timeout"}, "auth refresh": set(),
            "model list": {"type"}, "canvas ls": {"limit", "cursor"},
            "canvas create": {"project-id"},
            "resource upload": {"file", "type", "project-id", "resource-id"},
            "resource get": {"project-id"}, "resource download": {"project-id", "output"},
            "node quote": {"project-id", "node-id"},
            "node confirm": {"project-id", "node-id", "credit-ceiling"},
            "node run": {"project-id", "node-id", "submit-id", "credit-token"},
            "node show": {"project-id", "node-id"}, "operation status": {"project-id"},
        }
        common = {"project-id", "node-id", "update-id", "title", "mode", "model", "prompt", "ratio", "resolution", "ref"}
        required["node create image"] = common | {"count"}
        required["node create video"] = common | {"duration"}
        globals_ = {str(x.get("name")) for x in schema.get("globalFlags", [])}
        if (not {"format", "non-interactive", "profile"} <= globals_
                or any(name not in commands or not flags <= commands[name] for name, flags in required.items())):
            raise CanvasCLIError("CLI_RUNTIME_INCOMPATIBLE", "Canvas CLI is missing required command contracts")
        return {"available": True, "version": version["version"], "commit": version.get("commit"),
                "distribution": version["distribution"], "schema_version": str(schema["schemaVersion"])}

    def profile(self, account: dict[str, Any]) -> str:
        """The epoch also isolates OS keyring entries after an account reset."""
        directory = self.account_directory(account)
        with self._lock(_account_id(account)):
            path = directory / "credential-profile"
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if not _PROFILE.fullmatch(value):
                    raise CanvasCLIError("CLI_PROFILE_INVALID", "Stored Canvas account profile is invalid")
                return value
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            namespace = hashlib.sha256(str(self.data_dir).encode()).hexdigest()[:10]
            value = f"ijm-{namespace}-account-{_account_id(account)}"
            path.write_text(value, encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            return value

    def invalidate(self, account_id: int) -> None:
        """Retain old recovery data and retire its credential profile permanently.

        The caller must first verify no remote tasks/auth sessions are active.
        Renaming does not revoke the upstream token, but the new keyring profile
        ensures that it can never be reused implicitly by this local account.
        """
        account = {"id": account_id}
        with self._lock(_account_id(account)):
            with self._guard:
                if account_id in self._processes:
                    raise CanvasCLIError("CLI_ACCOUNT_BUSY", "Canvas account has an active command")
            directory = self.account_directory(account)
            parent = directory.parent.resolve()
            if parent != (self.data_dir / "canvas-cli").resolve():
                raise CanvasCLIError("CLI_PATH_INVALID", "Canvas account directory is invalid")
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.exists():
                retired = parent / f"retired-account-{account_id}-{uuid.uuid4().hex}"
                directory.rename(retired)
            directory.mkdir(mode=0o700)
            path = directory / "credential-profile"
            path.write_text(f"ijm-account-{account_id}-{uuid.uuid4().hex}", encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)

    def environment(self, account: dict[str, Any]) -> dict[str, str]:
        directory = self.account_directory(account)
        names = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL",
                 "TZ", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        env = {k: v for k, v in os.environ.items() if k.upper() in names}
        for child in ("home", "config", "data", "state", "cache", "tmp", "business"):
            target = directory / child
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                target.chmod(0o700)
        env.update({"HOME": str(directory / "home"), "USERPROFILE": str(directory / "home"),
                    "APPDATA": str(directory / "config"), "LOCALAPPDATA": str(directory / "data"),
                    "XDG_CONFIG_HOME": str(directory / "config"),
                    "XDG_DATA_HOME": str(directory / "data"),
                    "XDG_STATE_HOME": str(directory / "state"),
                    "XDG_CACHE_HOME": str(directory / "cache"),
                    "DREAMINA_CANVAS_STATE_DIR": str(directory / "business"),
                    "TMPDIR": str(directory / "tmp"), "TMP": str(directory / "tmp"),
                    "TEMP": str(directory / "tmp"), "BROWSER": "false"})
        proxy = account_proxy(account)
        env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                    "http_proxy": proxy, "https_proxy": proxy,
                    "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"})
        return env

    def _lock(self, account_id: int) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(account_id, threading.RLock())

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def cancel(self, account: dict[str, Any] | int) -> None:
        account_id = account if isinstance(account, int) else _account_id(account)
        with self._guard:
            process = self._processes.get(account_id)
        if process:
            self._terminate(process)

    def close(self) -> None:
        self._closed.set()
        with self._guard:
            processes = list(self._processes.values())
        for process in processes:
            self._terminate(process)

    def run(self, account: dict[str, Any], args: list[str], *,
            timeout: float | None = None) -> dict[str, Any]:
        """Return the complete JSON envelope; raise a typed, sanitized failure."""
        account_id = _account_id(account)
        if not args or any(not isinstance(a, str) or "\x00" in a for a in args):
            raise CanvasCLIError("CLI_ARGUMENT_INVALID", "Canvas command arguments are invalid")
        if any(a in {"--profile", "--format", "--yes"} or a.startswith("--profile=") for a in args):
            raise CanvasCLIError("CLI_ARGUMENT_INVALID", "Canvas transport controls profile and output flags")
        with self._lock(account_id):
            if self._closed.is_set():
                raise CanvasCLIError("CLI_STOPPING", "Canvas CLI transport is stopping")
            return self._execute(account, args, timeout)

    def _execute(self, account: dict[str, Any], args: list[str], timeout: float | None) -> dict[str, Any]:
        account_id = _account_id(account)
        command = [self.binary, "--format", "json", "--non-interactive",
                   "--profile", self.profile(account), *args]
        env = self.environment(account)
        kwargs: dict[str, Any] = {"env": env, "cwd": str(self.account_directory(account)),
                                  "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.PIPE}
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise CanvasCLIError("CLI_UNAVAILABLE", f"Canvas CLI could not start: {type(exc).__name__}") from None
        with self._guard:
            self._processes[account_id] = process
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        def collect(stream: Any, index: int) -> None:
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    if len(buffers[index]) + len(chunk) > self.max_output_bytes:
                        overflow.set()
                        break
                    buffers[index].extend(chunk)
            except (OSError, ValueError):
                pass
        readers = [threading.Thread(target=collect, args=(stream, i), daemon=True)
                   for i, stream in enumerate((process.stdout, process.stderr))]
        for reader in readers:
            reader.start()
        budget = max(0.1, float(timeout if timeout is not None else self.timeout))
        deadline = time.monotonic() + budget
        timed_out = False
        try:
            while process.poll() is None:
                if overflow.is_set() or time.monotonic() >= deadline:
                    timed_out = not overflow.is_set()
                    self._terminate(process)
                    break
                time.sleep(0.025)
            for reader in readers:
                reader.join(timeout=2)
        finally:
            if process.poll() is None:
                self._terminate(process)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
            with self._guard:
                self._processes.pop(account_id, None)
        paid_submit = args[:2] == ["node", "run"]
        if overflow.is_set():
            raise CanvasCLIError("CLI_OUTPUT_LIMIT", "Canvas CLI output exceeded its limit",
                                 outcome_unknown=paid_submit)
        if timed_out:
            raise CanvasCLIError("CLI_COMMAND_TIMEOUT", "Canvas CLI command timed out",
                                 outcome_unknown=paid_submit)
        envelope: dict[str, Any] | None = None
        for raw in buffers:
            try:
                value = json.loads(bytes(raw).decode("utf-8-sig"))
                if isinstance(value, dict) and isinstance(value.get("ok"), bool):
                    envelope = value
                    break
            except (UnicodeError, ValueError):
                pass
        if envelope is None:
            raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas CLI returned no valid JSON result",
                                 exit_code=process.returncode, outcome_unknown=paid_submit)
        if process.returncode != 0 or not envelope.get("ok"):
            error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
            # A structured service/transport error still may arrive after the
            # provider accepted the run. Only positively established pre-submit
            # rejection can release the account's credit reservation.
            local_rejection = (process.returncode == 2 and error.get("class") == "invalid_input")
            auth_rejection = error.get("code") == "cli.authentication_required"
            unconfirmed = error.get("code") in {"cli.generation_confirmation_required",
                                                 "cli.credit_ceiling_exceeded"}
            local_storage = error.get("localStorage") or envelope.get("localStorage") or {}
            before_submit = isinstance(local_storage, dict) and local_storage.get("submissionPhase") == "before_submit"
            credit_rejection = _credit_rejection(envelope, error)
            unknown = paid_submit and (_has_accepted(envelope) or not (
                local_rejection or auth_rejection or unconfirmed or before_submit or credit_rejection))
            raise CanvasCLIError(str(error.get("code") or "CLI_COMMAND_FAILED"),
                                 str(error.get("message") or "Canvas CLI command failed"),
                                 details=envelope, exit_code=process.returncode, outcome_unknown=unknown)
        if not isinstance(envelope.get("data"), dict):
            raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas CLI data is not an object",
                                 outcome_unknown=paid_submit)
        return envelope
