"""Jimeng account tools cannot enter an international login or CLI session."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import Body, HTTPException
import pytest

from app.auto_login import AutoLoginManager
from app.browser import BrowserError, account_browser_options
from app.canvas_auth import CanvasAuthManager
from app.errors import (BOUNDED_RETRY_FAILURE_MESSAGE, GenerationCapabilityDenied,
                        RegionGenerationDenied, public_error)
from app.store import Store


@pytest.mark.parametrize('proxy', ['', 'socks5://synthetic.invalid:20001'])
def test_jimeng_rejects_international_authorization_before_any_side_effect(tmp_path, proxy):
    store = Store(str(tmp_path / 'accounts.db'))
    row = store.upsert_account({'name': 'synthetic-cn', 'execution_mode': 'jimeng_web',
                                'proxy_url': proxy, 'enabled': False})
    before = store.account(row['id'], private=True)
    service = SimpleNamespace(_lock=threading.RLock(), _active={})
    automatic = AutoLoginManager(store, service)
    client, browser = Mock(), Mock()
    canvas = CanvasAuthManager(store, service, client_factory=client, browser_factory=browser)
    try:
        automatic._pool.shutdown(wait=True)
        canvas._pool.shutdown(wait=True)
        automatic._pool = Mock()
        canvas._pool = Mock()
        with pytest.raises(ValueError, match='Jimeng'):
            automatic.start(row['id'])
        with pytest.raises(ValueError, match='Jimeng'):
            canvas.start(row['id'])
        automatic._pool.submit.assert_not_called()
        canvas._pool.submit.assert_not_called()
        client.assert_not_called()
        browser.assert_not_called()
        assert store.account(row['id'], private=True) == before
    finally:
        automatic.close()
        canvas.close()


@pytest.mark.parametrize('mode,options', [
    ('jimeng_web', {'site': 'jimeng'}), ('browser', {}), ('canvas_cli', {}),
])
def test_browser_admin_view_uses_the_accounts_actual_site(mode, options):
    # Importing the application entry point opens its configured live Store.
    # Execute only this real endpoint helper with explicit, inert dependencies.
    tree = ast.parse((Path(__file__).parents[1] / 'app/main.py').read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_browser']
    assert len(functions) == 1
    row = {'id': 7, 'cdp_port': 19999, 'execution_mode': mode}
    service = SimpleNamespace(browser_view=Mock(return_value=row), open_browser_view=Mock(return_value=row))
    factory = Mock()
    namespace = {'Any': object, 'CDPBrowser': factory, 'service': service,
                 'BrowserError': BrowserError, 'HTTPException': HTTPException,
                 'account_browser_options': account_browser_options}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'main-browser-site', 'exec'), namespace)
    account, view = namespace['_browser'](7)
    assert account is row and view is factory.return_value
    factory.assert_called_once_with(19999, **options)
    service.browser_view.assert_called_once_with(7)
    service.open_browser_view.assert_not_called()


@pytest.mark.parametrize('endpoint,method', [('_create', 'create'), ('create_image_response', 'create_image')])
@pytest.mark.parametrize('error', [RegionGenerationDenied, GenerationCapabilityDenied])
def test_new_admission_rejections_use_approved_messages_and_no_acceptance(endpoint, method, error):
    tree = ast.parse((Path(__file__).parents[1] / 'app/main.py').read_text(encoding='utf-8'))
    names = {'_admission_rejection', endpoint}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in functions:
        node.decorator_list = []
    service = SimpleNamespace(create=Mock(), create_image=Mock())
    diagnostic = 'synthetic Jimeng region/capability detail for administrator'
    getattr(service, method).side_effect = error(diagnostic)
    logger = Mock()
    namespace = {'Any': object, 'service': service, 'HTTPException': HTTPException, 'Body': Body,
                 'GenerationCapabilityDenied': GenerationCapabilityDenied, 'RegionGenerationDenied': RegionGenerationDenied,
                 'public_error': public_error, 'logger': logger}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'main-admission', 'exec'), namespace)
    with pytest.raises(HTTPException) as caught:
        namespace[endpoint]({'model': 'synthetic'})
    assert caught.value.status_code == 422
    assert caught.value.detail == BOUNDED_RETRY_FAILURE_MESSAGE
    assert caught.value.headers == {'X-IJMAPI-Submit-Outcome': 'not-accepted'}
    logger.info.assert_called_once_with('Generation rejected before submission: %s', getattr(service, method).side_effect)
    getattr(service, method).assert_called_once_with({'model': 'synthetic'})


def test_existing_request_validation_keeps_its_specific_diagnostic():
    tree = ast.parse((Path(__file__).parents[1] / 'app/main.py').read_text(encoding='utf-8'))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_create')
    service = SimpleNamespace(create=Mock(side_effect=ValueError('unsupported aspect_ratio')))
    namespace = {'Any': object, 'service': service, 'HTTPException': HTTPException,
                 'GenerationCapabilityDenied': GenerationCapabilityDenied, 'RegionGenerationDenied': RegionGenerationDenied}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'main-existing-validation', 'exec'), namespace)
    with pytest.raises(HTTPException) as caught:
        namespace['_create']({'model': 'synthetic'})
    assert caught.value.status_code == 422
    assert caught.value.detail == 'unsupported aspect_ratio'
    assert caught.value.headers == {'X-IJMAPI-Submit-Outcome': 'not-accepted'}
