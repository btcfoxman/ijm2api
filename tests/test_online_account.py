"""Online accounts retain one profile and keep task browsers warm."""

import asyncio
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.browser import BrowserError
from app.browser_login import BrowserLoginManager, LoginError
from app.service import Service
from app.store import Store


class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(gc.collect)
        self.store = Store(str(Path(folder.name) / 'data.db'))
        self.service = Service(self.store)
        self.account = self.store.create_pending_account('online-one', 'socks5://proxy.test:20002')

    def test_pending_creation_does_not_overwrite_account(self):
        self.assertFalse(self.account['enabled'])
        with self.assertRaises(ValueError):
            self.store.create_pending_account('online-one', 'socks5://proxy.test:20002')

    def test_runtime_reuses_browser_and_closes_only_for_exclusive_login(self):
        runtime = self.service.browser_runtime
        account = self.store.account(self.account['id'], private=True)
        with patch('app.browser_runtime.start_managed_browser', return_value=19301) as start, \
             patch('app.browser_runtime.stop_managed_browser') as stop:
            first, owned1 = runtime.acquire(account)
            second, owned2 = runtime.acquire(account)
            self.assertEqual(first['cdp_port'], 19301)
            self.assertEqual(second['cdp_port'], 19301)
            self.assertEqual(start.call_count, 2)
            with self.assertRaises(BrowserError):
                runtime.close_idle(self.account['id'])
            runtime.release(first, owned1)
            runtime.release(second, owned2)
            stop.assert_not_called()
            runtime.close_idle(self.account['id'])
            stop.assert_called_once_with(self.account['id'])


class OnlineLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.folder.name) / 'data.db'))
        self.service = Service(self.store)
        self.service.update_settings({'chrome_user_data_root': self.folder.name})
        self.account = self.store.create_pending_account('online-two', 'socks5://proxy.test:20002')
        self.manager = BrowserLoginManager(self.store, self.service)

    async def asyncTearDown(self):
        await self.manager.close()
        gc.collect()
        self.folder.cleanup()

    async def test_login_finishes_into_managed_profile(self):
        desktops = []

        class FakeDesktop:
            def __init__(self, profile, proxy):
                self.profile, self.proxy = profile, proxy
                self.cdp_port, self.vnc_port, self.password = 29999, 29998, 'test'
                self.closed = False
                desktops.append(self)

            @staticmethod
            def available():
                return True

            async def start(self):
                pass

            def alive(self):
                return not self.closed

            async def close(self):
                self.closed = True

        client = Mock()
        client.account_state.return_value = {'available_balance': 123, 'buckets': {'gift': 123}}
        client.browser.project_id.return_value = ''
        with patch('app.browser_login.LoginDesktop', FakeDesktop), \
             patch('app.browser_login.UpstreamClient', return_value=client), \
             patch.object(self.service.browser_runtime, 'close_idle'):
            session = await self.manager.start(self.account['id'])
            self.assertEqual(desktops[0].profile, Path(self.folder.name) / str(self.account['id']))
            self.assertEqual(desktops[0].proxy, 'socks5://proxy.test:20002')
            result = await self.manager.finish(session['active']['session_id'])
            self.assertFalse(desktops[0].closed)
            self.assertEqual(len(desktops), 1)
            again = await self.manager.start(self.account['id'])
            self.assertEqual(len(desktops), 1)
            await self.manager.cancel(again['active']['session_id'])
        self.assertTrue(result['enabled'])
        self.assertEqual(result['cdp_port'], 29999)
        self.assertEqual(result['project_id'], '')
        self.assertEqual(result['balance'], 123)
        self.assertIsNone(self.manager.active)
        with patch('app.browser_runtime.browser_alive', return_value=True), \
             patch('app.browser_runtime.start_managed_browser') as start:
            current, owned = self.service.browser_runtime.acquire(
                self.store.account(self.account['id'], private=True))
            self.assertEqual(current['cdp_port'], 29999)
            start.assert_not_called()
            self.service.browser_runtime.release(current, owned)

    async def test_active_task_blocks_profile_takeover(self):
        self.service._active[self.account['id']] = 1
        with patch('app.browser_login.LoginDesktop') as desktop:
            desktop.available.return_value = True
            with self.assertRaises(LoginError):
                await self.manager.start(self.account['id'])
        self.assertEqual(self.store.account(self.account['id'])['status'], 'pending')
