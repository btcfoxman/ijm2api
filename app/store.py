"""Small durable SQLite store for Dreamina accounts, tasks, and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .catalog import MODELS


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
        value["upstream_request"] = {"model": value["normalized"]["upstream_model"],
            "duration_sec": value["normalized"]["duration"],
            "resolution": MODELS[value["model"]].generation_resolution,
            "output_resolution": value["normalized"]["resolution"],
            "ratio": value["normalized"]["aspect_ratio"],
            "media_counts": {key: len(value["normalized"][key])
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
        now = time.time()
        with self._lock, self._connect() as db:
            old = db.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
            if old:
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
        return self.account(account_id) or {}

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
                AND status IN ('preparing','submitted','running')""", (account_id,)).fetchone()[0])

    def update_account(self, account_id: int, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "cdp_port", "proxy_url", "project_id", "max_concurrency", "priority", "managed_profile",
                   "enabled", "status", "balance", "last_error", "last_checkin_date"}
        data = {key: value for key, value in values.items() if key in allowed}
        if "max_concurrency" in data and not 1 <= int(data["max_concurrency"]) <= 50:
            raise ValueError("max_concurrency must be between 1 and 50")
        if "priority" in data:
            data["priority"] = _priority(data["priority"])
        if "balance_details" in values:
            data["balance_details_json"] = _json(values["balance_details"])
        data["updated_at"] = time.time()
        with self._lock, self._connect() as db:
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
