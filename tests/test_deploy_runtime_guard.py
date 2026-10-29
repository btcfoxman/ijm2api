import json

import pytest

from app import deploy_runtime_guard as guard


def ready():
    return {"canvas_cli_runtime": {"available": True, "version": "1.2.0",
        "distribution": "global", "schema_version": "1",
        "proxy_transport": "graftcp", "proxy_version": "0.8.3"}}


@pytest.mark.parametrize("field,value", [
    ("available", False), ("available", 1), ("version", "1.3.0"),
    ("distribution", "cn"), ("schema_version", "2"),
    ("proxy_transport", "test_unwrapped"), ("proxy_version", "0.8.2"),
])
def test_app_health_does_not_mask_unavailable_or_incompatible_cli(field, value):
    payload = ready()
    payload["canvas_cli_runtime"][field] = value
    assert not guard.runtime_ready(payload)


@pytest.mark.parametrize("payload", [None, [], {}, {"canvas_cli_runtime": None}])
def test_absent_cached_cli_contract_fails_closed(payload):
    assert not guard.runtime_ready(payload)


def test_pinned_protected_cli_cache_is_ready():
    assert guard.runtime_ready(ready())


def test_live_gate_only_reads_loopback_cache_and_keeps_admin_out_of_output(monkeypatch, capsys):
    seen = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return "http://127.0.0.1:8808/api/settings"
        def read(self, limit):
            assert limit == 1048577
            return json.dumps(ready()).encode()
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert request.get_method() == "GET"
            assert timeout == 4
            assert request.get_header("X-admin-token") == "private-ci-token"
            return Response()
    def opener(*handlers):
        assert isinstance(handlers[0], guard.ProxyHandler)
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], guard._NoRedirect)
        return Opener()
    monkeypatch.setenv("IJM_ADMIN_TOKEN", "private-ci-token")
    monkeypatch.setattr(guard, "build_opener", opener)
    assert guard.main() == 0
    assert len(seen) == 1
    assert capsys.readouterr() == ("", "")


def test_missing_admin_refuses_before_http(monkeypatch):
    monkeypatch.delenv("IJM_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(guard, "build_opener", lambda *a: pytest.fail("Must not contact HTTP"))
    assert guard.main() == 1


def test_redirect_handler_never_sends_credential_to_location():
    assert guard._NoRedirect().redirect_request(None, None, 307, "", {}, "https://example.org") is None


def test_deployment_cli_gate_is_inside_shared_health_and_rollback_boundary():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "scripts/deploy_pre.sh").read_text(encoding="utf-8")
    health = source.split("healthy() {", 1)[1].split("check_preserved_config", 1)[0]
    assert "/health" in health and "app/deploy_runtime_guard.py" in health
    assert '&& healthy; then' in source
    assert '"${rollback[@]}" up -d --no-deps --no-build --pull never' in source
