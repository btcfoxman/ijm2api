"""Canvas media uses a total budget; browser media keeps its existing path."""
from types import SimpleNamespace
from unittest.mock import Mock
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import zlib

import pytest
import requests

from app.media import MediaError, _deadline_chunks, _download


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


@pytest.fixture
def real_reference_server():
    payload = b'RIFF' + b'original-reference-audio-bytes' * 400

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_GET(self):
            version, framing, encoding = self.path.strip('/').split('/')
            # Exercise HTTP/1.0's implicit close and HTTP/1.1's explicit close,
            # plus keep-alive responses and their released response socket.
            self.protocol_version = version.replace('-', '/')
            body = payload
            if encoding in {'gzip', 'x-gzip'}:
                body = gzip.compress(payload)
            elif encoding == 'gzip-multiple':
                body = gzip.compress(payload[:400]) + gzip.compress(payload[400:])
            elif encoding == 'deflate':
                body = zlib.compress(payload)
            elif encoding == 'deflate-raw':
                compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
                body = compressor.compress(payload) + compressor.flush()
            self.send_response(200)
            self.send_header('Content-Type', 'audio/wav')
            if encoding != 'identity':
                self.send_header('Content-Encoding', {'gzip-multiple': 'gzip', 'deflate-raw': 'deflate'}.get(encoding, encoding))
            if framing == 'chunked':
                self.send_header('Transfer-Encoding', 'chunked')
            else:
                self.send_header('Content-Length', str(len(body)))
            if framing != 'keepalive':
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            if framing == 'chunked':
                # Small independent chunks also split compression headers,
                # trailer bytes and concatenated-member boundaries.
                for offset in range(0, len(body), 3):
                    part = body[offset:offset+3]
                    self.wfile.write(f'{len(part):x}\r\n'.encode() + part + b'\r\n')
                self.wfile.write(b'0\r\n\r\n')
            else:
                self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', payload
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize('protocol,framing', [
    ('HTTP-1.0', 'length'), ('HTTP-1.1', 'length'),
    ('HTTP-1.1', 'keepalive'), ('HTTP-1.1', 'chunked'),
])
@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'x-gzip', 'gzip-multiple', 'deflate', 'deflate-raw'])
def test_real_http_stream_preserves_original_media_bytes(real_reference_server, protocol, framing, encoding):
    origin, original = real_reference_server
    with requests.Session() as session:
        session.trust_env = False
        with session.get(f'{origin}/{protocol}/{framing}/{encoding}', stream=True, timeout=(1, 2)) as response:
            response.raise_for_status()
            assert b''.join(_deadline_chunks(response, time.monotonic()+3)) == original


def test_real_closed_socket_is_not_reconfigured_after_final_content_length_read(real_reference_server):
    origin, original = real_reference_server
    with requests.Session() as session:
        session.trust_env = False
        with session.get(f'{origin}/HTTP-1.0/length/identity', stream=True, timeout=(1, 2)) as response:
            iterator = _deadline_chunks(response, time.monotonic()+3)
            chunks = []
            for chunk in iterator:
                chunks.append(chunk)
                if response.raw.closed:
                    break
            assert b''.join(chunks) == original
            assert response.raw.closed
            # The EOF read previously called settimeout on a closed socket.
            assert list(iterator) == []
