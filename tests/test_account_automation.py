"""Account readiness and login safety without contacting Dreamina."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import json
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.account_automation import AccountAutomation, AutomationError, _CLICK, _IDENTITY, _OPEN_LOGIN_TARGET, _VIEW, _WATERMARK
from app.browser import BrowserError


ACCOUNT = {'id': 42, 'cdp_port': 19342, 'proxy_url': 'socks5://proxy.invalid:10000',
           'login_email': 'operator@example.invalid'}
IDENTITY = {'authenticated': True, 'user_id': 'synthetic-user', 'email': 'operator@example.invalid'}
READY = {'close_watermark': True, 'close_watermark_popup_tip': True, 'aigc_compliance_confirmed': True}


def browser_with_settings(settings):
    browser = Mock()
    browser.resource_timing_capture.side_effect = nullcontext
    browser.evaluate.side_effect = lambda expression, **_: IDENTITY.copy() if expression == _IDENTITY else {'stage': 'waiting'}
    browser.post.return_value = {'user_custom_settings': settings.copy()}
    return browser


class ReadinessTests(unittest.TestCase):
    def test_identity_only_does_not_depend_on_or_request_generation_settings(self):
        browser = browser_with_settings(READY)
        browser.post.side_effect = BrowserError('settings are unavailable')
        automation = AccountAutomation({**ACCOUNT, 'login_user_id': 'synthetic-user'}, browser=browser)
        with patch.object(automation, '_settings', side_effect=AutomationError('setup_required', 'unavailable')) as settings:
            identity = automation.read_identity()
        self.assertEqual(identity, {'user_id': 'synthetic-user', 'email': ACCOUNT['login_email']})
        settings.assert_not_called()
        browser.post.assert_not_called()
        browser.command.assert_not_called()

    def test_read_ready_still_requires_settings_when_identity_succeeds(self):
        browser = browser_with_settings(READY)
        browser.post.side_effect = BrowserError('settings are unavailable')
        automation = AccountAutomation({**ACCOUNT, 'login_user_id': 'synthetic-user'}, browser=browser)
        with self.assertRaises(AutomationError) as error:
            automation.read_ready()
        self.assertEqual(error.exception.status, 'setup_required')
        browser.post.assert_called_once_with('/mweb/v1/get_settings', {})

    def test_identity_only_preserves_challenge_and_verification_guards(self):
        for stage, expected in [('challenge', 'challenge_required'), ('verification', 'verification_required')]:
            with self.subTest(stage=stage):
                browser = browser_with_settings(READY)
                browser.evaluate.return_value = {'stage': stage}
                browser.evaluate.side_effect = None
                automation = AccountAutomation(ACCOUNT, browser=browser)
                with patch.object(automation, '_identity') as identity:
                    with self.assertRaises(AutomationError) as error:
                        automation.read_identity()
                self.assertEqual(error.exception.status, expected)
                identity.assert_not_called()
                browser.post.assert_not_called()

    def test_identity_only_still_rejects_wrong_identity_email_and_unauthenticated_state(self):
        variants = [
            {**IDENTITY, 'user_id': 'other-user'},
            {**IDENTITY, 'email': 'other@example.invalid'},
            {**IDENTITY, 'authenticated': False},
            {'transport_error': True},
        ]
        for value in variants:
            with self.subTest(value=value):
                browser = browser_with_settings(READY)
                browser.evaluate.side_effect = lambda expression, **_: value if expression == _IDENTITY else {'stage': 'waiting'}
                automation = AccountAutomation({**ACCOUNT, 'login_user_id': 'synthetic-user'}, browser=browser)
                with self.assertRaises(AutomationError) as error:
                    automation.read_identity()
                self.assertEqual(error.exception.status, 'login_required')
                browser.post.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for identity URL verification')
    def test_identity_reuses_actual_sdk_url_without_region_guess_or_probe_pollution(self):
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8'));
let calls=[];
const context={URL,AbortController,setTimeout:()=>0,clearTimeout:()=>{},location:{origin:'https://dreamina.capcut.com'},
 globalThis:{__locationInfo:{code:'AU'}},performance:{getEntriesByType:()=>payload.resources.map(name=>({name}))},
 fetch:async(url,options)=>{calls.push({url,credentials:options.credentials});return {ok:true,json:async()=>({
  data:{user_id_str:'synthetic-user',email:'operator@example.invalid'}})};}};
Promise.resolve(vm.runInNewContext(payload.source,context)).then(result=>process.stdout.write(JSON.stringify({calls,result})));
'''
        sdk = 'https://dreamina.capcut.com/passport/web/account/info/?sdk_version=2.1.10-tiktok&aid=513641&webid=synthetic'
        old_probe = 'https://dreamina.capcut.com/passport/web/account/info/?account_sdk_source=web&aid=513695'
        foreign = 'https://unrelated.example.invalid/passport/web/account/info/?sdk_version=2.1.10-tiktok&aid=513695'
        wrong_path = 'https://dreamina.capcut.com/passport/web/account/info/other?sdk_version=2.1.10-tiktok&aid=513695'
        legacy = sdk.replace('aid=513641', 'aid=513695')
        variants = [([sdk, old_probe], sdk), ([sdk, foreign, wrong_path, old_probe], sdk),
                    ([legacy, old_probe], legacy), ([old_probe, foreign, wrong_path], None), ([], None)]
        for resources, expected_url in variants:
            with self.subTest(resources=resources):
                completed = subprocess.run(['node', '-e', program],
                                           input=json.dumps({'resources': resources, 'source': _IDENTITY}),
                                           text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                data = json.loads(completed.stdout)
                if expected_url is None:
                    self.assertEqual(data['calls'], [])
                    self.assertIs(data['result']['transport_error'], True)
                else:
                    self.assertEqual(data['calls'], [{'url': expected_url, 'credentials': 'include'}])
                    self.assertIs(data['result']['authenticated'], True)

    def test_read_ready_is_read_only_and_requires_server_booleans(self):
        browser = browser_with_settings({**READY, 'close_watermark': 'true'})
        result = AccountAutomation(ACCOUNT, browser=browser).read_ready()
        self.assertIs(result['identity_verified'], True)
        self.assertIs(result['watermark_ready'], False)
        self.assertIs(result['agreement_ready'], True)
        browser.post.assert_called_once_with('/mweb/v1/get_settings', {})
        browser.command.assert_not_called()

    def test_already_ready_does_not_write_or_change_popup_preference(self):
        browser = browser_with_settings({**READY, 'close_watermark_popup_tip': False})
        result = AccountAutomation(ACCOUNT, browser=browser).ensure_ready()
        self.assertIs(result['watermark_ready'], True)
        self.assertIs(result['agreement_ready'], True)
        self.assertIs(result['settings']['close_watermark_popup_tip'], False)
        browser.post.assert_called_once_with('/mweb/v1/get_settings', {})

    def test_only_missing_settings_are_written_then_read_back(self):
        browser = browser_with_settings(READY)
        browser.post.side_effect = [
            {'user_custom_settings': {**READY, 'aigc_compliance_confirmed': False}}, {},
            {'user_custom_settings': READY.copy()},
        ]
        result = AccountAutomation(ACCOUNT, browser=browser).ensure_ready()
        self.assertIs(result['agreement_ready'], True)
        self.assertEqual(browser.post.call_args_list[1].args,
                         ('/mweb/v1/update_settings', {'custom_settings': {'aigc_compliance_confirmed': True}}))
        self.assertEqual(browser.post.call_args_list[2].args, ('/mweb/v1/get_settings', {}))

    def test_watermark_requires_normal_ui_and_never_writes_a_raw_entitlement_bypass(self):
        browser = browser_with_settings(READY)
        browser.post.side_effect = [
            {'user_custom_settings': {**READY, 'close_watermark': False}},
            {'user_custom_settings': READY.copy()}
        ]
        automation = AccountAutomation(ACCOUNT, browser=browser)
        with patch.object(automation, '_enable_watermark') as enable:
            result = automation.ensure_ready()
        enable.assert_called_once_with()
        self.assertIs(result['watermark_ready'], True)
        self.assertEqual([call.args[0] for call in browser.post.call_args_list],
                         ['/mweb/v1/get_settings', '/mweb/v1/get_settings'])

    def test_watermark_membership_popup_stops_without_buying_or_raw_preference_write(self):
        browser = browser_with_settings({**READY, 'close_watermark': False})
        automation = AccountAutomation(ACCOUNT, browser=browser)
        clock = [0.0]
        with patch.object(automation, '_watermark_action', return_value={'stage': 'blocked'}) as action, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            with self.assertRaises(AutomationError) as caught:
                automation.ensure_ready()
        self.assertEqual(caught.exception.status, 'setup_required')
        self.assertTrue(all(call.args == () for call in action.call_args_list))
        self.assertEqual(clock[0], 30)
        browser.post.assert_called_once_with('/mweb/v1/get_settings', {})

    def test_watermark_ui_only_opens_known_settings_toggles_and_saves(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        state = {'stage': 'avatar', 'checked': False, 'saved': False}
        actions = []

        def action(command='view'):
            if command != 'view':
                actions.append(command)
                if command == 'open_avatar':
                    state['stage'] = 'menu'
                elif command == 'open_setting':
                    state['stage'] = 'modal'
                elif command == 'toggle':
                    state['checked'] = True
                elif command == 'save':
                    state.update(stage='waiting', saved=True)
            return state.copy()

        with patch.object(automation, '_watermark_action', side_effect=action), \
             patch.object(automation, '_settings', side_effect=lambda: {'close_watermark': state['saved']}), \
             patch('app.account_automation.time.sleep'):
            automation._enable_watermark()
        self.assertEqual(actions, ['open_avatar', 'open_setting', 'toggle', 'save'])

    def test_watermark_known_modal_can_mount_after_blocked_unknown_without_unknown_clicks(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        states = iter([{'stage': 'avatar'}, {'stage': 'menu'}, {'stage': 'blocked'}, {'stage': 'unknown'},
                       {'stage': 'modal', 'checked': False}, {'stage': 'modal', 'checked': True}])
        actions, clock = [], [0.0]

        def action(command='view'):
            if command == 'view':
                return next(states)
            actions.append(command)
            return {'stage': 'opened'}

        with patch.object(automation, '_watermark_action', side_effect=action), \
             patch.object(automation, '_settings', side_effect=lambda: {'close_watermark': 'save' in actions}), \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            automation._enable_watermark()
        self.assertEqual(actions, ['open_avatar', 'open_setting', 'toggle', 'save'])
        self.assertGreaterEqual(clock[0], 4)

    def test_watermark_rechecks_avatar_after_first_profile_navigation_then_completes_once(self):
        # Real account56: first opened click left stage=avatar on Personal;
        # a fresh second normal click exposed menu -> modal -> saved readback.
        for opens_needed in (2, 3):
            with self.subTest(opens_needed=opens_needed):
                browser = browser_with_settings(READY)
                automation = AccountAutomation(ACCOUNT, browser=browser)
                clock, dispatches, actions = [0.0], [], []
                state = {'stage': 'avatar', 'checked': False, 'saved': False, 'target_changed': True}

                def action(command='view'):
                    if command == 'view':
                        return state.copy()
                    if command == 'open_avatar':
                        # A rejected fresh hit test sends no mouse events and
                        # must not consume one of the three actual clicks.
                        if state['target_changed']:
                            state['target_changed'] = False
                            return {'stage': 'waiting'}
                        dispatches.append(clock[0])
                        if len(dispatches) == opens_needed:
                            state['stage'] = 'menu'
                        return {'stage': 'opened'}
                    actions.append(command)
                    if command == 'open_setting':
                        state['stage'] = 'modal'
                    elif command == 'toggle':
                        state['checked'] = True
                    elif command == 'save':
                        state.update(stage='unknown', saved=True)
                    return {'stage': 'saved' if command == 'save' else 'opened'}

                with patch.object(automation, '_watermark_action', side_effect=action), \
                     patch.object(automation, '_settings', side_effect=lambda: {'close_watermark': state['saved']}), \
                     patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
                     patch('app.account_automation.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                    automation._enable_watermark()
                self.assertEqual(len(dispatches), opens_needed)
                self.assertTrue(all(right-left >= 1 for left, right in zip(dispatches, dispatches[1:])))
                self.assertEqual(actions, ['open_setting', 'toggle', 'save'])
                self.assertLess(clock[0], 30)
                browser.post.assert_not_called()

    def test_watermark_avatar_retry_is_bounded_and_never_replays_other_actions_or_blocked_ui(self):
        for after_first, expected_clicks, expected_actions in (
                ('avatar', 3, []), ('blocked', 1, []), ('menu_then_avatar', 1, ['open_setting'])):
            with self.subTest(after_first=after_first):
                browser = browser_with_settings(READY)
                automation = AccountAutomation(ACCOUNT, browser=browser)
                clock, dispatches, actions = [0.0], [], []
                state = {'stage': 'avatar'}

                def action(command='view'):
                    if command == 'view':
                        return state.copy()
                    if command == 'open_avatar':
                        dispatches.append(clock[0])
                        state['stage'] = 'menu' if after_first == 'menu_then_avatar' else after_first
                    else:
                        actions.append(command)
                        state['stage'] = 'avatar'
                    return {'stage': 'opened'}

                with patch.object(automation, '_watermark_action', side_effect=action), \
                     patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
                     patch('app.account_automation.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                    with self.assertRaises(AutomationError) as caught:
                        automation._enable_watermark()
                self.assertEqual(caught.exception.status, 'setup_required')
                self.assertEqual(len(dispatches), expected_clicks)
                self.assertTrue(all(right-left >= 1 for left, right in zip(dispatches, dispatches[1:])))
                self.assertEqual(actions, expected_actions)
                self.assertGreaterEqual(clock[0], 30)
                self.assertLess(clock[0], 30.31)
                browser.post.assert_not_called()

    def test_watermark_saved_readback_wins_over_unknown_close_animation(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        stages = iter([{'stage': 'modal', 'checked': True}, {'stage': 'unknown'}])
        actions = []

        def action(command='view'):
            if command == 'view':
                return next(stages)
            actions.append(command)
            return {'stage': 'saved'}

        with patch.object(automation, '_watermark_action', side_effect=action), \
             patch.object(automation, '_settings', side_effect=[{'close_watermark': False}, {'close_watermark': True}]) as readback, \
             patch('app.account_automation.time.sleep'):
            automation._enable_watermark()
        self.assertEqual(actions, ['save'])
        self.assertEqual(readback.call_count, 2)

    def test_watermark_unknown_controls_never_receive_actions_and_expire(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        clock = [0.0]
        with patch.object(automation, '_watermark_action', return_value={'stage': 'unknown'}) as action, \
             patch.object(automation, '_settings') as readback, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            with self.assertRaises(AutomationError) as caught:
                automation._enable_watermark()
        self.assertEqual(caught.exception.status, 'setup_required')
        self.assertEqual(clock[0], 30)
        self.assertTrue(all(call.args == () for call in action.call_args_list))
        readback.assert_not_called()

    def test_watermark_transition_never_waits_through_challenge_or_verification(self):
        for stage, status in [('challenge', 'challenge_required'), ('verification', 'verification_required')]:
            for current in [{'stage': 'blocked'}, {'stage': 'unknown'},
                            {'stage': 'modal', 'checked': False}, {'stage': 'modal', 'checked': True}]:
                with self.subTest(stage=stage, current=current):
                    browser = browser_with_settings(READY)
                    browser.evaluate.side_effect = None
                    browser.evaluate.return_value = {'stage': stage}
                    automation = AccountAutomation(ACCOUNT, browser=browser)
                    with patch.object(automation, '_watermark_action', return_value=current) as action, \
                         patch.object(automation, '_settings') as readback, \
                         patch('app.account_automation.time.sleep') as sleep:
                        with self.assertRaises(AutomationError) as caught:
                            automation._enable_watermark()
                    self.assertEqual(caught.exception.status, status)
                    action.assert_called_once_with()
                    readback.assert_not_called()
                    sleep.assert_not_called()

    def test_successful_write_without_confirmed_readback_does_not_mark_ready(self):
        browser = browser_with_settings({**READY, 'aigc_compliance_confirmed': False})
        with self.assertRaises(AutomationError) as caught:
            AccountAutomation(ACCOUNT, browser=browser).ensure_ready()
        self.assertEqual(caught.exception.status, 'setup_required')
        self.assertEqual(browser.post.call_count, 3)

    def test_wrong_profile_identity_never_writes_settings(self):
        browser = browser_with_settings(READY)
        browser.evaluate.side_effect = lambda expression, **_: {
            **IDENTITY, 'email': 'another@example.invalid'} if expression == _IDENTITY else {'stage': 'waiting'}
        with self.assertRaises(AutomationError) as caught:
            AccountAutomation(ACCOUNT, browser=browser).ensure_ready()
        self.assertEqual(caught.exception.status, 'login_required')
        browser.post.assert_not_called()

    def test_masked_identity_requires_saved_binding_or_explicit_manual_initial_verification(self):
        masked = {**IDENTITY, 'email': 'o***tor@example.invalid'}
        for account, initial, accepted in [
            (ACCOUNT, False, False), (ACCOUNT, True, True),
            ({**ACCOUNT, 'login_user_id': IDENTITY['user_id']}, False, True),
            ({**ACCOUNT, 'login_user_id': 'different-user'}, True, False),
        ]:
            with self.subTest(account_bound=bool(account.get('login_user_id')), initial=initial, accepted=accepted):
                browser = browser_with_settings(READY)
                browser.evaluate.side_effect = lambda expression, **_: masked.copy() if expression == _IDENTITY else {'stage': 'waiting'}
                automation = AccountAutomation(account, browser=browser, allow_initial_identity=initial)
                if accepted:
                    result = automation.ensure_ready()
                    self.assertEqual(result['identity'], {'user_id': IDENTITY['user_id'], 'email': masked['email']})
                    self.assertEqual(automation.verified_user_id, IDENTITY['user_id'])
                else:
                    with self.assertRaises(AutomationError) as caught:
                        automation.ensure_ready()
                    self.assertEqual(caught.exception.status, 'login_required')
                    browser.post.assert_not_called()

    def test_masked_identity_shape_prefix_suffix_and_bound_user_must_all_match(self):
        for email in ['o**tor@example.invalid', 'o****tor@example.invalid', 'op***tor@example.invalid',
                      'x***tor@example.invalid', 'o***tor@other.invalid', 'o***@example.invalid',
                      'o***example.invalid', 'o********']:
            with self.subTest(email=email):
                browser = browser_with_settings(READY)
                browser.evaluate.side_effect = lambda expression, **_: {**IDENTITY, 'email': email} if expression == _IDENTITY else {'stage': 'waiting'}
                with self.assertRaises(AutomationError):
                    AccountAutomation({**ACCOUNT, 'login_user_id': IDENTITY['user_id']}, browser=browser,
                                      allow_initial_identity=True).read_ready()
                browser.post.assert_not_called()

    def test_legacy_managed_account_without_login_email_accepts_sdk_identity_but_honors_saved_user_id(self):
        for bound, accepted in [('', True), (IDENTITY['user_id'], True), ('another-user', False)]:
            with self.subTest(bound=bound, accepted=accepted):
                browser = browser_with_settings(READY)
                browser.evaluate.side_effect = lambda expression, **_: {**IDENTITY, 'email': 'o***tor@example.invalid'} if expression == _IDENTITY else {'stage': 'waiting'}
                legacy = {key: value for key, value in ACCOUNT.items() if key != 'login_email'}
                automation = AccountAutomation({**legacy, 'login_user_id': bound}, browser=browser)
                if accepted:
                    result = automation.ensure_ready()
                    self.assertIs(result['identity_verified'], True)
                    self.assertEqual(result['identity']['user_id'], IDENTITY['user_id'])
                else:
                    with self.assertRaises(AutomationError):
                        automation.ensure_ready()
                    browser.post.assert_not_called()

    def test_fresh_password_continue_allows_masked_initial_binding_and_same_instance_readback(self):
        browser = browser_with_settings(READY)
        state = {'authenticated': False}
        masked = {**IDENTITY, 'email': 'o***tor@example.invalid'}
        browser.evaluate.side_effect = lambda expression, **_: masked.copy() if expression == _IDENTITY and state['authenticated'] else (
            {'authenticated': False} if expression == _IDENTITY else {'stage': 'waiting'})
        automation = AccountAutomation(ACCOUNT, browser=browser)

        def action(stage, *_):
            self.assertEqual(stage, 'password')
            state['authenticated'] = True
            return True

        clock = {'now': 0.0}
        with patch.object(automation, '_view', side_effect=[{'stage': 'password'}, {'stage': 'waiting'}]), \
             patch.object(automation, '_action', side_effect=action) as submit, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock['now']), \
             patch('app.account_automation.time.sleep', side_effect=lambda seconds: clock.update(now=clock['now'] + 2.1)):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        self.assertEqual(result['identity']['user_id'], IDENTITY['user_id'])
        self.assertEqual(automation.read_ready()['identity']['user_id'], IDENTITY['user_id'])
        submit.assert_called_once_with('password', ACCOUNT['login_email'], 'synthetic-password')

    def test_failed_password_action_does_not_authorize_masked_identity_initial_binding(self):
        browser = browser_with_settings(READY)
        browser.evaluate.side_effect = lambda expression, **_: {**IDENTITY, 'email': 'o***tor@example.invalid'} if expression == _IDENTITY else {'stage': 'waiting'}
        automation = AccountAutomation(ACCOUNT, browser=browser)
        self.assertFalse(automation._password_submitted)
        with self.assertRaises(AutomationError):
            automation.read_ready()
        browser.post.assert_not_called()

    def test_anonymous_or_missing_identity_never_writes_settings(self):
        for identity in ({'authenticated': False}, {'authenticated': True, 'user_id': ''}):
            with self.subTest(identity=identity):
                browser = browser_with_settings(READY)
                browser.evaluate.side_effect = lambda expression, **_: identity if expression == _IDENTITY else {'stage': 'waiting'}
                with self.assertRaises(AutomationError):
                    AccountAutomation(ACCOUNT, browser=browser).ensure_ready()
                browser.post.assert_not_called()

    def test_captcha_and_otp_are_never_bypassed_with_authenticated_settings(self):
        for stage, status in [('challenge', 'challenge_required'), ('verification', 'verification_required')]:
            with self.subTest(stage=stage):
                browser = browser_with_settings(READY)
                browser.evaluate.return_value = {'stage': stage}
                browser.evaluate.side_effect = None
                with self.assertRaises(AutomationError) as caught:
                    AccountAutomation(ACCOUNT, browser=browser).ensure_ready()
                self.assertEqual(caught.exception.status, status)
                browser.post.assert_not_called()

    def test_setting_network_failure_has_fixed_non_sensitive_error(self):
        browser = browser_with_settings(READY)
        browser.post.side_effect = BrowserError('private cookie or signed request data')
        with self.assertRaises(AutomationError) as caught:
            AccountAutomation(ACCOUNT, browser=browser).ensure_ready()
        self.assertEqual(caught.exception.status, 'setup_required')
        self.assertNotIn('private', str(caught.exception))

    def test_concurrent_same_account_updates_preferences_only_once(self):
        state = {**READY, 'aigc_compliance_confirmed': False}
        writes = []
        lock = threading.Lock()
        browser = browser_with_settings(READY)

        def request(path, body):
            with lock:
                if path == '/mweb/v1/get_settings':
                    return {'user_custom_settings': state.copy()}
                self.assertEqual(path, '/mweb/v1/update_settings')
                time.sleep(.01)
                state.update(body['custom_settings'])
                writes.append(body)
                return {}

        browser.post.side_effect = request
        first = AccountAutomation(ACCOUNT, browser=browser)
        second = AccountAutomation(ACCOUNT, browser=browser)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: item.ensure_ready(), [first, second]))
        self.assertTrue(all(result['agreement_ready'] is True for result in results))
        self.assertEqual(len(writes), 1)

    def test_ensure_ready_closes_known_promotion_before_identity_and_readback_without_changing_preferences(self):
        browser = browser_with_settings(READY)
        views = [{'stage': 'promotion'}, {'stage': 'waiting'}, {'stage': 'waiting'}]

        def evaluate(expression, **_):
            if expression == _IDENTITY:
                return IDENTITY.copy()
            return views.pop(0) if views else {'stage': 'waiting'}

        browser.evaluate.side_effect = evaluate
        automation = AccountAutomation(ACCOUNT, browser=browser)
        with patch.object(automation, '_action', return_value=True) as action:
            result = automation.ensure_ready()
        self.assertIs(result['identity_verified'], True)
        self.assertIs(result['watermark_ready'], True)
        action.assert_called_once_with('promotion', '', '')
        browser.post.assert_called_once_with('/mweb/v1/get_settings', {})

    def test_promotion_close_is_single_and_bounded(self):
        browser = browser_with_settings(READY)
        browser.evaluate.side_effect = None
        browser.evaluate.return_value = {'stage': 'promotion'}
        automation = AccountAutomation(ACCOUNT, browser=browser)
        clock = [0.0]
        with patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            with self.assertRaises(AutomationError) as caught:
                automation.ensure_ready()
        self.assertEqual(caught.exception.status, 'setup_required')
        action.assert_called_once_with('promotion', '', '')
        browser.post.assert_not_called()

    def test_ensure_ready_waits_for_unknown_fading_wrapper_then_checks_identity(self):
        browser = browser_with_settings(READY)
        views = [{'stage': 'promotion'}, {'stage': 'unknown'}, {'stage': 'unknown'}, {'stage': 'waiting'}]

        def evaluate(expression, **_):
            return IDENTITY.copy() if expression == _IDENTITY else views.pop(0) if views else {'stage': 'waiting'}

        browser.evaluate.side_effect = evaluate
        automation = AccountAutomation(ACCOUNT, browser=browser)
        with patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.sleep'):
            result = automation.ensure_ready()
        self.assertIs(result['identity_verified'], True)
        action.assert_called_once_with('promotion', '', '')
        browser.post.assert_called_once_with('/mweb/v1/get_settings', {})

    def test_unknown_after_promotion_close_stays_bounded_and_never_clicks_it(self):
        browser = browser_with_settings(READY)
        calls = [0]

        def evaluate(_expression, **_):
            calls[0] += 1
            return {'stage': 'promotion' if calls[0] == 1 else 'unknown'}

        browser.evaluate.side_effect = evaluate
        automation = AccountAutomation(ACCOUNT, browser=browser)
        clock = [0.0]
        with patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + .2)):
            with self.assertRaises(AutomationError) as caught:
                automation.ensure_ready()
        self.assertEqual(caught.exception.status, 'setup_required')
        self.assertLess(clock[0], 5.3)
        action.assert_called_once_with('promotion', '', '')
        browser.post.assert_not_called()


class LoginTests(unittest.TestCase):
    def test_resource_timing_session_wraps_entire_login_and_cleans_up_on_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                browser = browser_with_settings(READY)
                events = []

                @contextmanager
                def capture():
                    events.append('enter')
                    try:
                        yield
                    finally:
                        events.append('exit')

                browser.resource_timing_capture.side_effect = capture
                automation = AccountAutomation(ACCOUNT, browser=browser)

                def login(*_):
                    self.assertEqual(events, ['enter'])
                    if failure:
                        raise AutomationError('login_required', 'synthetic interrupted login')
                    return {'identity_verified': True}

                with patch.object(automation, '_run_login', side_effect=login) as run_login:
                    if failure:
                        with self.assertRaises(AutomationError):
                            automation.run(ACCOUNT['login_email'], 'synthetic-password')
                    else:
                        self.assertIs(automation.run(ACCOUNT['login_email'], 'synthetic-password')['identity_verified'], True)
                self.assertEqual(events, ['enter', 'exit'])
                run_login.assert_called_once_with(ACCOUNT['login_email'], 'synthetic-password', None)

    def test_run_waits_for_unknown_promotion_animation_before_identity_ready(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        sequence = [{'stage': 'promotion'}, {'stage': 'unknown'}, {'stage': 'unknown'}, {'stage': 'waiting'}]
        with patch.object(automation, '_view', side_effect=sequence), \
             patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.sleep'):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        action.assert_called_once_with('promotion', ACCOUNT['login_email'], 'synthetic-password')

    def test_run_unknown_after_known_close_expires_without_clicking_unknown(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        clock, count = [0.0], [0]

        def view():
            count[0] += 1
            return {'stage': 'promotion' if count[0] == 1 else 'unknown'}

        with patch.object(automation, '_view', side_effect=view), \
             patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + .2)):
            with self.assertRaises(AutomationError) as caught:
                automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertEqual(caught.exception.status, 'setup_required')
        self.assertGreaterEqual(clock[0], 30)
        self.assertLess(clock[0], 30.5)
        action.assert_called_once_with('promotion', ACCOUNT['login_email'], 'synthetic-password')

    def test_password_exit_unknown_animation_only_waits_then_verifies_identity(self):
        browser = browser_with_settings(READY)
        authenticated = [False]
        browser.evaluate.side_effect = lambda expression, **_: (IDENTITY.copy() if authenticated[0] else {'authenticated': False}) if expression == _IDENTITY else {'stage': 'waiting'}
        automation = AccountAutomation(ACCOUNT, browser=browser)
        clock, count = [0.0], [0]
        sequence = ['password', *['unknown'] * 10, 'waiting']

        def view():
            stage = sequence[min(count[0], len(sequence) - 1)]
            count[0] += 1
            return {'stage': stage}

        def action(stage, *_):
            self.assertEqual(stage, 'password')
            authenticated[0] = True
            return True

        with patch.object(automation, '_view', side_effect=view), \
             patch.object(automation, '_action', side_effect=action) as submit, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        self.assertEqual(result['identity']['user_id'], IDENTITY['user_id'])
        self.assertGreater(clock[0], 5)
        self.assertLess(clock[0], 30)
        submit.assert_called_once_with('password', ACCOUNT['login_email'], 'synthetic-password')
        browser.post.assert_called_once_with('/mweb/v1/get_settings', {})

    def test_persistent_unknown_after_password_never_reads_identity_or_clicks_again(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        clock, count = [0.0], [0]

        def view():
            count[0] += 1
            return {'stage': 'password' if count[0] == 1 else 'unknown'}

        with patch.object(automation, '_view', side_effect=view), \
             patch.object(automation, '_identity', return_value=None) as identity, \
             patch.object(automation, '_action', return_value=True) as submit, \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + .2)):
            with self.assertRaises(AutomationError) as caught:
                automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertEqual(caught.exception.status, 'setup_required')
        self.assertGreaterEqual(clock[0], 30)
        self.assertLess(clock[0], 30.5)
        identity.assert_called_once_with()
        submit.assert_called_once_with('password', ACCOUNT['login_email'], 'synthetic-password')
        automation.browser.post.assert_not_called()

    def test_challenge_during_known_close_grace_period_is_never_waited_past(self):
        for stage, status in [('challenge', 'challenge_required'), ('verification', 'verification_required')]:
            with self.subTest(stage=stage):
                automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
                with patch.object(automation, '_view', side_effect=[{'stage': 'promotion'}, {'stage': stage}]), \
                     patch.object(automation, '_action', return_value=True) as action, \
                     patch('app.account_automation.time.sleep'):
                    with self.assertRaises(AutomationError) as caught:
                        automation.run(ACCOUNT['login_email'], 'synthetic-password')
                self.assertEqual(caught.exception.status, status)
                action.assert_called_once_with('promotion', ACCOUNT['login_email'], 'synthetic-password')

    def test_existing_authenticated_session_only_closes_promotion_without_replaying_password(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        with patch.object(automation, '_view', side_effect=[{'stage': 'promotion'}, {'stage': 'waiting'}]), \
             patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.sleep'):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        action.assert_called_once_with('promotion', ACCOUNT['login_email'], 'synthetic-password')

    def test_top_login_uses_trusted_mouse_events_and_never_credential_input(self):
        browser = browser_with_settings(READY)
        browser.evaluate.side_effect = [{'stage': 'signed_out'}, {'x': 1140, 'y': 94}]
        automation = AccountAutomation(ACCOUNT, browser=browser)
        self.assertTrue(automation._action('signed_out', ACCOUNT['login_email'], 'synthetic-password'))
        commands = [(call.args[0], call.args[1] if len(call.args) > 1 else {})
                    for call in browser.command.call_args_list]
        self.assertEqual([name for name, _ in commands], ['Page.bringToFront'] + ['Input.dispatchMouseEvent'] * 3)
        self.assertEqual([body.get('type') for _, body in commands[1:]], ['mouseMoved', 'mousePressed', 'mouseReleased'])
        self.assertTrue(all(body['x'] == 1140 and body['y'] == 94 for _, body in commands[1:]))
        self.assertEqual(browser.evaluate.call_args_list[1].args, (_OPEN_LOGIN_TARGET,))

    def test_ambiguous_top_login_coordinates_do_not_click(self):
        for target in (None, {}, {'x': True, 'y': 20}, {'x': float('nan'), 'y': 20}):
            with self.subTest(target=target):
                browser = browser_with_settings(READY)
                browser.evaluate.side_effect = [{'stage': 'signed_out'}, target]
                self.assertFalse(AccountAutomation(ACCOUNT, browser=browser)._action('signed_out', 'unused', 'unused'))
                self.assertEqual([call.args[0] for call in browser.command.call_args_list], ['Page.bringToFront'])

    def test_mouse_release_is_attempted_if_press_response_is_lost(self):
        browser = browser_with_settings(READY)
        browser.evaluate.side_effect = [{'stage': 'signed_out'}, {'x': 1140, 'y': 94}]

        def command(name, body=None):
            if body and body.get('type') == 'mousePressed':
                raise BrowserError('synthetic transport failure')
            return {}

        browser.command.side_effect = command
        with self.assertRaises(BrowserError):
            AccountAutomation(ACCOUNT, browser=browser)._action('signed_out', 'unused', 'unused')
        self.assertEqual(browser.command.call_args.args[1]['type'], 'mouseReleased')

    def test_noncredential_navigation_retries_are_bounded(self):
        for stage in ('signed_out', 'provider'):
            with self.subTest(stage=stage):
                clock = [0.0]
                automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
                with patch.object(automation, '_identity', return_value=None), \
                     patch.object(automation, '_view', return_value={'stage': stage}), \
                     patch.object(automation, '_action', return_value=True) as action, \
                     patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
                     patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
                    with self.assertRaises(AutomationError) as caught:
                        automation.run(ACCOUNT['login_email'], 'synthetic-password')
                self.assertEqual(caught.exception.status, 'login_required')
                self.assertEqual(action.call_count, 3)
                self.assertGreaterEqual(clock[0], automation.timeout)
                self.assertLess(clock[0], automation.timeout + 1)

    def test_navigation_not_dispatched_does_not_spend_real_click_budget(self):
        clock, state = [0.0], {'attempts': 0, 'authenticated': False}
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        attempt_times = []

        def action(stage, *_):
            self.assertEqual(stage, 'signed_out')
            state['attempts'] += 1
            attempt_times.append(clock[0])
            if state['attempts'] < 5:
                return False
            state['authenticated'] = True
            return True

        with patch.object(automation, '_view', side_effect=lambda: {'stage': 'waiting' if state['authenticated'] else 'signed_out'}), \
             patch.object(automation, '_identity', side_effect=lambda: IDENTITY.copy() if state['authenticated'] else None), \
             patch.object(automation, '_action', side_effect=action), \
             patch.object(automation, 'ensure_ready', return_value={'identity_verified': True}), \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        self.assertEqual(attempt_times, [0, 8, 16, 24, 32])

    def test_spent_navigation_budget_waits_for_late_panel_without_more_navigation(self):
        clock, count = [0.0], [0]
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))

        def action(stage, *_):
            self.assertEqual(stage, 'signed_out')
            count[0] += 1
            return True

        with patch.object(automation, '_view', side_effect=lambda: {'stage': 'waiting' if clock[0] >= 35 else 'signed_out'}), \
             patch.object(automation, '_identity', side_effect=lambda: IDENTITY.copy() if clock[0] >= 35 else None), \
             patch.object(automation, '_action', side_effect=action), \
             patch.object(automation, 'ensure_ready', return_value={'identity_verified': True}), \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        self.assertEqual(count[0], 3)
        self.assertGreaterEqual(clock[0], 35)

    def test_ignored_first_login_open_recovers_without_replaying_credentials(self):
        clock, state = [0.0], {'stage': 'signed_out', 'opens': 0, 'authenticated': False}
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        evidence = {'identity_verified': True, 'watermark_ready': True, 'agreement_ready': True}

        def action(stage, _email, _password):
            if stage == 'signed_out':
                state['opens'] += 1
                if state['opens'] == 2:
                    state['stage'] = 'provider'
            elif stage == 'provider':
                state['stage'] = 'email'
            elif stage == 'email':
                state['stage'] = 'password'
            elif stage == 'password':
                state.update(stage='waiting', authenticated=True)
            return True

        with patch.object(automation, '_identity', side_effect=lambda: IDENTITY if state['authenticated'] else None), \
             patch.object(automation, '_view', side_effect=lambda: {'stage': state['stage']}), \
             patch.object(automation, '_action', side_effect=action) as perform, \
             patch.object(automation, 'ensure_ready', return_value=evidence), \
             patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
             patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
            self.assertEqual(automation.run(ACCOUNT['login_email'], 'synthetic-password'), evidence)
        self.assertEqual([call.args[0] for call in perform.call_args_list],
                         ['signed_out', 'signed_out', 'provider', 'email', 'password'])

    def test_navigation_after_credential_continue_does_not_restart_login(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        with patch.object(automation, '_identity', return_value=None), \
             patch.object(automation, '_view', side_effect=[{'stage': 'email'}, {'stage': 'provider'}]), \
             patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.sleep'):
            with self.assertRaises(AutomationError) as caught:
                automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertEqual(caught.exception.status, 'login_failed')
        action.assert_called_once_with('email', ACCOUNT['login_email'], 'synthetic-password')

    def test_logged_in_run_does_not_replay_password(self):
        browser = browser_with_settings(READY)
        automation = AccountAutomation(ACCOUNT, browser=browser)
        callback = Mock()
        result = automation.run(ACCOUNT['login_email'], 'synthetic-password', callback)
        self.assertIs(result['identity_verified'], True)
        self.assertNotIn('Input.insertText', [call.args[0] for call in browser.command.call_args_list])
        self.assertEqual([call.args[0] for call in callback.call_args_list], ['login', 'settings', 'verifying'])

    def test_human_review_stages_never_click_or_read_credentials(self):
        for stage, status in [('challenge', 'challenge_required'), ('verification', 'verification_required'),
                              ('unknown', 'setup_required')]:
            with self.subTest(stage=stage):
                browser = browser_with_settings(READY)
                browser.evaluate.side_effect = None
                browser.evaluate.return_value = {'stage': stage}
                clock = [0.0]
                with patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
                     patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + .2)):
                    with self.assertRaises(AutomationError) as caught:
                        AccountAutomation(ACCOUNT, browser=browser).run(ACCOUNT['login_email'], 'synthetic-password')
                self.assertEqual(caught.exception.status, status)
                browser.post.assert_not_called()
                self.assertTrue(browser.command.call_args_list)
                self.assertTrue(all(call.args[0] == 'Page.bringToFront' for call in browser.command.call_args_list))

    def test_no_repeated_password_submission_while_login_is_pending(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        automation.timeout = .04
        with patch.object(automation, '_identity', return_value=None), \
             patch.object(automation, '_view', return_value={'stage': 'password'}), \
             patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.sleep'):
            with self.assertRaises(AutomationError):
                automation.run(ACCOUNT['login_email'], 'synthetic-password')
        action.assert_called_once_with('password', ACCOUNT['login_email'], 'synthetic-password')

    def test_input_failure_does_not_click_continue(self):
        browser = browser_with_settings(READY)
        browser.evaluate.side_effect = [{'stage': 'password'}, True, False]
        automation = AccountAutomation(ACCOUNT, browser=browser)
        self.assertFalse(automation._action('password', ACCOUNT['login_email'], 'synthetic-password'))
        self.assertFalse(any(_CLICK.splitlines()[0] in str(call.args[0]) and 'target.click()' in str(call.args[0])
                             for call in browser.evaluate.call_args_list))

    def test_known_optional_questionnaire_closes_without_selecting_occupation(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        with patch.object(automation, '_view', side_effect=[{'stage': 'questionnaire'}, {'stage': 'waiting'}]), \
             patch.object(automation, '_action', return_value=True) as action, \
             patch('app.account_automation.time.sleep'):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        action.assert_called_once_with('questionnaire', ACCOUNT['login_email'], 'synthetic-password')

    def test_navigation_transient_after_submit_does_not_replay_credentials(self):
        automation = AccountAutomation(ACCOUNT, browser=browser_with_settings(READY))
        with patch.object(automation, '_view', side_effect=[BrowserError('tab navigating'), {'stage': 'waiting'}]), \
             patch.object(automation, '_action') as action, \
             patch('app.account_automation.time.sleep'):
            result = automation.run(ACCOUNT['login_email'], 'synthetic-password')
        self.assertIs(result['identity_verified'], True)
        action.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM allowlist verification')
    def test_watermark_menu_uses_only_inner_avatar_container_never_profile_navigation_wrapper(self):
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8')),cfg=payload.config;
let clicks=[];
const make=name=>({checkVisibility:()=>true,getBoundingClientRect:()=>({width:40,height:40,left:10,right:50,top:10,bottom:50}),
 click(){clicks.push(name);},contains(e){return e===this;},querySelectorAll:()=>[],innerText:'',textContent:''});
const inner=make('menu-inner'),outer=make('profile-outer');
const exact='[class*="top-actions-logged-in-"] [class*="avatar-action-shell-"] [class*="avatar-container-"]';
const doc={elementFromPoint:()=>inner,querySelectorAll(s){
 if(s===exact)return cfg.missing?[]:cfg.duplicate?[inner,inner]:[inner];
 if(s==='[class*="top-actions-logged-in-"] [class*="avatar-action-shell-"] > [class*="avatar-action-"]')return [];
 if(s.includes('avatar-action-'))return [outer];
 return [];
}};
const context={document:doc,innerHeight:960,innerWidth:1280,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})};
const result=vm.runInNewContext(payload.source,context);process.stdout.write(JSON.stringify({result,clicks}));
'''
        for config, allowed in [({}, True), ({'missing': True}, False), ({'duplicate': True}, False)]:
            with self.subTest(config=config):
                completed = subprocess.run(['node', '-e', program],
                                           input=json.dumps({'config': config, 'source': _WATERMARK.replace('__ACTION__', json.dumps('open_avatar'))}),
                                           text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result['result']['stage'], 'avatar_target' if allowed else 'waiting')
                self.assertEqual(result['clicks'], [])

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM allowlist verification')
    def test_watermark_avatar_matches_observed_account40_hierarchy_without_synthetic_clicks(self):
        # Account40 has a modern action wrapping the legacy profile link.
        # Only a trusted click on the modern action opened its menu in the real UI.
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8')),cfg=payload.config;
let clicks=[],nodes=[],queries=[];
function make(id,cls,parent=null){const e={id,className:cls,parentElement:parent,innerText:'',textContent:'',
 checkVisibility:()=>!(cfg.hidden&&id==='menu-inner'),
 getBoundingClientRect:()=>({width:40,height:40,left:10,right:50,top:10,bottom:50}),
 click(){clicks.push(id);},contains(other){while(other){if(other===this)return true;other=other.parentElement;}return false;},
 querySelectorAll:()=>[]};nodes.push(e);return e;}
const top=make('logged-top',cfg.foreign?'top-actions-logged-out-X':'top-actions-BtxNrX top-actions-logged-in-S1DSeN');
const credit=make('credits','credit-action-surEHb',top),shell=make('profile-shell','avatar-action-shell-eadixq',credit);
if(!cfg.missing){
 const inner=make('menu-inner',cfg.legacy?'avatar-container-legacy':'avatar-action-t6xclo',shell);
 if(cfg.nestedLegacy)make('legacy-inner','avatar-container-legacy',inner);
 if(cfg.duplicate)make('menu-duplicate','avatar-action-t6xclo',shell);
}
const unrelated=make('unrelated-avatar','avatar-action-unrelated');
const modal=make('unknown-or-paid','lv-modal-wrapper');
let hitId=null;
function has(e,part){const m=/^\[class\*="([^"]+)"\]$/.exec(part);return !!(e&&m&&e.className.includes(m[1]));}
function matches(e,selector){const parts=selector.split(/\s+/),last=parts.pop();if(!has(e,last))return false;
 let cursor=e;
 while(parts.length){const part=parts.pop();
  if(part==='>'){cursor=cursor.parentElement;if(!has(cursor,parts.pop()))return false;}
  else {cursor=cursor.parentElement;while(cursor&&!has(cursor,part))cursor=cursor.parentElement;if(!cursor)return false;}
 }
 return true;
}
const document={elementFromPoint(){const hit=cfg.covered?unrelated:nodes.find(e=>e.id===(cfg.nestedLegacy?'legacy-inner':'menu-inner'));
 hitId=hit?.id||null;return hit;},querySelectorAll(s){
 queries.push(s);
 if(s==='[role=dialog],dialog,.lv-modal-wrapper')return cfg.modal?[modal]:[];
 return nodes.filter(e=>matches(e,s));
}};
const context={document,innerHeight:960,innerWidth:1280,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})};
const result=vm.runInNewContext(payload.source,context);process.stdout.write(JSON.stringify({result,clicks,queries,hitId}));
'''
        cases = [({}, 'menu-inner'), ({'legacy': True}, 'menu-inner'),
                 ({'nestedLegacy': True}, 'menu-inner'), ({'missing': True}, None),
                 ({'duplicate': True}, None), ({'hidden': True}, None),
                 ({'foreign': True}, None), ({'modal': True}, None), ({'covered': True}, None)]
        for config, target in cases:
            with self.subTest(config=config):
                completed = subprocess.run(['node', '-e', program],
                    input=json.dumps({'config': config, 'source': _WATERMARK.replace('__ACTION__', json.dumps('open_avatar'))}),
                    text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result['result']['stage'], 'blocked' if config.get('modal') else 'avatar_target' if target else 'waiting')
                self.assertEqual(result['clicks'], [])
                if target:
                    self.assertEqual(result['result'], {'stage': 'avatar_target', 'x': 30, 'y': 30})
                if config.get('nestedLegacy') or config.get('duplicate'):
                    self.assertFalse(any('avatar-container-' in query for query in result['queries']))

    def test_watermark_avatar_dispatches_trusted_mouse_click_and_releases_after_press_failure(self):
        browser = browser_with_settings(READY)
        browser.evaluate.side_effect = None
        browser.evaluate.return_value = {'stage': 'avatar_target', 'x': 30, 'y': 40}
        automation = AccountAutomation(ACCOUNT, browser=browser)
        self.assertEqual(automation._watermark_action('open_avatar'), {'stage': 'opened'})
        events = [call.args[1] for call in browser.command.call_args_list if call.args[0] == 'Input.dispatchMouseEvent']
        self.assertEqual(events, [
            {'type': 'mouseMoved', 'x': 30, 'y': 40, 'button': 'none', 'buttons': 0},
            {'type': 'mousePressed', 'x': 30, 'y': 40, 'button': 'left', 'buttons': 1, 'clickCount': 1},
            {'type': 'mouseReleased', 'x': 30, 'y': 40, 'button': 'left', 'buttons': 0, 'clickCount': 1},
        ])
        for value in (True, float('nan'), -1, None):
            with self.subTest(invalid_coordinate=value):
                browser.command.reset_mock()
                browser.evaluate.return_value = {'stage': 'avatar_target', 'x': value, 'y': 40}
                self.assertEqual(automation._watermark_action('open_avatar'), {'stage': 'waiting'})
                browser.command.assert_called_once_with('Page.bringToFront')
        browser.command.reset_mock()
        browser.evaluate.return_value = {'stage': 'blocked'}
        self.assertEqual(automation._watermark_action('open_avatar'), {'stage': 'blocked'})
        browser.command.assert_called_once_with('Page.bringToFront')
        browser.evaluate.return_value = {'stage': 'avatar_target', 'x': 30, 'y': 40}
        def command(name, arguments=None):
            if name == 'Input.dispatchMouseEvent' and arguments['type'] == 'mousePressed':
                raise BrowserError('synthetic press failure')
        browser.command.side_effect = command
        with self.assertRaises(BrowserError):
            automation._watermark_action('open_avatar')
        self.assertEqual(browser.command.call_args.args[1]['type'], 'mouseReleased')

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM allowlist verification')
    def test_known_promotion_dom_only_allows_close_and_rejects_other_offers_or_additional_dialogs(self):
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8')),cfg=payload.config;
let clicks=[];
const make=text=>({innerText:text,textContent:text,disabled:false,className:'',classList:[],checkVisibility:()=>true,
 getBoundingClientRect:()=>({width:400,height:40,left:10,right:410,top:10,bottom:50}),getAttribute:()=>null,
 querySelector:()=>null,querySelectorAll:()=>[],contains(e){return e===this;},click(){clicks.push(text);}});
const offer=make('Get offer'),close=make('Close'),panel=make('offer panel'),extra=make('Unknown consent');
const body=cfg.body||'Save up to 50% on plans to unlock Seedance. Seedance 2.5 at 720p from A$0.12/sec. Upgrade for up to 500 free Seedance 2.5 videos + 6,000 free GPT Image 2.5 images.';
const root=make(body);close.querySelectorAll=()=>cfg.badClose?[]:[make('')];
root.querySelectorAll=s=>s==='.lv-modal.lv-modal-simple.lv-modal-closable'?(cfg.badPanel?[]:[panel]):
 s==='button'?(cfg.extraButton?[offer,make('Subscribe')]:[offer]):s==='.lv-modal-close-icon'?
 (cfg.duplicateClose?[close,close]:[close]):s==='input,select,textarea'&&cfg.input?[make('')]:[];
const document={querySelectorAll(s){
 if(s==='.lv-modal-wrapper.wrapper-u1VKtB')return cfg.otherClass?[]:[root];
 if(s.startsWith('[role=dialog],dialog'))return cfg.extraDialog?[root,extra]:[root];
 if(s==='button,[role=button]')return [offer];
 return [];
}};
const context={document,innerHeight:960,innerWidth:1280,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})};
const view=vm.runInNewContext(payload.view,context),action=vm.runInNewContext(payload.action,context);
process.stdout.write(JSON.stringify({view,action,clicks}));
'''
        variants = [({}, True), ({'body': 'Save up to 50% on plans to unlock Seedance. '
            'Seedance 2.5 at 720p from A$0.12/sec. Upgrade for up to 500 Seedance 2.5 + '
            '6,000 GPT Image 2.5 free generations. Get offer'}, True),
                    ({'body': 'Upgrade now. Get offer'}, False), ({'otherClass': True}, False),
                    ({'extraDialog': True}, False), ({'extraButton': True}, False), ({'badClose': True}, False),
                    ({'duplicateClose': True}, False), ({'input': True}, False), ({'badPanel': True}, False)]
        for config, allowed in variants:
            with self.subTest(config=config):
                data = {'config': config, 'view': _VIEW, 'action': _CLICK.replace('__ACTION__', json.dumps('promotion'))}
                completed = subprocess.run(['node', '-e', program], input=json.dumps(data),
                                           text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result['view']['stage'], 'promotion' if allowed else 'unknown')
                self.assertIs(result['action'], allowed)
                self.assertEqual(result['clicks'], ['Close'] if allowed else [])

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM allowlist verification')
    def test_us_provider_without_heading_or_button_roles_requires_complete_official_structure(self):
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8')),cfg=payload.config;
let clicked=[];
const make=text=>({innerText:text,textContent:text,disabled:false,className:'',classList:[],checkVisibility:()=>true,
 getBoundingClientRect:()=>({width:400,height:40,left:10,right:410,top:10,bottom:50}),getAttribute:()=>null,
 querySelector:()=>null,querySelectorAll:()=>[],contains(e){return e===this;},click(){clicked.push(text);}});
const labels=cfg.labels||['Continue with Google','Continue with TikTok','Continue with Facebook','Continue with email','Continue with CapCut Mobile'];
const choices=labels.map(make),policies=(cfg.policy||['Terms of Service','Privacy Policy']).map(make);
const root=make('official provider'),wrapper=make('official login wrapper'),extra=make('Privacy preferences'),sidebar=make('Sign in to start creating');
root.closest=()=>cfg.noWrapper?null:wrapper;wrapper.contains=e=>e===root||e===wrapper;
root.querySelectorAll=s=>s==='.lv_new_third_part_sign_in_expand-button'?choices:
 s==='.lv_new_sign_in_panel_wide-policy a.policy'?policies:s==='input'&&cfg.input?[make('')]:[];
const document={querySelectorAll(s){
 if(s==='.lv_new_sign_in_panel_wide_new_base_page.modal')return cfg.duplicate?[root,root]:[root];
 if(s.startsWith('[role=dialog],dialog'))return cfg.extraDialog?[wrapper,extra]:[wrapper];
 if(s==='button,[role=button]')return [sidebar];
 return [];
}};
const context={document,innerHeight:960,innerWidth:1280,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})};
const view=vm.runInNewContext(payload.view,context),action=vm.runInNewContext(payload.action,context);
process.stdout.write(JSON.stringify({view,action,clicked}));
'''
        variants = [({}, True), ({'extraDialog': True}, False), ({'duplicate': True}, False),
                    ({'noWrapper': True}, False), ({'input': True}, False),
                    ({'policy': ['Terms of Service']}, False),
                    ({'labels': ['Continue with email']}, False)]
        for config, allowed in variants:
            with self.subTest(config=config):
                data = {'config': config, 'view': _VIEW, 'action': _CLICK.replace('__ACTION__', json.dumps('provider'))}
                completed = subprocess.run(['node', '-e', program], input=json.dumps(data),
                                           text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result['view']['stage'], 'provider' if allowed else 'unknown')
                self.assertIs(result['action'], allowed)
                self.assertEqual(result['clicked'], ['Continue with email'] if allowed else [])

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM allowlist verification')
    def test_observed_combined_email_password_panel_uses_one_continue_without_provider_heading(self):
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8')),cfg=payload.config;
let clicks=0;
const make=text=>({innerText:text,textContent:text,disabled:false,className:'',classList:[],checkVisibility:()=>true,
 getBoundingClientRect:()=>({width:400,height:40,left:10,right:410,top:10,bottom:50}),getAttribute:()=>null,
 querySelector:()=>null,querySelectorAll:()=>[],contains(e){return e===this;},click(){clicks++;}});
const heading=make('Welcome back'),root=make('Welcome back Continue'),wrapper=make('official login wrapper'),extra=make('Unknown consent');
const email=Object.assign(make(''),{type:'email',name:'username',autocomplete:'username',placeholder:'Enter email'});
const password=Object.assign(make(''),{type:'password',name:'password',autocomplete:'current-password',placeholder:'Enter password'});
const next=make('Continue');next.disabled=!!cfg.disabled;
heading.parentElement=root;root.parentElement=wrapper;wrapper.contains=e=>[heading,root,email,password,next,wrapper].includes(e);
root.querySelector=s=>s.includes('password')?password:s.includes('username')?email:null;
root.querySelectorAll=s=>s==='button,[role=button]'?[next]:s==='input[type=email][name=username]'?[email]:[];
const document={querySelectorAll(s){
 if(s==='h1,h2,h3,[role=heading]')return [heading];if(s==='input')return [email,password];
 if(s==='button,[role=button]')return [next];
 if(s.startsWith('[role=dialog],dialog'))return cfg.extraDialog?[wrapper,extra]:[wrapper];
 return [];
}};
const context={document,innerHeight:960,innerWidth:1280,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})};
const view=vm.runInNewContext(payload.view,context);
process.stdout.write(JSON.stringify({view}));
'''
        for config, expected in [({}, {'stage': 'password', 'disabled': False}),
                                 ({'disabled': True}, {'stage': 'password', 'disabled': True}),
                                 ({'extraDialog': True}, {'stage': 'unknown', 'reason': 'setup_required'})]:
            with self.subTest(config=config):
                completed = subprocess.run(['node', '-e', program], input=json.dumps({'config': config, 'view': _VIEW}),
                                           text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(json.loads(completed.stdout)['view'], expected)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM allowlist verification')
    def test_top_login_dom_ignores_sidebar_prompts_and_rejects_hidden_or_covered_targets(self):
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8')),cfg=payload.config;
const label={innerText:cfg.label||'Sign in',textContent:cfg.label||'Sign in',checkVisibility:()=>!cfg.hidden,
 getBoundingClientRect:()=>({width:80,height:28,left:1100,right:1180,top:80,bottom:108})};
const wrapper={checkVisibility:()=>!cfg.hidden,querySelectorAll:()=>[label],contains:e=>e===label,
 getBoundingClientRect:()=>({width:160,height:28,left:1100,right:1260,top:80,bottom:108})};
const sidebar={innerText:'Sign in to start creating',querySelectorAll:()=>[],contains:()=>false};
const doc={querySelectorAll(s){
 if(s.startsWith('[role=dialog],dialog'))return cfg.dialog?[wrapper]:[];
 if(s==='[role=button][class*="login-button-wrapper-"]')return cfg.noTop?[]:cfg.duplicate?[wrapper,wrapper]:[wrapper];
 return [sidebar,sidebar,wrapper];
},elementFromPoint:()=>cfg.covered?sidebar:label};
const context={document:doc,innerHeight:960,innerWidth:1280,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})};
process.stdout.write(JSON.stringify(vm.runInNewContext(payload.source,context)));
'''
        for config, allowed in [({}, True), ({'noTop': True}, False), ({'duplicate': True}, False),
                                 ({'hidden': True}, False), ({'covered': True}, False),
                                 ({'dialog': True}, False), ({'label': 'Subscribe'}, False)]:
            with self.subTest(config=config):
                completed = subprocess.run(['node', '-e', program], input=json.dumps({'config': config, 'source': _OPEN_LOGIN_TARGET}),
                                           text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result, {'x': 1140, 'y': 94} if allowed else None)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for DOM allowlist verification')
    def test_questionnaire_dom_signature_rejects_other_dialogs_and_changed_options(self):
        program = r'''
const vm=require('vm'),payload=JSON.parse(require('fs').readFileSync(0,'utf8'));
let clicks=0;
const make=(text,hidden=false)=>({innerText:text,textContent:text,className:'',classList:[],disabled:false,
 checkVisibility:()=>!hidden,getBoundingClientRect:()=>({width:400,height:40,top:10,left:10,bottom:50,right:410}),
 getAttribute:()=>null,querySelector:()=>null,querySelectorAll:()=>[],contains(e){return e===this;},click(){clicks++;}});
const expected=['Art professional','Designer','TV and film industry professional',
 'Digital marketing and e-commerce professional','Social media content creator','Tech professional','Other (please specify)'];
const cfg=payload.config,title=make(cfg.title||'What role best describes you?'),description=make("We'll use this to personalize your experience.");
const labels=cfg.options||expected,options=labels.map(make),close=make('');
const root=make('role questionnaire',!!cfg.hidden),extra=make('Subscription or privacy terms');
root.querySelector=s=>s.includes('title-')?title:s.includes('desc-')?description:null;
root.querySelectorAll=s=>s.includes('option-text-')?options:s.includes('close-btn-')?(cfg.closeCount===0?[]:cfg.closeCount===2?[close,make('')]:[close]):[];
const doc={querySelectorAll(s){
 if(s==='[class*="user-portrait-dialog-"]')return [root];
 if(s.startsWith('[role=dialog],dialog'))return cfg.extraDialog?[root,extra]:[root];
 if(s==='button,[role=button]')return [close,...options];
 return [];
}};
const context={document:doc,innerHeight:960,innerWidth:1280,getComputedStyle:()=>({display:'block',visibility:'visible',opacity:'1'})};
const view=vm.runInNewContext(payload.view,context),clicked=vm.runInNewContext(payload.action,context);
process.stdout.write(JSON.stringify({view,clicked,clicks}));
'''
        cases = [({}, True), ({'title': 'Privacy preferences'}, False), ({'extraDialog': True}, False),
                 ({'closeCount': 0}, False), ({'closeCount': 2}, False), ({'hidden': True}, False),
                 ({'options': ['Art professional', 'Designer']}, False)]
        for config, allowed in cases:
            with self.subTest(config=config):
                data = {'config': config, 'view': _VIEW,
                        'action': _CLICK.replace('__ACTION__', json.dumps('questionnaire'))}
                completed = subprocess.run(['node', '-e', program], input=json.dumps(data),
                                           text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result['view']['stage'] == 'questionnaire', allowed)
                self.assertIs(result['clicked'], allowed)
                self.assertEqual(result['clicks'], int(allowed))

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for browser-script syntax verification')
    def test_browser_scripts_compile_and_action_allowlist_excludes_generation(self):
        javascript = 'for (const text of JSON.parse(require("fs").readFileSync(0,"utf8"))) new Function("return "+text);'
        expressions = [_IDENTITY, _VIEW, _OPEN_LOGIN_TARGET, _CLICK.replace('__ACTION__', json.dumps('email')),
                       _WATERMARK.replace('__ACTION__', json.dumps('view'))]
        completed = subprocess.run(['node', '-e', javascript], input=json.dumps(expressions),
                                   text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for expression in expressions:
            self.assertNotIn('/generate', expression)
        self.assertNotIn('Confirm', _CLICK)
        self.assertNotIn('Save preference', _CLICK)


if __name__ == '__main__':
    unittest.main()
