"""Delivery presentation is a pure, whitelist-only view of canonical tasks."""
from copy import deepcopy

import pytest

from app.result_delivery import result_delivery_projection


def task(phase='downloading', **delivery):
    return {'execution_backend': 'canvas_cli', 'status': 'running', 'progress': 90,
        'upstream_context': {'result_delivery': {'version': 1, 'phase': phase,
            'retrying': False, **delivery}}}


@pytest.mark.parametrize('phase,retrying,expected', [
    ('generating', False, 'generating'), ('querying', False, 'querying'),
    ('querying', True, 'querying'), ('downloading', False, 'downloading'),
    ('downloading', True, 'downloading_retry'), ('transferring', False, 'transferring'),
    ('transferring', True, 'transferring_retry'),
])
def test_phase_labels_do_not_change_status_progress_or_input(phase, retrying, expected):
    original = task(phase, retrying=retrying)
    before = deepcopy(original)
    result = result_delivery_projection(original)
    assert result['result_stage'] == expected
    assert result['result_delivery_diagnostics'] == {'phase': phase, 'retrying': retrying}
    assert original == before
    assert original['status'] == 'running' and original['progress'] == 90


@pytest.mark.parametrize('stage', ['resume', 'create', 'upload', 'complete', 'presign',
                                  'checkpoint', 'validation', 'transfer', 'download', 'query', 'done'])
def test_fixed_transfer_and_validation_stages_are_preserved(stage):
    result = result_delivery_projection(task('transferring', stage=stage))
    assert result['result_delivery_diagnostics']['stage'] == stage


def test_diagnostics_keep_only_safe_scalars_and_observation_times():
    original = task('transferring', retrying=True, stage='upload', updated_at=100.5,
        generation_success_observed_at=80, download_completed_at=90,
        transfer_completed_at=95, token='PRIVATE', url='https://private.invalid/?token=PRIVATE',
        upload_checkpoints={'path': '/private/FILE'}, download_receipts=['PRIVATE'],
        last_error={'code': 'RESULT_TRANSFER_FAILED', 'stage': 'upload', 'http_status': 507,
            'retryable': True, 'at': 100, 'message': 'PRIVATE', 'detail': 'PRIVATE',
            'trace_url': 'https://private.invalid/PRIVATE'})
    assert result_delivery_projection(original) == {
        'result_stage': 'transferring_retry', 'result_delivery_diagnostics': {
            'phase': 'transferring', 'retrying': True, 'stage': 'upload', 'updated_at': 100.5,
            'generation_success_observed_at': 80, 'download_completed_at': 90,
            'transfer_completed_at': 95,
            'last_error': {'code': 'RESULT_TRANSFER_FAILED', 'stage': 'upload',
                'http_status': 507, 'retryable': True, 'at': 100}}}


@pytest.mark.parametrize('status,stage', [('succeeded', 'completed'), ('failed', 'failed'),
    ('expired', 'expired'), ('cancelled', 'cancelled'), ('canceled', 'cancelled')])
def test_terminal_status_wins_over_stale_phase_error_and_unknown(status, stage):
    original = task('transferring', retrying=True, last_error={'code': 'RESULT_TRANSFER_FAILED'},
        generation_success_observed_at=80, transfer_completed_at=100)
    original.update(status=status, error_code='UPSTREAM_SUBMIT_OUTCOME_UNKNOWN')
    assert result_delivery_projection(original) == {'result_stage': stage,
        'result_delivery_diagnostics': {'generation_success_observed_at': 80,
                                        'transfer_completed_at': 100}}


@pytest.mark.parametrize('code', ['UPSTREAM_SUBMIT_OUTCOME_UNKNOWN', 'CLI_SUBMISSION_UNCONFIRMED'])
def test_presentation_does_not_lift_legacy_unknown_guard(code):
    original = task('transferring', retrying=True)
    original.update(status='submitted', error_code=code)
    assert result_delivery_projection(original) == {}


@pytest.mark.parametrize('change', [
    {'execution_backend': 'browser'}, {'status': 'queued'}, {'status': 'preparing'},
    {'status': []}, {'upstream_context': None}, {'upstream_context': []},
    {'upstream_context': {'result_delivery': {}}},
    {'upstream_context': {'result_delivery': {'version': True, 'phase': 'downloading'}}},
    {'upstream_context': {'result_delivery': {'version': 2, 'phase': 'downloading'}}},
    {'upstream_context': {'result_delivery': {'version': 1, 'phase': []}}},
    {'upstream_context': {'result_delivery': {'version': 1, 'phase': 'unknown'}}},
    {'upstream_context': {'result_delivery': {'version': 1, 'phase': 'completed'}}},
])
def test_missing_invalid_and_incompatible_metadata_preserve_legacy_presentation(change):
    original = task()
    original.update(change)
    assert result_delivery_projection(original) == {}


@pytest.mark.parametrize('value', [True, '100', -1, float('nan'), float('inf'), 10**1000])
def test_invalid_timestamps_are_not_published(value):
    result = result_delivery_projection(task(updated_at=value, generation_success_observed_at=value,
        last_error={'at': value}))
    assert result == {'result_stage': 'downloading',
                       'result_delivery_diagnostics': {'phase': 'downloading', 'retrying': False}}


def test_malformed_or_secret_bearing_error_values_are_ignored_without_crashing():
    result = result_delivery_projection(task(retrying='true', stage=[],
        last_error={'code': 'https://private.invalid/?token=SECRET', 'stage': {},
                    'http_status': True, 'retryable': 1, 'at': 'SECRET'}))
    assert result == {'result_stage': 'downloading',
                       'result_delivery_diagnostics': {'phase': 'downloading', 'retrying': False}}
    assert result_delivery_projection({**task(), 'error_code': []})['result_stage'] == 'downloading'


def test_native_download_error_code_is_kept_without_raw_message():
    result = result_delivery_projection(task(last_error={'code': 'cli.download_read_failed',
        'stage': 'download', 'message': 'private provider text'}))
    assert result['result_delivery_diagnostics']['last_error'] == {
        'code': 'cli.download_read_failed', 'stage': 'download'}
