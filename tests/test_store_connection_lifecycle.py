"""Store connection scopes own their real SQLite handles and transactions."""
from contextlib import closing
import sqlite3

import pytest

from app.store import Store


@pytest.fixture
def connections(monkeypatch):
    original_connect = sqlite3.connect
    opened = []

    class TrackedConnection(sqlite3.Connection):
        close_calls = 0

        def close(self):
            self.close_calls = getattr(self, 'close_calls', 0) + 1
            return super().close()

    def connect(*args, **kwargs):
        kwargs['factory'] = TrackedConnection
        connection = original_connect(*args, **kwargs)
        opened.append(connection)  # Strong refs make the check independent of GC.
        return connection

    monkeypatch.setattr('app.store.sqlite3.connect', connect)
    yield opened, original_connect
    # The tested code must close every handle; cleanup also runs on a red test.
    for connection in opened:
        if not getattr(connection, 'close_calls', 0):
            connection.close()


def assert_closed(connection):
    assert connection.close_calls == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')


def scalar(original_connect, path, query):
    with closing(original_connect(str(path))) as connection:
        return connection.execute(query).fetchone()[0]


def test_successful_transaction_commits_before_explicit_close_and_keeps_connection_configuration(tmp_path, connections):
    opened, original_connect = connections
    store = Store(str(tmp_path / 'store.db'))
    for connection in opened:
        assert_closed(connection)
    with store._connect() as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert connection.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
        connection.execute("INSERT INTO settings(key,value) VALUES('committed','42')")
        assert connection.in_transaction
    assert_closed(connection)
    assert scalar(original_connect, store.path, "SELECT value FROM settings WHERE key='committed'") == '42'


def test_body_exception_rolls_back_before_explicit_close(tmp_path, connections):
    opened, original_connect = connections
    store = Store(str(tmp_path / 'store.db'))
    error = RuntimeError('synthetic body error')
    with pytest.raises(RuntimeError) as caught:
        with store._connect() as connection:
            connection.execute("INSERT INTO settings(key,value) VALUES('rolled-back','42')")
            raise error
    assert caught.value is error
    assert_closed(connection)
    assert scalar(original_connect, store.path, "SELECT COUNT(*) FROM settings WHERE key='rolled-back'") == 0
    assert all(item.close_calls == 1 for item in opened)


def test_commit_error_rolls_back_and_still_closes_real_connection(tmp_path, connections):
    _, original_connect = connections
    store = Store(str(tmp_path / 'store.db'))
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        with store._connect() as connection:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
            connection.execute('CREATE TABLE child(parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')
            connection.execute('INSERT INTO child(parent_id) VALUES(99)')
    assert_closed(connection)
    assert scalar(original_connect, store.path, 'SELECT COUNT(*) FROM child') == 0


@pytest.mark.parametrize('failed_pragma', ['PRAGMA journal_mode=WAL', 'PRAGMA busy_timeout=30000'])
def test_connection_initialization_error_closes_already_open_sqlite_handle(tmp_path, monkeypatch, failed_pragma):
    original_connect = sqlite3.connect
    opened = []
    error = sqlite3.OperationalError('synthetic pragma failure')

    class FailingConnection(sqlite3.Connection):
        close_calls = 0

        def execute(self, sql, *args, **kwargs):
            if sql == failed_pragma:
                raise error
            return super().execute(sql, *args, **kwargs)

        def close(self):
            self.close_calls += 1
            return super().close()

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=FailingConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr('app.store.sqlite3.connect', connect)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            Store(str(tmp_path / 'store.db'))
        assert caught.value is error and len(opened) == 1
        assert_closed(opened[0])
    finally:
        for connection in opened:
            if not connection.close_calls:
                connection.close()


def test_store_schema_initialization_failure_closes_connection(tmp_path, connections):
    opened, original_connect = connections
    path = tmp_path / 'broken-schema.db'
    with closing(original_connect(str(path))) as connection:
        with connection:
            connection.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY)')
    with pytest.raises(sqlite3.OperationalError, match='no such column'):
        Store(str(path))
    assert len(opened) == 1
    assert_closed(opened[0])


def test_pending_account_can_read_inserted_cursor_id_after_scope_closes(tmp_path, connections):
    opened, _ = connections
    store = Store(str(tmp_path / 'store.db'))
    account = store.create_pending_account('pending-account', 'socks5://proxy.test:20001')
    assert account['id'] > 0 and account['name'] == 'pending-account'
    assert store.account(account['id'])['name'] == 'pending-account'
    assert all(connection.close_calls == 1 for connection in opened)


def test_early_return_and_repeated_public_calls_leave_no_open_connections(tmp_path, connections):
    opened, _ = connections
    store = Store(str(tmp_path / 'store.db'))
    for index in range(20):
        assert store.update_settings({'iteration': index})['iteration'] == index
        assert store.get_settings()['iteration'] == index
        assert store.account(9999) is None
        assert store.update_task('missing-task', {'status': 'failed'}) == {}
        assert store.delete_account(9999) is False
        assert store.scheduling_rows() == {'history': [], 'inflight': []}
    assert len(opened) > 100
    for connection in opened:
        assert_closed(connection)
