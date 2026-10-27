"""Online accounts retain one profile and keep task browsers warm."""

import asyncio
import gc
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.browser import BrowserError, BrowserRejected
from app.browser_login import BrowserLoginManager, LoginError, _verification_failure
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
        self.setup_check = patch('app.browser_login.AccountAutomation')
        self.setup = self.setup_check.start()
        self.setup.return_value.ensure_ready.return_value = {'identity_verified': True,
            'watermark_ready': True, 'agreement_ready': True, 'identity': {'user_id': 'synthetic-manual-user'}}
        self.addCleanup(self.setup_check.stop)

    async def asyncTearDown(self):
        await self.manager.close()
        self.service.stop()
        gc.collect()
        self.folder.cleanup()

    def active_login(self):
        previous = self.store.account(self.account['id'], private=True)
        desktop = Mock(cdp_port=29999)
        desktop.alive.return_value = True
        self.store.update_account(self.account['id'], {'enabled': False, 'status': 'logging_in'})
        active = {'id': 'test-session', 'ticket': 'test-viewer',
                  'account_id': self.account['id'], 'desktop': desktop,
                  'previous': previous, 'enable_after': True,
                  'deadline': time.monotonic() + 120, 'phase': 'login'}
        self.manager.active = active
        return active

    async def test_failed_credit_verification_preserves_session_for_retry(self):
        active = self.active_login()
        client = Mock()
        client.account_state.side_effect = [BrowserError('Dreamina browser fetch failed'),
                                            {'available_balance': 123, 'buckets': {'gift': 123}}]
        client.browser.project_id.return_value = ''
        with patch('app.browser_login.UpstreamClient', return_value=client) as factory:
            with self.assertRaisesRegex(LoginError, r'\(credit read\).*credit request failed'):
                await self.manager.finish(active['id'])
            self.assertIs(self.manager.viewer(active['ticket']), active)
            self.assertEqual(active['phase'], 'login')
            self.assertFalse(self.store.account(self.account['id'])['enabled'])
            self.assertEqual(self.store.account(self.account['id'])['status'], 'logging_in')
            active['desktop'].close.assert_not_called()
            self.assertEqual(factory.call_args.args[0]['cdp_port'], 29999)
            result = await self.manager.finish(active['id'])
        self.assertTrue(result['enabled'])
        self.assertEqual(result['balance'], 123)
        self.assertIsNone(self.manager.active)

    async def test_failed_browser_setup_reports_stage_and_retains_viewer(self):
        active = self.active_login()
        with patch('app.browser_login.UpstreamClient', side_effect=BrowserError(
                'account proxy must be a SOCKS5 or HTTP proxy with host and port')):
            with self.assertRaisesRegex(LoginError, r'\(browser setup\).*proxy configuration is invalid'):
                await self.manager.finish(active['id'])
        self.assertIs(self.manager.viewer(active['ticket']), active)

    async def test_unconfirmed_generation_preferences_keep_manual_login_disabled(self):
        active = self.active_login()
        self.setup.return_value.ensure_ready.return_value = {'watermark_ready': True, 'agreement_ready': False}
        client = Mock()
        client.account_state.return_value = {'available_balance': 123, 'buckets': {}}
        with patch('app.browser_login.UpstreamClient', return_value=client):
            with self.assertRaisesRegex(LoginError, 'generation preferences'):
                await self.manager.finish(active['id'])
        self.assertFalse(self.store.account(self.account['id'])['enabled'])
        self.assertIs(self.manager.viewer(active['ticket']), active)

    async def test_manual_finish_requires_a_verified_sdk_id_and_retains_the_viewer(self):
        active = self.active_login()
        self.setup.return_value.ensure_ready.return_value = {'identity_verified': True,
            'watermark_ready': True, 'agreement_ready': True, 'identity': {}}
        client = Mock()
        client.account_state.return_value = {'available_balance': 123, 'buckets': {}}
        with patch('app.browser_login.UpstreamClient', return_value=client):
            with self.assertRaisesRegex(LoginError, 'identity is not confirmed'):
                await self.manager.finish(active['id'])
        self.assertFalse(self.store.account(self.account['id'])['enabled'])
        self.assertEqual(self.store.account(self.account['id'], private=True)['login_user_id'], '')
        self.assertIs(self.manager.viewer(active['ticket']), active)
        self.assertIs(self.setup.call_args.kwargs['allow_initial_identity'], True)

    async def test_automatic_login_blocks_manual_profile_takeover(self):
        self.service.auto_login_busy = lambda account_id: True
        with patch('app.browser_login.LoginDesktop') as desktop:
            desktop.available.return_value = True
            with self.assertRaisesRegex(LoginError, 'automatic login'):
                await self.manager.start(self.account['id'])

    async def test_unknown_verification_failure_does_not_expose_exception_data(self):
        active = self.active_login()
        client = Mock()
        client.account_state.side_effect = RuntimeError('cookie=private; url=https://private.test')
        with patch('app.browser_login.UpstreamClient', return_value=client):
            with self.assertRaises(LoginError) as caught:
                await self.manager.finish(active['id'])
        self.assertEqual(str(caught.exception),
                         'browser login verification failed (credit read): account verification could not complete')
        self.assertIs(self.manager.viewer(active['ticket']), active)

    def test_browser_failure_diagnostics_are_fixed_whitelisted_messages(self):
        for error in (BrowserError('private token=secret'),
                      BrowserError('Dreamina browser fetch failed; cookie=secret'),
                      BrowserRejected('Dreamina ret=9: private cookie=secret')):
            with self.subTest(error_type=type(error).__name__):
                value = _verification_failure(error, 'credit read')
                self.assertIn('(credit read)', value)
                self.assertNotIn('secret', value)
                self.assertNotIn('cookie', value)
                self.assertNotIn('token', value)

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
        self.assertEqual(self.store.account(self.account['id'], private=True)['login_user_id'], 'synthetic-manual-user')
        self.assertNotIn('login_user_id', result)
        self.assertNotIn('synthetic-manual-user', str(result))
        self.assertIs(self.setup.call_args.kwargs['allow_initial_identity'], True)
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
