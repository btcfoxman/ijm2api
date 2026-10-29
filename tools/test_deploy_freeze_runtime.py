"""Hosted-only Docker smoke of the real cutover controller, with synthetic data.

No provider, service API, credentials, CLI executable, or application is started.
The fixtures use a private temporary bind mount and a Python PID 1 holding a
SQLite WAL open.  Run only in a GitHub-hosted Linux job with Docker available.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
TASK = "ijm_" + "a" * 16
RACE_TASK = "ijm_" + "b" * 16
SERVICE_SOURCE = b"# synthetic immutable startup source; no application\n"
SOURCE_SHA = hashlib.sha256(SERVICE_SOURCE).hexdigest()
HEARTBEAT = """import pathlib,sqlite3,time
root=pathlib.Path('/app/data')
with (root/(name+'.heartbeat')).open('ab',buffering=0) as beat:
 (root/(name+'.ready')).touch()
 while True:
  beat.write(b'x')
  if (root/'queued-after-freeze').is_file():
   (root/'executed-after-freeze').touch()
   d=sqlite3.connect(str(root/'ijm2api.db'))
   d.execute(\"INSERT INTO tasks SELECT ?, 'queued', account_id, execution_backend, upstream_context_json,error_code,request_json,normalized_json,run_id,thread_id FROM tasks LIMIT 1\",('ijm_'+('c'*16),))
   d.commit();d.close()
  time.sleep(0.01)
"""


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def docker(args, *, input=None, timeout=30, optional=False):
    result = subprocess.run(["docker", *args], input=input, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode and not optional:
        # Docker output can contain host paths.  Keep the CI assertion bounded.
        raise RuntimeError("SyntheticDockerOperationFailed:" + args[0])
    return result.stdout.strip() if result.returncode == 0 else None


def require_hosted():
    if (os.name != "posix" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.environ.get("RUNNER_OS") != "Linux"):
        raise RuntimeError("This smoke is restricted to GitHub-hosted Linux runners")


def fixture_program(active):
    # This schema intentionally contains no real request or authentication data.
    context = {"backend": "canvas_cli", "task_id": TASK, "account_id": 1,
        "profile": "synthetic-profile", "expected_user_id": "synthetic-user",
        "phase": "accepted", "submission_phase": "submitted", "kind": "video",
        "output_count": 1, "project_id": "00000000-0000-4000-8000-000000000001",
        "node_id": "node_0123456789", "submit_id": "00000000-0000-4000-8000-000000000002",
        "resource_ids": ["00000000-0000-4000-8000-000000000003"],
        "observation_deadline": time.time() + 600, "model": "seedance_2.0_mini", "mode": "m2v"}
    return """import json,pathlib,sqlite3,subprocess,sys,time
root=pathlib.Path('/app/data')
marker=root/'canvas-cli/account-1/credential-profile'
marker.parent.mkdir(parents=True,exist_ok=True);marker.write_text('synthetic-profile')
db=sqlite3.connect(str(root/'ijm2api.db'))
db.executescript('''CREATE TABLE accounts(id INTEGER,enabled INTEGER,status TEXT,execution_mode TEXT,
cli_auth_status TEXT,login_user_id TEXT,cli_user_id TEXT,proxy_url TEXT);
CREATE TABLE tasks(id TEXT,status TEXT,account_id INTEGER,execution_backend TEXT,
upstream_context_json TEXT,error_code TEXT,request_json TEXT,normalized_json TEXT,run_id TEXT,thread_id TEXT);
CREATE TABLE snapshot_probe(value TEXT);
INSERT INTO accounts VALUES(1,1,'active','canvas_cli','ready','synthetic-user','synthetic-user','http://127.0.0.1:9');''')
db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',(%r,%r,1,'canvas_cli',%r,'UPSTREAM_SUBMIT_OUTCOME_UNKNOWN','{}','{}','',''))
db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
db.execute("INSERT INTO snapshot_probe VALUES('uncheckpointed-synthetic-WAL-row')");db.commit()
heartbeat=%r
subprocess.Popen([sys.executable,'-B','-c',"name='child';"+heartbeat])
while not (root/'child.ready').is_file():time.sleep(0.01)
(root/'ready').write_text('ready')
exec(heartbeat,{'name':'parent'})
""" % (TASK, "submitted" if active else "succeeded", json.dumps(context), HEARTBEAT)


class Trace:
    def __init__(self, service, data, *, inject_race=False, expect_success=False, pause_timeout=False):
        self.service = service
        self.data = data
        self.inject_race = inject_race
        self.expect_success = expect_success
        self.pause_timeout = pause_timeout
        self.paused = False
        self.companion = None
        self.events = []
        self.frozen_heartbeats = None

    def __call__(self, args, *, input=None, timeout=30):
        assert args[0] == "docker"
        command = args[1:]
        if command[0] == "run":
            assert not self.paused and "--read-only" in command
            assert command[command.index("--network") + 1] == "none"
            assert command[command.index("--volumes-from") + 1] == self.service + ":ro"
            assert command[command.index("--pid") + 1] == "container:" + self.service
            value = docker(command, input=input, timeout=timeout)
            self.companion = value
            self.events.append("companion_joined_before_pause")
            return value
        if command[0] == "pause":
            assert self.companion and command[1] == self.service
            if self.inject_race:
                docker(["exec", self.service, "python", "-B", "-c",
                    "import sqlite3;d=sqlite3.connect('/app/data/ijm2api.db');"
                    "d.execute(\"INSERT INTO tasks SELECT ?, 'preparing', account_id, execution_backend, "
                    "upstream_context_json,error_code,request_json,normalized_json,run_id,thread_id FROM tasks LIMIT 1\","
                    "(" + repr(RACE_TASK) + ",));d.commit();d.close()"])
                self.events.append("new_preparing_inserted_before_pause")
            value = docker(command, input=input, timeout=timeout)
            self.paused = True
            self.events.append("old_paused")
            if self.pause_timeout:
                self.events.append("pause_client_failed_after_real_pause")
                raise RuntimeError("SyntheticPauseClientTimeoutAfterEffect")
            if self.expect_success:
                self.frozen_heartbeats = {p.name:p.stat().st_size for p in self.data.glob('*.heartbeat')}
                assert len(self.frozen_heartbeats) == 2 and all(self.frozen_heartbeats.values())
                (self.data / 'queued-after-freeze').touch()
                self.events.append("synthetic_request_queued_while_frozen")
            return value
        if command[0] == "exec":
            assert self.paused and command[2] == self.companion
            self.events.append("frozen_companion_read")
        if command[0] == "unpause":
            assert command[1] == self.service
            self.events.append("abort_unpaused_original")
        if command[:2] == ["rm", "--force"] and command[2] == self.service:
            assert self.paused
            self.events.append("old_removed_while_frozen")
        return docker(command, input=input, timeout=timeout)


def run_case(controller, guard, image, name, *, active, query, race=False, pause_timeout=False):
    service = "ijm-freeze-smoke-" + uuid.uuid4().hex[:16]
    trace = None
    with tempfile.TemporaryDirectory(prefix="ijm-freeze-smoke-") as directory:
        root = Path(directory)
        data, app, release, proc = (root / value for value in ("data", "app", "release", "proc"))
        for folder in (data, app, release, proc):
            folder.mkdir(mode=0o700)
        # Hosted uid owns these synthetic mounts; cap-drop ALL container root
        # cannot bypass 0700.  No credentials exist here; production is untouched.
        data.chmod(0o777)
        app.chmod(0o755)
        (app / "service.py").write_bytes(SERVICE_SOURCE)
        try:
            old_id = docker(["run", "--detach", "--name", service, "--pull", "never",
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--memory", "128m", "--pids-limit", "32",
                "--mount", "type=bind,source=" + str(data) + ",target=/app/data",
                "--mount", "type=bind,source=" + str(app) + ",target=/app/app,readonly",
                "--entrypoint", "python", image, "-B", "-u", "-c", fixture_program(active)])
            deadline = time.monotonic() + 20
            while not (data / "ready").is_file():
                if time.monotonic() >= deadline:
                    raise RuntimeError("SyntheticFixtureDidNotStart")
                time.sleep(0.1)
            assert (data / "ijm2api.db-wal").stat().st_size > 32
            if query:
                baseline = guard.check(data / "ijm2api.db", {TASK}, data,
                    app / "service.py", SOURCE_SHA, proc)
                assert baseline["mode"] == "accepted_queries_only"
                (release / "query-resume-baseline.safe.json").write_text(json.dumps(baseline))
            should_reject = race or pause_timeout or (active and not query)
            trace = Trace(old_id, data, inject_race=race, expect_success=not should_reject, pause_timeout=pause_timeout)
            rejected = False
            try:
                receipt = controller.freeze_remove(service, image, release,
                    json.dumps([TASK]) if query else "", SOURCE_SHA, run=trace)
            except RuntimeError:
                rejected = True
                receipt = json.loads((release / "frozen-cutover.safe.json").read_text())
            assert rejected == should_reject, "UnexpectedCutoverDecision"
            assert trace.events[0] == "companion_joined_before_pause"
            if not pause_timeout:
                assert "frozen_companion_read" in trace.events
            if should_reject:
                state = json.loads(docker(["inspect", service]))[0]["State"]
                assert state["Running"] and not state["Paused"]
                assert receipt["original_unpaused_after_abort"] is True
                assert not receipt["original_removed_while_frozen"]
                assert "old_removed_while_frozen" not in trace.events
                with closing(sqlite3.connect((data / "ijm2api.db").as_uri() + "?mode=ro", uri=True)) as db:
                    assert db.execute("SELECT status FROM tasks WHERE id=?", (TASK,)).fetchone()[0] == "submitted"
                    if race:
                        assert db.execute("SELECT status FROM tasks WHERE id=?", (RACE_TASK,)).fetchone()[0] == "preparing"
            else:
                assert receipt["passed"] and receipt["original_removed_while_frozen"]
                assert docker(["inspect", service], optional=True) is None
                assert "abort_unpaused_original" not in trace.events
                time.sleep(0.2)
                assert {p.name:p.stat().st_size for p in data.glob('*.heartbeat')} == trace.frozen_heartbeats, "OldProcessExecutedAfterFreeze"
                assert (data / 'queued-after-freeze').is_file()
                assert not (data / 'executed-after-freeze').exists(), "QueuedRequestExecutedDuringForceRemoval"
                snapshot = release / "frozen.ijm2api.db"
                assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == receipt["frozen_backup_sha256"]
                with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as db:
                    assert db.execute("SELECT value FROM snapshot_probe").fetchall() == [("uncheckpointed-synthetic-WAL-row",)]
                    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
                with closing(sqlite3.connect((data / "ijm2api.db").as_uri() + "?mode=ro", uri=True)) as db:
                    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
            # PID 1 death can auto-remove its namespace companion asynchronously.
            cleanup_deadline = time.monotonic() + 5
            while trace.companion and docker(["inspect", trace.companion], optional=True) is not None:
                if time.monotonic() >= cleanup_deadline:
                    raise RuntimeError("SyntheticCompanionWasNotCleaned")
                time.sleep(0.1)
            return {"name": name, "passed": True, "rejected": rejected,
                "events": trace.events, "synthetic_only": True, "provider_calls": 0}
        finally:
            if trace and trace.companion:
                docker(["rm", "--force", trace.companion], optional=True)
            docker(["rm", "--force", service], optional=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12.15-slim")
    args = parser.parse_args()
    require_hosted()
    if docker(["image", "inspect", args.image], optional=True) is None:
        docker(["pull", args.image], timeout=180)
    controller = load_module("synthetic_frozen_cutover", "scripts/frozen_cutover.py")
    guard = load_module("synthetic_query_guard", "scripts/query_resume_guard.py")
    cases = [run_case(controller, guard, args.image, "accepted_query_WAL_success", active=True, query=True),
        run_case(controller, guard, args.image, "new_preparing_race_aborts_unpauses", active=True, query=True, race=True),
        run_case(controller, guard, args.image, "pause_timeout_after_effect_unpauses", active=True, query=True, pause_timeout=True),
        run_case(controller, guard, args.image, "default_idle_success", active=False, query=False),
        run_case(controller, guard, args.image, "default_active_stays_blocked", active=True, query=False)]
    print(json.dumps({"passed": True, "cases": cases, "count": len(cases), "provider_calls": 0}))


if __name__ == "__main__":
    main()
