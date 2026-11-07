"""Real ASGI boundaries without importing main or starting live task workers."""
import ast
from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import Body, FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.admission import AdmissionRejected, register_admission_errors
from app.canvas_cli import sanitize
from app.errors import (GenerationCapabilityDenied, RegionGenerationDenied,
                        RETRYABLE_GENERATION_MESSAGE, public_error)
from app.region_policy import DEFAULT_REGION_GENERATION_POLICY
from test_canvas_execution import context, owner


def boundary(service):
    # Execute the actual endpoint functions; importing app.main would open its
    # configured Store. The app here has no lifespan/auth/worker side effects.
    source = Path(__file__).parents[1] / 'app/main.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {'_admission_rejection', '_create', 'create_image_response'}]
    for node in functions:
        node.decorator_list = []
    namespace = {'Any': object, 'service': service, 'HTTPException': HTTPException, 'Body': Body,
                 'AdmissionRejected': AdmissionRejected, 'RegionGenerationDenied': RegionGenerationDenied,
                 'GenerationCapabilityDenied': GenerationCapabilityDenied, 'public_error': public_error,
                 'sanitize': sanitize, 'logger': logging.getLogger('admission-boundary-fixture')}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
    app = FastAPI()
    register_admission_errors(app)
    app.post('/v1/responses')(namespace['create_image_response'])
    app.post('/v1/videos')(namespace['_create'])
    return TestClient(app)


def assert_channel_denial(response, code):
    assert response.status_code == 503
    assert response.headers['x-ijmapi-submit-outcome'] == 'not-accepted'
    assert response.headers['x-ijmapi-error-code'] == code
    value = response.json()
    assert value['detail'] == RETRYABLE_GENERATION_MESSAGE
    assert value['error'] == {'code': code, 'message': RETRYABLE_GENERATION_MESSAGE,
        'type': 'upstream_unavailable', 'retryable': True, 'phase': 'admission',
        'failure_scope': 'channel', 'generation_dispatched': False, 'outcome': 'not_accepted'}
    assert value['submission_outcome'] == 'not-accepted'
    assert not any(key in value for key in ('id', 'task_id', 'generation_id', 'upstream_task_id'))


def test_live_shape_image_policy_denial_is_retryable_channel_unavailability_without_task(context, monkeypatch):
    store, service, _ = context
    owner(store)
    policy = deepcopy(DEFAULT_REGION_GENERATION_POLICY)
    policy['other'] = {'enabled': True, 'models': {
        'sd-2-0-fast': {'min_duration': 4, 'max_duration': 15},
        'sd-2-0-mini': {'min_duration': 4, 'max_duration': 15},
        'sd-2-5': {'min_duration': 4, 'max_duration': 24},
        'sd-2-5-480p-preview': {'min_duration': 4, 'max_duration': 30},
    }}
    store.update_settings({'region_generation_policy': policy})
    accounts_before, settings_before = store.accounts(private=True), store.get_settings()
    create_task = Mock(wraps=store.create_task)
    monkeypatch.setattr(store, 'create_task', create_task)
    monkeypatch.setattr(service.canvas_cli, 'run', Mock(side_effect=AssertionError('CLI forbidden before admission')))
    api = boundary(service)
    try:
        response = api.post('/v1/responses', json={'model': 'seedream-5.0-pro', 'prompt': 'Synthetic',
            'resolution': '2k', 'ratio': '9:16', 'n': 1, 'background': True})
        assert_channel_denial(response, 'REGION_GENERATION_DENIED')
        assert 'region' not in response.text and 'seedream' not in response.text
        assert store.tasks() == []
        assert store.accounts(private=True) == accounts_before
        assert store.get_settings() == settings_before
        create_task.assert_not_called()
        service._pool.submit.assert_not_called()
        service.canvas_cli.run.assert_not_called()
        service.browser_runtime.acquire.assert_not_called()
    finally:
        api.close()


@pytest.mark.parametrize('path,method', [('/v1/responses', 'create_image'), ('/v1/videos', 'create')])
@pytest.mark.parametrize('error,code', [(RegionGenerationDenied, 'REGION_GENERATION_DENIED'),
                                      (GenerationCapabilityDenied, 'JIMENG_PROTOCOL_UNAVAILABLE')])
def test_typed_preflight_denials_keep_safe_code_but_never_raw_reason(path, method, error, code, caplog):
    service = SimpleNamespace(create=Mock(), create_image=Mock())
    getattr(service, method).side_effect = error('synthetic policy reason token=PRIVATE_CREDENTIAL')
    api = boundary(service)
    try:
        with caplog.at_level(logging.WARNING):
            response = api.post(path, json={'model': 'synthetic'})
        assert_channel_denial(response, code)
        assert 'PRIVATE_CREDENTIAL' not in response.text and 'synthetic policy' not in response.text
        assert 'synthetic policy reason' in caplog.text
        assert 'PRIVATE_CREDENTIAL' not in caplog.text
        getattr(service, method).assert_called_once()
    finally:
        api.close()


def test_real_image_parameter_error_stays_422_and_does_not_become_retryable_policy_error(context, monkeypatch):
    store, service, _ = context
    owner(store)
    create_task = Mock(wraps=store.create_task)
    monkeypatch.setattr(store, 'create_task', create_task)
    api = boundary(service)
    try:
        response = api.post('/v1/responses', json={'model': 'seedream-5.0-pro', 'prompt': 'Synthetic',
                                                'resolution': '1k', 'ratio': '9:16', 'n': 1})
        assert response.status_code == 422
        assert response.json()['detail'] == 'unsupported image resolution: 1k'
        assert 'error' not in response.json()
        assert response.headers['x-ijmapi-submit-outcome'] == 'not-accepted'
        assert 'x-ijmapi-error-code' not in response.headers
        create_task.assert_not_called()
        assert store.tasks() == []
    finally:
        api.close()
