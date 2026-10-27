"""A settings confirmation must never resume the site's pending paid Generate UI."""
from unittest.mock import Mock, patch

import pytest

from app.browser import BrowserRejected
from app.catalog import normalize_request
from app.client import DreaminaClient


def client():
    value = object.__new__(DreaminaClient)
    value.account = {'id': 1, 'cdp_port': 19301, 'proxy_url': 'socks5://proxy.test:20001'}
    value.browser = Mock()
    value.browser.post.return_value = {'aigc_data': {'history_record_id': 'history',
                                                  'task': {'task_id': 'task', 'submit_id': 'submit'}}}
    return value


def test_confirmed_settings_are_required_before_the_single_generation_request():
    value = client()
    with patch('app.account_automation.AccountAutomation') as setup:
        setup.return_value.ensure_ready.return_value = {'watermark_ready': True, 'agreement_ready': True}
        result = value.submit(normalize_request({'model': 'sd-2-0-mini', 'prompt': 'synthetic', 'duration': 4}),
                              {'images': [], 'videos': [], 'audios': []})
    assert result == {'run_id': 'submit', 'thread_id': 'history'}
    setup.return_value.ensure_ready.assert_called_once()
    assert value.browser.post.call_count == 1
    assert value.browser.post.call_args.args[0] == '/mweb/v1/aigc_draft/generate'


@pytest.mark.parametrize('ready', [
    {'watermark_ready': True, 'agreement_ready': False},
    {'watermark_ready': False, 'agreement_ready': True},
    {'watermark_ready': 'false', 'agreement_ready': True},
])
def test_missing_or_unverified_preferences_do_not_reach_generation(ready):
    value = client()
    with patch('app.account_automation.AccountAutomation') as setup:
        setup.return_value.ensure_ready.return_value = ready
        with pytest.raises(BrowserRejected):
            value.submit(normalize_request({'model': 'sd-2-0-mini', 'prompt': 'synthetic', 'duration': 4}),
                         {'images': [], 'videos': [], 'audios': []})
    value.browser.post.assert_not_called()
