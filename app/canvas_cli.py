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
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .canvas_account_lane import AccountLane


PINNED_VERSION = "1.2.0"
PINNED_PROXY_VERSION = "0.8.3"
PINNED_GLOBAL_LINUX_SHA256 = "2bb3bd62585178017a153dcdaf40991e0ac78a80edbeefebbe611cee193ef649"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
TRANSPORT_DIAGNOSTIC_COMMANDS = frozenset({('auth', 'account'), ('auth', 'status'),
    ('auth', 'login'), ('auth', 'wait'), ('auth', 'refresh'), ('auth', 'logout'),
    ('model', 'list'), ('canvas', 'ls'), ('canvas', 'create'), ('node', 'create'),
    ('node', 'show'), ('node', 'quote'), ('node', 'confirm'), ('node', 'run'),
    ('resource', 'upload'), ('resource', 'get'), ('resource', 'download'), ('operation', 'status'),
    ('version',), ('schema',)})
_SECRET_KEY = re.compile(r"token|password|secret|authorization|cookie|devicecode|privatekey", re.I)
_PROFILE = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_STDERR_SYMPTOMS = {
    'ptrace_diagnostic': (b'ptrace',),
    'permission_denied': (b'operation not permitted', b'permission denied'),
    'proxy_auth': (b'proxy authentication', b'407 proxy'),
    'connection_refused': (b'connection refused',),
    'connection_reset': (b'connection reset', b'broken pipe'),
    'dns_failure': (b'no such host', b'lookup failed', b'temporary failure in name resolution'),
    'tls_failure': (b'tls handshake', b'certificate verify', b'x509:'),
    'unexpected_eof': (b'unexpected eof',),
    'process_panic': (b'panic:', b'segmentation fault'),
}


def command_failure_facts(error) -> dict:
    """Fixed scalar facts only: no exception prose, stdout, stderr or locators."""
    result = {}
    exit_code = getattr(error, 'exit_code', None)
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        result['exit_code'] = exit_code
    details = getattr(error, 'details', {})
    details = details if isinstance(details, dict) else {}
    source = details.get('error') or {}
    if isinstance(source, dict):
        for key in ('class', 'reason', 'serviceCode', 'requiredAction', 'retryable'):
            value = source.get(key)
            if (isinstance(value, (bool, int)) or isinstance(value, str)
                    and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', value)):
                result[key] = value
    transport = details.get('transport_diagnostic') or {}
    if isinstance(transport, dict):
        safe = {}
        for kind in ('stdout', 'stderr'):
            size, digest = transport.get(kind + '_bytes'), transport.get(kind + '_sha256')
            if isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= MAX_OUTPUT_BYTES:
                safe[kind + '_bytes'] = size
            if isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest):
                safe[kind + '_sha256'] = digest
        symptoms = transport.get('stderr_symptoms')
        if isinstance(symptoms, list):
            safe['stderr_symptoms'] = [name for name in _STDERR_SYMPTOMS if name in symptoms]
        if safe:
            result['transport'] = safe
    return result


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
    value = re.sub(r"(?i)((?:access[_-]?token|refresh[_-]?token|password|secret|device[_-]?code|credit[_-]?(?:confirmation[_-]?)?token|token)\s*[=:]\s*)[^\s,;&\"']+", r"\1[redacted]", value)
    value = re.sub(r"(https?://[^\s?#]+)\?[^\s]+", r"\1?[redacted]", value)
    return value[:3000]


def admission_verdict(state: Any) -> str:
    """Official run admission verdict; generation/resource states are separate."""
    if not isinstance(state, str):
        return "unknown"
    value = state.strip().upper().removeprefix("RUN_")
    return value.lower() if value in {"ACCEPTED", "REJECTED"} else "unknown"


def _has_accepted(envelope: dict[str, Any]) -> bool:
    # Preserve any partial acceptance. A query/recovery error mentioning a
    # balance must not turn a paid/uncertain submission into an account retry.
    for body in (envelope.get("data"), envelope.get("partialData")):
        if not isinstance(body, dict):
            continue
        items = body.get("items")
        if not isinstance(items, list):
            if "items" in body:
                return True
            continue
        for item in items:
            # A partial/ambiguous receipt is not positive evidence of a safe
            # pre-submit credit rejection. Never turn it into a paid retry.
            if (not isinstance(item, dict) or item.get("resources")
                    or admission_verdict(item.get("state")) != "rejected"):
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
                 timeout: float = 120, max_output_bytes: int = MAX_OUTPUT_BYTES,
                 proxy_binary: str | None = None, allow_unwrapped_for_tests: bool = False) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.binary = binary or os.getenv("IJM_CANVAS_CLI_BINARY", "dreamina-canvas")
        self.proxy_binary = (proxy_binary if proxy_binary is not None
                             else os.getenv("IJM_CANVAS_PROXY_BINARY", "/usr/local/bin/graftcp"))
        # This constructor-only seam is for fake subprocess tests on machines
        # without Linux ptrace. There is no settings/environment/API bypass.
        self._allow_unwrapped_for_tests = bool(allow_unwrapped_for_tests)
        self.timeout = max(1, float(timeout))
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self._guard = threading.Lock()
        self._locks: dict[int, threading.RLock] = {}
        self._lanes: dict[int, AccountLane] = {}
        self._project_locks: dict[tuple[int, str], threading.RLock] = {}
        self._processes: dict[int, set[subprocess.Popen[bytes]]] = {}
        self._closed = threading.Event()
        # Cache the artifact check once, never read the binary per command.
        self._pinned_global_artifact = False
        try:
            path = shutil.which(self.binary)
            if path:
                digest = hashlib.sha256()
                with open(path, 'rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                self._pinned_global_artifact = digest.hexdigest() == PINNED_GLOBAL_LINUX_SHA256
        except OSError:
            pass
        self._parallel_verified = self._allow_unwrapped_for_tests

    def account_directory(self, account: dict[str, Any]) -> Path:
        return self.data_dir / "canvas-cli" / f"account-{_account_id(account)}"

    def verify_runtime(self) -> dict[str, Any]:
        """Inspect local version/schema once; no account auth or network calls.

        A reserved probe profile deliberately has a dead loopback proxy, so a
        changed binary cannot silently borrow the service's default egress.
        Service startup can catch this error and keep explicit browser mode up.
        """
        self._parallel_verified = False
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
        self._parallel_verified = self._allow_unwrapped_for_tests or self._pinned_global_artifact
        return {"available": True, "version": version["version"], "commit": version.get("commit"),
                "distribution": version["distribution"], "schema_version": str(schema["schemaVersion"]),
                "proxy_transport": "test_unwrapped" if self._allow_unwrapped_for_tests else "graftcp",
                "proxy_version": None if self._allow_unwrapped_for_tests else PINNED_PROXY_VERSION,
                "parallel_account_commands": self._parallel_verified}

    def profile(self, account: dict[str, Any], *, deadline: float | None = None) -> str:
        """The epoch also isolates OS keyring entries after an account reset."""
        directory = self.account_directory(account)
        lock = self._lock(_account_id(account))
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas profile deadline expired')
        acquired = lock.acquire() if remaining is None else lock.acquire(timeout=remaining)
        if not acquired:
            raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas profile deadline expired while waiting for metadata')
        try:
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
        finally:
            lock.release()

    def invalidate(self, account_id: int) -> None:
        """Retain old recovery data and retire its credential profile permanently.

        The caller must first verify no remote tasks/auth sessions are active.
        Renaming does not revoke the upstream token, but the new keyring profile
        ensures that it can never be reused implicitly by this local account.
        """
        account = {"id": account_id}
        with self.account_guard(account, exclusive=True, deadline=time.monotonic() + self.timeout), self._lock(_account_id(account)):
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
        account_proxy(account)
        # graftcp owns every socket (including Go SDK transports with Proxy=nil).
        # HTTP_PROXY here would send an already proxied CLI connection through
        # the account proxy a second time. Never inherit ambient proxy options.
        env["GOMAXPROCS"] = "2"
        return env

    def _proxy_config(self, account: dict[str, Any]) -> Path:
        parsed = urlsplit(account_proxy(account))
        host = str(parsed.hostname)
        endpoint = f"[{host}]:{parsed.port}" if ":" in host else f"{host}:{parsed.port}"
        lines = ["dns_proxy = true", "dns_server = 1.1.1.1:53", "udp_proxy = true",
                 "ignore_local = false"]
        if parsed.scheme in {"socks5", "socks5h"}:
            user, password = unquote(parsed.username or ""), unquote(parsed.password or "")
            # v0.8.3's config parser trims field values and has no quoting. A
            # decoded newline must never become another routing/config option.
            if any(value != value.strip() or any(c in value for c in "\r\n\x00")
                   or len(value.encode("utf-8")) > 255 for value in (user, password)):
                raise CanvasCLIError("CLI_PROXY_CREDENTIAL_UNSUPPORTED", "Account proxy credentials cannot be represented safely by the proxy transport")
            lines.extend(["select_proxy_mode = only_socks5", f"socks5 = {endpoint}",
                          f"socks5_username = {user}", f"socks5_password = {password}"])
        elif parsed.scheme == "http" and parsed.username is None and parsed.password is None:
            # graftcp 0.8.3 ConfigureHTTPProxy accepts host:port, not an auth URL.
            # only_http_proxy rejects generic UDP instead of falling back direct.
            lines.extend(["select_proxy_mode = only_http_proxy", f"http_proxy = {endpoint}"])
        else:
            raise CanvasCLIError("CLI_PROXY_TRANSPORT_UNSUPPORTED", "Canvas proxy transport supports SOCKS5 and unauthenticated HTTP; authenticated HTTP or HTTPS requires manual browser mode")
        directory = self.account_directory(account) / "proxy"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            directory.chmod(0o700)
        path = directory / ("graftcp-" + uuid.uuid4().hex + ".conf")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
        return path

    def _lock(self, account_id: int) -> threading.RLock:
        """Short profile/epoch file access only; never a network command lock."""
        with self._guard:
            return self._locks.setdefault(account_id, threading.RLock())

    def _lane(self, account_id: int) -> AccountLane:
        with self._guard:
            # A few contract-test runners deliberately borrow account_guard.
            if not hasattr(self, '_lanes'):
                self._lanes = {}
            return self._lanes.setdefault(account_id, AccountLane(clock=lambda: time.monotonic()))

    @contextmanager
    def account_guard(self, account: dict[str, Any], *, deadline: float | None = None,
                      exclusive: bool = False):
        """Hold task/profile identity stable while allowing independent tasks."""
        lane = CanvasCLI._lane(self, _account_id(account))
        exclusive = exclusive or not getattr(self, '_parallel_verified', False)
        try:
            lane.acquire(exclusive=exclusive, deadline=deadline, stopped=self._closed)
        except TimeoutError:
            raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas task deadline expired while waiting for its account') from None
        except InterruptedError:
            raise CanvasCLIError('CLI_STOPPING', 'Canvas CLI transport is stopping') from None
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas task deadline expired before starting its account sequence')
            if self._closed.is_set():
                raise CanvasCLIError('CLI_STOPPING', 'Canvas CLI transport is stopping')
            yield
        finally:
            lane.release(exclusive=exclusive)

    @contextmanager
    def command_guard(self, account: dict[str, Any], args: list[str], *,
                      deadline: float | None = None):
        # Unknown/future auth commands remain writers; only these proved reads
        # may share the pinned global DevicePassport credential-file reader.
        exclusive = bool(args) and args[0] == 'auth' and (len(args) < 2 or args[1] not in {'account', 'status'})
        project = None
        draft_command = len(args) > 1 and args[0] == 'node' and args[1] != 'show'
        if draft_command:
            values = []
            value_flags = {'--project-id', '--node-id', '--update-id', '--title', '--mode', '--model',
                           '--prompt', '--ratio', '--resolution', '--ref', '--duration', '--count',
                           '--submit-id', '--credit-token', '--credit-ceiling'}
            index = 2
            while index < len(args):
                flag, equals, value = args[index].partition('=')
                if flag == '--project-id':
                    values.append(value if equals else args[index + 1] if index + 1 < len(args) else '')
                index += 2 if not equals and flag in value_flags else 1
            project = values[0] if len(values) == 1 and values[0] else None
            # An unbound/ambiguous raw mutation cannot bypass draft isolation.
            exclusive = exclusive or project is None
        with self.account_guard(account, deadline=deadline, exclusive=exclusive):
            if project is None:
                yield
            else:
                with self.project_guard(account, project, deadline=deadline):
                    yield

    @contextmanager
    def project_guard(self, account: dict[str, Any], project_id: str, *, deadline: float | None = None):
        """Only draft-save/quote and confirm/run sections exclude each other."""
        if not isinstance(project_id, str) or not project_id or len(project_id) > 128:
            raise CanvasCLIError('CLI_ARGUMENT_INVALID', 'Canvas project binding is invalid')
        with self._guard:
            lock = self._project_locks.setdefault((_account_id(account), project_id), threading.RLock())
        while True:
            if self._closed.is_set():
                raise CanvasCLIError('CLI_STOPPING', 'Canvas CLI transport is stopping')
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas draft deadline expired while waiting for its project')
            if lock.acquire(timeout=min(.05, remaining) if remaining is not None else .05):
                break
        try:
            if self._closed.is_set():
                raise CanvasCLIError('CLI_STOPPING', 'Canvas CLI transport is stopping')
            if deadline is not None and time.monotonic() >= deadline:
                raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas draft deadline expired before its critical section')
            yield
        finally:
            lock.release()

    @staticmethod
    def _linux_identity(pid: int) -> tuple[int, int, int, str] | None:
        try:
            fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
            return int(fields[2]), int(fields[3]), int(fields[19]), fields[0]
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _capture_process_group(process: subprocess.Popen[bytes]) -> None:
        """Remember identity only for the new session created by this invocation."""
        if os.name == 'nt':
            return
        identity = CanvasCLI._linux_identity(process.pid)
        if identity is not None and identity[:2] == (process.pid, process.pid):
            process._canvas_owned_group = (process.pid, identity[2])
            process._canvas_known_members = {process.pid: identity[2]}
            CanvasCLI._owned_group_members(process, observe=True)

    @staticmethod
    def _owned_group_members(process: subprocess.Popen[bytes], *, observe: bool = False) -> list[int]:
        token = getattr(process, '_canvas_owned_group', None)
        if not isinstance(token, tuple) or len(token) != 2:
            return []
        leader, born = token
        if (not isinstance(leader, int) or not isinstance(born, int) or leader <= 1
                or leader != process.pid):
            return []
        current = CanvasCLI._linux_identity(leader)
        # A PID reused by another session must never become our cleanup target.
        if current is not None and current[:3] != (leader, leader, born):
            return []
        known = getattr(process, '_canvas_known_members', {})
        if current is None:
            anchored = False
            for pid, start in tuple(known.items()):
                identity = CanvasCLI._linux_identity(pid)
                if (pid != leader and identity is not None and identity[:3] == (leader, leader, start)
                        and identity[3] != 'Z'):
                    anchored = True
                    break
            if not anchored:
                return []  # A vanished/reused session number alone proves no ownership.
        members = []
        for path in Path('/proc').glob('[0-9]*'):
            identity = CanvasCLI._linux_identity(int(path.name))
            if identity is not None and identity[:2] == (leader, leader):
                if identity[2] < born:
                    return []
                if observe and current is not None:
                    known[int(path.name)] = identity[2]
                if identity[3] != 'Z':
                    members.append(int(path.name))
        return members

    @staticmethod
    def _poll_with_group_observation(process: subprocess.Popen[bytes]) -> int | None:
        """Observe an exited child before reap; ordinary waits never scan /proc."""
        if (getattr(process, 'returncode', None) is None
                and getattr(process, '_canvas_owned_group', None)
                and all(hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT'))):
            try:
                ended = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except (ChildProcessError, OSError):
                pass  # No ownership inference from an unavailable observation.
            else:
                if ended is None:
                    return None
                CanvasCLI._owned_group_members(process, observe=True)
        return process.poll()

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "nt":
                if process.poll() is not None:
                    return
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                members = CanvasCLI._owned_group_members(process, observe=True)
                if not members:
                    # Capture may lose a very fast wrapper. Terminate only the
                    # still-unreaped direct child represented by this Popen.
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=.25)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=.5)
                    return
                os.killpg(process.pid, signal.SIGTERM)
                grace = time.monotonic() + .25
                while time.monotonic() < grace and CanvasCLI._owned_group_members(process):
                    process.poll()
                    time.sleep(.025)
                # The wrapper may have exited while a stopped descendant still
                # owns the pipes. Recheck ownership, not just wrapper.poll().
                if CanvasCLI._owned_group_members(process):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if process.poll() is None:
                    process.kill()  # Only this still-unreaped direct Popen child.
                    process.wait(timeout=.5)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Cleanup must not replace the original command failure.

    @staticmethod
    def _windows_pipe_ready(stream: Any) -> tuple[bool, int]:
        """Peek without a background reader holding a BufferedReader lock."""
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        peek.restype = wintypes.BOOL
        available = wintypes.DWORD()
        handle = msvcrt.get_osfhandle(stream.fileno())
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in {109, 232}:  # broken pipe / no data (closed pipe)
                return True, 0
            raise OSError('Canvas pipe observation failed')
        return False, available.value

    def _collect_output(self, process: subprocess.Popen[bytes], deadline: float) -> tuple[list[bytearray], str]:
        """Collect both pipes within one deadline; never close a concurrent read."""
        buffers = [bytearray(), bytearray()]
        pending = {stream.fileno(): (stream, index)
                   for index, stream in enumerate((process.stdout, process.stderr))}
        selector = selectors.DefaultSelector() if os.name != 'nt' else None
        exited_at = None
        failure = ''
        poll = lambda: self._poll_with_group_observation(process)

        def known_complete_response() -> bool:
            if poll() != 0:
                return False
            valid = False
            for raw in buffers:
                try:
                    value = json.loads(bytes(raw).decode('utf-8-sig'))
                except (UnicodeError, ValueError):
                    continue
                valid = valid or isinstance(value, dict) and isinstance(value.get('ok'), bool)
            if not valid:
                return False
            # Do not mistake a valid prefix for the whole response while more
            # bytes (including an overflow or trailing garbage) are readable.
            try:
                for fd, (stream, _) in pending.items():
                    if os.name == 'nt':
                        _, available = self._windows_pipe_ready(stream)
                    else:
                        import array
                        import fcntl
                        import termios
                        length = array.array('i', [0])
                        fcntl.ioctl(fd, termios.FIONREAD, length, True)
                        available = length[0]
                    if available:
                        return False
            except OSError:
                return False
            return True

        try:
            if selector is not None:
                for fd in pending:
                    os.set_blocking(fd, False)
                    selector.register(fd, selectors.EVENT_READ)
            while pending or poll() is None:
                now = time.monotonic()
                # Preserve an already observed normal receipt at the clock
                # edge; EOF on an inherited pipe is not an admission verdict.
                if known_complete_response():
                    return buffers, ''
                if now >= deadline:
                    failure = 'timeout'
                    break
                if poll() is not None:
                    exited_at = now if exited_at is None else exited_at
                    if pending and now - exited_at >= .25:
                        failure = 'drain_timeout'
                        break
                wait = min(.025, max(0, deadline - now))
                ready = [key.fd for key, _ in selector.select(wait)] if selector is not None else []
                if selector is None:
                    for fd, (stream, _) in list(pending.items()):
                        eof, available = self._windows_pipe_ready(stream)
                        if eof or available:
                            ready.append((fd, 0 if eof else min(65536, available)))
                    if not ready:
                        time.sleep(wait)
                elif not pending:
                    time.sleep(wait)
                for entry in ready:
                    fd, amount = entry if isinstance(entry, tuple) else (entry, 65536)
                    stream, index = pending[fd]
                    while True:
                        try:
                            chunk = os.read(fd, amount) if amount else b''
                        except BlockingIOError:
                            break
                        if not chunk:
                            if selector is not None:
                                selector.unregister(fd)
                            del pending[fd]
                            break
                        if len(buffers[index]) + len(chunk) > self.max_output_bytes:
                            return buffers, 'overflow'
                        buffers[index].extend(chunk)
                        if time.monotonic() >= deadline:
                            return buffers, '' if known_complete_response() else 'timeout'
                        if selector is None:
                            eof, available = self._windows_pipe_ready(stream)
                            if not eof and not available:
                                break
                            amount = 0 if eof else min(65536, available)
                if known_complete_response():
                    return buffers, ''
        except (OSError, ValueError):
            failure = 'read_failed'
        finally:
            if selector is not None:
                selector.close()
        return buffers, failure

    def cancel(self, account: dict[str, Any] | int) -> None:
        account_id = account if isinstance(account, int) else _account_id(account)
        with self._guard:
            processes = tuple(self._processes.get(account_id, ()))
        for process in processes:
            self._terminate(process)

    def close(self) -> None:
        self._closed.set()
        with self._guard:
            processes = [process for values in self._processes.values() for process in tuple(values)]
        for process in processes:
            self._terminate(process)

    def run(self, account: dict[str, Any], args: list[str], *,
            timeout: float | None = None, deadline: float | None = None) -> dict[str, Any]:
        """Return the complete JSON envelope; raise a typed, sanitized failure."""
        account_id = _account_id(account)
        if not args or any(not isinstance(a, str) or "\x00" in a for a in args):
            raise CanvasCLIError("CLI_ARGUMENT_INVALID", "Canvas command arguments are invalid")
        if any(a in {"--profile", "--format", "--yes"} or a.startswith("--profile=") for a in args):
            raise CanvasCLIError("CLI_ARGUMENT_INVALID", "Canvas transport controls profile and output flags")
        with self.command_guard(account, args, deadline=deadline):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas task deadline expired before starting its command')
                timeout = min(self.timeout if timeout is None else timeout, remaining)
            if self._closed.is_set():
                raise CanvasCLIError("CLI_STOPPING", "Canvas CLI transport is stopping")
            return self._execute(account, args, timeout, deadline=deadline)

    def _execute(self, account: dict[str, Any], args: list[str], timeout: float | None, *,
                 deadline: float | None = None) -> dict[str, Any]:
        account_id = _account_id(account)
        command = [self.binary, "--format", "json", "--non-interactive",
                   "--profile", self.profile(account, deadline=deadline), *args]
        env = self.environment(account)
        proxy_config = None
        if not self._allow_unwrapped_for_tests:
            if not sys.platform.startswith("linux") or not self.proxy_binary:
                raise CanvasCLIError("CLI_PROXY_WRAPPER_REQUIRED", "Canvas requires its pinned Linux per-account proxy transport")
            proxy_config = self._proxy_config(account)
            command = [self.proxy_binary, "--config", str(proxy_config), "--", *command]
        kwargs: dict[str, Any] = {"env": env, "cwd": str(self.account_directory(account)),
                                  "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.PIPE, "bufsize": 0}
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kwargs["start_new_session"] = True
        if deadline is not None and time.monotonic() >= deadline:
            if proxy_config:
                proxy_config.unlink(missing_ok=True)
            raise CanvasCLIError('CLI_TASK_DEADLINE_EXCEEDED', 'Canvas task deadline expired before starting its command')
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            if proxy_config:
                proxy_config.unlink(missing_ok=True)
            code = "CLI_PROXY_WRAPPER_UNAVAILABLE" if not self._allow_unwrapped_for_tests else "CLI_UNAVAILABLE"
            raise CanvasCLIError(code, f"Canvas protected command could not start: {type(exc).__name__}") from None
        self._capture_process_group(process)
        with self._guard:
            self._processes.setdefault(account_id, set()).add(process)
            stopping = self._closed.is_set()
        budget = max(0.1, float(timeout if timeout is not None else self.timeout))
        deadline = min(deadline, time.monotonic() + budget) if deadline is not None else time.monotonic() + budget
        try:
            if stopping:
                raise CanvasCLIError('CLI_STOPPING', 'Canvas CLI stopped while starting its command',
                                     outcome_unknown=args[:2] == ['node', 'run'])
            buffers, collection_failure = self._collect_output(process, deadline)
        finally:
            self._terminate(process)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
            with self._guard:
                members = self._processes.get(account_id)
                if members is not None:
                    members.discard(process)
                    if not members:
                        self._processes.pop(account_id, None)
            if proxy_config:
                proxy_config.unlink(missing_ok=True)
        paid_submit = args[:2] == ["node", "run"]
        transport_diagnostic = {f'{name}_bytes': len(data) for name, data in zip(('stdout', 'stderr'), buffers)}
        transport_diagnostic.update({f'{name}_sha256': hashlib.sha256(data).hexdigest()
                                     for name, data in zip(('stdout', 'stderr'), buffers)})
        stderr_lower = bytes(buffers[1]).lower()
        symptoms = [name for name, needles in _STDERR_SYMPTOMS.items()
                    if any(needle in stderr_lower for needle in needles)]
        if symptoms:
            transport_diagnostic['stderr_symptoms'] = symptoms
        if tuple(args[:2]) in TRANSPORT_DIAGNOSTIC_COMMANDS:
            transport_diagnostic['command_pair'] = list(args[:2])
        error_details = {'transport_diagnostic': transport_diagnostic}
        if collection_failure == 'overflow':
            raise CanvasCLIError("CLI_OUTPUT_LIMIT", "Canvas CLI output exceeded its limit",
                                 details=error_details, exit_code=process.returncode, outcome_unknown=paid_submit)
        if collection_failure == 'timeout':
            raise CanvasCLIError("CLI_COMMAND_TIMEOUT", "Canvas CLI command timed out",
                                 details=error_details, exit_code=process.returncode, outcome_unknown=paid_submit)
        if collection_failure:
            raise CanvasCLIError('CLI_OUTPUT_DRAIN_TIMEOUT' if collection_failure == 'drain_timeout' else 'CLI_OUTPUT_READ_FAILED',
                                 'Canvas CLI output did not close within its bounded collection window',
                                 details=error_details, exit_code=process.returncode, outcome_unknown=paid_submit)
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
                                 details=error_details, exit_code=process.returncode, outcome_unknown=paid_submit)
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
                                 details={**envelope, **error_details}, exit_code=process.returncode, outcome_unknown=unknown)
        if not isinstance(envelope.get("data"), dict):
            raise CanvasCLIError("CLI_INVALID_RESPONSE", "Canvas CLI data is not an object",
                                 details=error_details, outcome_unknown=paid_submit)
        return envelope
