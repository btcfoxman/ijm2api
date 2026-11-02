"""Observed pre-run transport failures enter durable retries with input ownership intact."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from test_canvas_execution import context, owner, task, fake_client
from test_canvas_preparation_retry_integration import upload_candidate
from test_canvas_preparation_transport import transport


@pytest.mark.parametrize('phase', ['uploading', 'saving'])
@pytest.mark.parametrize('failures', [1, 2])
def test_input_resources_do_not_block_zero_run_transport_retry_or_contaminate_replacement(context, phase, failures):
    store, service, _ = context
    first = owner(store, 'original', balance=200, priority=2)
    second = owner(store, 'replacement', balance=200)
    task(store)
    accounts = {value['id']: value for value in (first, second)}
    clients = {identifier: fake_client(store, service, account, quote=170)
               for identifier, account in accounts.items()}
    checkpoint = upload_candidate(first)
    checkpoint['binding']['project_id'] = f"project-{first['id']}"
    observed = {identifier: [] for identifier in accounts}

    for identifier, client in clients.items():
        original = client.prepare.side_effect

        def prepare(normalized, task_id, assets, *, persist, deadline, checkpoint=None,
                    identifier=identifier, original=original):
            observed[identifier].append(deepcopy(checkpoint))
            if identifier == first['id'] and len(observed[identifier]) <= failures:
                persist({'backend': 'canvas_cli', 'account_id': identifier, 'task_id': task_id,
                    'profile': f'synthetic-{identifier}', 'project_id': f'project-{identifier}',
                    'node_id': f'node-{task_id}-{identifier}', 'submit_id': f'submit-{task_id}-{identifier}',
                    'phase': phase, 'resource_ids': [],
                    'resources': {'images': ['uploaded-input'], 'videos': [], 'audios': []},
                    'uploaded_resources': ['uploaded-input'], 'upload_checkpoint': deepcopy(saved_checkpoint)})
                raise transport()
            return original(normalized, task_id, assets, persist=persist, deadline=deadline)

        client.prepare.side_effect = prepare

    saved_checkpoint = deepcopy(checkpoint)
    with patch.object(service, 'canvas_client', side_effect=lambda account: clients[account['id']]):
        service._run('task')

    row = store.task('task')
    assert row['status'] == 'succeeded', row['error_message']
    budget = row['upstream_context']['preparation_retry_budget']
    expected_owners = [first['id'], first['id']] + ([second['id']] if failures == 2 else [])
    assert [entry['account_id'] for entry in budget['attempts']] == expected_owners
    assert observed[first['id']] == [None, saved_checkpoint]
    assert observed[second['id']] == ([None] if failures == 2 else [])
    winner = second if failures == 2 else first
    assert row['account_id'] == winner['id']
    assert sum(client.submit.call_count for client in clients.values()) == 1
    clients[winner['id']].submit.assert_called_once()
    assert all(value == 0 for value in service._active.values())
    assert service._credit_reservations == {}
