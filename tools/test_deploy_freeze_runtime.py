"""Hosted-only Docker smoke of the real cutover controller, with synthetic data.

No provider, service API, credentials, CLI executable, or application is started.
The fixtures cover both an open SQLite WAL and an idle WAL-mode database with
no sidecars. Run only in a GitHub-hosted Linux job with Docker available.
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
        # Every command here targets synthetic containers only. Preserve the
        # bounded cause so a rejected gate cannot hide its actual failure.
        error = RuntimeError("SyntheticDockerOperationFailed:" + args[0] + ":" + result.stderr[-3000:].strip())
        if isinstance(input, str) and input.startswith("# CGROUP_SINGLE_FILE_KILL\n"):
            error.returncode, error.command_stderr = result.returncode, result.stderr[-4096:]
        raise error
    if result.returncode:
        return None
    return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()


def save_diagnostic(value):
    directory = Path(os.environ["RUNNER_TEMP"]) / "ijm-freeze-smoke-evidence"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / ("diagnostic-" + uuid.uuid4().hex + ".json")
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
    destination.chmod(0o600)
    print("SYNTHETIC_FREEZE_DIAGNOSTIC " + json.dumps(value, sort_keys=True), flush=True)


def assert_frozen_heartbeats(trace, data, case, receipt):
    after = {p.name:p.stat().st_size for p in data.glob('*.heartbeat')}
    trace.record_heartbeats('assertion')
    before_remove = getattr(trace, 'pre_remove_heartbeats', None)
    after_remove = getattr(trace, 'post_remove_heartbeats', None)
    if (after != trace.frozen_heartbeats or before_remove != trace.frozen_heartbeats
            or after_remove != trace.frozen_heartbeats):
        save_diagnostic({'case': case, 'phase': 'frozen_heartbeat_integrity',
            'after_pause': trace.frozen_heartbeats, 'before_remove': before_remove,
            'after_remove': after_remove, 'final': after,
            'queued_request_executed': (data / 'executed-after-freeze').exists(),
            'heartbeat_stages': trace.heartbeat_stages,
            'receipt': receipt, 'events': trace.events, 'synthetic_only': True})
    assert before_remove == trace.frozen_heartbeats, 'OldProcessExecutedBeforeRemoval'
    assert after_remove == trace.frozen_heartbeats, 'OldProcessExecutedDuringRemoval'
    assert after == trace.frozen_heartbeats, 'OldProcessExecutedAfterFreeze'


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
marker.parent.mkdir(parents=True,exist_ok=True)
# Synthetic root-created directories must allow hosted uid cleanup after Docker.
marker.parent.parent.chmod(0o777);marker.parent.chmod(0o777)
marker.write_text('synthetic-profile')
db=sqlite3.connect(str(root/'ijm2api.db'))
(root/'ijm2api.db').chmod(0o600)
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
if not %r:
 # Exercise the real clone+backup peak above the former 128MiB tmpfs limit.
 db.execute('CREATE TABLE snapshot_padding(payload BLOB)')
 db.execute('INSERT INTO snapshot_padding VALUES(zeroblob(72*1024*1024))');db.commit()
 db.close()
 with (root/'ijm2api.db').open('rb') as reader:assert reader.read(20)[18:20]==bytes([2,2])
 assert not (root/'ijm2api.db-wal').exists() and not (root/'ijm2api.db-shm').exists()
heartbeat=%r
subprocess.Popen([sys.executable,'-B','-c',"name='child';"+heartbeat])
while not (root/'child.ready').is_file():time.sleep(0.01)
(root/'ready').write_text('ready')
exec(heartbeat,{'name':'parent'})
""" % (TASK, "submitted" if active else "succeeded", json.dumps(context), active, HEARTBEAT)


class Trace:
    def __init__(self, service, data, *, inject_race=False, expect_success=False, pause_timeout=False,
                 exporter=None, companion_start_timeout=False, release=None):
        self.service = service
        self.data = data
        self.inject_race = inject_race
        self.expect_success = expect_success
        self.pause_timeout = pause_timeout
        self.companion_start_timeout = companion_start_timeout
        self.exporter = exporter
        self.release = release
        self.paused = False
        self.companion = None
        self.killer = None
        self.heartbeat_stages = {}
        self.frozen_read_count = 0
        self.events = []
        self.frozen_heartbeats = None
        self.kill_commands = 0

    def export_backup(self, companion_id, destination):
        assert self.paused and companion_id == self.companion
        self.events.append("frozen_binary_backup_export")
        self.exporter(companion_id, destination)
        self.record_heartbeats('export')

    def record_heartbeats(self, stage):
        self.heartbeat_stages[stage] = {
            'sizes': {p.name:p.stat().st_size for p in self.data.glob('*.heartbeat')},
            'queued_marker': (self.data / 'queued-after-freeze').exists(),
            'executed_marker': (self.data / 'executed-after-freeze').exists()}

    def __call__(self, args, *, input=None, timeout=30):
        assert args[0] == "docker"
        command = args[1:]
        if command[0] == 'update':
            assert command[-1] == self.service
            assert command[1] in {'--restart=no', '--restart=unless-stopped'}
            value = docker(command, input=input, timeout=timeout)
            self.events.append('restart_policy_suppressed' if command[1] == '--restart=no' else 'abort_restart_policy_restored')
            return value
        if command[0] == "run":
            assert not self.paused and "--read-only" in command
            assert timeout == 90
            labels = [command[index + 1] for index, value in enumerate(command) if value == '--label']
            assert len(labels) == 2 and labels[0] == 'io.ijm2api.frozen-companion.owner=' + self.service
            role = labels[1].removeprefix('io.ijm2api.frozen-companion.role=')
            assert role in {'guard', 'killer'}
            assert command.count('--cap-drop') == 1
            assert command[command.index('--cap-drop') + 1] == 'ALL'
            assert not {'--privileged', 'DAC_OVERRIDE', 'SYS_ADMIN', 'SYS_PTRACE'}.intersection(command)
            assert command[command.index("--network") + 1] == "none"
            mounts = [command[index + 1] for index, value in enumerate(command) if value == '--mount']
            if role == 'guard':
                assert command.count('--cap-add') == 1 and command[command.index('--cap-add') + 1] == 'DAC_READ_SEARCH'
                assert command[command.index('--volumes-from') + 1] == self.service + ':ro'
                assert command[command.index('--pid') + 1] == 'container:' + self.service
                assert not mounts
            else:
                assert '--pid' not in command and '--volumes-from' not in command and '--cap-add' not in command
                assert command[command.index('--cgroupns') + 1] == 'host'
                assert len(mounts) == 6 and sum(not value.endswith(',readonly') for value in mounts) == 1
                assert sum('/proc/' in value and ',dst=/run/ijm-original-process/' in value and value.endswith(',readonly') for value in mounts) == 2
                assert command[-5:-2] == ['-I', '-S', '-B']
            value = docker(command, input=input, timeout=timeout)
            if role == 'guard': self.companion = value
            else: self.killer = value
            actual = json.loads(docker(['inspect', value]))[0]
            persistent = [mount for mount in actual['Mounts'] if mount['Destination'] != '/tmp']
            if role == 'guard':
                assert all(not mount['RW'] for mount in persistent)
                self.events.append('companion_joined_before_pause')
            else:
                assert len(persistent) == 6 and [mount['Destination'] for mount in persistent if mount['RW']] == ['/run/ijm-frozen-cgroup/cgroup.kill']
                assert actual['HostConfig']['PidMode'] == '' and not actual['HostConfig']['CapAdd']
                self.events.append('isolated_killer_joined_before_pause')
            if self.companion_start_timeout and role == 'guard':
                actual = json.loads(docker(['inspect', value]))[0]
                assert actual['State']['Running'] and not actual['State'].get('Paused')
                assert actual['Config']['Labels']['io.ijm2api.frozen-companion.owner'] == self.service
                self.events.append('companion_started_then_run_return_lost')
                self.companion_start_timeout = False
                raise subprocess.TimeoutExpired(args, timeout)
            return value
        if command[0] == "pause":
            assert self.companion and self.killer and command[1] == self.service
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
                self.record_heartbeats('after_pause')
                (self.data / 'queued-after-freeze').touch()
                self.events.append("synthetic_request_queued_while_frozen")
            return value
        if command[0] == "exec":
            if input and input.startswith('# ORIGINAL_PID_NAMESPACE_WITNESS\n'):
                assert command[2] == self.companion and not self.paused and self.killer is None and command[-5:] == ['python', '-I', '-S', '-B', '-']
                return docker(command, input=input, timeout=timeout)
            if command[2] == self.service and input and 'SnapshotSizingDatabaseMissing' in input:
                assert not self.paused and self.companion is None and 'sqlite' not in input
                return docker(command, input=input, timeout=timeout)
            if input and 'CGROUP_SINGLE_FILE_ACCESS' in input:
                assert not self.paused and command[2] == self.killer
                self.events.append('single_file_access_checked_before_pause')
                return docker(command, input=input, timeout=timeout)
            if input and 'CGROUP_SINGLE_FILE_KILL' in input:
                assert self.paused and command[2] == self.killer and self.frozen_read_count == 2
                assert command[-5:] == ['python', '-I', '-S', '-B', '-']
                self.kill_commands += 1
                assert self.kill_commands == 1
                manifest = self.release / 'frozen-prekill.safe.json'
                attempt = json.loads((self.release / 'frozen-kill-attempt.safe.json').read_text())
                assert attempt['kill_attempted'] is True and attempt['automatic_kill_replay_allowed'] is False
                assert attempt['prekill_manifest_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
                with (self.release / 'frozen.ijm2api.db').open('rb') as reader:
                    assert hashlib.file_digest(reader, 'sha256').hexdigest() == json.loads(manifest.read_text())['frozen_backup_sha256']
                self.events.append('durable_evidence_verified_before_kill')
                self.pre_remove_heartbeats = {p.name:p.stat().st_size for p in self.data.glob('*.heartbeat')}
                self.record_heartbeats('before_cgroup_kill')
                self.events.append('single_file_cgroup_kill_once')
                return docker(command, input=input, timeout=timeout)
            assert self.paused and command[2] == self.companion
            self.events.append("frozen_companion_read")
            value = docker(command, input=input, timeout=timeout)
            self.frozen_read_count += 1
            self.record_heartbeats('first_guard' if self.frozen_read_count == 1 else 'final_guard')
            return value
        if command[0] == "unpause":
            assert command[1] == self.service
            self.events.append("abort_unpaused_original")
        if command[0] == "rm" and command[-1] == self.service:
            assert command == ['rm', self.service], 'ForceRemovalOfOriginalForbidden'
            assert self.paused
            assert self.kill_commands == 1
            original = json.loads(docker(['inspect', self.service]))[0]
            assert original['State']['Running'] is False and original['State']['Pid'] == 0
            assert original['HostConfig']['RestartPolicy'] == {'Name': 'no', 'MaximumRetryCount': 0}
            self.events.append("old_removed_while_frozen")
            value = docker(command, input=input, timeout=timeout)
            self.post_remove_heartbeats = {p.name:p.stat().st_size for p in self.data.glob('*.heartbeat')}
            self.record_heartbeats('after_remove')
            return value
        if command[:2] == ["rm", "--force"] and command[2] == self.companion and not self.paused:
            self.events.append('owned_startup_companion_cleanup')
        if command[:2] == ["rm", "--force"] and command[2] == self.killer:
            self.events.append('owned_isolated_killer_cleanup')
        return docker(command, input=input, timeout=timeout)


def query_fixture(image, data, sql, parameters=(), *, service=None):
    """Read the root-owned 0600 fixture without relaxing its real permissions."""
    program = ("import json,sqlite3\ndb=sqlite3.connect('file:/app/data/ijm2api.db?mode=ro',uri=True)\n"
        "db.execute('PRAGMA query_only=ON')\nprint(json.dumps(db.execute(" + repr(sql) + "," + repr(parameters) + ").fetchall()))")
    if service:
        args = ['exec', service, 'python', '-B', '-c', program]
    else:
        # The original remains read-only even when missing WAL sidecars. A
        # private clone lets SQLite initialize its own disposable WAL index.
        program = ("import json,pathlib,shutil,sqlite3,tempfile\n"
            "with tempfile.TemporaryDirectory(dir='/tmp') as temporary:\n"
            " root=pathlib.Path('/app/data');clone=pathlib.Path(temporary)\n"
            " for name in ('ijm2api.db','ijm2api.db-wal'):\n"
            "  if (root/name).is_file():shutil.copyfile(root/name,clone/name)\n"
            " db=sqlite3.connect((clone/'ijm2api.db').as_uri()+'?mode=ro',uri=True)\n"
            " db.execute('PRAGMA query_only=ON')\n print(json.dumps(db.execute(" + repr(sql) + "," + repr(parameters) + ").fetchall()))")
        args = ['run', '--rm', '--pull', 'never', '--network', 'none', '--read-only', '--user', '0:0',
            '--cap-drop', 'ALL', '--cap-add', 'DAC_READ_SEARCH', '--security-opt', 'no-new-privileges',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m,mode=0700',
            '--mount', 'type=bind,source=' + str(data) + ',target=/app/data,readonly',
            '--entrypoint', 'python', image, '-B', '-c', program]
    return json.loads(docker(args))


def fixture_fingerprint(service):
    program = ("import hashlib,json,pathlib\nroot=pathlib.Path('/app/data');facts={}\n"
        "for name in ('ijm2api.db','ijm2api.db-wal','canvas-cli/account-1/credential-profile'):\n"
        " path=root/name\n"
        " if path.is_file():\n"
        "  with path.open('rb') as reader:facts[name]=hashlib.file_digest(reader,'sha256').hexdigest()\n"
        " else:facts[name]=None\n"
        "print(json.dumps(facts))")
    return json.loads(docker(['exec', service, 'python', '-B', '-c', program]))


def run_case(controller, guard, image, name, *, active, query, race=False, pause_timeout=False,
             companion_start_timeout=False):
    service = "ijm-freeze-smoke-" + uuid.uuid4().hex[:16]
    trace = None
    with tempfile.TemporaryDirectory(prefix="ijm-freeze-smoke-") as directory:
        root = Path(directory)
        data, app, release, proc = (root / value for value in ("data", "app", "release", "proc"))
        for folder in (data, app, release, proc):
            folder.mkdir(mode=0o700)
        # Reproduce the live cross-uid boundary: owner-only data, root-owned
        # 0600 database. Only the original dummy may bypass write permissions.
        data.chmod(0o700)
        app.chmod(0o755)
        (app / "service.py").write_bytes(SERVICE_SOURCE)
        try:
            old_id = docker(["run", "--detach", "--name", service, "--pull", "never",
                '--restart', 'unless-stopped',
                "--network", "none", "--read-only", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "DAC_OVERRIDE",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m,mode=0700",
                "--security-opt", "no-new-privileges", "--memory", "128m", "--pids-limit", "32",
                "--mount", "type=bind,source=" + str(data) + ",target=/app/data",
                "--mount", "type=bind,source=" + str(app) + ",target=/app/app,readonly",
                "--entrypoint", "python", image, "-B", "-u", "-c", fixture_program(active)])
            deadline = time.monotonic() + 20
            while not (data / "ready").is_file():
                if time.monotonic() >= deadline:
                    save_diagnostic({"case": name, "phase": "fixture_start",
                        "synthetic_container_logs": (docker(["logs", "--tail", "40", service], optional=True) or "")[-3000:]})
                    raise RuntimeError("SyntheticFixtureDidNotStart")
                time.sleep(0.1)
            if active:
                assert (data / "ijm2api.db-wal").stat().st_size > 32
            else:
                assert not (data / "ijm2api.db-wal").exists() and not (data / "ijm2api.db-shm").exists()
                assert (data / 'ijm2api.db').stat().st_size > 64 * 1024 * 1024
            if query:
                baseline = json.loads(docker(['exec', '-i', service, 'python', '-B', '-'],
                    input=controller.companion_program(json.dumps([TASK]), SOURCE_SHA)))
                assert baseline["mode"] == "accepted_queries_only"
                (release / "query-resume-baseline.safe.json").write_text(json.dumps(baseline))
            startup_before = fixture_fingerprint(service) if companion_start_timeout else None
            heartbeat_before = {p.name: p.stat().st_size for p in data.glob('*.heartbeat')}
            should_reject = race or pause_timeout or companion_start_timeout or (active and not query)
            trace = Trace(old_id, data, inject_race=race, expect_success=not should_reject,
                pause_timeout=pause_timeout, exporter=controller.export_frozen_backup,
                companion_start_timeout=companion_start_timeout, release=release)
            rejected = False
            original_error = None
            try:
                receipt = controller.freeze_remove(service, image, release,
                    json.dumps([TASK]) if query else "", SOURCE_SHA, run=trace, exporter=trace.export_backup)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                rejected = True
                original_error = error
                receipt_file = release / "frozen-cutover.safe.json"
                receipt = json.loads(receipt_file.read_text()) if receipt_file.is_file() else {}
                save_diagnostic({"case": name, "phase": "freeze_remove", "error_type": type(error).__name__,
                    "error_message": str(error)[-4000:], "receipt": receipt, "events": trace.events,
                    "expected_rejection": should_reject, "synthetic_only": True})
            assert rejected == should_reject, "UnexpectedCutoverDecision"
            assert trace.events[0] == 'restart_policy_suppressed'
            assert trace.events.index('companion_joined_before_pause') > trace.events.index('restart_policy_suppressed')
            if not pause_timeout and not companion_start_timeout:
                assert "frozen_companion_read" in trace.events
            if should_reject:
                state = json.loads(docker(["inspect", service]))[0]["State"]
                assert state["Running"] and not state["Paused"]
                assert json.loads(docker(['inspect', service]))[0]['HostConfig']['RestartPolicy'] == {'Name': 'unless-stopped', 'MaximumRetryCount': 0}
                assert receipt['original_restart_policy_restored_after_abort'] is True
                assert trace.kill_commands == 0
                assert not receipt["original_removed_while_frozen"]
                assert "old_removed_while_frozen" not in trace.events
                if companion_start_timeout:
                    assert isinstance(original_error, subprocess.TimeoutExpired)
                    assert not trace.paused and not receipt['original_unpaused_after_abort']
                    assert not {'old_paused', 'abort_unpaused_original', 'frozen_companion_read'}.intersection(trace.events)
                    assert 'owned_startup_companion_cleanup' in trace.events
                    assert len(heartbeat_before) == 2 and all(heartbeat_before.values())
                    heartbeat_deadline = time.monotonic() + 3
                    while not all((data / name).stat().st_size > size for name, size in heartbeat_before.items()):
                        if time.monotonic() >= heartbeat_deadline:
                            raise RuntimeError('OriginalHeartbeatsStoppedBeforeAnyPause')
                        time.sleep(0.05)
                    assert fixture_fingerprint(service) == startup_before
                else:
                    assert receipt["original_unpaused_after_abort"] is True
                    assert query_fixture(image, data, "SELECT status FROM tasks WHERE id=?", (TASK,), service=service) == [["submitted"]]
                    if race:
                        assert query_fixture(image, data, "SELECT status FROM tasks WHERE id=?", (RACE_TASK,), service=service) == [["preparing"]]
            else:
                assert receipt["passed"] and receipt["original_removed_while_frozen"]
                assert receipt['kill_attempted'] is True and trace.kill_commands == 1
                assert receipt['cgroup_death']['original_pid_gone'] is True
                assert receipt['cgroup_death']['empty_proof'] in {'held_events_populated_zero', 'held_original_cgroup_deleted'}
                assert 'abort_restart_policy_restored' not in trace.events
                prekill = release / 'frozen-prekill.safe.json'
                attempted = json.loads((release / 'frozen-kill-attempt.safe.json').read_text())
                assert hashlib.sha256(prekill.read_bytes()).hexdigest() == attempted['prekill_manifest_sha256']
                assert json.loads(prekill.read_text())['frozen_backup_sha256'] == receipt['frozen_backup_sha256']
                assert json.loads((release / 'frozen-death.safe.json').read_text())['kill_attempts'] == 1
                assert docker(["inspect", service], optional=True) is None
                assert "abort_unpaused_original" not in trace.events
                time.sleep(0.2)
                assert_frozen_heartbeats(trace, data, name, receipt)
                assert (data / 'queued-after-freeze').is_file()
                assert not (data / 'executed-after-freeze').exists(), "QueuedRequestExecutedDuringCgroupKill"
                snapshot = release / "frozen.ijm2api.db"
                with snapshot.open('rb') as reader:
                    assert hashlib.file_digest(reader, 'sha256').hexdigest() == receipt["frozen_backup_sha256"]
                with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as db:
                    assert db.execute("SELECT value FROM snapshot_probe").fetchall() == [("uncheckpointed-synthetic-WAL-row",)]
                    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
                assert query_fixture(image, data, "SELECT COUNT(*) FROM tasks") == [[1]]
                if not active:
                    assert not (data / "ijm2api.db-wal").exists() and not (data / "ijm2api.db-shm").exists()
                    assert snapshot.stat().st_size > 64 * 1024 * 1024
            capacity = receipt.get('snapshot_capacity') or {}
            frozen = (receipt.get('frozen_guard') or {}).get('source_manifest') or {}
            # PID 1 death can auto-remove its namespace companion asynchronously.
            cleanup_deadline = time.monotonic() + 5
            while any(helper and docker(["inspect", helper], optional=True) is not None for helper in (trace.companion, trace.killer)):
                if time.monotonic() >= cleanup_deadline:
                    raise RuntimeError("SyntheticCompanionWasNotCleaned")
                time.sleep(0.1)
            return {"name": name, "passed": True, "rejected": rejected,
                "events": trace.events, "synthetic_only": True, "provider_calls": 0,
                "frozen_source_database_bytes": (frozen.get('ijm2api.db') or {}).get('size_bytes'),
                "frozen_backup_bytes": (release / 'frozen.ijm2api.db').stat().st_size if (release / 'frozen.ijm2api.db').is_file() else None,
                "tmpfs_budget_bytes": capacity.get('tmpfs_bytes'), "memory_limit_bytes": capacity.get('memory_bytes'),
                "kill_commands": trace.kill_commands, "cgroup_death": receipt.get('cgroup_death'),
                "isolated_killer_verified": (receipt.get('killer_identity') or {}).get('private_sibling_verified'),
                "restart_policy_restored_after_abort": receipt.get('original_restart_policy_restored_after_abort'),
                "source_layout": "uncheckpointed_WAL" if active else "WAL_mode_without_sidecars"}
        finally:
            if trace and trace.companion:
                docker(["rm", "--force", trace.companion], optional=True)
            if trace and trace.killer:
                docker(["rm", "--force", trace.killer], optional=True)
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
        run_case(controller, guard, args.image, "companion_start_timeout_after_effect_cleans_without_pause", active=True, query=True, companion_start_timeout=True),
        run_case(controller, guard, args.image, "default_idle_success", active=False, query=False),
        run_case(controller, guard, args.image, "default_active_stays_blocked", active=True, query=False)]
    # Only an explicitly selected diagnostic image enables this bounded stress.
    # The ordinary release gate remains the original six scenarios.
    if os.environ.get('SMOKE_ONLY_IMAGE_TAG'):
        for attempt in range(2, 6):
            cases.append(run_case(controller, guard, args.image,
                'default_idle_success_stress_' + str(attempt), active=False, query=False))
    print(json.dumps({"passed": True, "cases": cases, "count": len(cases), "provider_calls": 0}))


if __name__ == "__main__":
    main()
