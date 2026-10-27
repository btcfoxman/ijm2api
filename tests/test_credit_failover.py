"""A definitive credit rejection may switch accounts under one public task ID."""

import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.browser import BrowserError, BrowserRejected
from app.catalog import normalize_request
from app.service import Service
from app.store import Store


class CreditFailoverTests(unittest.TestCase):
    def test_submit_credit_rejection_switches_once_and_keeps_task_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "tasks.db"))
            service = Service(store)
            accounts = []
            clients = {}
            for index, (balance, priority) in enumerate(((100, 2), (90, 2), (1000, 1)), 1):
                account = store.upsert_account({"name": f"account-{index}",
                    "cdp_port": 19300 + index, "proxy_url": "socks5://proxy.test:20002",
                    "priority": priority})
                store.update_account(account["id"], {"status": "active", "balance": balance})
                accounts.append(account["id"])
                client = Mock()
                client.account_state.return_value = {"available_balance": balance}
                client.upload_inputs.return_value = {"images": [], "videos": [], "audios": []}
                client.submit.return_value = {"run_id": "second-run", "thread_id": "second-history"}
                client.poll.return_value = {"status": "succeeded",
                    "result_urls": ["https://example.test/result.mp4"]}
                clients[account["id"]] = client
            clients[accounts[0]].submit.side_effect = BrowserRejected(
                "Dreamina ret=1006: Not enough credits left or no relevant benefits")
            request = {"model": "sd-2-5", "prompt": "A blue sky", "duration": 4}
            store.create_task("ijm_credit_test", request, normalize_request(request))
            with patch.object(service.browser_runtime, "acquire", side_effect=lambda account: (account, False)), \
                 patch.object(service.browser_runtime, "release"), \
                 patch("app.service.DreaminaClient", side_effect=lambda account, **_: clients[account["id"]]):
                service._run("ijm_credit_test")
            task = store.task("ijm_credit_test")
            self.assertEqual(task["status"], "succeeded")
            self.assertEqual(task["id"], "ijm_credit_test")
            self.assertEqual(task["account_id"], accounts[1])
            self.assertEqual(task["attempted_account_ids"], [accounts[0], accounts[1]])
            clients[accounts[0]].submit.assert_called_once()
            clients[accounts[1]].submit.assert_called_once()
            clients[accounts[2]].submit.assert_not_called()
            service.stop()
            del service, store
            gc.collect()

    def test_second_credit_rejection_stops_without_third_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "tasks.db"))
            service = Service(store)
            clients = {}
            for index in range(3):
                account = store.upsert_account({"name": f"account-{index}",
                    "cdp_port": 19301 + index, "proxy_url": "socks5://proxy.test:20002"})
                store.update_account(account["id"], {"status": "active", "balance": 100 - index})
                client = Mock()
                client.account_state.return_value = {"available_balance": 100 - index}
                client.upload_inputs.return_value = {"images": [], "videos": [], "audios": []}
                client.submit.side_effect = BrowserRejected("Dreamina ret=1006: Not enough credits left")
                clients[account["id"]] = client
            request = {"model": "sd-2-5", "prompt": "A blue sky", "duration": 4}
            store.create_task("ijm_credit_limit", request, normalize_request(request))
            with patch.object(service.browser_runtime, "acquire", side_effect=lambda account: (account, False)), \
                 patch.object(service.browser_runtime, "release"), \
                 patch("app.service.DreaminaClient", side_effect=lambda account, **_: clients[account["id"]]):
                service._run("ijm_credit_limit")
            task = store.task("ijm_credit_limit")
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["error_code"], "INSUFFICIENT_CREDITS")
            self.assertEqual(len(task["attempted_account_ids"]), 2)
            self.assertEqual(sum(client.submit.call_count for client in clients.values()), 2)
            service.stop()
            del service, store
            gc.collect()

    def test_ambiguous_submit_outcome_never_switches_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "tasks.db"))
            service = Service(store)
            account = store.upsert_account({"name": "account-1", "cdp_port": 19301,
                "proxy_url": "socks5://proxy.test:20002"})
            store.update_account(account["id"], {"status": "active", "balance": 100})
            request = {"model": "sd-2-5", "prompt": "A blue sky", "duration": 4}
            store.create_task("ijm_ambiguous", request, normalize_request(request))
            client = Mock()
            client.account_state.return_value = {"available_balance": 100}
            client.upload_inputs.return_value = {"images": [], "videos": [], "audios": []}
            client.submit.side_effect = BrowserError("network closed during submission")
            with patch.object(service.browser_runtime, "acquire", side_effect=lambda value: (value, False)), \
                 patch.object(service.browser_runtime, "release"), \
                 patch("app.service.DreaminaClient", return_value=client):
                service._run("ijm_ambiguous")
            self.assertEqual(store.task("ijm_ambiguous")["error_code"],
                             "UPSTREAM_SUBMIT_OUTCOME_UNKNOWN")
            client.submit.assert_called_once()
            service.stop()
            del service, store
            gc.collect()
