"""Canvas media uses a total budget; browser media keeps its existing path."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.media import MediaError, _download


def response_fixture():
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.headers = {'Content-Type': 'image/jpeg'}
    response.raw = SimpleNamespace(_connection=SimpleNamespace(sock=Mock()), closed=False, read1=Mock())
    response.status_code = 200
    return response


def test_slow_reference_stream_checks_total_deadline_without_renewing_idle_timeout(monkeypatch):
    clock = {'now': 0.0}
    response = response_fixture()
    def drip(*args, **kwargs):
        clock['now'] += 1
        return b'x'
    response.raw.read1.side_effect = drip
    session = Mock()
    session.get.return_value = response
    monkeypatch.setattr('app.media.requests.Session', lambda: session)
    monkeypatch.setattr('app.media.time', SimpleNamespace(monotonic=lambda: clock['now']))
    with pytest.raises(MediaError, match='deadline exceeded'):
        _download('https://example.test/reference.jpg', 'images', 'socks5://unused.invalid:20001',
                  allow_direct_fallback=False, deadline=3)
    assert clock['now'] == 3
    assert response.raw.read1.call_count == 3
    assert [call.args[0] for call in response.raw._connection.sock.settimeout.call_args_list] == [3, 2, 1]
    assert session.get.call_count == 1
    assert session.get.call_args.kwargs['timeout'].total == 3
    assert session.get.call_args.kwargs['headers']['Accept-Encoding'] == 'identity'
    response.iter_content.assert_not_called()
    session.close.assert_called_once()


def test_browser_reference_download_preserves_legacy_stream_and_timeout(monkeypatch):
    response = response_fixture()
    response.iter_content.return_value = [b'original-browser-bytes']
    session = Mock()
    session.get.return_value = response
    monkeypatch.setattr('app.media.requests.Session', lambda: session)
    data, mime, name = _download('https://example.test/reference.jpg', 'images',
                                'socks5://unused.invalid:20001', allow_direct_fallback=False)
    assert (data, mime, name) == (b'original-browser-bytes', 'image/jpeg', 'reference.jpg')
    assert session.get.call_args.kwargs['timeout'] == (10, 45)
    response.iter_content.assert_called_once_with(1024 * 1024)
    response.raw.read1.assert_not_called()


def test_expired_reference_budget_never_opens_session(monkeypatch):
    session = Mock()
    monkeypatch.setattr('app.media.requests.Session', session)
    monkeypatch.setattr('app.media.time', SimpleNamespace(monotonic=lambda: 10))
    with pytest.raises(MediaError, match='deadline exceeded'):
        _download('https://example.test/reference.jpg', 'images', 'socks5://unused.invalid:20001', deadline=10)
    session.assert_not_called()
