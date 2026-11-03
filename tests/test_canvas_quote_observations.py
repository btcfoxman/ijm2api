"""Quote diagnostics retain decisions, never prompts, URLs, credentials or receipts."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from app.canvas_cli import CanvasCLIError
from app.canvas_timing import PreparationTiming


def observe(timing, result):
    return timing.command(object(), {'id': 19, 'proxy_url': 'private-proxy'},
        ['node', 'quote', '--project-id', 'private-project', '--node-id', 'private-node'],
        {}, 1, lambda: result)


def test_nonconfirmable_quote_records_shape_and_typed_reason_without_private_values():
    timing = PreparationTiming('quote-diagnostic')
    result = {'confirmable': False, 'confirmationRequired': True, 'totalMaxCredits': 0,
        'items': [{'nodeId': 'private-node', 'prompt': 'private-prompt',
                   'generationDiff': {'duration': {'before': 'private-text', 'after': 5},
                                      'https://private/?token=secret': 'private-token'},
                   'error': {'class': 'invalid_input', 'requiredAction': 'fix_input',
                             'retryable': False, 'serviceCode': 20103,
                             'code': 'private-opaque-error', 'message': 'private-upstream-text',
                             'url': 'https://private/?token=secret', 'httpStatus': 400}}]}
    before = deepcopy(result)
    assert observe(timing, result) is result and result == before
    row = timing.snapshot()['recent_commands'][0]
    facts = row['quote_facts']
    assert row['outcome'] == 'success' and facts['confirmable'] is False
    assert facts['total_max_credits'] == 0 and facts['item_count'] == 1
    item = facts['items'][0]
    assert item['requested_node_matches'] is True and item['generation_diff_fields'] == ['duration']
    assert item['class'] == 'invalid_input' and item['requiredAction'] == 'fix_input'
    assert item['serviceCode'] == 20103 and item['http_status'] == 400
    assert 'code_sha256' in item and item['retryable'] is False
    assert not any(secret in json.dumps(timing.snapshot()) for secret in (
        'private-', 'https://', 'secret', 'private-opaque-error'))


def test_missing_or_multiple_items_remain_observable_without_choosing_a_generation():
    timing = PreparationTiming('quote-shapes')
    assert observe(timing, {'confirmable': True, 'items': []})['items'] == []
    assert observe(timing, {'items': [None] * 100})['items'] == [None] * 100
    rows = timing.snapshot()['recent_commands']
    assert rows[0]['quote_facts']['item_count'] == 0
    assert rows[1]['quote_facts']['item_count'] == 100
    assert len(rows[1]['quote_facts']['items']) == 3
    assert timing.snapshot()['commands']['node quote']['count'] == 2


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), -1, 10**20, 'private-token'])
def test_untrusted_credit_and_error_fields_cannot_pollute_quote_audit(value):
    timing = PreparationTiming('quote-values')
    observe(timing, {'confirmable': 'private-token', 'totalMaxCredits': value,
        'items': [{'error': {'class': 'private-class', 'requiredAction': 'private-action',
                             'retryable': 'private-token', 'serviceCode': value}}]})
    facts = timing.snapshot()['recent_commands'][0]['quote_facts']
    assert 'total_max_credits' not in facts and 'confirmable' not in facts
    assert 'private-' not in json.dumps(timing.snapshot())


def test_quote_observation_neither_replaces_failed_command_nor_retries():
    timing = PreparationTiming('quote-network');failure = CanvasCLIError('cli.transport_failed', 'network')
    calls = []
    def fail():
        calls.append(True)
        raise failure
    with pytest.raises(CanvasCLIError) as caught:
        timing.command(object(), {'id': 19}, ['node', 'quote'], {}, 1, fail)
    assert caught.value is failure and calls == [True]
    assert 'quote_facts' not in timing.snapshot()['recent_commands'][0]


def test_broken_quote_diagnostic_never_changes_original_command_result():
    timing = PreparationTiming('quote-observer');result = {'confirmable': False}
    with patch('app.canvas_timing._quote_observation', side_effect=RuntimeError('observer failed')):
        assert observe(timing, result) is result
    assert timing.snapshot()['recent_commands'][0]['outcome'] == 'success'
