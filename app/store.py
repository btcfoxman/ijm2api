"""Small durable SQLite store for Dreamina accounts, tasks, and settings."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .catalog import MODELS
from .account_import import masked_email, normalize_email


ACCOUNT_SECRET_FIELDS = {"login_email", "login_password", "login_user_id", "login_enable_after", "cookie", "cookies", "user_agent"}
ACCOUNT_SETUP_FIELDS = {"login_stage", "watermark_ready", "agreement_ready", "setup_checked_at"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _priority(value: Any) -> int:
    if isinstance(value, bool) or not str(value).strip().isdigit():
        raise ValueError("priority must be a non-negative integer")
    result = int(value)
    if result > 2_147_483_647:
        raise ValueError("priority must not exceed 2147483647")
    return result


class Store:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    cdp_port INTEGER NOT NULL DEFAULT 0,
                    proxy_url TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    balance REAL,
                    balance_details_json TEXT NOT NULL DEFAULT '{}',
                    last_checkin_date TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    account_id INTEGER,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT NOT NULL,
                    normalized_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '[]',
                    upstream_result_json TEXT NOT NULL DEFAULT '[]',
                    result_transfer_json TEXT NOT NULL DEFAULT '{}',
                    media_inputs_json TEXT NOT NULL DEFAULT '[]',
                    thread_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    attempted_account_ids_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    estimated_cost REAL NOT NULL DEFAULT 0,
                    actual_cost REAL,
                    balance_before REAL,
                    balance_after REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS tasks_account ON tasks(account_id, status);
                CREATE TABLE IF NOT EXISTS media_cache (
                    account_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    asset_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (account_id, kind, digest)
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            if "managed_profile" not in {row[1] for row in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN managed_profile INTEGER NOT NULL DEFAULT 0")
            if "priority" not in {row[1] for row in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            account_columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
            for name, declaration in (("login_email", "TEXT NOT NULL DEFAULT ''"),
                    ("login_password", "TEXT NOT NULL DEFAULT ''"), ("login_stage", "TEXT NOT NULL DEFAULT ''"),
                    ("watermark_ready", "INTEGER NOT NULL DEFAULT 0"), ("agreement_ready", "INTEGER NOT NULL DEFAULT 0"),
                    ("setup_checked_at", "REAL NOT NULL DEFAULT 0"), ("login_enable_after", "INTEGER DEFAULT NULL"),
                    ("login_user_id", "TEXT NOT NULL DEFAULT ''")):
                if name not in account_columns:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS accounts_login_email ON accounts(LOWER(login_email)) WHERE login_email<>''")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            for name, default in (("upstream_result_json", "[]"),
                                  ("result_transfer_json", "{}"),
                                  ("attempted_account_ids_json", "[]")):
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _account(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        email = str(value.get("login_email") or "")
        value["email_configured"] = bool(email)
        value["password_configured"] = bool(value.get("login_password"))
        value["login_identifier"] = masked_email(email)
        for key in ACCOUNT_SECRET_FIELDS:
            value.pop(key, None)
        for key in ("watermark_ready", "agreement_ready"):
            value[key] = bool(value.get(key))
        value["enabled"] = bool(value["enabled"])
        value["managed_profile"] = bool(value["managed_profile"])
        value["balance_details"] = json.loads(value.pop("balance_details_json"))
        value["last_balance"] = value["balance"]
        value["email"] = ""
        value["plan"] = "Dreamina"
        return value

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["attempted_account_ids"] = json.loads(value.pop("attempted_account_ids_json"))
        value["request"] = json.loads(value.pop("request_json"))
        value["normalized"] = json.loads(value.pop("normalized_json"))
        value["result_urls"] = json.loads(value.pop("result_json"))
        value["upstream_result_urls"] = json.loads(value.pop("upstream_result_json"))
        value["result_transfer"] = json.loads(value.pop("result_transfer_json"))
        value["media_inputs"] = json.loads(value.pop("media_inputs_json"))
        value["prompt"] = value["request"].get("prompt", "")
        value["channel"] = "ijmapi"
        value["generation_id"] = value["run_id"]
        value["caller_request"] = value["request"]
        normalized = value["normalized"]
        if normalized.get("kind") == "image":
            value["upstream_request"] = {
                "model": normalized["upstream_model"],
                "resolution": normalized["resolution"],
                "ratio": normalized["aspect_ratio"],
                "count": normalized["count"],
                "media_counts": {"images": len(normalized["images"])},
            }
        else:
            value["upstream_request"] = {"model": normalized["upstream_model"],
                "duration_sec": normalized["duration"],
                "resolution": MODELS[value["model"]].generation_resolution,
                "output_resolution": normalized["resolution"],
                "ratio": normalized["aspect_ratio"],
                "media_counts": {key: len(normalized[key])
                                 for key in ("images", "videos", "audios")}}
        value["upstream_response"] = {"run_id": value["run_id"],
            "thread_id": value["thread_id"], "status": value["status"],
            "error_code": value["error_code"], "error_message": value["error_message"]}
        return value

    def accounts(self, *, private: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [dict(row) if private else self._account(row) for row in rows]

    def account(self, account_id: int, *, private: bool = False) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return (dict(row) if private else self._account(row)) if row else None

    def upsert_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("account name is required")
        max_concurrency = int(payload.get("max_concurrency") or 1)
        if not 1 <= max_concurrency <= 50:
            raise ValueError("max_concurrency must be between 1 and 50")
        priority = _priority(payload["priority"]) if "priority" in payload else None
        credentials = self._credential_values(payload)
        now = time.time()
        with self._lock, self._connect() as db:
            old = db.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
            if old:
                credentials = self._credential_values(payload, old)
                self._assert_credentials_idle(db, old, credentials)
                if old["managed_profile"] and str(payload.get("proxy_url", old["proxy_url"])) != old["proxy_url"]:
                    raise ValueError("online account proxy change requires profile reset")
                values = {"proxy_url": payload.get("proxy_url", old["proxy_url"]),
                          "project_id": str(payload.get("project_id", old["project_id"])),
                          "cdp_port": int(payload.get("cdp_port") or old["cdp_port"]),
                          "max_concurrency": max_concurrency,
                          "priority": old["priority"] if priority is None else priority,
                          "enabled": int(bool(payload.get("enabled", old["enabled"]))) }
                db.execute("""UPDATE accounts SET proxy_url=:proxy_url,project_id=:project_id,
                    cdp_port=:cdp_port,max_concurrency=:max_concurrency,priority=:priority,
                    enabled=:enabled,updated_at=:updated_at WHERE id=:id""",
                    {**values, "updated_at": now, "id": old["id"]})
                account_id = int(old["id"])
            else:
                cursor = db.execute("""INSERT INTO accounts(name,cdp_port,proxy_url,project_id,
                    max_concurrency,priority,enabled,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (name, int(payload.get("cdp_port") or 0), str(payload.get("proxy_url") or ""),
                     str(payload.get("project_id") or ""),
                     max_concurrency, priority if priority is not None else 0,
                     int(bool(payload.get("enabled", True))), now))
                account_id = int(cursor.lastrowid)
                if credentials:
                    db.execute("UPDATE accounts SET enabled=0,status='pending',managed_profile=1 WHERE id=?", (account_id,))
            if credentials:
                db.execute("UPDATE accounts SET " + ",".join(f"{key}=?" for key in credentials) + " WHERE id=?",
                           (*credentials.values(), account_id))
                if old and any(old[key] != value for key, value in credentials.items()):
                    status = "disabled" if not old["enabled"] and old["status"] == "disabled" else "pending"
                    db.execute("UPDATE accounts SET enabled=0,status=?,managed_profile=1,login_stage='',"
                               "watermark_ready=0,agreement_ready=0,setup_checked_at=0,login_enable_after=NULL,login_user_id='' WHERE id=?", (status, account_id))
        return self.account(account_id) or {}

    @staticmethod
    def _credential_values(payload: dict[str, Any], old: sqlite3.Row | dict[str, Any] | None = None) -> dict[str, str]:
        values = {}
        if payload.get("login_email"):
            values["login_email"] = normalize_email(payload["login_email"])
            if old and old["login_email"] and values["login_email"] != old["login_email"]:
                raise ValueError("login identity change requires a separate account")
        if payload.get("login_password"):
            password = str(payload["login_password"])
            if "\x00" in password:
                raise ValueError("password contains an invalid character")
            values["login_password"] = password
        return values

    @staticmethod
    def _assert_credentials_idle(db: sqlite3.Connection, old: sqlite3.Row, values: dict[str, Any]) -> None:
        if not any(old[key] != value for key, value in values.items()):
            return
        busy = db.execute("SELECT COUNT(*) FROM tasks WHERE account_id=? AND status IN ('preparing','submitted','running','submission_unknown')",
                          (old["id"],)).fetchone()[0]
        if busy or old["status"] in {"active", "login_pending", "logging_in", "profile_resetting"}:
            raise ValueError("account identity is protected while active or in use")
        if old["cdp_port"] and not old["managed_profile"]:
            raise ValueError("external profile requires an explicit profile reset before credential import")

    def find_import_account(self, email: str, name: str) -> dict[str, Any] | None:
        with self._connect() as db:
            if email:
                rows = db.execute("SELECT * FROM accounts WHERE LOWER(login_email)=? OR (login_email='' AND LOWER(name)=?)",
                                  (email.casefold(), email.casefold())).fetchall()
                if len(rows) > 1:
                    raise ValueError("multiple existing accounts match the login identity")
                row = rows[0] if rows else None
            else:
                row = db.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        return dict(row) if row is not None else None

    def import_account(self, payload: dict[str, Any], *, busy: bool = False) -> tuple[dict[str, Any], bool]:
        """Called under the service login lock; retain existing profiles and proxies."""
        with self._lock:
            existing = self.find_import_account(payload.get("login_email", ""), payload["name"])
            if existing:
                credentials = self._credential_values(payload, existing)
                changed = any(existing[key] != value for key, value in credentials.items())
                if existing["proxy_url"] and payload["proxy_url"] != existing["proxy_url"]:
                    raise ValueError("existing proxy requires an explicit profile reset")
                proxy_changed = payload["proxy_url"] != existing["proxy_url"]
                if proxy_changed and (existing["cdp_port"] or existing["managed_profile"]):
                    raise ValueError("existing profile proxy cannot be replaced")
                if not changed and not proxy_changed:
                    return self.account(existing["id"]) or {}, False
                if busy or self.active_account_count(existing["id"]) or existing["status"] in {
                        "active", "logging_in", "login_pending", "profile_resetting"}:
                    raise ValueError("account is active or in use")
                values = {**credentials, "proxy_url": payload["proxy_url"]}
                if credentials:
                    status = "disabled" if not existing["enabled"] and existing["status"] == "disabled" else "pending"
                    values.update(enabled=False, managed_profile=True, status=status, login_stage="",
                                  watermark_ready=False, agreement_ready=False, setup_checked_at=0)
                return self.update_account(existing["id"], values), True
            with self._connect() as db:
                collision = db.execute("SELECT id FROM accounts WHERE name=?", (payload["name"],)).fetchone()
            if collision:
                raise ValueError("account name belongs to a different login identity")
            account = self.upsert_account({key: value for key, value in payload.items()
                if key in {"name", "proxy_url", "login_email", "login_password"}})
            return account, True

    def create_pending_account(self, name: str, proxy_url: str,
                               max_concurrency: int = 1) -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("account name is required")
        if not proxy_url.strip():
            raise ValueError("account proxy is required")
        if not 1 <= max_concurrency <= 50:
            raise ValueError("max_concurrency must be between 1 and 50")
        with self._lock, self._connect() as db:
            try:
                cursor = db.execute("""INSERT INTO accounts
                    (name,proxy_url,max_concurrency,managed_profile,enabled,status,updated_at)
                    VALUES(?,?,?,1,0,'pending',?)""",
                    (name, proxy_url.strip(), max_concurrency, time.time()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("account name already exists") from exc
        return self.account(int(cursor.lastrowid)) or {}

    def active_account_count(self, account_id: int) -> int:
        with self._connect() as db:
            return int(db.execute("""SELECT COUNT(*) FROM tasks WHERE account_id=?
                AND status IN ('preparing','submitted','running','submission_unknown')""", (account_id,)).fetchone()[0])

    def scheduling_rows(self) -> dict[str, list[dict[str, Any]]]:
        """Return bounded raw history and every bound unresolved credit hold."""
        columns = ("id,account_id,model,status,normalized_json,request_json,estimated_cost,actual_cost,"
                   "balance_before,balance_after,created_at,completed_at,updated_at")
        with self._connect() as db:
            history = db.execute(f"""SELECT {columns} FROM tasks
                WHERE status='succeeded' AND actual_cost IS NOT NULL
                ORDER BY COALESCE(completed_at,updated_at) DESC,updated_at DESC LIMIT 1000""").fetchall()
            inflight = db.execute(f"""SELECT {columns} FROM tasks WHERE account_id IS NOT NULL AND
                (status NOT IN ('succeeded','failed','expired','cancelled')
                 OR (status IN ('failed','expired') AND error_code='UPSTREAM_SUBMIT_OUTCOME_UNKNOWN'))
                ORDER BY created_at,id""").fetchall()
        return {"history": [dict(row) for row in history], "inflight": [dict(row) for row in inflight]}

    def update_account(self, account_id: int, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "cdp_port", "proxy_url", "project_id", "max_concurrency", "priority", "managed_profile",
                   "enabled", "status", "balance", "last_error", "last_checkin_date", "login_user_id", "login_enable_after", *ACCOUNT_SETUP_FIELDS}
        data = {key: value for key, value in values.items() if key in allowed}
        if "max_concurrency" in data and not 1 <= int(data["max_concurrency"]) <= 50:
            raise ValueError("max_concurrency must be between 1 and 50")
        if "priority" in data:
            data["priority"] = _priority(data["priority"])
        if "balance_details" in values:
            data["balance_details_json"] = _json(values["balance_details"])
        for key in ("watermark_ready", "agreement_ready"):
            if key in data:
                if not isinstance(data[key], (bool, int)) or data[key] not in (False, True):
                    raise ValueError("setup readiness must be a boolean")
                data[key] = int(data[key])
        if "login_enable_after" in data and data["login_enable_after"] is not None:
            intent = data["login_enable_after"]
            if not isinstance(intent, (bool, int)) or intent not in (False, True):
                raise ValueError("login enable intent must be a boolean or null")
            data["login_enable_after"] = int(intent)
        if "login_user_id" in data:
            identity = data["login_user_id"]
            if (not isinstance(identity, str) or len(identity) > 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in identity)):
                raise ValueError("login user ID must be a valid string")
        if "setup_checked_at" in data:
            data["setup_checked_at"] = float(data["setup_checked_at"] or 0)
            if not math.isfinite(data["setup_checked_at"]) or data["setup_checked_at"] < 0:
                raise ValueError("invalid setup check time")
        data["updated_at"] = time.time()
        with self._lock, self._connect() as db:
            old = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if old is None:
                raise KeyError(account_id)
            credentials = self._credential_values(values, old)
            self._assert_credentials_idle(db, old, credentials)
            data.update(credentials)
            if any(old[key] != value for key, value in credentials.items()):
                status = "disabled" if not old["enabled"] and old["status"] == "disabled" else "pending"
                data.update(enabled=0, managed_profile=1, status=status, login_stage="",
                            watermark_ready=0, agreement_ready=0, setup_checked_at=0, login_enable_after=None, login_user_id="")
            cursor = db.execute("UPDATE accounts SET " + ",".join(f"{key}=?" for key in data) + " WHERE id=?",
                                (*data.values(), account_id))
            if not cursor.rowcount:
                raise KeyError(account_id)
        return self.account(account_id) or {}

    def delete_account(self, account_id: int) -> bool:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM media_cache WHERE account_id=?", (account_id,))
            return bool(db.execute("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount)

    def media_cache_get(self, account_id: int, kind: str, digest: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("""SELECT asset_json FROM media_cache
                WHERE account_id=? AND kind=? AND digest=?""", (account_id, kind, digest)).fetchone()
        return json.loads(row["asset_json"]) if row else None

    def media_cache_put(self, account_id: int, kind: str, digest: str,
                        uploaded: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO media_cache(account_id,kind,digest,asset_json,created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(account_id,kind,digest) DO UPDATE SET
                asset_json=excluded.asset_json,created_at=excluded.created_at""",
                (account_id, kind, digest, _json(uploaded), time.time()))

    def media_cache_delete(self, account_id: int, kind: str, digest: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM media_cache WHERE account_id=? AND kind=? AND digest=?",
                       (account_id, kind, digest))

    def clear_media_cache(self, account_id: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM media_cache WHERE account_id=?", (account_id,))

    def create_task(self, task_id: str, request: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        media = [{"kind": kind[:-1] if kind != "images" else "image", "label": index + 1, "url": source}
                 for kind in ("images", "videos", "audios")
                 for index, source in enumerate(normalized[kind])]
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO tasks(id,model,status,request_json,normalized_json,media_inputs_json,
                        created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                       (task_id, normalized["model"], "queued", _json(request), _json(normalized),
                        _json(media), now, now))
        return self.task(task_id) or {}

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._task(row) for row in rows]

    def update_task(self, task_id: str, values: dict[str, Any]) -> dict[str, Any]:
        data = {key: value for key, value in values.items() if key in {"account_id", "status", "progress",
            "thread_id", "run_id", "error_code", "error_message", "estimated_cost", "actual_cost",
            "balance_before", "balance_after", "completed_at"}}
        if "result_urls" in values:
            data["result_json"] = _json(values["result_urls"])
        if "upstream_result_urls" in values:
            data["upstream_result_json"] = _json(values["upstream_result_urls"])
        if "result_transfer" in values:
            data["result_transfer_json"] = _json(values["result_transfer"])
        if "attempted_account_ids" in values:
            data["attempted_account_ids_json"] = _json(values["attempted_account_ids"])
        if "cost_evidence" in values:
            current = self.task(task_id) or {}
            normalized = dict(current.get("normalized") or {})
            normalized["_cost_evidence"] = {
                **normalized.get("_cost_evidence", {}), **values["cost_evidence"]}
            data["normalized_json"] = _json(normalized)
        data["updated_at"] = time.time()
        with self._lock, self._connect() as db:
            db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in data) + " WHERE id=?",
                       (*data.values(), task_id))
        return self.task(task_id) or {}

    def active_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','preparing','submitted','running')").fetchone()[0])

    def has_account_overlap(self, account_id: int, task_id: str,
                            started_at: float, finished_at: float) -> bool:
        with self._connect() as db:
            return bool(db.execute("""SELECT 1 FROM tasks
                WHERE account_id=? AND id<>? AND created_at<?
                  AND COALESCE(completed_at, 9999999999)>? LIMIT 1""",
                (account_id, task_id, finished_at, started_at)).fetchone())

    def clear_terminal_tasks(self) -> int:
        with self._lock, self._connect() as db:
            return db.execute("DELETE FROM tasks WHERE status IN ('succeeded','failed','expired')").rowcount

    def model_costs(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""SELECT t.id,t.model,t.normalized_json,t.actual_cost,t.updated_at
                FROM tasks AS t WHERE t.status='succeeded' AND t.actual_cost IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM tasks AS peer
                    WHERE peer.account_id=t.account_id AND peer.id<>t.id
                      AND peer.created_at<t.completed_at
                      AND COALESCE(peer.completed_at, 9999999999)>t.created_at)
                ORDER BY t.updated_at DESC LIMIT 1000""").fetchall()
        grouped: dict[tuple[str, int, str, int, int, int], dict[str, Any]] = {}
        for row in rows:
            request = json.loads(row["normalized_json"])
            if request.get("kind") == "image":
                continue
            key = (row["model"], int(request["duration"]), request["resolution"],
                   len(request["images"]), len(request["videos"]), len(request["audios"]))
            item = grouped.setdefault(key, {"model": key[0], "duration": key[1],
                "resolution": key[2], "image_count": key[3], "video_count": key[4],
                "audio_count": key[5], "cost": 0, "latest_cost": float(row["actual_cost"]),
                "samples": 0, "updated_at": row["updated_at"], "last_task_id": row["id"],
                "source": "observed"})
            item["cost"] += float(row["actual_cost"])
            item["samples"] += 1
        for item in grouped.values():
            item["cost"] = round(item["cost"] / item["samples"], 2)
        return list(grouped.values())

    def get_settings(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT key,value FROM settings").fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except ValueError:
                result[row["key"]] = row["value"]
        return result

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           [(key, _json(value)) for key, value in values.items()])
        return self.get_settings()
