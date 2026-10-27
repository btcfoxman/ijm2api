"""Account readiness and login safety without contacting Dreamina."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from app.account_automation import AccountAutomation, AutomationError, _CLICK, _IDENTITY, _VIEW, _WATERMARK
from app.browser import BrowserError


ACCOUNT = {'id': 42, 'cdp_port': 19342, 'proxy_url': 'socks5://proxy.invalid:10000',
           'login_email': 'operator@example.invalid'}
IDENTITY = {'authenticated': True, 'user_id': 'synthetic-user', 'email': 'operator@example.invalid'}
READY = {'close_watermark': True, 'close_watermark_popup_tip': True, 'aigc_compliance_confirmed': True}


def browser_with_settings(settings):
    browser = Mock()
    browser.evaluate.side_effect = lambda expression, **_: IDENTITY.copy() if expression == _IDENTITY else {'stage': 'waiting'}
    browser.post.return_value = {'user_custom_settings': settings.copy()}
    return browser


class ReadinessTests(unittest.TestCase):
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
        with patch.object(automation, '_watermark_action', return_value={'stage': 'blocked'}) as action:
            with self.assertRaises(AutomationError) as caught:
                automation.ensure_ready()
        self.assertEqual(caught.exception.status, 'setup_required')
        action.assert_called_once_with()
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


class LoginTests(unittest.TestCase):
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
                with self.assertRaises(AutomationError) as caught:
                    AccountAutomation(ACCOUNT, browser=browser).run(ACCOUNT['login_email'], 'synthetic-password')
                self.assertEqual(caught.exception.status, status)
                browser.post.assert_not_called()
                self.assertEqual([call.args[0] for call in browser.command.call_args_list], ['Page.bringToFront'])

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
 if(s==='[role=dialog],dialog')return cfg.extraDialog?[root,extra]:[root];
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
        expressions = [_IDENTITY, _VIEW, _CLICK.replace('__ACTION__', json.dumps('email')),
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
