"""Hermetic deployment-gate tests: temporary SQLite/proc, never CLI or HTTP."""
import copy
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("query_resume_guard", ROOT / "scripts/query_resume_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class QueryResumeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "ijm2api.db"
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.source = self.root / "service.py"
        self.source.write_text("# immutable startup query recovery\n", encoding="utf-8")
        self.source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.task_id = "ijm_" + "a" * 16
        self.context = {"backend": "canvas_cli", "task_id": self.task_id, "account_id": 1,
            "profile": "fixture-profile", "expected_user_id": "fixture-user", "phase": "accepted",
            "submission_phase": "submitted", "kind": "video", "output_count": 1,
            "project_id": "00000000-0000-4000-8000-000000000001", "node_id": "node_0123456789",
            "submit_id": "00000000-0000-4000-8000-000000000002",
            "resource_ids": ["00000000-0000-4000-8000-000000000003"], "observation_deadline": 5000,
            "model": "seedance_2.0_mini", "mode": "m2v"}
        marker = self.root / "canvas-cli/account-1/credential-profile"
        marker.parent.mkdir(parents=True)
        marker.write_text("fixture-profile", encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("""CREATE TABLE accounts(id INTEGER,enabled INTEGER,status TEXT,execution_mode TEXT,
                cli_auth_status TEXT,login_user_id TEXT,cli_user_id TEXT,proxy_url TEXT);
                CREATE TABLE tasks(id TEXT,status TEXT,account_id INTEGER,execution_backend TEXT,
                upstream_context_json TEXT,error_code TEXT,request_json TEXT,normalized_json TEXT,run_id TEXT,thread_id TEXT);""")
            db.execute("INSERT INTO accounts VALUES(1,1,'active','canvas_cli','ready','fixture-user','fixture-user','http://127.0.0.1:9')")
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)", (self.task_id,"submitted",1,"canvas_cli",json.dumps(self.context),"UPSTREAM_SUBMIT_OUTCOME_UNKNOWN","{}","{}","",""))

    def tearDown(self):
        self.temporary.cleanup()

    def run_guard(self, **kwargs):
        return guard.check(self.database, {self.task_id}, self.root, self.source, self.source_sha, self.proc, now=1000, **kwargs)

    def change_context(self, context):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE tasks SET upstream_context_json=? WHERE id=?", (json.dumps(context), self.task_id))

    def process(self, pair, profile="fixture-profile"):
        folder = self.proc / "123"
        folder.mkdir(exist_ok=True)
        args = ["/usr/local/bin/dreamina-canvas", "--format", "json", "--non-interactive", "--profile", profile, *pair]
        (folder / "cmdline").write_bytes(b"\0".join(value.encode() for value in args) + b"\0")

    def test_accepted_original_binding_allowed_and_database_unchanged(self):
        before = self.database.read_bytes()
        result = self.run_guard()
        self.assertTrue(result["passed"])
        self.assertEqual(result["mode"], "accepted_queries_only")
        self.assertEqual(result["database_writes"], 0)
        self.assertEqual(before, self.database.read_bytes())

    def test_unknown_or_dispatching_or_empty_submit_error_rejected(self):
        for update in ({"phase":"unknown"}, {"submission_phase":"run_dispatching"}, {"submit_error":{}}, {"submit_error":None}):
            with self.subTest(update=update):
                self.change_context({**self.context, **update})
                with self.assertRaises(ValueError): self.run_guard()

    def test_missing_or_mismatched_resource_and_account_rejected(self):
        for update in ({"resource_ids":[]}, {"resource_ids":["invalid"]}, {"account_id":2}, {"task_id":"ijm_"+"b"*16}, {"expected_user_id":"other"}, {"node_id":"invalid"}):
            with self.subTest(update=update):
                self.change_context({**self.context, **update})
                with self.assertRaises(ValueError): self.run_guard()

    def test_profile_epoch_and_startup_source_change_rejected(self):
        marker = self.root / "canvas-cli/account-1/credential-profile"
        marker.write_text("other", encoding="utf-8")
        with self.assertRaises(ValueError): self.run_guard()
        marker.write_text("fixture-profile", encoding="utf-8")
        self.source.write_text("# changed startup\n", encoding="utf-8")
        with self.assertRaises(ValueError): self.run_guard()

    def test_new_active_or_preparing_is_never_exempted(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE tasks SET status='preparing'")
        with self.assertRaises(ValueError): self.run_guard()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE tasks SET status='submitted'")
            db.execute("INSERT INTO tasks SELECT ?,status,account_id,execution_backend,upstream_context_json,error_code,request_json,normalized_json,run_id,thread_id FROM tasks", ("ijm_"+"b"*16,))
        with self.assertRaises(ValueError): self.run_guard()

    def test_poll_audit_changes_keep_digest_but_original_binding_changes_it(self):
        first = self.run_guard()["binding_sha256"]
        value = copy.deepcopy(self.context)
        value.update(last_query_error={"code":"UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"}, consecutive_submission_absent=2)
        self.change_context(value)
        self.assertEqual(first, self.run_guard()["binding_sha256"])
        value["resource_ids"] = ["00000000-0000-4000-8000-000000000004"]
        self.change_context(value)
        self.assertNotEqual(first, self.run_guard()["binding_sha256"])

    def test_only_bound_readonly_CLI_allowed(self):
        self.process(["operation", "status", "opaque-fixture"])
        self.assertEqual(self.run_guard()["query_processes"], 1)
        for pair in (["node","run"], ["node","confirm"], ["resource","upload"], ["resource","download"], ["auth","login"]):
            with self.subTest(pair=pair):
                self.process(pair)
                with self.assertRaises(ValueError): self.run_guard()
        self.process(["operation","status"], "different-profile")
        with self.assertRaises(ValueError): self.run_guard()

    def test_login_deadline_and_bad_input_fail_closed(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE accounts SET cli_auth_status='authorizing'")
        with self.assertRaises(ValueError): self.run_guard()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE accounts SET cli_auth_status='ready'")
        self.change_context({**self.context, "observation_deadline":999})
        with self.assertRaises(ValueError): self.run_guard()
        for raw in ('[]','{}','["bad"]',json.dumps([self.task_id,self.task_id])):
            with self.subTest(raw=raw), self.assertRaises(ValueError): guard.parse_expected(raw)

    def test_default_deployment_remains_idle_manual_input_only(self):
        workflow=(ROOT/'.github/workflows/deploy-test.yml').read_text(encoding='utf-8')
        script=(ROOT/'scripts/deploy_pre.sh').read_text(encoding='utf-8')
        self.assertIn("github.event_name == 'workflow_dispatch'",workflow)
        self.assertIn('default: \'\'',workflow)
        self.assertIn('test "${GITHUB_EVENT_NAME:-}" = workflow_dispatch',script)
        self.assertIn('active="$(active_commands)"',script)
        self.assertIn('test "$(active_commands)" -eq 0',script)
        self.assertIn('--no-build --pull never',script)
        self.assertNotIn('docker build',script)


if __name__ == "__main__":
    unittest.main()
