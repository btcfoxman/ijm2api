from copy import deepcopy

import pytest

from app.catalog import normalize_request
from test_canvas_admin_api import context


def stored_task(main, account_id, *, status='running'):
    body = {'model': 'sd-2-0-mini', 'prompt': 'Synthetic landscape', 'duration': 4}
    main.store.create_task('delivery-example', body, normalize_request(body))
    return main.store.update_task('delivery-example', {'account_id': account_id,
        'execution_backend': 'canvas_cli', 'status': status, 'progress': 95,
        'upstream_context': {'private_identity': 'PRIVATE-IDENTITY',
            'download_receipts': {'resource': {'path': '/private/FILE'}},
            'upload_checkpoints': {'upload': {'upload_id': 'PRIVATE-UPLOAD'}},
            'result_delivery': {'version': 1, 'phase': 'transferring', 'stage': 'upload',
                'retrying': True, 'updated_at': 100, 'download_completed_at': 90,
                'last_error': {'code': 'RESULT_TRANSFER_FAILED', 'stage': 'upload',
                    'http_status': 507, 'retryable': True, 'at': 99,
                    'message': 'PRIVATE-RAW', 'url': 'https://private.invalid/PRIVATE'}}}})


def test_admin_list_detail_and_caller_get_share_safe_stage_and_do_not_write(context):
    main, api, headers, account_id = context
    row = stored_task(main, account_id)
    before = deepcopy(main.store.task(row['id']))
    responses = [api.get('/api/tasks', headers=headers),
                 api.get(f'/api/tasks/{row["id"]}', headers=headers),
                 api.get(f'/v1/videos/{row["id"]}', headers={'X-API-Key': 'synthetic-canvas-api'})]
    for index, response in enumerate(responses):
        assert response.status_code == 200
        value = response.json()[0] if index == 0 else response.json()
        assert value['status'] == 'running'
        assert value['result_stage'] == 'transferring_retry'
        assert value['result_delivery_diagnostics'] == {
            'phase': 'transferring', 'stage': 'upload', 'retrying': True, 'updated_at': 100,
            'download_completed_at': 90, 'last_error': {'code': 'RESULT_TRANSFER_FAILED',
                'stage': 'upload', 'http_status': 507, 'retryable': True, 'at': 99}}
        for private in ('PRIVATE', '/private/', 'upload_checkpoints', 'download_receipts', 'upstream_context'):
            assert private not in response.text
    assert main.store.task(row['id']) == before
    main.service.canvas_cli.run.assert_not_called()


@pytest.mark.parametrize('status,expected', [('succeeded', 'completed'), ('failed', 'failed')])
def test_actual_admin_terminal_response_never_keeps_retry_phase(context, status, expected):
    main, api, headers, account_id = context
    row = stored_task(main, account_id, status=status)
    result = api.get(f'/api/tasks/{row["id"]}', headers=headers).json()
    assert result['status'] == status and result['result_stage'] == expected
    assert result['result_delivery_diagnostics'] == {'download_completed_at': 90}
    assert main.store.task(row['id']) == row
