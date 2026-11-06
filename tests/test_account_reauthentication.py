"""Residual sessions must reauthenticate normally before a binding can be saved."""
from contextlib import nullcontext
import json
import shutil
import subprocess
from unittest.mock import Mock, patch

import pytest

from app.account_automation import AccountAutomation, AutomationError, UnboundSessionError, _IDENTITY, _SIGN_OUT, _VIEW


ACCOUNT = {'id': 42, 'cdp_port': 19342, 'proxy_url': 'socks5://proxy.invalid:10000',
           'login_email': 'operator@example.invalid'}
MASKED = {'authenticated': True, 'user_id': 'synthetic-user', 'email': 'o***tor@example.invalid'}


def automation():
    browser = Mock()
    browser.resource_timing_capture.side_effect = nullcontext
    return AccountAutomation(ACCOUNT, browser=browser)


def test_only_unbound_matching_masked_identity_requests_reauthentication():
    for identity, bound, expected in [(MASKED, '', UnboundSessionError),
            ({**MASKED, 'email': 'x***tor@example.invalid'}, '', AutomationError),
            (MASKED, 'different-user', AutomationError)]:
        instance = automation()
        instance.account = {**ACCOUNT, 'login_user_id': bound}
        instance.browser.evaluate.return_value = identity
        with pytest.raises(expected) as error:
            instance._identity()
        assert type(error.value) is expected
        instance.browser.post.assert_not_called()


def test_unbound_session_signs_out_once_then_uses_one_normal_password_action():
    instance = automation()
    clock = [0.0]
    observed = Mock()
    ready = {'identity_verified': True, 'watermark_ready': True, 'agreement_ready': True,
             'identity': {'user_id': MASKED['user_id'], 'email': MASKED['email']}}
    with patch.object(instance, '_view', side_effect=[{'stage': 'waiting'}, {'stage': 'password'}, {'stage': 'waiting'}]), \
         patch.object(instance, '_identity', side_effect=[UnboundSessionError('login_required', 'synthetic unbound'), ready['identity']]), \
         patch.object(instance, '_restart_unbound_session') as restart, \
         patch.object(instance, '_action', return_value=True) as action, \
         patch.object(instance, 'ensure_ready', return_value=ready), \
         patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
         patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 2.1)):
        assert instance.run(ACCOUNT['login_email'], 'synthetic-password', on_identity=observed) == ready
    restart.assert_called_once_with()
    action.assert_called_once_with('password', ACCOUNT['login_email'], 'synthetic-password')
    observed.assert_called_once_with(ready['identity'])


def test_uncertain_password_result_never_restarts_or_resubmits_credentials():
    instance = automation()
    clock = [0.0]
    with patch.object(instance, '_view', side_effect=[{'stage': 'password'}, {'stage': 'waiting'}]), \
         patch.object(instance, '_identity', side_effect=[None, UnboundSessionError('login_required', 'synthetic unbound')]), \
         patch.object(instance, '_restart_unbound_session') as restart, \
         patch.object(instance, '_action', return_value=True) as action, \
         patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
         patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 2.1)):
        with pytest.raises(UnboundSessionError):
            instance.run(ACCOUNT['login_email'], 'synthetic-password')
    restart.assert_not_called()
    action.assert_called_once_with('password', ACCOUNT['login_email'], 'synthetic-password')


def test_signout_requires_an_explicit_logged_out_view_before_password_flow():
    instance = automation()
    clock = [0.0]
    instance.browser.evaluate.side_effect = lambda expression, **_: True if expression == _SIGN_OUT else {'stage': 'waiting'}
    with patch.object(instance, '_watermark_action', return_value={'stage': 'menu'}), \
         patch('app.account_automation.time.monotonic', side_effect=lambda: clock[0]), \
         patch('app.account_automation.time.sleep', side_effect=lambda _: clock.__setitem__(0, clock[0] + 1)):
        with pytest.raises(AutomationError, match='signed-out state was not confirmed'):
            instance._restart_unbound_session()
    assert [call.args[0] for call in instance.browser.evaluate.call_args_list].count(_SIGN_OUT) == 1


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for DOM control verification')
@pytest.mark.parametrize('config,allowed', [({}, True), ({'dialog': True}, False),
    ({'duplicate': True}, False), ({'missing_watermark': True}, False), ({'unknown_class': True}, False)])
def test_signout_uses_only_the_recorded_account_menu_control(config, allowed):
    program = r'''
const {expression,cfg}=JSON.parse(require('fs').readFileSync(0,'utf8'));let clicks=[];
const node=(text,cls='')=>({innerText:text,textContent:text,className:cls,checkVisibility:()=>true,
 getBoundingClientRect:()=>({width:100,height:30}),click(){clicks.push(text)}});
const logout=node('Sign out',cfg.unknown_class?'unknown':'settings-row-synthetic'),watermark=node('Remove watermarks'),offer=node('Get offer');
const panel={...node(''),querySelectorAll(){return [offer,...(cfg.missing_watermark?[]:[watermark]),logout,...(cfg.duplicate?[logout]:[])]}};
global.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:'1'});
global.document={querySelectorAll(s){return s.startsWith('[role=dialog]')?(cfg.dialog?[node('Unknown consent')]:[]):[panel]}};
const result=require('vm').runInThisContext(expression);process.stdout.write(JSON.stringify({result,clicks}));
'''
    completed = subprocess.run(['node', '-e', program], input=json.dumps({'expression': _SIGN_OUT, 'cfg': config}),
                               text=True, capture_output=True, check=True)
    result = json.loads(completed.stdout)
    assert result == {'result': allowed, 'clicks': ['Sign out'] if allowed else []}
