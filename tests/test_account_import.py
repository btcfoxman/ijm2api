"""Credential imports remain private, bounded, and preserve account identities."""
import gc
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.account_import import AUTO_PROXY_PORTS, AccountImporter, parse_import_lines, validate_proxy
from app.store import Store


class AccountImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(gc.collect)
        self.store = Store(str(Path(directory.name) / "tasks.db"))
        self.importer = AccountImporter(self.store)

    def saved(self, text="person@example.test| secret "):
        report = self.importer.import_text(text)
        self.assertFalse(report["errors"])
        return self.store.account(report["results"][0]["account_id"], private=True)

    def test_parse_preserves_password_and_supports_delimiters(self):
        records, errors = parse_import_lines(
            " Person@Example.test | secret \nsecond@example.test\t\t secret\t\n"
            "third@example.test----pass ----http://proxy.test:20033\nlegacy|https://proxy.test:20034")
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["login_email"], "person@example.test")
        self.assertEqual(records[0]["login_password"], " secret ")
        self.assertEqual(records[1]["login_password"], "pass ")
        self.assertEqual(records[2]["kind"], "legacy")

    def test_parse_rejects_extra_columns_and_errors_do_not_echo_secrets(self):
        records, errors = parse_import_lines("person@example.test|secret-value|socks5://proxy.test:20001|extra\n"
            "second@example.test||socks5://proxy.test:20001\n"
            "third@example.test|secret-value|http://user:private-value@proxy.test:20001")
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 3)
        self.assertNotIn("secret-value", str(errors))
        self.assertNotIn("private-value", str(errors))

    def test_proxy_validation_never_rewrites_explicit_value(self):
        proxy = "socks5://proxy.example.test:20032"
        self.assertEqual(validate_proxy(proxy), proxy)
        for invalid in ("", "proxy.test:80", "http://proxy.test", "http://proxy.test:0",
                        "http://proxy.test:65536", "http://proxy.test:80/", "http://proxy.test:80?a=b",
                        "socks5h://proxy.test:80", "http://user:pass@proxy.test:80", "http://proxy.test:80\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_proxy(invalid)

    def test_new_credentials_disabled_until_login_and_persist_privately(self):
        saved = self.saved()
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["status"], "pending")
        self.assertTrue(saved["managed_profile"])
        self.assertEqual(saved["login_password"], " secret ")
        reopened = Store(str(self.store.path))
        self.assertEqual(reopened.account(saved["id"], private=True)["login_password"], " secret ")
        public = reopened.account(saved["id"])
        self.assertTrue(public["email_configured"])
        self.assertTrue(public["password_configured"])
        self.assertEqual(public["login_identifier"], "pe***@example.test")
        serialized = json.dumps(public)
        for secret in ("person@example.test", " secret ", '"login_password"', '"login_email"'):
            self.assertNotIn(secret, serialized)

    def test_default_pool_uses_all_32_configured_ports_in_order(self):
        self.assertEqual(len(AUTO_PROXY_PORTS), 32)
        report = self.importer.import_text("\n".join(f"person{i}@example.test|password" for i in range(33)))
        self.assertEqual(report["count"], 33)
        accounts = self.store.accounts(private=True)
        self.assertEqual([int(item["proxy_url"].rsplit(":", 1)[1]) for item in accounts[:32]], list(AUTO_PROXY_PORTS))
        self.assertEqual(accounts[32]["proxy_url"], accounts[0]["proxy_url"])

    def test_explicit_proxy_preserved_and_omission_reuses_existing(self):
        proxy = "https://proxy.example.test:20040"
        saved = self.saved(f"person@example.test|password|{proxy}")
        self.assertEqual(saved["proxy_url"], proxy)
        report = self.importer.import_text("PERSON@example.test|different password")
        self.assertEqual(report["count"], 1)
        updated = self.store.account(saved["id"], private=True)
        self.assertEqual(updated["proxy_url"], proxy)
        self.assertEqual(updated["login_password"], "different password")
        self.assertFalse(updated["enabled"])

    def test_different_explicit_proxy_cannot_change_existing_profile(self):
        saved = self.saved()
        report = self.importer.import_text("person@example.test|new secret|socks5://proxy.test:21000")
        self.assertEqual(report["count"], 0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(self.store.account(saved["id"], private=True), saved)
        self.assertNotIn("new secret", json.dumps(report))

    def test_case_insensitive_duplicates_do_not_enqueue_or_mutate(self):
        saved = self.saved()
        before = self.store.account(saved["id"], private=True)
        report = self.importer.import_text("PERSON@example.test| secret \nperson@EXAMPLE.test|different")
        self.assertEqual(report["count"], 0)
        self.assertEqual(report["duplicate_count"], 2)
        self.assertFalse(any(result["login_needed"] for result in report["results"]))
        self.assertEqual(self.store.accounts(private=True), [before])

    def test_active_account_cannot_change_credentials_but_duplicate_is_safe(self):
        saved = self.saved()
        self.store.update_account(saved["id"], {"status": "active", "enabled": True})
        before = self.store.account(saved["id"], private=True)
        rejected = self.importer.import_text("person@example.test|new password")
        self.assertEqual(rejected["count"], 0)
        self.assertEqual(self.store.account(saved["id"], private=True), before)
        duplicate = self.importer.import_text("person@example.test| secret ")
        self.assertEqual(duplicate["duplicate_count"], 1)
        self.assertEqual(self.store.account(saved["id"], private=True), before)

    def test_busy_callback_protects_pending_login(self):
        saved = self.saved()
        before = self.store.account(saved["id"], private=True)
        report = AccountImporter(self.store, login_busy=lambda account_id: account_id == saved["id"]).import_text(
            "person@example.test|new password")
        self.assertEqual(report["count"], 0)
        self.assertEqual(self.store.account(saved["id"], private=True), before)

    def test_explicitly_disabled_account_stays_disabled_after_reimport(self):
        saved = self.saved()
        self.store.update_account(saved["id"], {"enabled": False, "status": "disabled"})
        report = self.importer.import_text("person@example.test|new password")
        self.assertEqual(report["count"], 1)
        self.assertTrue(report["results"][0]["login_needed"])
        updated = self.store.account(saved["id"], private=True)
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["status"], "disabled")

    def test_external_profile_cannot_become_managed_through_import(self):
        saved = self.store.upsert_account({"name": "person@example.test", "cdp_port": 19304,
                                          "proxy_url": "socks5://proxy.test:20004"})
        before = self.store.account(saved["id"], private=True)
        report = self.importer.import_text("person@example.test|new password")
        self.assertEqual(report["count"], 0)
        self.assertEqual(self.store.account(saved["id"], private=True), before)

    def test_inflight_and_unknown_task_identity_are_protected(self):
        saved = self.saved()
        with sqlite3.connect(self.store.path) as db:
            db.execute("INSERT INTO tasks(id,account_id,model,status,request_json,normalized_json,created_at,updated_at) "
                       "VALUES('task',?,'sd-2-0-mini','submission_unknown','{}','{}',0,0)", (saved["id"],))
        before = self.store.account(saved["id"], private=True)
        self.assertEqual(self.store.active_account_count(saved["id"]), 1)
        self.assertEqual(self.importer.import_text("person@example.test|new password")["count"], 0)
        self.assertEqual(self.store.account(saved["id"], private=True), before)
        with self.assertRaises(ValueError):
            self.store.update_account(saved["id"], {"login_password": "other secret"})

    def test_legacy_names_preserved_and_cannot_adopt_other_identity_name(self):
        report = self.importer.import_text("manual-01|socks5://proxy.test:20001")
        self.assertEqual(report["count"], 1)
        self.assertFalse(report["results"][0]["login_needed"])
        name = "ijm-" + hashlib.sha256(b"person@example.test").hexdigest()[:12]
        self.store.upsert_account({"name": name, "proxy_url": "socks5://proxy.test:20001"})
        before = self.store.accounts(private=True)
        self.assertEqual(self.importer.import_text("person@example.test|secret")["count"], 0)
        self.assertEqual(self.store.accounts(private=True), before)

    def test_empty_password_never_clears_and_changed_password_resets_setup(self):
        saved = self.saved()
        self.store.update_account(saved["id"], {"watermark_ready": True, "agreement_ready": True, "setup_checked_at": 123})
        self.store.update_account(saved["id"], {"login_password": ""})
        self.assertEqual(self.store.account(saved["id"], private=True)["login_password"], " secret ")
        self.store.update_account(saved["id"], {"login_password": "new secret"})
        public = self.store.account(saved["id"])
        self.assertFalse(public["watermark_ready"])
        self.assertFalse(public["agreement_ready"])
        self.assertEqual(public["setup_checked_at"], 0)
        with self.assertRaises(ValueError):
            self.store.update_account(saved["id"], {"watermark_ready": "false"})

    def test_private_enable_intent_is_nullable_persistent_and_not_public(self):
        saved = self.saved()
        self.assertIsNone(saved["login_enable_after"])
        for intent in (False, True, 0, 1, None):
            with self.subTest(intent=intent):
                self.store.update_account(saved["id"], {"login_enable_after": intent})
                reopened = Store(str(self.store.path))
                self.assertEqual(reopened.account(saved["id"], private=True)["login_enable_after"], intent)
                self.assertNotIn("login_enable_after", reopened.account(saved["id"]))
                self.assertNotIn("login_enable_after", json.dumps(reopened.accounts()))
        for invalid in ("false", "true", 1.0, 0.0, 2, -1, {}, []):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.update_account(saved["id"], {"login_enable_after": invalid})
        self.assertIsNone(self.store.account(saved["id"], private=True)["login_enable_after"])

    def test_credential_change_clears_previous_enable_intent(self):
        saved = self.saved()
        self.store.update_account(saved["id"], {"login_enable_after": False})
        self.store.update_account(saved["id"], {"login_password": ""})
        self.store.update_account(saved["id"], {"login_password": " secret "})
        self.assertEqual(self.store.account(saved["id"], private=True)["login_enable_after"], 0)
        self.store.update_account(saved["id"], {"login_password": "replacement"})
        self.assertIsNone(self.store.account(saved["id"], private=True)["login_enable_after"])
        self.store.update_account(saved["id"], {"login_enable_after": True})
        report = self.importer.import_text("person@example.test|another password")
        self.assertEqual(report["count"], 1)
        self.assertIsNone(self.store.account(saved["id"], private=True)["login_enable_after"])
        self.store.update_account(saved["id"], {"login_enable_after": False})
        self.store.upsert_account({"name": saved["name"], "login_password": "fourth password"})
        self.assertIsNone(self.store.account(saved["id"], private=True)["login_enable_after"])

    def test_private_user_binding_persists_without_public_projection(self):
        saved = self.saved()
        self.assertEqual(saved["login_user_id"], "")
        public = self.store.update_account(saved["id"], {"login_user_id": "synthetic-user-12345"})
        self.assertNotIn("login_user_id", public)
        reopened = Store(str(self.store.path))
        self.assertEqual(reopened.account(saved["id"], private=True)["login_user_id"], "synthetic-user-12345")
        self.assertNotIn("synthetic-user-12345", json.dumps(reopened.accounts()))
        self.assertNotIn("login_user_id", json.dumps(reopened.accounts()))
        for invalid in (None, 12345, False, [], {}, "bad\x00id", "bad\nid", "x" * 257):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.update_account(saved["id"], {"login_user_id": invalid})
        self.assertEqual(self.store.account(saved["id"], private=True)["login_user_id"], "synthetic-user-12345")
        self.store.update_account(saved["id"], {"login_user_id": ""})
        self.assertEqual(self.store.account(saved["id"], private=True)["login_user_id"], "")

    def test_real_credential_changes_clear_user_binding_but_duplicates_keep_it(self):
        saved = self.saved()
        self.store.update_account(saved["id"], {"login_user_id": "synthetic-user-12345"})
        self.store.update_account(saved["id"], {"login_password": "", "login_email": "PERSON@example.test"})
        report = self.importer.import_text("person@example.test| secret ")
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(self.store.account(saved["id"], private=True)["login_user_id"], "synthetic-user-12345")
        for method, password in (("update", "second password"), ("import", "third password"), ("upsert", "fourth password")):
            with self.subTest(method=method):
                self.store.update_account(saved["id"], {"login_user_id": "synthetic-user-12345"})
                if method == "update":
                    self.store.update_account(saved["id"], {"login_password": password})
                elif method == "import":
                    self.assertEqual(self.importer.import_text("person@example.test|" + password)["count"], 1)
                else:
                    self.store.upsert_account({"name": saved["name"], "login_password": password})
                self.assertEqual(self.store.account(saved["id"], private=True)["login_user_id"], "")
        self.store.update_account(saved["id"], {"login_user_id": "synthetic-user-12345"})
        with self.assertRaises(ValueError):
            self.store.update_account(saved["id"], {"login_email": "different@example.test"})
        self.assertEqual(self.store.account(saved["id"], private=True)["login_user_id"], "synthetic-user-12345")

    def test_attaching_email_to_idle_legacy_account_clears_user_binding(self):
        saved = self.store.upsert_account({"name": "manual", "proxy_url": "socks5://proxy.test:20004"})
        self.store.update_account(saved["id"], {"login_user_id": "synthetic-user-12345"})
        self.store.update_account(saved["id"], {"login_email": "person@example.test", "login_password": "synthetic password"})
        self.assertEqual(self.store.account(saved["id"], private=True)["login_user_id"], "")

    def test_unconfigured_proxy_pool_fails_without_direct_account(self):
        report = AccountImporter(self.store, proxy_urls=()).import_text("person@example.test|secret")
        self.assertEqual(report["count"], 0)
        self.assertEqual(self.store.accounts(), [])

    def test_old_database_migrates_without_altering_account(self):
        saved = self.store.upsert_account({"name": "legacy", "proxy_url": "socks5://proxy.test:20001"})
        with sqlite3.connect(self.store.path) as db:
            db.execute("DROP INDEX accounts_login_email")
            for column in ("login_email", "login_password", "login_stage", "watermark_ready", "agreement_ready", "setup_checked_at", "login_enable_after", "login_user_id"):
                db.execute(f"ALTER TABLE accounts DROP COLUMN {column}")
        reopened = Store(str(self.store.path))
        public = reopened.account(saved["id"])
        self.assertEqual(public["name"], "legacy")
        self.assertEqual(public["proxy_url"], saved["proxy_url"])
        self.assertFalse(public["password_configured"])
        self.assertFalse(public["agreement_ready"])
        self.assertFalse(public["watermark_ready"])
        self.assertIsNone(reopened.account(saved["id"], private=True)["login_enable_after"])
        self.assertEqual(reopened.account(saved["id"], private=True)["login_user_id"], "")

    def test_scheduling_rows_are_raw_readonly_and_inflight_is_unbounded(self):
        normalized = '{"reference_video_seconds":4}'
        request = '{"duration":4}'
        rows = [(f"success-{index}", 1, "succeeded", normalized, request, 3, 2, index + 1, index + 1, "")
                for index in range(1001)]
        rows.extend((f"inflight-{index}", 1, ("preparing", "submitting", "submitted", "running")[index % 4],
                     normalized, request, 3, None, -10, None, "") for index in range(1002))
        rows.extend([
            ("queued-bound", 1, "queued", normalized, request, 3, None, 0, None, ""),
            ("unknown-failed", 1, "failed", normalized, request, 3, None, 0, 1, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"),
            ("unbound", None, "preparing", normalized, request, 3, None, 0, None, ""),
            ("success-without-actual", 1, "succeeded", normalized, request, 3, None, 9999, 9999, ""),
            ("failed-actual", 1, "failed", normalized, request, 3, 2, 9999, 9999, ""),
            ("expired", 1, "expired", normalized, request, 3, None, 0, 1, ""),
            ("cancelled", 1, "cancelled", normalized, request, 3, None, 0, 1, ""),
            ("success-stale-unknown", 1, "succeeded", normalized, request, 3, None, 0, 1, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"),
            ("cancelled-stale-unknown", 1, "cancelled", normalized, request, 3, None, 0, 1, "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN"),
        ])
        with sqlite3.connect(self.store.path) as db:
            db.executemany("INSERT INTO tasks(id,account_id,model,status,normalized_json,request_json,estimated_cost,"
                           "actual_cost,created_at,updated_at,completed_at,error_code) VALUES(?,?,'synthetic-model',?,?,?,?,?,?,1,?,?)", rows)
        connection = self.store._connect()
        queries = []
        connection.set_trace_callback(queries.append)
        try:
            with patch.object(self.store, "_connect", return_value=connection), patch.object(
                    self.store, "_task", side_effect=AssertionError("scheduler must avoid public task projection")):
                value = self.store.scheduling_rows()
            self.assertEqual(connection.total_changes, 0)
        finally:
            connection.close()
        self.assertTrue(all(query.lstrip().upper().startswith("SELECT ") for query in queries))
        self.assertEqual(len(value["history"]), 1000)
        self.assertEqual(value["history"][0]["id"], "success-1000")
        self.assertEqual(value["history"][-1]["id"], "success-1")
        self.assertEqual(len(value["inflight"]), 1004)
        self.assertIn("unknown-failed", {row["id"] for row in value["inflight"]})
        self.assertTrue(all(row["normalized_json"] == normalized and row["request_json"] == request
                            for row in value["history"] + value["inflight"]))

    @unittest.skipUnless(shutil.which("node"), "Node is required for the UI helper contract")
    def test_ui_progress_retry_and_safe_results(self):
        script = r"""
const fs = require('fs'); const vm = require('vm'); const assert = require('assert');
const elements = new Map();
const element = (id) => { if (!elements.has(id)) elements.set(id, {textContent:'', innerHTML:'', hidden:true, classList:{toggle(){}}}); return elements.get(id); };
const context = vm.createContext({document:{querySelector:element}, window:{}, console});
const source = fs.readFileSync('app/static/app.js','utf8');
vm.runInContext(source.slice(0,source.indexOf('function mediaFromTask')),context);
vm.runInContext(`state.accounts = [{id:1,name:'<script>unsafe</script>',login_identifier:'pe***@example.test',status:'logging_in',enabled:false,password_configured:true,login_stage:'queued',max_concurrency:1}, {id:2,name:'manual',status:'pending',enabled:false,max_concurrency:1}]; state.accountFilter = 'disabled'; renderAccounts();`,context);
const html = element('#accountsBody').innerHTML;
assert(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
assert(!html.includes('<script>unsafe'));
assert.strictEqual((html.match(/data-action="auto-login"/g)||[]).length,1);
assert(html.includes('data-action="auto-login" data-id="1"'));
assert(html.includes('等待自动登录'));
vm.runInContext(`state.batchReport={input_count:1,count:1,duplicate_count:0,login_started_count:1,results:[{line:1,account_id:1,login_identifier:'pe***@example.test',message:'saved'}]}; state.loginJobs=[{account_id:1,phase:'ready',message:'<img onerror=unsafe>'}]; renderBatchResult();`,context);
assert(element('#batchResult').textContent.includes('登录及设置完成'));
assert(element('#batchResult').textContent.includes('<img onerror=unsafe>'));
assert.strictEqual(element('#batchResult').innerHTML,'');
assert(source.includes('api("/api/accounts/login-results")'));
assert(source.includes('form.elements.namedItem("text").value = ""'));
"""
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
