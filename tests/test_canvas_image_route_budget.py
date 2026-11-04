"""A transient direct failure must leave time to retry the original image."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.canvas_service import CanvasServiceMixin
from app.media import MediaError, _download, media_error_diagnostic
from test_canvas_public_media_route import ACCOUNT, SOURCE, sessions, response


def image_response():
    value = response()
    value.headers = {'Content-Type': 'image/png'}
    return value


def clock_for(monkeypatch):
    clock = {'now': 0.0}
    monkeypatch.setattr('app.media.time', SimpleNamespace(monotonic=lambda: clock['now'],
        sleep=lambda seconds: clock.update(now=clock['now'] + seconds)))
    return clock


def test_first_route_deadline_leaves_time_for_same_original_image_on_next_route(tmp_path, sessions, monkeypatch):
    actions, opened = sessions
    clock = clock_for(monkeypatch)

    def stalled(*args, **kwargs):
        clock['now'] += kwargs['timeout'].total
        raise MediaError('synthetic per-route deadline', category='deadline')

    actions.extend([stalled, image_response()])
    result = CanvasServiceMixin()._canvas_local_assets({'images': [SOURCE]}, ACCOUNT, tmp_path, deadline=30)
    # Public images stay URL-first; exercise only the original lazy FILE path.
    path = result['images'][0]['fallback_file']()
    assert path.read_bytes() == b'fixture-video-content'
    assert len(opened) == 2 and clock['now'] == 7.5
    assert opened[0].get.call_args.kwargs['proxies'] == {}
    assert opened[1].get.call_args.kwargs['proxies']['https'].startswith('socks5h://')
    assert opened[0].get.call_args.args == opened[1].get.call_args.args == (SOURCE,)


def test_all_routes_share_original_thirty_seconds_and_last_direct_get_is_reachable(sessions, monkeypatch):
    actions, opened = sessions
    clock = clock_for(monkeypatch)

    def stalled(*args, **kwargs):
        clock['now'] += kwargs['timeout'].total
        raise MediaError('synthetic per-route deadline', category='deadline')

    actions.extend([stalled] * 4)
    with pytest.raises(MediaError) as caught:
        _download(SOURCE, 'images', ACCOUNT['proxy_url'], deadline=30,
            allow_direct_fallback=True, fair_route_budget=True)
    assert clock['now'] == 30 and len(opened) == 4
    assert [item.get.call_args.kwargs['proxies'] == {} for item in opened] == [True, False, False, True]
    diagnostic = media_error_diagnostic(caught.value)
    assert len(diagnostic['route_attempts']) == 4
    assert [item['budget_seconds'] for item in diagnostic['route_attempts']] == [7.5] * 4
    assert [item['route'] for item in diagnostic['route_attempts']] == [
        'direct_media', 'account_proxy', 'account_proxy', 'direct_media']
    assert diagnostic['route_attempts'][-1]['elapsed_seconds'] == 7.5
    assert 'caller-signature' not in str(diagnostic) and 'fixture-password' not in str(diagnostic)


def test_last_direct_retry_can_complete_after_two_failed_account_routes(sessions, monkeypatch):
    actions, opened = sessions
    clock = clock_for(monkeypatch)

    def stalled(*args, **kwargs):
        clock['now'] += kwargs['timeout'].total
        raise MediaError('synthetic per-route deadline', category='deadline')

    actions.extend([stalled, stalled, stalled, image_response()])
    contents, _, _ = _download(SOURCE, 'images', ACCOUNT['proxy_url'], deadline=30,
        allow_direct_fallback=True, fair_route_budget=True)
    assert contents == b'fixture-video-content' and clock['now'] == 22.5
    assert len(opened) == 4 and opened[-1].get.call_args.kwargs['timeout'].total == 7.5


def test_legacy_path_keeps_its_original_shared_budget_without_opt_in(sessions, monkeypatch):
    actions, opened = sessions
    clock = clock_for(monkeypatch)

    def stalled(*args, **kwargs):
        clock['now'] += kwargs['timeout'].total
        raise MediaError('synthetic original deadline', category='deadline')

    actions.extend([stalled])
    with pytest.raises(MediaError):
        _download(SOURCE, 'images', ACCOUNT['proxy_url'], deadline=30, allow_direct_fallback=True)
    assert clock['now'] == 30 and len(opened) == 1


@pytest.mark.parametrize('host', ['dreamina.capcut.com', 'localhost', '127.0.0.1'])
def test_route_budget_cannot_enable_a_direct_upstream_or_local_connection(sessions, monkeypatch, host):
    actions, opened = sessions
    clock = clock_for(monkeypatch)

    def stalled(*args, **kwargs):
        clock['now'] += kwargs['timeout'].total
        raise MediaError('synthetic original deadline', category='deadline')

    actions.extend([stalled])
    with pytest.raises(MediaError):
        _download('https://' + host + '/image.png', 'images', ACCOUNT['proxy_url'],
            deadline=30, fair_route_budget=True)
    assert len(opened) == 1 and opened[0].get.call_args.kwargs['proxies']
    assert clock['now'] == 30
