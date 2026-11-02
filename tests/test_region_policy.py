"""Region policy is pure and cannot infer transport or image duration support."""

import copy
import ast
import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException

from app.region_policy import (DEFAULT_REGION_GENERATION_POLICY, account_requires_proxy,
    normalize_region, region_allows_request, region_denial_detail,
    validate_region_generation_policy)
from app.store import EXECUTION_MODES, Store


class RegionPolicyTests(unittest.TestCase):
    def policy(self, model="sd-2-0", minimum=5, maximum=10):
        policy = copy.deepcopy(DEFAULT_REGION_GENERATION_POLICY)
        policy["cn"]["models"] = {model: {"min_duration": minimum, "max_duration": maximum}}
        return policy

    def test_defaults_deny_cn_and_preserve_legacy_regions(self):
        request = {"model": "sd-2-0", "duration": 5}
        self.assertFalse(region_allows_request({"region": "cn"}, request, {}))
        for account in ({}, {"region": "us"}, {"region": "other"}):
            self.assertTrue(region_allows_request(account, request, {}))
        self.assertFalse(region_allows_request({"region": "unknown"}, request, {}))

    def test_region_and_backend_are_independent(self):
        for mode in ("canvas_cli", "browser", "jimeng_web"):
            self.assertFalse(region_allows_request({"region": "cn", "execution_mode": mode},
                                                 {"model": "sd-2-0", "duration": 5}, {}))
            self.assertEqual(account_requires_proxy({"execution_mode": mode}), mode != "jimeng_web")
        self.assertTrue(account_requires_proxy({"region": "cn"}))
        self.assertEqual(normalize_region(None), "other")
        with self.assertRaises(ValueError):
            normalize_region("CN")

    def test_exact_alias_and_duration_edges(self):
        settings = {"region_generation_policy": self.policy()}
        for duration, expected in ((4, False), (5, True), (10, True), (11, False), (True, False)):
            with self.subTest(duration=duration):
                request = {"model": "sd-2-0", "duration": duration}
                self.assertEqual(region_allows_request({"region": "cn"}, request, settings), expected)
        for model in ("sd-2-0-fast", "sd-2-0-1080p", "dreamina_seedance_40_pro"):
            self.assertFalse(region_allows_request({"region": "cn"},
                             {"model": model, "duration": 5}, settings))
        self.assertIn("not submitted", region_denial_detail({"region": "cn"},
                      {"model": "sd-2-0", "duration": 4}, settings))

    def test_image_rules_have_no_seconds_and_keep_normalized_alias_identity(self):
        policy = self.policy()
        policy["cn"]["models"] = {"seedream-5.0-pro-2k": {}}
        settings = {"region_generation_policy": policy}
        for duration in (0, None, 5):
            self.assertTrue(region_allows_request({"region": "cn"},
                {"kind": "image", "model": "SEEDREAM-5.0-PRO-2K", "duration": duration}, settings))
        self.assertFalse(region_allows_request({"region": "cn"},
            {"kind": "image", "model": "seedream-5.0-pro-4k", "duration": 0}, settings))
        policy["cn"]["models"]["seedream-5.0-pro-2k"] = {"min_duration": 4, "max_duration": 10}
        with self.assertRaises(ValueError):
            validate_region_generation_policy(policy)

    def test_invalid_replacements_are_rejected_without_mutating_input(self):
        cases = []
        for rule in ({}, {"min_duration": True, "max_duration": 10},
                     {"min_duration": 5.0, "max_duration": 10},
                     {"min_duration": 10, "max_duration": 5},
                     {"min_duration": 4, "max_duration": 16},
                     {"min_duration": 3, "max_duration": 10},
                     {"min_duration": 4, "max_duration": 10, "extra": 1}):
            policy = self.policy()
            policy["cn"]["models"]["sd-2-0"] = rule
            cases.append(policy)
        for alias in ("arbitrary-upstream-key", "seedream-5.0-lite-1.5k", "SD-2-0"):
            policy = self.policy()
            policy["cn"]["models"] = {alias: {}}
            cases.append(policy)
        cases.extend([{}, {**self.policy(), "unknown": {}}, None])
        policy = self.policy()
        policy["us"]["enabled"] = 1
        cases.append(policy)
        for candidate in cases:
            original = copy.deepcopy(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_region_generation_policy(candidate)
            self.assertEqual(candidate, original)
            self.assertFalse(region_allows_request({"region": "us"},
                {"model": "sd-2-0", "duration": 5}, {"region_generation_policy": candidate}))


class AccountRegionStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(gc.collect)
        self.store = Store(str(Path(folder.name) / "test.db"))

    def test_pending_jimeng_can_be_direct_and_defaults_cn(self):
        account = self.store.create_pending_account("jimeng", "", execution_mode="jimeng_web")
        self.assertEqual((account["region"], account["execution_mode"], account["proxy_url"]),
                         ("cn", "jimeng_web", ""))
        self.assertFalse(account["enabled"])
        for mode in ("canvas_cli", "browser"):
            with self.assertRaises(ValueError):
                self.store.create_pending_account(mode, "", execution_mode=mode, region="cn")
        international = self.store.create_pending_account("legacy", "socks5://proxy.test:20002")
        self.assertEqual((international["region"], international["execution_mode"]), ("other", "canvas_cli"))

    def test_account_region_round_trip_does_not_infer_backend(self):
        account = self.store.upsert_account({"name": "one", "execution_mode": "jimeng_web", "region": None})
        self.assertEqual(account["region"], "cn")
        self.store.update_account(account["id"], {"region": "us"})
        account = self.store.upsert_account({"name": "one"})
        self.assertEqual((account["region"], account["execution_mode"]), ("us", "jimeng_web"))
        account = self.store.update_account(account["id"], {"execution_mode": "canvas_cli", "region": "cn"})
        self.assertEqual((account["region"], account["execution_mode"]), ("cn", "canvas_cli"))
        with self.assertRaises(ValueError):
            self.store.update_account(account["id"], {"region": "invalid"})
        self.assertEqual(self.store.account(account["id"])["region"], "cn")

    def test_existing_database_adds_other_region(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("ALTER TABLE accounts DROP COLUMN region")
            db.execute("INSERT INTO accounts(name,proxy_url,updated_at) VALUES('legacy','proxy',0)")
        migrated = Store(str(self.store.path))
        self.assertEqual(migrated.accounts()[0]["region"], "other")


class AccountRegionApiGuardTests(unittest.TestCase):
    def guards(self, active=0):
        # Isolate the real guards without importing main's application lifespan,
        # opening its configured database or creating browser/runtime workers.
        tree = ast.parse((Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8"))
        names = {"_assert_account_mutable", "_validate_account_fields"}
        module = ast.Module(body=[item for item in tree.body if isinstance(item, ast.FunctionDef)
                                 and item.name in names], type_ignores=[])
        namespace = {"Any": object, "HTTPException": HTTPException, "normalize_region": normalize_region,
                     "EXECUTION_MODES": EXECUTION_MODES,
                     "auto_login": SimpleNamespace(busy=lambda _: False),
                     "canvas_auth": SimpleNamespace(busy=lambda _: False),
                     "browser_login": SimpleNamespace(active=None),
                     "service": SimpleNamespace(_active={7: active}),
                     "store": SimpleNamespace(active_account_count=Mock(return_value=0))}
        exec(compile(module, "main-account-guards", "exec"), namespace)
        return namespace

    def test_region_mutation_cannot_change_an_active_accounts_admission(self):
        guards = self.guards(active=1)
        account = {"id": 7, "status": "active", "region": "cn"}
        with self.assertRaises(HTTPException) as caught:
            guards["_assert_account_mutable"](account, {"region": "us"})
        self.assertEqual(caught.exception.status_code, 409)
        guards["_assert_account_mutable"](account, {"region": "cn"})
        guards["_assert_account_mutable"](account, {"name": "label only"})

    def test_invalid_fields_fail_before_proxy_or_browser_side_effects(self):
        validate = self.guards()["_validate_account_fields"]
        for body in ({"region": "unknown"}, {"region": True}, {"region": None}, {"execution_mode": "unknown"},
                     {"execution_mode": []}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate(body)
        for mode in EXECUTION_MODES:
            validate({"region": "us", "execution_mode": mode})


if __name__ == "__main__":
    unittest.main()
