"""Health history reads stay bounded, private and read-only across restart."""
from copy import deepcopy
import json
import sqlite3

import pytest

from app.canvas_account_health import account_cooldowns
from app.catalog import normalize_request
from app.store import Store
from test_canvas_account_health import ACCOUNT, MODEL, NOW, PROFILE, REQUEST, failure


@pytest.fixture
def store_case(tmp_path):
    store = Store(str(tmp_path / 'history.db'))
    row = store.upsert_account({'name': 'test-owner', 'proxy_url': ACCOUNT['proxy_url'],
        'execution_mode': 'canvas_cli'})
    account = {**ACCOUNT, 'id': row['id']}
    store.update_account(row['id'], {key: value for key, value in account.items() if key != 'id'})
    return store, account


def put(store, account, row):
    row = deepcopy(row)
    identifier = row['id']
    row['account_id'] = account['id']
    bound = row['upstream_context']
    bound['account_id'] = account['id']
    bound['upload_checkpoint']['binding']['account_id'] = account['id']
    private = 'original-private-prompt https://example.test/media?private=secret'
    body = {'model': row['model'], 'prompt': private, 'duration': 4,
            'image_urls': ['https://private.example/image?secret=1']}
    normalized = {**normalize_request(body), 'kind': 'video'}
    store.create_task(identifier, body, normalized,
        execution_backend=row['execution_backend'])
    return store.update_task(identifier, {key: value for key, value in row.items()
        if key not in {'id', 'model', 'normalized', 'created_at'}})


def fingerprint(store):
    with store._connect() as db:
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY 1')]
                for name in ('tasks', 'accounts', 'settings')}


def test_projected_real_rows_support_health_and_survive_restart_without_modifying_old_tasks(store_case):
    store, account = store_case
    before = put(store, account, failure(severe=True))
    original = fingerprint(store)
    rows = store.canvas_account_health_rows(since=NOW-86400)
    assert len(rows) == 1 and rows[0]['normalized'] == {**REQUEST}
    result = account_cooldowns(rows, [account], REQUEST, profiles={account['id']: PROFILE}, now=NOW)
    assert result[account['id']]['reason'] == 'original_dispatch_system_failure'
    assert fingerprint(store) == original and store.task(before['id']) == before
    restarted = Store(str(store.path))
    again = restarted.canvas_account_health_rows(since=NOW-86400)
    assert again == rows
    assert account_cooldowns(again, [account], REQUEST, profiles={account['id']: PROFILE}, now=NOW) == result


def test_history_projection_omits_prompts_material_urls_error_prose_and_arbitrary_extras(store_case):
    store, account = store_case
    row = failure(severe=True)
    bound = row['upstream_context']
    bound['submit_error'].update(message='private-prompt secret-key', traceId='secret-id',
        raw_response='https://signed.example/result?token=private')
    bound['previous_submissions'] = [{'prompt': 'private-prompt secret-key'}]
    bound['image_model_route'] = {'account_id': account['id'], 'effective_model': 'public-model',
        'debug_note': 'private-key'}
    bound['missing_resource_recovery']['secret_url'] = 'https://private.example/x'
    bound['preparation_timing']['commands']['arbitrary'] = {'prompt': 'private-prompt secret-key'}
    put(store, account, row)
    rows = store.canvas_account_health_rows(since=NOW-86400)
    encoded = json.dumps(rows)
    for value in ('private-prompt', 'secret-key', 'secret-id', 'private-key', 'https://', 'original-private-prompt'):
        assert value not in encoded
    assert rows[0]['upstream_context']['previous_submissions'] is True
    assert rows[0]['upstream_context']['submit_error']['code'] == 'cli.invalid_service_response'


def test_history_uses_readonly_uri_query_only_and_executes_no_writes(store_case, monkeypatch):
    store, account = store_case
    put(store, account, failure(severe=True))
    original = fingerprint(store)
    native = sqlite3.connect
    connections = []; statements = []; totals = []
    class ReadConnection(sqlite3.Connection):
        def close(self):
            totals.append(self.total_changes)
            super().close()
    def connect(*args, **kwargs):
        assert '?mode=ro' in args[0] and kwargs['uri'] is True
        connection = native(*args, **kwargs, factory=ReadConnection)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection
    with monkeypatch.context() as patch:
        patch.setattr('app.store.sqlite3.connect', connect)
        assert store.canvas_account_health_rows(since=NOW-86400)
    assert len(connections) == 1 and totals == [0]
    assert statements[0].upper() == 'PRAGMA QUERY_ONLY=ON'
    assert all(statement.lstrip().upper().startswith(('PRAGMA ', 'WITH ')) for statement in statements)
    assert fingerprint(store) == original


def test_history_limit_and_time_bounds_do_not_get_renewed_by_later_updates(store_case):
    store, account = store_case
    put(store, account, failure('latest', ago=5))
    put(store, account, failure('previous', ago=30))
    put(store, account, failure('old', ago=90000))
    rows = store.canvas_account_health_rows(since=NOW-86400, limit=1)
    assert [row['id'] for row in rows] == ['latest']
    rows = store.canvas_account_health_rows(since=NOW-86400, limit=5)
    assert [row['id'] for row in rows] == ['latest', 'previous']


def test_browser_pending_and_unbound_rows_cannot_enter_canvas_health_history(store_case):
    store, account = store_case
    row = failure('browser', severe=True); row['execution_backend'] = 'browser'
    put(store, account, row)
    pending = put(store, account, failure('pending'))
    store.update_task(pending['id'], {'status': 'queued'})
    assert store.canvas_account_health_rows(since=NOW-86400) == []


def test_malformed_legacy_json_and_explicit_manual_null_are_safely_ignored(store_case):
    store, account = store_case
    put(store, account, failure('malformed', severe=True))
    with store._connect() as db:
        db.execute("UPDATE tasks SET upstream_context_json='broken-json' WHERE id='malformed'")
    manual = failure('manual', severe=True)
    manual['upstream_context']['manual_termination'] = None
    put(store, account, manual)
    rows = store.canvas_account_health_rows(since=NOW-86400)
    assert len(rows) == 2
    assert account_cooldowns(rows, [account], REQUEST, profiles={account['id']: PROFILE}, now=NOW) == {}


@pytest.mark.parametrize('kwargs', [
    {'since': True}, {'since': -1}, {'since': float('nan')}, {'since': float('inf')},
    {'since': 0, 'limit': True}, {'since': 0, 'limit': 0}, {'since': 0, 'limit': 1001},
])
def test_history_rejects_unbounded_or_invalid_inputs(store_case, kwargs):
    with pytest.raises(ValueError):
        store_case[0].canvas_account_health_rows(**kwargs)
