"""Read-only, explicit maintenance gate for already accepted Canvas queries."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

ACTIVE = {"queued", "preparing", "submitting", "submitted", "running"}
READ_COMMANDS = {("operation", "status"), ("node", "show"), ("node", "find"),
                 ("resource", "get"), ("auth", "account")}
LOCAL_ID = re.compile(r"ijm_[a-f0-9]{16}")
UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_expected(raw):
    value = json.loads(raw)
    if (not isinstance(value, list) or not 1 <= len(value) <= 20
            or any(not isinstance(item, str) or not LOCAL_ID.fullmatch(item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("expected_task_list_invalid")
    return set(value)


def command_pair(args):
    tokens, profile = list(args[1:]), None
    while tokens and tokens[0].startswith("--"):
        flag = tokens.pop(0)
        if flag == "--non-interactive":
            continue
        if flag in {"--format", "--profile"} and tokens:
            value = tokens.pop(0)
            if flag == "--profile":
                if profile is not None:
                    return None, None
                profile = value
            continue
        return None, None
    return tuple(tokens[:2]), profile


def inspect_processes(proc_root, profiles):
    readers = 0
    for process in Path(proc_root).glob("[0-9]*"):
        try:
            args = [os.fsdecode(value) for value in (process / "cmdline").read_bytes().split(b"\0") if value]
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise ValueError("process_visibility_denied") from None
        if not args or not Path(args[0]).name.startswith("dreamina-canvas"):
            continue
        pair, profile = command_pair(args)
        if pair not in READ_COMMANDS or profile not in profiles:
            raise ValueError("non_query_or_unbound_CLI_process")
        readers += 1
    return readers


def check(database, expected, data_root, startup_source, startup_sha256, proc_root=Path("/proc"), now=None):
    now = time.time() if now is None else now
    if (not re.fullmatch(r"[a-f0-9]{64}", startup_sha256)
            or Path(startup_source).is_symlink()
            or hashlib.sha256(Path(startup_source).read_bytes()).hexdigest() != startup_sha256):
        raise ValueError("startup_recovery_source_changed")
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        accounts = {row["id"]: dict(row) for row in db.execute("SELECT * FROM accounts")}
        tasks = [dict(row) for row in db.execute("SELECT * FROM tasks") if row["status"] in ACTIVE]
    if any(row.get("status") == "logging_in" or row.get("cli_auth_status") in {"queued", "authorizing", "manual_required"} for row in accounts.values()):
        raise ValueError("account_login_or_authorization_active")
    if not tasks:
        readers = inspect_processes(proc_root, set())
        return {"passed": True, "mode": "idle", "active_count": 0, "binding_sha256": None,
                "query_processes": readers, "expected_task_count": len(expected)}
    if {row["id"] for row in tasks} != expected:
        raise ValueError("active_task_set_differs_from_manual_list")
    bindings, profiles = [], set()
    for row in tasks:
        context = json.loads(row.get("upstream_context_json") or "{}")
        account = accounts.get(row.get("account_id"))
        if (not account or row.get("execution_backend") != "canvas_cli" or row["status"] not in {"submitted", "running"}
                or context.get("backend") != "canvas_cli" or context.get("task_id") != row["id"]
                or type(context.get("account_id")) is not int or context["account_id"] != row["account_id"]
                or context.get("phase") != "accepted" or context.get("submission_phase") not in {"accepted", "submitted"}
                or "submit_error" in context or context.get("reconciliation_paused")
                or context.get("reconciliation_closed") or "manual_termination" in context
                or row.get("error_code") not in {"", "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"}):
            raise ValueError("task_not_explicitly_accepted_query_only")
        if (not account.get("enabled") or account.get("status") != "active"
                or account.get("execution_mode") != "canvas_cli" or account.get("cli_auth_status") != "ready"):
            raise ValueError("accepted_account_not_ready")
        user = str(account.get("login_user_id") or "")
        if not user or str(account.get("cli_user_id") or "") != user or str(context.get("expected_user_id") or "") != user:
            raise ValueError("accepted_account_identity_changed")
        for key in ("project_id", "submit_id"):
            if not isinstance(context.get(key), str) or not UUID.fullmatch(context[key]):
                raise ValueError("accepted_binding_invalid")
        if not isinstance(context.get("node_id"), str) or not re.fullmatch(r"node_[0123456789abcdefghjkmnpqrstvwxyz]{10}", context["node_id"]):
            raise ValueError("accepted_node_binding_invalid")
        resources = context.get("resource_ids")
        if (context.get("kind") != "video" or type(context.get("output_count")) is not int or context["output_count"] != 1
                or not isinstance(resources, list) or len(resources) != 1
                or not isinstance(resources[0], str) or not UUID.fullmatch(resources[0])):
            raise ValueError("accepted_original_resources_missing")
        deadline = context.get("observation_deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or not now < deadline:
            raise ValueError("original_observation_deadline_expired")
        account_dir = Path(data_root) / "canvas-cli" / ("account-" + str(row["account_id"]))
        marker = account_dir / "credential-profile"
        if (account_dir.is_symlink() or (Path(data_root) / "canvas-cli").is_symlink()
                or not account_dir.resolve().is_relative_to((Path(data_root) / "canvas-cli").resolve())
                or marker.is_symlink() or not marker.is_file()):
            raise ValueError("credential_profile_marker_unavailable")
        profile = marker.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", profile) or context.get("profile") != profile:
            raise ValueError("credential_profile_epoch_changed")
        profiles.add(profile)
        if row.get("run_id") and row["run_id"] != context["submit_id"]:
            raise ValueError("row_submission_binding_changed")
        if row.get("thread_id") and row["thread_id"] != context["project_id"]:
            raise ValueError("row_project_binding_changed")
        bindings.append({"task": row["id"], "request_sha256": hashlib.sha256(row["request_json"].encode()).hexdigest(),
            "normalized_sha256": hashlib.sha256(row["normalized_json"].encode()).hexdigest(),
            "context": {key: context.get(key) for key in ("task_id", "account_id", "profile", "expected_user_id", "project_id", "node_id", "submit_id", "resource_ids", "kind", "output_count", "model", "mode", "observation_deadline")},
            "profile_epoch_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            "account_identity_sha256": digest({key: account.get(key) for key in ("id", "enabled", "execution_mode", "cli_auth_status", "login_user_id", "cli_user_id", "proxy_url")})})
    readers = inspect_processes(proc_root, profiles)
    return {"passed": True, "mode": "accepted_queries_only", "active_count": len(tasks),
            "expected_task_count": len(expected), "binding_sha256": digest(sorted(bindings, key=lambda value: value["task"])),
            "local_task_ids": sorted(expected), "query_processes": readers,
            "new_generation_or_confirmation_allowed": False, "database_writes": 0}


def main():
    try:
        root = Path(os.environ.get("IJM_DATA_DIR", "/app/data"))
        expected = parse_expected(os.environ["IJM_DEPLOY_QUERY_ONLY_TASK_IDS"])
        value = check(root / "ijm2api.db", expected, root, Path("/app/app/service.py"), os.environ["IJM_DEPLOY_STARTUP_SHA256"])
    except Exception as error:
        value = {"passed": False, "blocking_count": 1, "reason": str(error) if isinstance(error, ValueError) else type(error).__name__, "database_writes": 0}
    print(json.dumps(value))


if __name__ == "__main__":
    main()
