"""Prepared references stay with a funded account; no real CLI or network calls."""
from unittest.mock import Mock

from app.canvas_client import CanvasClient
from test_canvas_execution import context, owner, task
from test_canvas_transport import FakeRunner


class BoundRunner(FakeRunner):
    def __init__(self, store, service, account, identifier, *, fresh_balance=None):
        super().__init__(service.canvas_cli.account_directory(account))
        self.store, self.account, self.identifier = store, account, identifier
        self.fresh_balance = account['balance'] if fresh_balance is None else fresh_balance
        self.runs = 0

    def profile(self, account):
        assert account['id'] == self.account['id']
        return f"bound-profile-{account['id']}"

    def run(self, account, args, **kwargs):
        assert account['id'] == self.account['id']
        if args[:2] == ['auth', 'account']:
            self.calls.append(list(args))
            return {'ok': True, 'data': {'userId': account['login_user_id'],
                'credits': {'available': self.fresh_balance - 20 * self.runs}}}
        if args[:2] == ['canvas', 'create']:
            durable = self.store.task(self.identifier)['upstream_context']
            assert durable['account_id'] == account['id'] and durable['project_id'] == args[-1]
            self.persisted.append(durable)
        if args[:2] == ['node', 'run']:
            self.runs += 1
            durable = self.store.task(self.identifier)
            assert self.runs == 1 and durable['actual_cost'] is None
            assert durable['account_id'] == account['id']
            assert durable['upstream_context']['submission_phase'] == 'run_dispatching'
            assert durable['upstream_context']['submit_id'] == args[args.index('--submit-id') + 1]
        return super().run(account, args, **kwargs)


def setup_seven_sources(context, monkeypatch, *, first_fresh_balance=None):
    store, service, _ = context
    first = owner(store, 'larger-ready', balance=100)
    backup = owner(store, 'smaller-ready', balance=50)
    sources = [f'https://fixture.example/image-{index}.png' for index in range(7)]
    row = task(store, 'seven-sources', image_urls=sources)
    runners = {account['id']: BoundRunner(store, service, account, row['id'],
        fresh_balance=first_fresh_balance if account['id'] == first['id'] else None)
        for account in (first, backup)}
    clients = {account['id']: CanvasClient(runners[account['id']], account) for account in (first, backup)}
    factory = Mock(side_effect=lambda account: clients[account['id']])
    monkeypatch.setattr(service, 'canvas_client', factory)
    monkeypatch.setattr(service.canvas_cli, 'run', Mock(side_effect=AssertionError('real CLI forbidden')))
    monkeypatch.setattr('app.canvas_service._download', Mock(side_effect=AssertionError('network download forbidden')))
    return store, service, first, backup, row, sources, runners, factory


def test_seven_uploaded_sources_and_funded_quote_keep_original_account_and_one_paid_run(context, monkeypatch):
    store, service, first, backup, row, sources, runners, factory = setup_seven_sources(context, monkeypatch)
    service._run(row['id'])
    result = store.task(row['id'])
    assert result['status'] == 'succeeded', result['error_message']
    assert result['account_id'] == first['id'] and result['attempted_account_ids'] == [first['id']]
    assert [call.args[0]['id'] for call in factory.call_args_list] == [first['id']]
    original = runners[first['id']]
    assert runners[backup['id']].calls == []  # No prepare, upload, create, quote or run on the smaller account.
    uploads = [args for args in original.calls if args[:2] == ['resource', 'upload']]
    assert [args[args.index('--source-url') + 1] for args in uploads] == sources
    assert len(uploads) == 7 and len({args[-1] for args in uploads}) == 7
    context_result = result['upstream_context']
    assert [args[-1] for args in uploads] == context_result['resources']['images']
    assert context_result['uploaded_resources'] == context_result['resources']['images']
    for pair in (['node', 'create'], ['node', 'quote'], ['node', 'confirm'], ['node', 'run']):
        commands = [args for args in original.calls if args[:2] == pair]
        assert len(commands) == 1
        assert commands[0][commands[0].index('--project-id') + 1] == context_result['project_id']
        assert commands[0][commands[0].index('--node-id') + 1] == context_result['node_id']
    assert original.runs == 1
    assert [entry['reason'] for entry in context_result['preparation_attempts']] == ['accepted']
    service.canvas_cli.run.assert_not_called()
    assert not service._credit_reservations


def test_actual_fresh_funding_rejection_still_switches_before_any_run(context, monkeypatch):
    store, service, first, backup, row, _, runners, factory = setup_seven_sources(
        context, monkeypatch, first_fresh_balance=7)
    service._run(row['id'])
    result = store.task(row['id'])
    assert result['status'] == 'succeeded', result['error_message']
    assert [call.args[0]['id'] for call in factory.call_args_list] == [first['id'], backup['id']]
    assert result['account_id'] == backup['id'] and result['attempted_account_ids'] == [backup['id']]
    assert runners[first['id']].runs == 0 and runners[backup['id']].runs == 1
    assert len([a for a in runners[first['id']].calls if a[:2] == ['node', 'quote']]) == 1
    assert not [a for a in runners[first['id']].calls if a[:2] == ['node', 'confirm']]
    assert [entry['reason'] for entry in result['upstream_context']['preparation_attempts']] == ['insufficient_credits', 'accepted']
    service.canvas_cli.run.assert_not_called()
    assert not service._credit_reservations


def test_priority_and_smallest_funded_balance_still_apply_before_preparation(context):
    store, service, _ = context
    owner(store, 'larger', balance=100, priority=9)
    smaller = owner(store, 'smaller', balance=30, priority=9)
    owner(store, 'lower-priority', balance=22, priority=8)
    row = task(store, 'before-preparation')
    before = store.task(row['id'])
    selected = service._reserve_account(20, task_id=row['id'])
    assert selected['id'] == smaller['id']
    assert store.task(row['id']) == before
    service._release_account(smaller['id'], row['id'])
