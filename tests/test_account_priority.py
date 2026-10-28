"""Account priority must preserve the existing balance and ID tie breakers."""

import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.service import Service
from app.store import Store


class AccountPriorityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(gc.collect)
        self.store = Store(str(Path(directory.name) / "tasks.db"))

    def account(self, name, balance, priority=0):
        saved = self.store.upsert_account({"execution_mode": "browser", "name": name, "proxy_url": "socks5://proxy.test:20002",
                                           "priority": priority})
        self.store.update_account(saved["id"], {"status": "active", "balance": balance})
        return saved["id"]

    def test_priority_defaults_to_zero_and_survives_upsert(self):
        account_id = self.account("one", 100)
        self.assertEqual(self.store.account(account_id)["priority"], 0)
        self.store.update_account(account_id, {"priority": "3"})
        self.store.upsert_account({"execution_mode": "browser", "name": "one", "proxy_url": "socks5://proxy.test:20002"})
        self.assertEqual(self.store.account(account_id)["priority"], 3)
        for invalid in (-1, 1.5, True, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.update_account(account_id, {"priority": invalid})

    def test_highest_priority_then_balance_then_account_id(self):
        low = self.account("low", 100)
        first = self.account("first", 40, 2)
        second = self.account("second", 40, 2)
        third = self.account("third", 20, 2)
        service = Service(self.store)
        self.addCleanup(service.stop)
        self.assertEqual([service._reserve_account()["id"] for _ in range(4)],
                         [first, second, third, low])

    def test_existing_database_migrates_priorities_to_zero(self):
        with sqlite3.connect(self.store.path) as db:
            db.execute("ALTER TABLE accounts DROP COLUMN priority")
            db.execute("""INSERT INTO accounts(name,proxy_url,updated_at)
                          VALUES('legacy','socks5://proxy.test:20002',0)""")
        migrated = Store(str(self.store.path))
        self.assertEqual(migrated.accounts()[0]["priority"], 0)


if __name__ == "__main__":
    unittest.main()
