"""Video cost observations and advisory comparison. Vendored into independent services.

This module never submits generations or writes routing/account-budget settings.
Only explicit adapter evidence is accepted; no recursive guessing of numeric fields.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

LOG = logging.getLogger("cost_reference")
ACTUAL_SOURCES = {"actual", "isolated_balance"}
CONTEXT_KEYS = {"workflow", "aspect_ratio", "generate_audio", "image_count", "video_count",
                "audio_count", "frame_count", "billing_mode", "plan", "quality", "batch_size",
                "account_scoped", "input_mode", "requested_resolution", "recipe", "advanced_mode",
                "generation_type", "audio", "generateAudio", "n", "workflow_id", "fixed_reference_video"}
FAILED = {"failed", "error", "cancelled", "canceled", "expired", "nsfw", "rejected"}


def workbook_model(label: str) -> tuple[str, str, list[str]] | None:
    """Explicit aliases for the user's SD worksheet; no fuzzy provider/model guesses."""
    values = {
        "sd2.0-480p": ("doubao-seedance-2-0-260128-480p", "480p", "seedance-2.0"),
        "sd2.0-fast-480p": ("doubao-seedance-2-0-fast-260128-480p", "480p", "seedance-2.0-fast"),
        "sd2.0-mini-480p": ("doubao-seedance-2-0-mini-260615-480p", "480p", "seedance-2.0-mini"),
        "sd2.0-fast": ("doubao-seedance-2-0-fast-260128", "720p", "seedance-2.0-fast"),
        "sd2.0-mini": ("doubao-seedance-2-0-mini-260615", "720p", "seedance-2.0-mini"),
        "sd2.0": ("doubao-seedance-2-0-260128", "720p", "seedance-2.0"),
        "sd2.0-1080p": ("doubao-seedance-2-0-260128-1080p", "1080p", "seedance-2.0"),
        "sd2.0-4k": ("doubao-seedance-2-0-260128-4k", "4k", "seedance-2.0"),
        "sd2.5-480p": ("sd-2-5-480p", "480p", "seedance-2.5"),
        "sd2.5-720p": ("sd-2-5", "720p", "seedance-2.5"),
        "sd2.5-1080p": ("sd-2-5-1080p", "1080p", "seedance-2.5"),
        "minimax-h3-768p": ("minimax-h3-768p", "768p", "minimax-h3-768p"),
        "minimax-h3-2k": ("minimax-h3", "2k", "minimax-h3"),
        "wan-3.0-480p": ("wan-3.0-480p", "480p", "wan-3.0-480p"),
        "wan-3.0-720p": ("wan-3.0", "720p", "wan-3.0"),
        "wan-3.0-1080p": ("wan-3.0-1080p", "1080p", "wan-3.0-1080p"),
        "kling-4.0": ("kling-4.0", "720p", "kling-4.0"),
        "kling-4.0-1080p": ("kling-4.0-1080p", "1080p", "kling-4.0-1080p"),
    }
    value = values.get(label)
    return (value[0], value[1], list({label, value[0], value[2]})) if value else None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def number(value: Any, *, optional: bool = True) -> Decimal | None:
    if value is None or value == "":
        return None if optional else Decimal(0)
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid decimal amount") from None
    if not result.is_finite() or result < 0:
        raise ValueError("amount must be finite and nonnegative")
    return result


def amount(value: Any) -> str | None:
    value = number(value)
    return format(value.normalize(), "f") if value is not None else None


def resolution(value: Any) -> str:
    raw = str(value or "unknown").strip().lower().replace("×", "x")
    return {"standard": "480p", "hd": "720p", "full_hd": "1080p", "full-hd": "1080p",
            "2k": "2k", "4k": "4k"}.get(raw, raw)


def decode_task(raw: dict[str, Any]) -> dict[str, Any]:
    task = dict(raw)
    for key, value in list(task.items()):
        if isinstance(value, str) and (key.endswith("_json") or key in {"raw_status", "usage", "upstream_response", "upstream_request", "request", "profile", "result"}):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
            task[key] = value
        if key.endswith("_json"):
            task.setdefault(key[:-5], value)
    return task


def normalize(observation: dict[str, Any], instance: str, service: str) -> dict[str, Any]:
    allowed = {"task_id", "attempt_id", "channel", "execution_source", "model", "upstream_model",
               "duration", "requested_duration", "reference_seconds", "requested_reference_seconds",
               "billable_reference_seconds", "resolution", "unit", "charged", "refunded", "quoted",
               "source", "status", "observed_at", "upstream_task_id", "parent_task_id", "components",
               "source_instance_id", "billing_instance_id", "observation_id", "settlement_state", "service", "upstream_instance_id",
               "attempt_order", "is_current"}
    out = {k: v for k, v in observation.items() if k in allowed}
    for key in ("task_id", "attempt_id", "channel", "execution_source", "model", "upstream_model", "unit", "source", "status"):
        out[key] = str(out.get(key) or ("generation" if key == "attempt_id" else "unknown"))[:256]
    if out["task_id"] == "unknown":
        raise ValueError("observation requires task_id")
    out["source_instance_id"] = str(out.get("source_instance_id") or instance)
    out["billing_instance_id"] = str(out.get("billing_instance_id") or out["source_instance_id"])
    out["service"] = str(out.get("service") or service)
    out["observation_id"] = str(out.get("observation_id") or hashlib.sha256(
        dumps([out["source_instance_id"], out["task_id"], out["attempt_id"]]).encode()).hexdigest())
    out["resolution"] = resolution(out.get("resolution"))
    for key in ("duration", "requested_duration", "reference_seconds", "requested_reference_seconds",
                "billable_reference_seconds", "charged", "refunded", "quoted"):
        out[key] = amount(out.get(key))
    charged, refunded = number(out["charged"]), number(out["refunded"], optional=False)
    out["net_usage"] = amount(max(charged - refunded, Decimal(0))) if charged is not None else None
    # An upstream quote in an unfortunately named actual_cost field is never evidence.
    if out["source"] not in ACTUAL_SOURCES:
        out["reference_kind"] = "estimate" if out["quoted"] is not None else "unknown"
    else:
        out["reference_kind"] = "actual" if charged is not None else "unknown"
    out["reference_eligible"] = not (out["status"].lower() in FAILED and charged is not None and
                                     refunded >= charged and (refunded > 0 or charged == 0))
    if out.get("settlement_state") == "incomplete":
        out["reference_eligible"] = False
    context = observation.get("billing_context") or {}
    out["billing_context"] = {k: v for k, v in context.items() if k in CONTEXT_KEYS and v is not None and isinstance(v, (str, int, float, bool))}
    # Adapter timestamps must describe evidence changes, not collection time.
    out["observed_at"] = str(out.get("observed_at") or "0")
    out["source_label"] = SOURCE_LABELS.get(out["source"], "未知")
    if isinstance(out.get("components"), list):
        out["components"] = [normalize(item, out["source_instance_id"], service) for item in out["components"][:100]]
    return out


SOURCE_LABELS = {"actual": "实扣", "isolated_balance": "实扣", "quote": "预扣",
                 "submission_reported": "预扣", "formula": "公式推", "spreadsheet": "表格推", "unknown": "未知"}
PROVIDER_ALIASES = {"sdhapi": "higg2api", "sakapi": "ak2api", "magapi": "mag2api", "draapi": "dra2api",
                    "polapi": "pol2api", "fbapi": "freebeat2api", "picapi": "pic2api", "nimapi": "nim2api",
                    "ijmapi": "ijm2api", "pipapi": "pip2api", "nadapi": "nad2api", "eleapi": "ele2api", "cliapi": "helper", "kreapi": "kre2api",
                    "jdcapi": "jdc2api", "hgapi": "hg2api", "miapi": "mi2api", "tapapi": "tap2api"}
OWNED_UPSTREAMS = {"higg2api", "ak2api", "mag2api", "dra2api", "pol2api", "freebeat2api", "pic2api",
                   "nim2api", "ijm2api", "pip2api", "helper", "kre2api", "jdc2api", "hg2api", "mi2api",
                   "tap2api", "art2api", "kliapi", "nad2api", "ele2api"}


def display_amount(value):
    parsed = number(value)
    return format(parsed, ".2f").rstrip("0").rstrip(".") if parsed is not None else "-"


def provider_identity(value):
    value = str(value or "").lower()
    return PROVIDER_ALIASES.get(value, value)


def table_baseline(candidate, baselines):
    provider = provider_identity(candidate.get("channel") or candidate.get("service"))
    names = {str(candidate.get(k) or "").lower() for k in ("model", "upstream_model")}
    for row in baselines or []:
        if provider not in {provider_identity(row.get("channel")), provider_identity(row.get("service"))}:
            continue
        if row.get("connection") and candidate.get("connection") and row["connection"] != candidate["connection"]:
            continue
        aliases = {str(v).lower() for v in [row.get("model"), row.get("raw_label"), *(row.get("model_aliases") or [])] if v}
        if names & aliases and resolution(row.get("resolution")) == resolution(candidate.get("resolution")):
            return row
    return None


def table_estimate(candidate, baseline):
    if not baseline:
        return None, "unknown"
    usage, duration = number(baseline.get("usage_15", baseline.get("usage"))), number(candidate.get("duration"))
    reference = number(candidate.get("reference_seconds"))
    if usage is None or duration is None or reference is None:
        return None, "unknown"
    if duration != 15 and baseline.get("scale_duration") is not True:
        return None, "unknown"
    result = usage * duration / Decimal(15)
    if reference:
        rate = number(baseline.get("reference_rate"))
        if rate is None:
            return None, "unknown"
        result += rate * reference
    return result, "spreadsheet"


def credit_price(candidate, prices, baseline=None):
    """Only a compatible credit-pool purchase can price a task's consumption."""
    unit = str(candidate.get("unit") or "")
    if unit in {"", "unknown", "mixed"}:
        return None, None
    # A maintained provider pool owns its selector even when its amount was
    # explicitly cleared. Never resurrect a legacy price behind that tombstone.
    for price in sorted(prices, key=lambda p: int(p.get("matrix_revision") or 0), reverse=True):
        if price.get("origin") != "matrix" or price.get("mode", "credits") != "credits":
            continue
        selector = dict(price)
        if provider_identity(selector.get("channel")) == provider_identity(candidate.get("channel")):
            selector["channel"] = candidate.get("channel")
        if price.get("currency", "CNY").upper() != "CNY" or str(price.get("unit") or "credits") != unit or not matches_price(selector, candidate):
            continue
        paid, purchased = number(price.get("purchase_amount")), number(price.get("purchase_units"))
        return (paid / purchased if paid is not None and purchased else None), price
    if baseline and baseline.get("purchase_units") is not None:
        pool = baseline
        # Table quantities are explicitly denominated in credits. A different
        # known unit requires its own price, never a silent currency conversion.
        if str(pool.get("unit") or "credits") == unit:
            paid, purchased = number(pool.get("purchase_amount")), number(pool.get("purchase_units"))
            if paid is not None and purchased and pool.get("currency", "CNY").upper() == "CNY":
                return paid / purchased, pool
    if baseline and baseline.get("matrix_managed"):
        return None, None
    compatible = []
    for price in prices:
        if price.get("mode", "credits") != "credits" or price.get("currency", "CNY").upper() != "CNY":
            continue
        if str(price.get("unit") or "credits") != unit:
            continue
        selector = dict(price)
        if provider_identity(selector.get("channel")) == provider_identity(candidate.get("channel")):
            selector["channel"] = candidate.get("channel")
        if not matches_price(selector, candidate, [baseline] if baseline else []):
            continue
        paid, purchased = number(price.get("purchase_amount")), number(price.get("purchase_units"))
        if paid is not None and purchased:
            compatible.append(price)
    compatible.sort(key=lambda r: ({"workbook": 0, "purchase_default": 1}.get(r.get("origin"), 2), specificity(r)), reverse=True)
    if compatible:
        pool = compatible[0]
        return number(pool["purchase_amount"]) / number(pool["purchase_units"]), pool
    return None, None


def final_observation(observations):
    """Select one attempt before looking at any amounts; an unknown last stays last."""
    if not observations:
        return None
    indexed = list(enumerate(observations))
    current = [(i, row) for i, row in indexed if row.get("is_current") is True]
    if current:
        indexed = current
    ordered = [(i, row) for i, row in indexed if isinstance(row.get("attempt_order"), (int, float)) and not isinstance(row.get("attempt_order"), bool)]
    if ordered and len(ordered) == len(indexed):
        return max(ordered, key=lambda pair: (pair[1]["attempt_order"], pair[0]))[1]
    return indexed[-1][1]


def project_task_cost(observations, prices=(), baselines=(), *, instance="local", service_name="", connections=None,
                      rules=(), money_enabled=True):
    """Privacy-safe UI projection, separate from the complete spending ledger."""
    rows = [normalize(row, instance, service_name) for row in observations]
    selected = final_observation(rows)
    # Nested hubs preserve all billed attempts in components for accounting.
    # The compact task column intentionally follows only the final leaf.
    while selected and selected.get("components"):
        children = selected["components"]
        if not any("attempt_order" in child or child.get("is_current") for child in children):
            children = sorted(children, key=evidence_time)
        selected = final_observation(children)
    usage, source, money, pool = None, "unknown", None, None
    baseline = table_baseline(selected or {}, baselines)
    candidate = dict(selected or {})
    mapping = connections or {}
    candidate["connection"] = mapping.get(candidate.get("channel"), next(
        (value for key, value in mapping.items() if provider_identity(key) == provider_identity(candidate.get("channel"))), "local"))
    if selected:
        source = selected.get("source", "unknown")
        if source in ACTUAL_SOURCES:
            usage = number(selected.get("net_usage"))
        elif source in {"quote", "submission_reported", "formula", "spreadsheet"}:
            usage = number(selected.get("quoted"))
            if usage is None and source == "submission_reported":
                usage = number(selected.get("charged"))
        inference = None
        if usage is None and rules:
            # Infer only this final attempt's dimensions. Other tasks and prior
            # attempts are deliberately not passed as historical debit evidence.
            inference = compare_candidate(candidate, [], [], list(rules), list(baselines))
            usage = number(inference.get("usage"))
            if usage is not None:
                source = "spreadsheet" if inference.get("source") == "manual" else inference.get("source", "unknown")
                candidate["unit"] = inference.get("unit") or candidate.get("unit", "credits")
        if usage is None:
            usage, source = table_estimate(candidate, baseline)
            if usage is not None:
                candidate["unit"] = str(baseline.get("unit") or "credits")
        if usage is None:
            source = "unknown"
        if money_enabled:
            rate, pool = credit_price(candidate, prices, baseline)
            if usage is not None and rate is not None:
                money = usage * rate
    result = {"usage": amount(usage), "usage_display": display_amount(usage), "money_enabled": bool(money_enabled), "source": source,
            "source_label": SOURCE_LABELS.get(source, "未知"), "unit": candidate.get("unit", "unknown"),
            "channel": candidate.get("channel", ""), "execution_source": candidate.get("execution_source", ""),
            "selected_observation": selected,
            "details": {"observations": rows, "advisory_only": True}}
    if money_enabled:
        result.update(cost=amount(money), cost_display=display_amount(money), currency="CNY")
        result["details"].update(baseline=baseline, purchase=pool)
        if selected and inference:
            result["details"]["inference_source"] = inference.get("source", "unknown")
    return result


class CostStore:
    def __init__(self, path: str | Path, service: str):
        self.path, self.service = str(path), service
        self.lock = threading.RLock()
        self.ready = False

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self.lock, self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS costref_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS costref_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS costref_observations(id TEXT PRIMARY KEY,body TEXT NOT NULL,local INTEGER NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS costref_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS costref_config(kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS costref_peers(channel TEXT PRIMARY KEY,connection TEXT NOT NULL DEFAULT '',instance TEXT NOT NULL DEFAULT '',cursor INTEGER NOT NULL DEFAULT 0,last_ok REAL,error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS costref_bindings(channel TEXT NOT NULL,connection TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(channel,connection,id));
                CREATE TABLE IF NOT EXISTS costref_media_cache(digest TEXT PRIMARY KEY,seconds TEXT,checked REAL NOT NULL,error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS costref_media_jobs(id TEXT PRIMARY KEY,body TEXT NOT NULL,urls TEXT NOT NULL,next_try REAL NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS costref_balance_windows(task_id TEXT PRIMARY KEY,account TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS costref_failed_events(seq INTEGER PRIMARY KEY,task_json TEXT NOT NULL,error TEXT NOT NULL,failed_at REAL NOT NULL);
            """)
            event_columns = {r[1] for r in db.execute("PRAGMA table_info(costref_events)")}
            for name, definition in (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("retry_at", "REAL NOT NULL DEFAULT 0")):
                if name not in event_columns:
                    db.execute(f"ALTER TABLE costref_events ADD COLUMN {name} {definition}")
            db.execute("INSERT OR IGNORE INTO costref_meta VALUES('instance',?)", (str(uuid.uuid4()),))
            self.instance = db.execute("SELECT value FROM costref_meta WHERE key='instance'").fetchone()[0]
            columns = [r[1] for r in db.execute("PRAGMA table_info(tasks)")]
            if columns:
                # Durable snapshots also capture a deletion before the next 30s collection.
                def snapshot(prefix):
                    # Python 3.12 SQLite limits a function to 127 arguments.
                    chunks = [columns[i:i + 40] for i in range(0, len(columns), 40)]
                    objects = ["json_object(" + ",".join("'" + c + "'," + prefix + '."' + c + '"' for c in chunk) + ")" for chunk in chunks]
                    result = objects[0]
                    for extra in objects[1:]:
                        result = f"json_patch({result},{extra})"
                    return result
                for event, prefix in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                    db.execute(f'DROP TRIGGER IF EXISTS costref_task_{event.lower()}')
                    payload = snapshot(prefix) if event == "DELETE" else f"json_object('_costref_live',1,'id',{prefix}.id)"
                    db.execute(f'CREATE TRIGGER costref_task_{event.lower()} AFTER {event} ON tasks BEGIN INSERT INTO costref_events(task_json) VALUES({payload}); END')
                if not db.execute("SELECT 1 FROM costref_meta WHERE key='backfilled'").fetchone():
                    db.execute("INSERT INTO costref_events(task_json) SELECT json_object('_costref_live',1,'id',id) FROM tasks")
                    db.execute("INSERT INTO costref_meta VALUES('backfilled','1')")
            self.ready = True

    def put_observation(self, observation: dict[str, Any], *, local: bool = True, binding: tuple[str, str] | None = None):
        item = normalize(observation, self.instance, self.service)
        with self.lock, self.db() as db:
            old = db.execute("SELECT body,local FROM costref_observations WHERE id=?", (item["observation_id"],)).fetchone()
            if old:
                previous = json.loads(old[0])
                # A later poll with missing audit fields must not erase confirmed evidence.
                if not item.get("components"):
                    for key in ("charged", "quoted", "duration", "reference_seconds", "billable_reference_seconds"):
                        if item.get(key) is None and previous.get(key) is not None:
                            item[key] = previous[key]
                    if number(previous.get("refunded"), optional=False) > number(item.get("refunded"), optional=False):
                        item["refunded"] = previous["refunded"]
                    if previous.get("source") in ACTUAL_SOURCES and item.get("source") not in ACTUAL_SOURCES:
                        item["source"] = previous["source"]
                if previous.get("components") and not item.get("components"):
                    for key in ("components", "charged", "refunded", "unit", "source", "execution_source", "settlement_state", "upstream_instance_id", "billing_instance_id"):
                        if key in previous:
                            item[key] = previous[key]
                item = normalize(item, self.instance, self.service)
            body = dumps(item)
            if not old or old[0] != body:
                db.execute("INSERT INTO costref_observations VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,local=MAX(local,excluded.local),updated=excluded.updated",
                           (item["observation_id"], body, int(local), time.time()))
                if local:
                    db.execute("INSERT INTO costref_changes(id,body) VALUES(?,?)", (item["observation_id"], body))
            if binding:
                db.execute("INSERT OR IGNORE INTO costref_bindings VALUES(?,?,?)", (*binding, item["observation_id"]))
        return item

    def collect(self, adapter: Callable, limit: int = 500) -> int:
        with self.lock, self.db() as db:
            events = list(db.execute("SELECT seq,task_json,attempts FROM costref_events WHERE retry_at<=? ORDER BY seq LIMIT ?", (time.time(), limit)))
        done = []
        for event in events:
            try:
                raw = json.loads(event["task_json"])
                if raw.get("_costref_live"):
                    with self.db() as db:
                        live = db.execute("SELECT * FROM tasks WHERE id=?", (raw["id"],)).fetchone()
                    if live is None:
                        done.append(event["seq"])
                        continue  # A durable DELETE snapshot follows this event.
                    raw = dict(live)
                task = decode_task(raw)
                self.balance_isolation(task)
                for item in adapter(task) or []:
                    private_urls = item.get("_reference_urls") or []
                    urls = [url for url in private_urls if isinstance(url, str) and urlsplit(url).scheme in {"https", "http"}]
                    if item.get("reference_seconds") is None and urls and len(urls) == len(private_urls):
                        cached = self.cached_seconds(urls)
                        if cached is not None:
                            item["reference_seconds"] = cached
                    saved = self.put_observation(item)
                    if saved["reference_seconds"] is None and urls and len(urls) == len(private_urls):
                        with self.lock, self.db() as db:
                            db.execute("INSERT INTO costref_media_jobs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,urls=excluded.urls",
                                       (saved["observation_id"], dumps(saved), dumps(urls), 0, time.time() + 86400))
                done.append(event["seq"])
            except Exception as exc:
                LOG.exception("Cost adapter failed for %s event %s", self.service, event["seq"])
                with self.lock, self.db() as db:
                    if event["attempts"] >= 2:
                        db.execute("INSERT OR REPLACE INTO costref_failed_events VALUES(?,?,?,?)", (event["seq"], event["task_json"], type(exc).__name__, time.time()))
                        done.append(event["seq"])
                    else:
                        db.execute("UPDATE costref_events SET attempts=attempts+1,retry_at=? WHERE seq=?", (time.time() + 30, event["seq"]))
        if done:
            with self.lock, self.db() as db:
                db.executemany("DELETE FROM costref_events WHERE seq=?", [(v,) for v in done])
        return len(done)

    def retry_failures(self):
        with self.lock, self.db() as db:
            count = db.execute("SELECT COUNT(*) FROM costref_failed_events").fetchone()[0]
            db.execute("INSERT INTO costref_events(task_json) SELECT task_json FROM costref_failed_events ORDER BY seq")
            db.execute("DELETE FROM costref_failed_events")
        return count

    def seed_workbook(self, snapshot: dict, connections: dict[str, str] | None = None):
        source = snapshot.get("source") or {}
        marker = "workbook:" + str(source.get("sha256") or "")
        with self.lock, self.db() as db:
            if db.execute("SELECT 1 FROM costref_meta WHERE key=?", (marker,)).fetchone():
                return
            # A revised workbook replaces only its previous defaults. User-edited
            # prices no longer carry origin=workbook and remain authoritative.
            db.execute("DELETE FROM costref_config WHERE kind IN ('baselines','prices') AND json_extract(body,'$.origin')='workbook'")
            for raw in snapshot.get("records", []):
                row = dict(raw)
                mapping = workbook_model(str(row.get("raw_label") or ""))
                if mapping:
                    row["model"], row["resolution"], row["model_aliases"] = mapping
                else:
                    row.update(model=str(row.get("raw_label") or ""), resolution="unknown", model_aliases=[])
                row.update(origin="workbook", unit="credits", requested_reference_seconds="0", reference_seconds=None)
                if connections is not None and row.get("channel") in connections:
                    row["connection"] = connections[row["channel"]]
                # Zero in this particular workbook is explicitly an omission.
                for key in ("usage", "unit_price", "reported_cost", "calculated_cost"):
                    value = number(row.get(key))
                    row[key] = amount(value) if value is not None and value > 0 else None
                key = str(row.get("id") or hashlib.sha256(dumps([row.get("channel"), row.get("raw_label")]).encode()).hexdigest())
                row["workbook_source"] = source
                db.execute("INSERT OR IGNORE INTO costref_config VALUES('baselines',?,?)", (key, dumps(row)))
                if mapping and row.get("unit_price") is not None and (connections is None or row.get("channel") in connections):
                    price = dict(channel=row["channel"], model=row["model"], resolution=row["resolution"], mode="credits", unit="credits",
                                 purchase_amount=row["unit_price"], purchase_units="1", currency="CNY", origin="workbook", workbook_source=source)
                    if row.get("connection"):
                        price["connection"] = row["connection"]
                    db.execute("INSERT OR IGNORE INTO costref_config VALUES('prices',?,?)", ("workbook:" + key, dumps(price)))
            db.execute("INSERT INTO costref_meta VALUES(?,?)", (marker, str(time.time())))

    def matrix_rows(self):
        """The simple table keeps old rules/prices intact and owns only edited pools."""
        rows, prices, pools = self.config("baselines"), self.config("prices"), self.config("matrix_prices")
        result = []
        for row in rows:
            row = dict(row, usage_15=row.get("usage_15", row.get("usage")), reference_rate=row.get("reference_rate"),
                       scale_duration=row.get("scale_duration") is True)
            pool = next((p for p in sorted(pools, key=lambda p: int(p.get("matrix_revision") or 0), reverse=True)
                         if provider_identity(p.get("channel")) == provider_identity(row.get("channel"))
                         and p.get("connection", "local") == row.get("connection", "local")), None)
            if pool:
                row.update({key: pool.get(key) for key in ("purchase_amount", "purchase_units")})
                row["matrix_managed"] = True
            else:
                candidate = dict(row, reference_seconds="0", duration="15")
                rate, legacy = credit_price(candidate, prices, row)
                if legacy:
                    row.update(purchase_amount=legacy.get("purchase_amount"), purchase_units=legacy.get("purchase_units"))
                elif row.get("unit_price") is not None:
                    row.update(purchase_amount=row["unit_price"], purchase_units="1")
            paid, purchased = number(row.get("purchase_amount")), number(row.get("purchase_units"))
            unit_price = paid / purchased if paid is not None and purchased else None
            usage = number(row.get("usage_15"))
            cost = usage * unit_price if usage is not None and unit_price is not None else None
            row.update(unit_price=amount(unit_price), cost=amount(cost), currency="CNY",
                       editable=row.get("resolution") not in {None, "", "unknown"} and bool(row.get("model")))
            row["reason"] = "" if row["editable"] else "模型或分辨率尚未映射"
            for key in ("usage_15", "reference_rate", "purchase_amount", "purchase_units", "unit_price", "cost"):
                row[key + "_display"] = display_amount(row.get(key))
            result.append(row)
        return result

    def save_matrix(self, key, patch, *, connection="local", service_name=None, _db=None, _revision=None, _source=None):
        if _db is None:
            with self.lock, self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self.save_matrix(key, patch, connection=connection, service_name=service_name,
                                 _db=db, _revision=str(time.time_ns()), _source=self.instance)
            return next(r for r in self.matrix_rows() if r["id"] == key)
        if not isinstance(patch, dict) or not key or len(key) > 256:
            raise ValueError("invalid matrix row")
        allowed = {"channel", "model", "resolution", "raw_label", "usage_15", "reference_rate", "purchase_amount", "purchase_units", "scale_duration"}
        if set(patch) - allowed:
            raise ValueError("unknown matrix field")
        with self.lock, nullcontext(_db) as db:
            previous = db.execute("SELECT body FROM costref_config WHERE kind='baselines' AND id=?", (key,)).fetchone()
            row = json.loads(previous[0]) if previous else {}
            if previous and any(patch.get(field, row.get(field)) != row.get(field) for field in ("channel", "model", "resolution")):
                raise ValueError("existing row dimensions cannot be changed")
            for name in ("channel", "model", "resolution", "raw_label"):
                if name in patch:
                    if not isinstance(patch[name], str) or len(patch[name]) > 256:
                        raise ValueError("invalid matrix dimension")
                    row[name] = patch[name].strip()
            if not row.get("channel") or not row.get("model") or resolution(row.get("resolution")) in {"", "unknown"}:
                raise ValueError("channel, model and resolution are required")
            row["resolution"] = resolution(row["resolution"])
            row.setdefault("raw_label", row["model"])
            row.setdefault("model_aliases", [row["model"]])
            row.setdefault("service", service_name or provider_identity(row["channel"]))
            row.setdefault("group", row["channel"])
            row.setdefault("usage_15", row.get("usage"))
            row.setdefault("reference_rate", None)
            if "scale_duration" in patch:
                if not isinstance(patch["scale_duration"], bool):
                    raise ValueError("scale_duration must be boolean")
                row["scale_duration"] = patch["scale_duration"]
            for name in ("usage_15", "reference_rate"):
                if name in patch:
                    row[name] = amount(patch[name])
            row.update(usage=row.get("usage_15"), origin="manual", matrix_managed=True, unit="credits",
                       duration="15", requested_reference_seconds="0", connection=connection,
                       matrix_revision=_revision, matrix_source=_source)
            # The same purchase pool applies to every model of this provider.
            if "purchase_amount" in patch or "purchase_units" in patch:
                existing_pool = db.execute("SELECT body FROM costref_config WHERE kind='matrix_prices' AND id=?", (row["channel"],)).fetchone()
                current = json.loads(existing_pool[0]) if existing_pool else next(
                    (r for r in self.matrix_rows() if r["id"] == key), {})
                pool = {name: amount(patch.get(name, current.get(name))) for name in ("purchase_amount", "purchase_units")}
                if pool["purchase_units"] is not None and number(pool["purchase_units"]) <= 0:
                    raise ValueError("purchased credits must be positive")
                pool.update(channel=row["channel"], service=row["service"], connection=connection,
                            unit="credits", currency="CNY", mode="credits", origin="matrix",
                            matrix_revision=_revision, matrix_source=_source)
                db.execute("INSERT INTO costref_config VALUES('matrix_prices',?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (row["channel"], dumps(pool)))
            db.execute("INSERT INTO costref_config VALUES('baselines',?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (key, dumps(row)))
        return dict(row, id=key)

    def import_matrix(self, rows, *, service_name, source_instance="peer"):
        """Authenticated peer edits are atomic; older edits never replace newer values."""
        applied = skipped = 0
        seen = set()
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for incoming in rows:
                if not isinstance(incoming, dict):
                    raise ValueError("invalid matrix row")
                key = str(incoming.get("id") or "")
                revision = str(incoming.get("revision") or "")
                if not key or key in seen or not revision.isdigit() or len(revision) > 25:
                    raise ValueError("matrix row ID and revision are required")
                seen.add(key)
                patch = {k: v for k, v in incoming.items() if k not in {"id", "revision"}}
                if provider_identity(patch.get("channel")) != service_name:
                    raise ValueError("matrix provider mismatch")
                previous = db.execute("SELECT body FROM costref_config WHERE kind='baselines' AND id=?", (key,)).fetchone()
                if previous and int(json.loads(previous[0]).get("matrix_revision") or 0) > int(revision):
                    skipped += 1
                    continue
                pool = db.execute("SELECT body FROM costref_config WHERE kind='matrix_prices' AND id=?", (patch.get("channel"),)).fetchone()
                if pool and int(json.loads(pool[0]).get("matrix_revision") or 0) > int(revision):
                    for field in ("purchase_amount", "purchase_units"):
                        if field in patch:
                            patch.pop(field)
                            skipped += 1
                self.save_matrix(key, patch, service_name=service_name, _db=db, _revision=revision, _source=source_instance)
                applied += 1
        return {"applied": applied, "skipped": skipped}

    def seed_purchases(self, snapshot: dict, connections: dict[str, str]):
        """Install explicit credit-pool purchase defaults once per registered entry.

        Editing or deleting a price is permanent. A later image must never reset
        a user's procurement configuration to the packaged defaults.
        """
        selector_keys = ("channel", "connection", "execution_source", "source_instance_id", "billing_instance_id", "unit", "model", "upstream_model", "resolution", "duration", "reference_seconds", "billing_context")
        def selector(row):
            return {k: row[k] for k in selector_keys if row.get(k) not in (None, "", "*")}
        with self.lock, self.db() as db:
            for entry in snapshot.get("entries", []):
                channel, key = str(entry["channel"]), str(entry["id"])
                marker = "purchase_default:" + key
                if (connections.get(channel) in (None, "") or
                        (connections.get(channel) == "local" and provider_identity(channel) != self.service) or
                        db.execute("SELECT 1 FROM costref_meta WHERE key=?", (marker,)).fetchone()):
                    continue
                unit = str(entry.get("unit") or "")
                purchased, paid = number(entry.get("purchase_units")), number(entry.get("purchase_amount"))
                if unit in {"", "unknown"} or purchased is None or purchased <= 0 or paid is None or paid <= 0:
                    raise ValueError("purchase defaults require a known unit and positive amounts")
                price = dict(channel=channel, connection=connections[channel], unit=unit, mode="credits",
                             purchase_amount=amount(paid), purchase_units=amount(purchased),
                             currency=snapshot.get("currency", "CNY"), origin="purchase_default")
                existing = [json.loads(row[0]) for row in db.execute("SELECT body FROM costref_config WHERE kind='prices'")]
                # Do not introduce an equivalent selector beside an existing
                # manual price: administrators must still be able to edit it.
                if not any(selector(row) == selector(price) for row in existing):
                    db.execute("INSERT OR IGNORE INTO costref_config VALUES('prices',?,?)", (key, dumps(price)))
                db.execute("INSERT INTO costref_meta VALUES(?,?)", (marker, str(time.time())))

    def reconcile(self):
        """Attach authoritative leaf evidence only to exactly associated local calls.

        Imported sample libraries are never counted as local spending. Exporting
        local parent records with stable components transports evidence through
        nested hubs without re-exporting another user's unrelated tasks.
        """
        with self.db() as db:
            local = [json.loads(r[0]) for r in db.execute("SELECT body FROM costref_observations WHERE local=1")]
            peers = {r["channel"]: dict(r) for r in db.execute("SELECT * FROM costref_peers")}
        grouped = {}
        for channel, peer in peers.items():
            rows = self.observations(channel=channel, connection=peer["connection"])
            for item in rows:
                if item["source_instance_id"] == peer["instance"]:
                    grouped.setdefault((channel, item["task_id"]), []).append(item)
        for parent in local:
            task_id = parent.get("upstream_task_id")
            peer = peers.get(parent["channel"], {})
            if not task_id or not peer or (parent.get("upstream_instance_id") and parent["upstream_instance_id"] != peer["instance"]):
                continue
            components = grouped.get((parent["channel"], task_id), [])
            if not components:
                continue
            # Pending pre-submission records have no debit and must not make a
            # later identified, billed upstream job permanently incomplete.
            billed = [r for r in components if r.get("charged") is not None or r.get("quoted") is not None]
            components = billed or components
            updated = dict(parent, components=components, upstream_instance_id=peer["instance"])
            units = {(r["execution_source"], r["unit"]) for r in components}
            known = [r for r in components if r.get("source") in ACTUAL_SOURCES and r.get("charged") is not None]
            if len(units) == 1:
                updated["execution_source"], updated["unit"] = next(iter(units))
                billing_instances = {r.get("billing_instance_id") or r["source_instance_id"] for r in components}
                updated["billing_instance_id"] = next(iter(billing_instances)) if len(billing_instances) == 1 else "mixed"
                updated["charged"] = amount(sum((number(r["charged"]) for r in known), Decimal(0))) if known else None
                updated["refunded"] = amount(sum((number(r.get("refunded"), optional=False) for r in known), Decimal(0)))
                updated["source"] = "actual" if known else "quote" if any(r.get("quoted") is not None for r in components) else "unknown"
                updated["quoted"] = amount(sum((number(r.get("quoted"), optional=False) for r in components), Decimal(0))) if any(r.get("quoted") is not None for r in components) else None
                updated["settlement_state"] = "complete" if len(known) == len(components) else "incomplete" if known else "estimated"
            else:
                updated.update(charged=None, refunded=None, quoted=None, unit="mixed", source="unknown", settlement_state="incomplete")
            self.put_observation(updated)

    def balance_isolation(self, task: dict):
        if task.get("account_id") is None or task.get("balance_before") is None:
            return
        start = evidence_time({"observed_at": task.get("submitted_at") or task.get("created_at")})
        end = evidence_time({"observed_at": task.get("completed_at") or task.get("updated_at")})
        if not start or end < start:
            task["_cost_balance_isolated"] = False
            return
        account = str(task["account_id"])
        with self.lock, self.db() as db:
            db.execute("INSERT INTO costref_balance_windows VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET end=MAX(end,excluded.end)", (task["id"], account, start, end))
            overlap = db.execute("SELECT 1 FROM costref_balance_windows WHERE account=? AND task_id<>? AND start<=? AND end>=? LIMIT 1", (account, task["id"], end, start)).fetchone()
            if not overlap:
                rows = db.execute("SELECT * FROM tasks WHERE account_id=? AND id<>?", (task["account_id"], task["id"]))
                for raw in rows:
                    other = dict(raw)
                    other_start = evidence_time({"observed_at": other.get("submitted_at") or other.get("created_at")})
                    terminal = str(other.get("status", "")).lower() in FAILED | {"succeeded", "success", "completed"}
                    other_end = evidence_time({"observed_at": other.get("completed_at") or other.get("updated_at")}) if terminal else time.time()
                    if other_start and other_start <= end and other_end >= start:
                        overlap = True
                        break
        task["_cost_balance_isolated"] = not bool(overlap)

    def cached_seconds(self, urls: list[str]) -> str | None:
        values = []
        with self.db() as db:
            for url in urls:
                row = db.execute("SELECT seconds FROM costref_media_cache WHERE digest=?", (hashlib.sha256(url.encode()).hexdigest(),)).fetchone()
                if row is None or row[0] is None:
                    return None
                values.append(number(row[0]))
        return amount(sum(values, Decimal(0)))

    def probe_media(self, limit=2):
        with self.db() as db:
            jobs = list(db.execute("SELECT * FROM costref_media_jobs WHERE next_try<=? ORDER BY next_try LIMIT ?", (time.time(), limit)))
        for job in jobs:
            urls = json.loads(job["urls"])
            for url in urls:
                digest = hashlib.sha256(url.encode()).hexdigest()
                with self.db() as db:
                    cached = db.execute("SELECT seconds,checked FROM costref_media_cache WHERE digest=?", (digest,)).fetchone()
                if cached and (cached[0] is not None or cached[1] > time.time() - 300):
                    continue
                seconds, error = probe_video_duration(url)
                with self.lock, self.db() as db:
                    db.execute("INSERT INTO costref_media_cache VALUES(?,?,?,?) ON CONFLICT(digest) DO UPDATE SET seconds=excluded.seconds,checked=excluded.checked,error=excluded.error",
                               (digest, seconds, time.time(), error))
            total = self.cached_seconds(urls)
            if total is not None:
                with self.lock, self.db() as db:
                    latest_job = db.execute("SELECT urls FROM costref_media_jobs WHERE id=?", (job["id"],)).fetchone()
                    latest = db.execute("SELECT body FROM costref_observations WHERE id=?", (job["id"],)).fetchone()
                    if not latest_job or latest_job[0] != job["urls"]:
                        continue  # The actual submitted input changed while probing.
                    row = json.loads(latest[0]) if latest else json.loads(job["body"])
                row["reference_seconds"] = total
                self.put_observation(row)
            with self.lock, self.db() as db:
                if total is not None or job["expires"] <= time.time():
                    db.execute("DELETE FROM costref_media_jobs WHERE id=?", (job["id"],))
                else:
                    db.execute("UPDATE costref_media_jobs SET next_try=? WHERE id=?", (time.time() + 300, job["id"]))

    def feed(self, cursor: int = 0, limit: int = 500):
        with self.db() as db:
            rows = list(db.execute("SELECT seq,body FROM costref_changes WHERE seq>? ORDER BY seq LIMIT ?", (max(cursor, 0), min(max(limit, 1), 1000))))
        return {"schema_version": 1, "source_instance_id": self.instance, "service": self.service,
                "cursor": rows[-1]["seq"] if rows else max(cursor, 0), "has_more": len(rows) == min(max(limit, 1), 1000),
                "observations": [dict(json.loads(r["body"]), sequence=r["seq"]) for r in rows]}

    def observations(self, *, channel: str = "", connection: str = "") -> list[dict]:
        with self.db() as db:
            if channel:
                rows = db.execute("SELECT o.body FROM costref_observations o JOIN costref_bindings b ON b.id=o.id WHERE b.channel=? AND b.connection=? ORDER BY o.updated DESC", (channel, connection))
            else:
                rows = db.execute("SELECT body FROM costref_observations ORDER BY updated DESC")
            return [json.loads(r[0]) for r in rows]

    def config(self, kind: str, *, redact: bool = False):
        with self.db() as db:
            values = [dict(json.loads(r["body"]), id=r["id"]) for r in db.execute("SELECT id,body FROM costref_config WHERE kind=? ORDER BY id", (kind,))]
        if redact:
            for value in values:
                if "token" in value:
                    value["token_configured"] = bool(value.pop("token"))
        return values

    def save_config(self, kind: str, key: str, value: dict):
        if kind not in {"prices", "rules", "sync", "baselines"}:
            raise ValueError("unknown configuration kind")
        value = dict(value)
        if kind == "baselines":
            value["origin"] = "manual"
        if kind == "prices" and value.get("origin") in {"workbook", "purchase_default"}:
            # A complete GET row can be submitted by an API client, as well as
            # the form. Either kind of administrator edit becomes manual.
            value.pop("origin")
        if kind in {"prices", "rules"}:
            if not value.get("channel"):
                raise ValueError("channel is required")
            for name in ("price", "purchase_amount", "purchase_units", "exchange_rate", "fixed", "generation_rate", "reference_rate", "value", "duration", "reference_seconds"):
                if name in value and value[name] not in (None, ""):
                    value[name] = amount(value[name])
            if kind == "prices":
                if value.get("mode", "credits") not in {"credits", "per_request", "per_second"}:
                    raise ValueError("invalid pricing mode")
                if value.get("mode", "credits") == "credits" and (number(value.get("purchase_units"), optional=False) <= 0 or value.get("purchase_amount") in (None, "")):
                    raise ValueError("purchase amount and positive purchased units are required")
                if value.get("mode") in {"per_request", "per_second"} and value.get("price") in (None, ""):
                    raise ValueError("direct price is required")
                if value.get("currency", "CNY").upper() != "CNY" and number(value.get("exchange_rate"), optional=False) <= 0:
                    raise ValueError("a positive manual CNY exchange rate is required")
            elif value.get("mode", "exact") not in {"exact", "linear"}:
                raise ValueError("invalid consumption rule mode")
            elif value.get("mode", "exact") == "exact" and value.get("value") in (None, ""):
                raise ValueError("exact consumption value is required")
        if kind == "sync":
            previous = next((r for r in self.config(kind) if r["id"] == key), {})
            if not value.get("token") and not value.get("clear_token"):
                value["token"] = previous.get("token", "")
            value.pop("clear_token", None)
            if value.get("base_url"):
                validate_base(value["base_url"])
        elif kind != "baselines":
            selector_keys = ("channel", "connection", "execution_source", "source_instance_id", "billing_instance_id", "unit", "model", "upstream_model", "resolution", "duration", "reference_seconds", "billing_context")
            if kind == "rules":
                selector_keys += ("mode",)
            selector = {k: value.get(k) for k in selector_keys if value.get(k) not in (None, "", "*")}
            for row in self.config(kind):
                other = {k: row.get(k) for k in selector_keys if row.get(k) not in (None, "", "*")}
                if row["id"] != key and selector == other:
                    raise ValueError("相同适用条件已有配置，请编辑现有条目")
        with self.lock, self.db() as db:
            db.execute("INSERT INTO costref_config VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (kind, key, dumps(value)))

    def delete_config(self, kind: str, key: str):
        if kind not in {"prices", "rules", "sync", "baselines"}:
            raise ValueError("unknown configuration kind")
        with self.lock, self.db() as db:
            db.execute("DELETE FROM costref_config WHERE kind=? AND id=?", (kind, key))

    def peer(self, channel: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM costref_peers WHERE channel=?", (channel,)).fetchone()
        return dict(row) if row else {}

    def update_peer(self, channel: str, connection: str, instance: str, cursor: int, error: str = ""):
        with self.lock, self.db() as db:
            db.execute("INSERT INTO costref_peers VALUES(?,?,?,?,?,?) ON CONFLICT(channel) DO UPDATE SET connection=excluded.connection,instance=excluded.instance,cursor=excluded.cursor,last_ok=CASE WHEN excluded.error='' THEN excluded.last_ok ELSE costref_peers.last_ok END,error=excluded.error",
                       (channel, connection, instance, cursor, time.time() if not error else None, error[:200]))


def validate_base(base: str) -> str:
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL must be HTTP(S), without credentials, query or fragment")
    return base.rstrip("/")


def probe_video_duration(url: str) -> tuple[str | None, str]:
    """Bounded background metadata read; never a generation or a submission prerequisite."""
    try:
        process = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "10000000",
                                  "-protocol_whitelist", "http,https,tcp,tls,crypto", "-probesize", "524288",
                                  "-analyzeduration", "1000000", "-show_entries", "format=duration",
                                  "-of", "json", url], capture_output=True, timeout=15, check=False)
        if process.returncode:
            return None, "probe_failed"
        value = amount(json.loads(process.stdout).get("format", {}).get("duration"))
        return (value, "") if value is not None and number(value) > 0 else (None, "duration_unknown")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, "probe_unavailable"


def connection_id(base: str) -> str:
    return hashlib.sha256(base.rstrip("/").encode()).hexdigest()[:24] if base else "local"


def matches(row: dict, candidate: dict, *, dimensions: bool = False) -> bool:
    for key in ("channel", "connection", "execution_source", "unit", "source_instance_id", "billing_instance_id", "model", "upstream_model", "resolution"):
        expected = row.get(key)
        if expected not in (None, "", "*") and str(expected).lower() != str(candidate.get(key, "")).lower():
            return False
    if dimensions:
        for key in ("duration", "reference_seconds"):
            if row.get(key) not in (None, "", "*") and amount(row[key]) != amount(candidate.get(key)):
                return False
    context = row.get("billing_context") or {}
    actual = candidate.get("billing_context") or {}
    return all(actual.get(k) == v for k, v in context.items())


def matches_price(row: dict, candidate: dict, baselines: list[dict] | None = None) -> bool:
    selector = {k: v for k, v in row.items() if k != "unit" or row.get("mode", "credits") == "credits"}
    if row.get("origin") == "workbook":
        # Only aliases explicitly imported with this exact workbook row are
        # equivalent. User price selectors retain their original semantics.
        linked = next((baseline for baseline in baselines or []
                       if row.get("id") == "workbook:" + str(baseline.get("id"))
                       and row.get("channel") == baseline.get("channel")
                       and row.get("model") == baseline.get("model")
                       and row.get("resolution") == baseline.get("resolution")
                       and (row.get("workbook_source") or {}).get("sha256") == (baseline.get("workbook_source") or {}).get("sha256")), None)
        if linked and str(candidate.get("model")) in (linked.get("model_aliases") or []):
            selector["model"] = candidate.get("model")
    return matches(selector, candidate, dimensions=True)


def specificity(row: dict) -> int:
    return sum(v not in (None, "", "*") for k, v in row.items() if k not in {"id", "value", "price", "fixed", "generation_rate", "reference_rate", "mode", "purchase_amount", "purchase_units", "currency", "exchange_rate"})


def formula(rule: dict, candidate: dict) -> Decimal | None:
    result = number(rule.get("fixed"), optional=False)
    for dimension, rate_key in (("duration", "generation_rate"), ("reference_seconds", "reference_rate")):
        rate = number(rule.get(rate_key), optional=False)
        if rate:
            value = number(candidate.get(dimension))
            if value is None:
                return None
            result += rate * value
    return result


def value_observation(item: dict) -> Decimal | None:
    if not item.get("reference_eligible", True):
        return None
    if item.get("source") in ACTUAL_SOURCES:
        return number(item.get("net_usage"))
    # Unknown-unit submissions are visible evidence, never an automatically priced estimate.
    if item.get("unit") not in {None, "", "unknown"}:
        return number(item.get("quoted"))
    return None


def evidence_time(item: dict) -> float:
    value = item.get("observed_at", 0)
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0


def compare_candidate(candidate: dict, observations: list[dict], prices: list[dict], rules: list[dict], baselines: list[dict] | None = None) -> dict:
    candidate = dict(candidate)
    for key in ("duration", "reference_seconds"):
        candidate[key] = amount(candidate.get(key))
    candidate["resolution"] = resolution(candidate.get("resolution"))
    evidence = []
    compatible = []
    for item in observations:
        if item.get("unit") in {None, "", "unknown"}:
            continue
        wanted = str(candidate.get("upstream_model") or candidate.get("model") or "").lower()
        names = {str(item.get("model", "")).lower(), str(item.get("upstream_model", "")).lower()}
        if wanted and wanted not in names:
            continue
        if candidate.get("execution_source") and candidate["execution_source"] != candidate.get("channel") and candidate["execution_source"] != item.get("execution_source"):
            continue
        if candidate.get("billing_instance_id") and candidate["billing_instance_id"] != item.get("billing_instance_id"):
            continue
        if resolution(item.get("resolution")) != candidate["resolution"]:
            continue
        if any(amount(item.get(k)) != candidate[k] for k in ("duration", "reference_seconds")):
            continue
        context, observed_context = candidate.get("billing_context") or {}, item.get("billing_context") or {}
        if any(k in context and context[k] != v for k, v in observed_context.items()):
            continue
        if value_observation(item) is not None:
            compatible.append(item)
    scenes = {dumps(r.get("billing_context") or {}) for r in compatible}
    context = candidate.get("billing_context") or {}
    if len(scenes) == 1:
        candidate["billing_context"] = {**json.loads(next(iter(scenes))), **context}
        evidence = compatible
    elif len(scenes) > 1:
        required_keys = set().union(*(set(json.loads(s)) for s in scenes))
        if required_keys <= set(context):
            evidence = [r for r in compatible if r.get("billing_context") and all(context.get(k) == v for k, v in r["billing_context"].items())]
    evidence.sort(key=evidence_time, reverse=True)
    # Actual evidence always precedes quote, regardless of import order.
    actual = next((r for r in evidence if r.get("source") in ACTUAL_SOURCES), None)
    quote = next((r for r in evidence if r.get("source") in {"quote", "submission_reported"}), None)
    selected_formula = next((r for r in evidence if r.get("source") == "formula"), None)
    selected = actual or quote or selected_formula
    if selected:
        for key in ("unit", "execution_source", "source_instance_id", "billing_instance_id"):
            candidate[key] = selected.get(key)
    maintained = table_baseline(candidate, baselines)
    pool_candidate = dict(candidate)
    if pool_candidate.get("unit") in (None, "", "unknown"):
        pool_candidate["unit"] = "credits"
    _, maintained_pool = credit_price(pool_candidate, [p for p in prices if p.get("origin") == "matrix"])
    if (maintained and maintained.get("matrix_managed")) or maintained_pool:
        # Editing the simple table supersedes legacy manual price/usage rules
        # for this provider/specification, without deleting their stored data.
        if actual or quote:
            usage, source = value_observation(actual or quote), (actual or quote)["source"]
        else:
            usage, source = table_estimate(candidate, maintained)
            candidate["unit"] = (maintained or {}).get("unit", "credits")
        rate, pool = credit_price(candidate, prices, maintained)
        money = usage * rate if usage is not None and rate is not None else None
        return dict(candidate, usage=amount(usage), source=source, cost=amount(money), currency="CNY",
                    available_scenes=[json.loads(s) for s in sorted(scenes)],
                    reason="" if money is not None else "维护表缺少消耗、参考视频耗额或采购额度单价",
                    sample=selected if actual or quote else None, baseline=maintained, purchase=pool,
                    samples=len(evidence), history_min=None, history_max=None)
    # A consumption rule defines its output unit; unit is not a matching prerequisite.
    def rule_match(rule, dimensions=False):
        return matches({key: value for key, value in rule.items() if key != "unit"}, candidate, dimensions=dimensions)
    exact = sorted([r for r in rules if r.get("mode", "exact") == "exact" and rule_match(r, dimensions=True)], key=specificity, reverse=True)
    linear = sorted([r for r in rules if r.get("mode") == "linear" and rule_match(r)], key=specificity, reverse=True)
    usage, source = None, "unknown"
    baseline = next((r for r in baselines or [] if r.get("channel") == candidate.get("channel") and
                     (not r.get("connection") or r["connection"] == candidate.get("connection")) and
                     str(candidate.get("model")) in (r.get("model_aliases") or [r.get("model")]) and
                     r.get("resolution") == candidate["resolution"] and candidate["duration"] == "15" and
                     str(candidate.get("requested_reference_seconds", candidate.get("reference_seconds"))) == "0" and
                     number(r.get("usage"), optional=False) > 0), None)
    if exact:
        usage, source = number(exact[0].get("value")), "manual"
        candidate["unit"] = exact[0].get("unit") or candidate.get("unit", "credits")
    elif actual or quote:
        usage, source = value_observation(actual or quote), (actual or quote).get("source", "unknown")
    elif baseline:
        usage, source = number(baseline["usage"]), "spreadsheet"
        candidate["unit"] = "credits"
    elif linear:
        usage, source = formula(linear[0], candidate), "formula"
        candidate["unit"] = linear[0].get("unit") or candidate.get("unit", "credits")
    elif selected_formula:
        usage, source = value_observation(selected_formula), "formula"
    matched_prices = sorted([r for r in prices if matches_price(r, candidate, baselines)], key=lambda r: ({"workbook": 0, "purchase_default": 1}.get(r.get("origin"), 2), specificity(r)), reverse=True)
    price = next(iter(matched_prices), None)
    money, reason = None, "缺少采购价或直接单价"
    if price:
        mode = price.get("mode", "credits")
        fx = number(price.get("exchange_rate"), optional=False) if price.get("currency", "CNY").upper() != "CNY" else Decimal(1)
        if mode == "credits":
            if usage is not None and candidate.get("unit") not in {None, "", "unknown"}:
                money = usage * number(price.get("purchase_amount"), optional=False) / number(price.get("purchase_units"), optional=False) * fx
            else:
                reason = "缺少同条件消耗、可信单位或完整计费上下文"
        elif mode == "per_request":
            money, source = number(price.get("price"), optional=False) * fx, "manual_price"
        elif number(candidate.get("duration")) is not None:
            money, source = number(price.get("price"), optional=False) * number(candidate["duration"]) * fx, "manual_price"
    if len(scenes) > 1 and not evidence and money is None:
        reason = "存在多个计费场景，请补充场景条件或从任务带入"
    return dict(candidate, usage=amount(usage), source=source, cost=amount(money), currency="CNY", available_scenes=[json.loads(s) for s in sorted(scenes)],
                reason="" if money is not None else reason, sample=selected if source != "spreadsheet" else None, baseline=baseline if source == "spreadsheet" else None,
                samples=len(evidence), history_min=amount(min((value_observation(r) for r in evidence), default=None)),
                history_max=amount(max((value_observation(r) for r in evidence), default=None)))


async def call(function, *args):
    if function is None:
        return None
    if inspect.iscoroutinefunction(function):
        return await function(*args)
    result = await asyncio.to_thread(function, *args)
    return await result if inspect.isawaitable(result) else result


class CostReference:
    def __init__(self, *, service_name, database_path, adapter, token_env, hub=False, channels=None, preview=None):
        self.service = service_name
        self.store = CostStore(database_path, service_name)
        self.adapter, self.token_env, self.hub = adapter, token_env, hub
        self.channels_callback, self.preview_callback = channels, preview
        self.sync_lock = asyncio.Lock()
        self.collect_lock = asyncio.Lock()

    def task_cost(self, task_id, observations=None, *, task=None, connection=None):
        """Project provided adapter rows directly, or read one local task without upstream calls."""
        if not self.store.ready:
            self.store.initialize()
        if observations is None:
            return self.task_costs([task_id])[str(task_id)]
        rows = [dict(row) for row in observations]
        baselines = self.store.matrix_rows() if self.hub else []
        if connection:
            baselines = [r for r in baselines if not r.get("connection") or r["connection"] == connection]
        result = project_task_cost(rows, self.store.config("prices") + self.store.config("matrix_prices") if self.hub else [], baselines,
                                   instance=self.store.instance, service_name=self.service,
                                   connections={row.get("channel"): connection for row in rows} if connection else None,
                                   rules=self.store.config("rules") if self.hub else [], money_enabled=self.hub)
        return dict(result, task_id=str(task_id))

    def task_costs(self, task_ids, *, connections=None):
        if not self.store.ready:
            self.store.initialize()
        keys = list(dict.fromkeys(str(key) for key in task_ids))
        if not keys:
            return {}
        if len(keys) > 200 or any(not key or len(key) > 256 for key in keys):
            raise ValueError("at most 200 valid task IDs are allowed")
        placeholders = ",".join("?" for _ in keys)
        with self.store.db() as db:
            tasks = {str(row["id"]): decode_task(dict(row)) for row in db.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders})", keys)}
            cached = {}
            for raw in db.execute(f"SELECT body FROM costref_observations WHERE local=1 AND json_extract(body,'$.task_id') IN ({placeholders})", keys):
                row = json.loads(raw[0])
                cached.setdefault(row["task_id"], {})[row["attempt_id"]] = row
        prices = self.store.config("prices") + self.store.config("matrix_prices") if self.hub else []
        baselines = self.store.matrix_rows() if self.hub else []
        result = {}
        for key in keys:
            rows = []
            if key in tasks:
                try:
                    rows = self.adapter(tasks[key]) or []
                except Exception:
                    LOG.warning("Cost task projection unavailable for %s", self.service)
            enriched = []
            for row in rows:
                previous = cached.get(key, {}).get(str(row.get("attempt_id") or "generation"), {})
                merged = dict(previous, **row)
                for field in ("duration", "reference_seconds", "billable_reference_seconds"):
                    if row.get(field) is None and previous.get(field) is not None:
                        merged[field] = previous[field]
                if previous.get("source") in ACTUAL_SOURCES and row.get("source") not in ACTUAL_SOURCES:
                    for field in ("source", "charged", "refunded", "unit"):
                        merged[field] = previous.get(field)
                if previous.get("components"):
                    merged["components"] = previous["components"]
                enriched.append(merged)
            relevant = baselines
            if connections is not None:
                relevant = [r for r in baselines if not r.get("connection") or connections.get(r.get("channel")) == r["connection"]]
            result[key] = dict(project_task_cost(enriched, prices, relevant, instance=self.store.instance,
                                                 service_name=self.service, connections=connections,
                                                 rules=self.store.config("rules") if self.hub else [], money_enabled=self.hub), task_id=key)
        return result

    async def matrix(self):
        if not self.hub:
            raise ValueError("pricing management is available only in the aggregators")
        await self.collect()
        registered = {row["id"]: row for row in await self.channels()}
        rows = self.store.matrix_rows()
        groups = {}
        for row in rows:
            row["registered"] = row["channel"] in registered if self.hub else provider_identity(row["channel"]) == self.service
            groups.setdefault(row["channel"], {key: row.get(key) for key in
                              ("channel", "group", "service", "registered", "purchase_amount", "purchase_units", "unit_price")})
            groups[row["channel"]]["header_cell"] = (row.get("source") or {}).get("header_cell", "")
        for channel in registered:
            groups.setdefault(channel, dict(channel=channel, group=channel, service=provider_identity(channel), registered=True))
        return {"service": self.service, "rows": rows, "groups": list(groups.values()), "advisory_only": True}

    async def push_matrix(self, channel_id, key, patch):
        # Procurement and manual estimates are local to each aggregator. Cost
        # collection is one way: only usage observations flow upstream to hubs.
        return {"status": "local", "reason": "pricing configuration stays in this aggregator"}

    async def collect(self):
        async with self.collect_lock:
            if not self.store.ready:
                await asyncio.to_thread(self.store.initialize)
                connections = {r["id"]: r["connection"] for r in await self.channels()} if self.hub else None
                for filename, seed_method in ((("cost_reference_seed.json", self.store.seed_workbook), ("purchase_prices.json", self.store.seed_purchases)) if self.hub else ()):
                    seed = Path(__file__).with_name(filename)
                    if seed.is_file():
                        snapshot = json.loads(seed.read_text(encoding="utf-8"))
                        if not self.hub:
                            field = "records" if filename == "cost_reference_seed.json" else "entries"
                            snapshot[field] = [r for r in snapshot.get(field, []) if
                                               provider_identity(r.get("service") or r.get("provider") or r.get("channel")) == self.service]
                            local_connections = {r["channel"]: "local" for r in snapshot[field]}
                        else:
                            local_connections = connections
                        await asyncio.to_thread(seed_method, snapshot, local_connections)
            # Bound work per request; the worker drains historical backfill in batches.
            count = await asyncio.to_thread(self.store.collect, self.adapter)
            if self.hub:
                await asyncio.to_thread(self.store.reconcile)
            return count

    async def channels(self):
        channels = await call(self.channels_callback) or []
        sync = {r["id"]: r for r in self.store.config("sync")}
        for channel in channels:
            override = sync.get(channel["id"], {})
            channel["base_url"] = override.get("base_url") or channel.get("base_url", "")
            channel["connection"] = connection_id(channel["base_url"])
            channel["token"] = override.get("token", "")
        return channels

    async def sync(self):
        if not self.hub:
            return
        import httpx
        async with self.sync_lock:
            channels = await self.channels()
            semaphore = asyncio.Semaphore(4)
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                async def pull(channel):
                    if not channel.get("base_url") or not channel.get("token"):
                        return
                    async with semaphore:
                        peer = self.store.peer(channel["id"])
                        base = validate_base(channel["base_url"])
                        connection = connection_id(base)
                        cursor = peer.get("cursor", 0) if peer.get("connection") == connection else 0
                        instance = peer.get("instance", "") if peer.get("connection") == connection else ""
                        try:
                            for _ in range(20):
                                response = await client.get(base + "/internal/cost-observations", params={"cursor": cursor, "limit": 500}, headers={"Authorization": "Bearer " + channel["token"]})
                                response.raise_for_status()
                                if len(response.content) > 8 * 1024 * 1024:
                                    raise ValueError("cost feed exceeds size limit")
                                feed = response.json()
                                remote_instance = str(feed["source_instance_id"])
                                if instance and instance != remote_instance and cursor:
                                    instance, cursor = remote_instance, 0
                                    continue
                                instance = remote_instance
                                for item in feed.get("observations", []):
                                    if item.get("source_instance_id") != instance:
                                        raise ValueError("feed source instance mismatch")
                                    await asyncio.to_thread(self.store.put_observation, item, local=False, binding=(channel["id"], connection))
                                next_cursor = int(feed["cursor"])
                                if next_cursor < cursor or (feed.get("has_more") and next_cursor == cursor):
                                    raise ValueError("invalid cost feed cursor")
                                cursor = next_cursor
                                self.store.update_peer(channel["id"], connection, instance, cursor)
                                if not feed.get("has_more"):
                                    break
                        except Exception as exc:
                            # Never store HTTP exception text that can include URLs/credentials.
                            error = "HTTP " + str(exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                            self.store.update_peer(channel["id"], connection, instance, cursor, error)
                await asyncio.gather(*(pull(c) for c in channels))
            await asyncio.to_thread(self.store.reconcile)

    async def compare(self, context):
        if not self.hub:
            raise ValueError("pricing comparison is available only in the aggregators")
        await self.collect()
        plan = await self.preview_plan(context)
        channels = {r["id"]: r for r in await self.channels()}
        local = [r for r in self.store.observations() if r["source_instance_id"] == self.store.instance]
        candidates = plan.get("candidates", [])
        if not candidates:
            candidates = [dict(context, channel=channel) for channel in plan.get("order", [])]
        for candidate in candidates:
            # Snapshot eligibility describes the caller's original inputs, even
            # when a channel clips all submitted reference videos away.
            original_video_count = context.get("video_count", (context.get("billing_context") or {}).get("video_count"))
            requested_reference = context.get("requested_reference_seconds", context.get("reference_seconds"))
            if number(original_video_count, optional=False) > 0 and number(requested_reference, optional=False) == 0:
                requested_reference = None
            candidate.setdefault("requested_reference_seconds", requested_reference)
        semaphore = asyncio.Semaphore(4)
        async def effective(raw):
            channel = channels.get(raw["channel"], {})
            if not channel.get("token") or not channel.get("base_url") or (raw["channel"] == "sdlapi" and self.service == "lingya2api"):
                return
            async with semaphore:
                import httpx
                request = dict(context, **raw)
                request["model"] = raw.get("upstream_model") or raw.get("model")
                request.pop("task_id", None)
                request.update(raw.get("billing_context") or {})
                try:
                    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                        response = await client.post(channel["base_url"].rstrip("/") + "/internal/cost-preview", json=request, headers={"Authorization": "Bearer " + channel["token"]})
                        response.raise_for_status()
                        preview = response.json()
                    first = next(iter(preview.get("candidates") or []), None)
                    if first:
                        raw["upstream_model"] = first.get("upstream_model") or first.get("model") or raw.get("upstream_model")
                        for key in ("duration", "reference_seconds", "resolution", "billing_context", "execution_source", "unit"):
                            if key in first:
                                raw[key] = first[key]
                except Exception:
                    # Missing/old optional preview is compatible; missing dimensions
                    # still cannot match known actual samples by pretending to be zero.
                    pass
        await asyncio.gather(*(effective(raw) for raw in candidates))
        result = []
        for raw in candidates:
            candidate = dict(context, **raw)
            channel = channels.get(candidate["channel"], {})
            candidate["connection"] = channel.get("connection", "local")
            observations = [r for r in local if r["channel"] == candidate["channel"]]
            if channel.get("base_url"):
                remote = self.store.observations(channel=candidate["channel"], connection=candidate["connection"])
                peer_instance = self.store.peer(candidate["channel"]).get("instance")
                if peer_instance:
                    remote = [r for r in remote if r["source_instance_id"] == peer_instance]
                    candidate["source_instance_id"] = peer_instance
                if candidate["channel"] == "sdlapi" and self.service == "lingya2api":
                    remote = await self.nested_candidates(channel, candidate, remote)
                    if candidate.get("nested_unresolved"):
                        observations = []
                    else:
                        observations = remote
                else:
                    # A controlled upstream's normalized ledger supersedes ambiguous
                    # legacy compatibility fields observed by the forwarding hub.
                    observations = remote or observations
            # Identity is independent of a sampled duration. This lets a pinned
            # default per-second rule fill an unsampled duration safely.
            wanted = str(candidate.get("upstream_model") or candidate.get("model") or "").lower()
            identity_rows = [r for r in observations if wanted in {str(r.get("model", "")).lower(), str(r.get("upstream_model", "")).lower()} and (not candidate.get("billing_instance_id") or candidate["billing_instance_id"] == r.get("billing_instance_id"))]
            identities = {(r.get("execution_source"), r.get("unit"), r.get("source_instance_id")) for r in identity_rows}
            if len(identities) == 1:
                execution_source, unit, instance = next(iter(identities))
                if candidate.get("execution_source") in (None, candidate["channel"], execution_source):
                    candidate.update(execution_source=execution_source, unit=unit, source_instance_id=instance)
            priced = compare_candidate(candidate, observations, self.store.config("prices") + self.store.config("matrix_prices"), self.store.config("rules"), self.store.matrix_rows())
            if candidate.get("nested_unresolved") and priced["source"] not in {"manual", "manual_price", "formula"}:
                priced.update(cost=None, reason="无法确定 Leo 当前手工路由的实际计费来源")
            result.append(priced)
        ranked = sorted((r for r in result if r["cost"] is not None), key=lambda r: number(r["cost"]))
        return {"current_order": plan.get("order", [r["channel"] for r in candidates]), "recommended_order": [r["channel"] for r in ranked],
                "ranked": ranked, "unpriced": [r for r in result if r["cost"] is None], "context": context, "advisory_only": True}

    async def preview_plan(self, context):
        plan = await call(self.preview_callback, context) or {}
        channels = {r["id"]: r for r in await self.channels()} if self.hub else {}
        for candidate in plan.get("candidates", []):
            urls = candidate.pop("_reference_urls", [])
            if candidate.get("reference_seconds") is None and urls:
                candidate["reference_seconds"] = self.store.cached_seconds(urls)
            peer = self.store.peer(candidate.get("channel", ""))
            if peer.get("instance") and peer.get("connection") == channels.get(candidate.get("channel"), {}).get("connection"):
                candidate["billing_instance_id"] = peer["instance"]
        return plan

    async def nested_candidates(self, channel, candidate, observations):
        import httpx
        if not channel.get("token"):
            candidate["nested_unresolved"] = True
            return []
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                nested_request = dict(candidate, model=candidate.get("upstream_model") or candidate.get("model"))
                nested_request.pop("task_id", None)  # The outer task ID is not a Leo task ID.
                nested_request.update(candidate.get("billing_context") or {})
                response = await client.post(channel["base_url"].rstrip("/") + "/internal/cost-preview", json=nested_request,
                                             headers={"Authorization": "Bearer " + channel["token"]})
                response.raise_for_status()
                plan = response.json()
            first = (plan.get("candidates") or [])[0]
            # The transport channel chooses the eligible branch; the normalized
            # observation supplies its real billing source (e.g. pol2api).
            candidate.pop("execution_source", None)
            candidate["nested_execution_channel"] = first["channel"]
            for key in ("upstream_model", "duration", "reference_seconds", "resolution", "billing_context", "billing_instance_id"):
                if key in first:
                    candidate[key] = first[key]
            return [r for r in observations if r["channel"] == first["channel"]]
        except Exception:
            candidate["nested_unresolved"] = True
            return []

    async def worker(self):
        while True:
            try:
                count = await self.collect()
                if self.hub:
                    await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Cost reference background collection failed for %s", self.service)
                count = 0
            await asyncio.sleep(0.1 if count == 500 else 30)

    async def media_worker(self):
        while True:
            try:
                await asyncio.to_thread(self.store.probe_media)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Background cost metadata read failed for %s", self.service)
            await asyncio.sleep(5)


def install_cost_reference(app, *, service_name, database_path, admin_dependency, adapter, token_env,
                           hub=False, channels=None, preview=None):
    from fastapi import Body, Depends, HTTPException, Request
    from fastapi.responses import HTMLResponse, Response
    # FastAPI resolves postponed annotations in the module namespace.
    globals()["Request"] = Request

    reference = CostReference(service_name=service_name, database_path=database_path, adapter=adapter,
                              token_env=token_env, hub=hub, channels=channels, preview=preview)
    def require_hub():
        if not reference.hub:
            raise HTTPException(403, "pricing management is available only in the aggregators")
    # Keep the host's startup/shutdown ordering, including hosts using lifespan rather than on_event.
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(host):
        async with original(host) as state:
            await reference.collect()
            worker = asyncio.create_task(reference.worker(), name=service_name + "-cost-reference")
            media_worker = asyncio.create_task(reference.media_worker(), name=service_name + "-cost-metadata")
            try:
                yield state
            finally:
                worker.cancel()
                media_worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                try:
                    await media_worker
                except asyncio.CancelledError:
                    pass
    app.router.lifespan_context = lifespan

    async def sync_auth(request: Request):
        expected = os.getenv(token_env, "").strip()
        provided = request.headers.get("authorization", "")
        provided = provided[7:].strip() if provided.lower().startswith("bearer ") else ""
        if not expected or not secrets.compare_digest(provided, expected):
            raise HTTPException(401, "cost sync authentication required")

    @app.get("/internal/cost-observations", dependencies=[Depends(sync_auth)])
    async def feed(cursor: int = 0, limit: int = 500):
        await reference.collect()
        return reference.store.feed(cursor, limit)

    @app.post("/internal/cost-preview", dependencies=[Depends(sync_auth)])
    async def internal_preview(body: dict = Body(...)):
        await reference.collect()
        try:
            plan = await reference.preview_plan(body) or {}
            dimensions = {"channel", "execution_source", "model", "upstream_model", "duration",
                          "requested_duration", "reference_seconds", "requested_reference_seconds",
                          "billable_reference_seconds", "resolution", "unit", "billing_context",
                          "billing_instance_id", "source_instance_id"}
            return {"order": plan.get("order", []), "candidates": [
                {key: value for key, value in item.items() if key in dimensions}
                for item in plan.get("candidates", []) if isinstance(item, dict)], "advisory_only": True}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/internal/cost-matrix", dependencies=[Depends(sync_auth)])
    async def internal_matrix(body: dict = Body(...)):
        # Old configuration stays in the database but can no longer be pushed
        # between services. The only cross-service cost feed is observations.
        raise HTTPException(410, "pricing configuration synchronization has been retired")

    @app.get("/api/cost-reference/assets/task-cost.js")
    async def task_asset():
        # This static program contains no configuration or task data; actual
        # projections remain protected by the host's administrator dependency.
        return Response(Path(__file__).with_name("task_cost.js").read_text(encoding="utf-8"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

    @app.post("/api/cost-reference/task-costs", dependencies=[Depends(admin_dependency)])
    async def task_costs(body: dict = Body(...)):
        await reference.collect()
        ids = body.get("task_ids")
        if not isinstance(ids, list) or any(not isinstance(key, str) for key in ids):
            raise HTTPException(422, "task_ids must be a list of task IDs")
        try:
            connections = {r["id"]: r["connection"] for r in await reference.channels()} if reference.hub else None
            return {"tasks": await asyncio.to_thread(reference.task_costs, ids, connections=connections),
                    "money_enabled": reference.hub, "advisory_only": True}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/cost-reference/task/{task_id}", dependencies=[Depends(admin_dependency)])
    async def task_cost(task_id: str):
        result = await task_costs({"task_ids": [task_id]})
        return result["tasks"][task_id]

    @app.get("/api/cost-reference/matrix", dependencies=[Depends(admin_dependency)])
    async def matrix():
        require_hub()
        return await reference.matrix()

    @app.put("/api/cost-reference/matrix/{key}", dependencies=[Depends(admin_dependency)])
    async def save_matrix(key: str, body: dict = Body(...)):
        require_hub()
        await reference.collect()
        previous = next((r for r in reference.store.config("baselines") if r["id"] == key), {})
        channel_id = body.get("channel", previous.get("channel", ""))
        if not reference.hub and provider_identity(channel_id) != service_name:
            raise HTTPException(422, "only this service's matrix can be edited")
        channels = {r["id"]: r for r in await reference.channels()} if reference.hub else {}
        connection = channels.get(channel_id, {}).get("connection", "local")
        try:
            row = await asyncio.to_thread(reference.store.save_matrix, key, body, connection=connection,
                                          service_name=provider_identity(channel_id))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"ok": True, "row": row, "propagation": await reference.push_matrix(channel_id, key, body)}

    @app.get("/api/cost-reference/page", dependencies=[Depends(admin_dependency)], response_class=HTMLResponse)
    async def page():
        template = Path(__file__).with_suffix(".html") if reference.hub else Path(__file__).with_name("usage_ledger.html")
        return HTMLResponse(template.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/api/cost-reference/state", dependencies=[Depends(admin_dependency)])
    async def state():
        await reference.collect()
        channel_list = await reference.channels()
        for c in channel_list:
            c["token_configured"] = bool(c.pop("token", ""))
            c["sync_status"] = reference.store.peer(c["id"])
        observations = reference.store.observations()
        with reference.store.db() as db:
            failed_count = db.execute("SELECT COUNT(*) FROM costref_failed_events").fetchone()[0]
        # Actual spend contains only this service's local observations, never its imported sample library.
        totals: dict[str, Decimal] = {}
        seen = set()
        def spending(item):
            if item.get("components"):
                for component in item["components"]:
                    spending(component)
                return
            if item["observation_id"] in seen:
                return
            seen.add(item["observation_id"])
            if item["source"] in ACTUAL_SOURCES and item["net_usage"] is not None:
                key = item["execution_source"] + "/" + item["unit"]
                totals[key] = totals.get(key, Decimal(0)) + number(item["net_usage"])
        for item in observations:
            if item["source_instance_id"] == reference.store.instance:
                spending(item)
        result = {"service": service_name, "hub": hub, "money_enabled": reference.hub, "instance": reference.store.instance,
                "channels": channel_list if reference.hub else [],
                "observations": observations[:2000], "observation_count": len(observations), "totals": {k: amount(v) for k, v in totals.items()},
                "failed_observations": failed_count, "advisory_only": True}
        if reference.hub:
            result.update(prices=reference.store.config("prices"), rules=reference.store.config("rules"),
                          baselines=reference.store.config("baselines"), sync=reference.store.config("sync", redact=True))
        return result

    @app.put("/api/cost-reference/{kind}/{key}", dependencies=[Depends(admin_dependency)])
    async def save(kind: str, key: str, body: dict = Body(...)):
        require_hub()
        await reference.collect()
        try:
            if kind in {"prices", "rules"} and not body.get("connection"):
                channel = next((c for c in await reference.channels() if c["id"] == body.get("channel")), {})
                body["connection"] = channel.get("connection", "local")
                peer = reference.store.peer(body.get("channel", ""))
                if peer.get("instance") and peer.get("connection") == body["connection"]:
                    body.setdefault("source_instance_id", peer["instance"])
            reference.store.save_config(kind, key, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"ok": True}

    @app.delete("/api/cost-reference/{kind}/{key}", dependencies=[Depends(admin_dependency)])
    async def delete(kind: str, key: str):
        require_hub()
        await reference.collect()
        try:
            reference.store.delete_config(kind, key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"ok": True}

    @app.post("/api/cost-reference/compare", dependencies=[Depends(admin_dependency)])
    async def compare(body: dict = Body(...)):
        require_hub()
        try:
            return await reference.compare(body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/cost-reference/sync", dependencies=[Depends(admin_dependency)])
    async def sync():
        require_hub()
        await reference.collect()
        if hub:
            await reference.sync()
        return {"ok": True}

    @app.post("/api/cost-reference/retry", dependencies=[Depends(admin_dependency)])
    async def retry():
        await reference.collect()
        count = reference.store.retry_failures()
        await reference.collect()
        return {"ok": True, "retried": count}

    app.state.cost_reference = reference
    return reference
