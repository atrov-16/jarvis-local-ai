"""The remaining Phase 15 boundaries: WebSocket header-only auth, localhost_only, and
generic 500 responses."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.api.http import create_app
from jarvis.api.network import (
    LOCALHOST_ONLY_DETAIL,
    UnsafeBindError,
    check_bind_host,
    is_loopback_host,
)
from jarvis.api.services.trace import TraceService
from jarvis.config.models import JarvisConfig, MemoryConfig, SecurityConfig
from jarvis.config.secrets import SecretManager
from jarvis.storage.unit_of_work import UnitOfWork

TOKEN = "DUMMY-network-boundary-token-6d1f"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REMOTE = ("203.0.113.7", 50123)  # TEST-NET-3: never a local address


def _app(tmp_path: Path, localhost_only: bool = True):
    config = JarvisConfig(
        memory=MemoryConfig(database_path=tmp_path / "net.sqlite"),
        security=SecurityConfig(localhost_only=localhost_only),
    )
    return create_app(
        config=config,
        secret_manager=SecretManager({"JARVIS_API_TOKEN": TOKEN}, use_keyring=False),
    )


# --- A. WebSocket authentication ----------------------------------------------------------


def test_websocket_accepts_a_valid_authorization_header(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/events", headers=AUTH) as websocket:
            client.portal.call(app.state.event_bus.publish, "test.ping", {"n": 1})
            event = websocket.receive_json()
    assert event["type"] == "test.ping"
    assert event["payload"] == {"n": 1}


@pytest.mark.parametrize(
    "path, headers",
    [
        (f"/v1/events?token={TOKEN}", {}),  # valid token, but in the URL
        ("/v1/events", {"Authorization": "Bearer wrong-token"}),
        ("/v1/events", {"Authorization": TOKEN}),  # no Bearer scheme
        ("/v1/events", {"X-Jarvis-Api-Token": TOKEN}),  # HTTP-only header
        ("/v1/events", {}),
    ],
    ids=["query-token", "wrong-token", "no-scheme", "other-header", "none"],
)
def test_websocket_rejects_anything_but_a_valid_bearer_header(
    tmp_path: Path, path: str, headers: dict[str, str]
) -> None:
    with TestClient(_app(tmp_path)) as client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(path, headers=headers):
                pass
    assert excinfo.value.code == 1008


def test_query_token_does_not_override_a_valid_header(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        with client.websocket_connect("/v1/events?token=garbage", headers=AUTH):
            pass  # accepted on the header; the query string is ignored


# --- B. localhost_only ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.4.5.6", "::1", "[::1]", "localhost", "LOCALHOST", "::ffff:127.0.0.1"],
)
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.20", "10.0.0.1", "::ffff:10.0.0.1", "203.0.113.7",
     "example.com", "localhost.example.com", "testclient", "", None],
)
def test_non_loopback_hosts(host: str | None) -> None:
    assert not is_loopback_host(host)


def test_bind_check() -> None:
    check_bind_host("127.0.0.1", localhost_only=True)
    check_bind_host("::1", localhost_only=True)
    check_bind_host("127.0.0.1", localhost_only=True, api_token_enabled=False)
    check_bind_host("0.0.0.0", localhost_only=False)  # an explicit, deliberate opt-out
    with pytest.raises(UnsafeBindError, match="localhost_only"):
        check_bind_host("0.0.0.0", localhost_only=True)
    # Never a network bind without authentication.
    with pytest.raises(UnsafeBindError, match="without authentication"):
        check_bind_host("0.0.0.0", localhost_only=False, api_token_enabled=False)


def _run_daemon_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str):
    from jarvis.app import daemon

    config_file = tmp_path / "config.toml"
    config_file.write_text(toml, encoding="utf-8")
    monkeypatch.setenv("JARVIS_DATABASE_PATH", str(tmp_path / "daemon.sqlite"))
    monkeypatch.setattr(daemon, "setup_logging", lambda level: None)
    with patch.object(daemon.uvicorn, "run") as run:
        daemon.run_daemon(config_file)
    return run


def test_daemon_refuses_a_non_loopback_bind(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(SystemExit, match="Refusing to start: server.host is '0.0.0.0'"):
        _run_daemon_with(tmp_path, monkeypatch, "[server]\nhost = '0.0.0.0'\n")


def test_daemon_binds_loopback_without_trusting_proxy_headers(tmp_path: Path, monkeypatch) -> None:
    run = _run_daemon_with(tmp_path, monkeypatch, "[server]\nhost = '127.0.0.1'\nport = 8799\n")
    kwargs = run.call_args.kwargs
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8799)
    assert kwargs["proxy_headers"] is False


def test_daemon_allows_a_network_bind_only_when_localhost_only_is_off(
    tmp_path: Path, monkeypatch
) -> None:
    run = _run_daemon_with(
        tmp_path, monkeypatch, "[server]\nhost = '0.0.0.0'\n[security]\nlocalhost_only = false\n"
    )
    assert run.call_args.kwargs["host"] == "0.0.0.0"


@pytest.mark.parametrize("client", [("127.0.0.1", 1), ("::1", 1)])
def test_local_peers_are_served(tmp_path: Path, client: tuple[str, int]) -> None:
    with TestClient(_app(tmp_path), client=client) as http:
        assert http.get("/health").status_code == 200
        assert http.get("/v1/projects", headers=AUTH).status_code == 200


def test_remote_peers_are_refused_even_with_a_valid_token(tmp_path: Path, caplog) -> None:
    with TestClient(_app(tmp_path), client=REMOTE) as http:
        for path, headers in [("/health", {}), ("/v1/projects", AUTH),
                              ("/v1/projects", {**AUTH, "X-Forwarded-For": "127.0.0.1"}),
                              ("/v1/projects", {**AUTH, "Host": "localhost"})]:
            response = http.get(path, headers=headers)
            assert response.status_code == 403
            assert response.json() == {"detail": LOCALHOST_ONLY_DETAIL}
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with http.websocket_connect("/v1/events", headers=AUTH):
                pass
    assert excinfo.value.code == 1008
    assert "non-local address 203.0.113.7" in caplog.text
    assert TOKEN not in caplog.text


def test_remote_peers_need_the_token_when_localhost_only_is_off(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, localhost_only=False), client=REMOTE) as http:
        assert http.get("/health").status_code == 200
        assert http.get("/v1/projects").status_code == 401
        assert http.get("/v1/projects", headers=AUTH).status_code == 200


# --- C. Generic 500 responses -----------------------------------------------------------


def _task(db_path: Path) -> str:
    async def insert() -> str:
        async with UnitOfWork(db_path).begin() as unit:
            assert unit.repositories is not None
            task_id = await unit.repositories.tasks.insert(
                title="T", user_request="r", status="completed"
            )
            await unit.repositories.tasks.insert_event(
                task_id=task_id, event_type="status_change", message="done", payload={}
            )
            return task_id

    return asyncio.run(insert())


def test_trace_of_existing_and_unknown_tasks(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as http:
        task_id = _task(tmp_path / "net.sqlite")
        ok = http.get(f"/v1/tasks/{task_id}/trace", headers=AUTH)
        assert ok.status_code == 200
        assert [e["summary"] for e in ok.json()["entries"]] == ["done"]

        missing = http.get("/v1/tasks/no-such-task/trace", headers=AUTH)
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Task not found."}


def test_trace_internal_errors_are_generic_for_clients_and_logged(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    async def broken(self, task_id: str, include_system: bool = False):
        raise RuntimeError(f"disk I/O error at /Users/me/secret/path.sqlite using {TOKEN}")

    monkeypatch.setattr(TraceService, "get_task_trace", broken)
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as http:
        with caplog.at_level(logging.ERROR, logger="jarvis.api.http"):
            response = http.get("/v1/tasks/abc/trace", headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "disk I/O" not in response.text and TOKEN not in response.text
    assert "Unhandled RuntimeError in GET /v1/tasks/abc/trace" in caplog.text
    # The handler's own log line names the error type and route, not the message; the full
    # traceback is logged by the server through the redaction filter.
    assert "disk I/O" not in caplog.text


def test_corrupt_stored_trace_data_is_a_500_not_a_404(tmp_path: Path) -> None:
    db_path = tmp_path / "net.sqlite"
    with TestClient(_app(tmp_path), raise_server_exceptions=False) as http:
        task_id = _task(db_path)

        async def corrupt() -> None:
            async with UnitOfWork(db_path).begin() as unit:
                assert unit.connection is not None
                await unit.connection.execute(
                    "UPDATE task_events SET payload_json = '{not json' WHERE task_id = ?",
                    (task_id,),
                )

        asyncio.run(corrupt())
        response = http.get(f"/v1/tasks/{task_id}/trace", headers=AUTH)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "Expecting" not in response.text  # no JSON parser message


def test_server_errors_still_raise_for_in_process_callers(tmp_path: Path, monkeypatch) -> None:
    async def broken(self, task_id: str, include_system: bool = False):
        raise RuntimeError("boom")

    monkeypatch.setattr(TraceService, "get_task_trace", broken)
    with TestClient(_app(tmp_path)) as http:
        with pytest.raises(RuntimeError, match="boom"):  # not swallowed by the handler
            http.get("/v1/tasks/abc/trace", headers=AUTH)


def test_daemon_refuses_a_network_bind_without_the_api_token(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(SystemExit, match="without authentication"):
        _run_daemon_with(
            tmp_path,
            monkeypatch,
            "[server]\nhost = '0.0.0.0'\n"
            "[security]\nlocalhost_only = false\napi_token_enabled = false\n",
        )


def _asgi_call(scope: dict) -> tuple[list[dict], bool]:
    """Run LocalhostOnlyMiddleware on one scope; return what it sent and whether it passed."""
    from jarvis.api.network import LocalhostOnlyMiddleware

    passed = []
    sent: list[dict] = []

    async def app(scope, receive, send) -> None:
        passed.append(True)

    async def receive() -> dict:
        return {"type": "http.request"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(LocalhostOnlyMiddleware(app)(scope, receive, send))
    return sent, bool(passed)


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_missing_client_address_on_tcp_is_refused(kind: str) -> None:
    sent, passed = _asgi_call({"type": kind, "client": None, "server": ("127.0.0.1", 8765)})
    assert not passed
    assert sent[0]["type"] == ("http.response.start" if kind == "http" else "websocket.close")


def test_missing_client_address_on_unix_socket_is_allowed() -> None:
    for server in (None, ("/run/jarvis.sock", None)):
        _, passed = _asgi_call({"type": "http", "client": None, "server": server})
        assert passed


def test_lifespan_scope_passes_through() -> None:
    _, passed = _asgi_call({"type": "lifespan"})
    assert passed


def test_workspace_link_errors_are_safe_client_errors(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as http:
        project = http.post("/v1/projects", json={"name": "P"}, headers=AUTH).json()["id"]
        workspace = http.post(
            "/v1/workspaces", json={"name": "W", "path": str(tmp_path / "w")}, headers=AUTH
        ).json()["id"]
        detail = {"detail": "Project or workspace not found, or the workspace is already linked."}

        unknown = http.post(f"/v1/projects/{project}/workspaces/nope", headers=AUTH)
        assert (unknown.status_code, unknown.json()) == (400, detail)
        assert "FOREIGN KEY" not in unknown.text

        assert http.post(f"/v1/projects/{project}/workspaces/{workspace}", headers=AUTH).json() == {
            "linked": True
        }
        again = http.post(f"/v1/projects/{project}/workspaces/{workspace}", headers=AUTH)
        assert (again.status_code, again.json()) == (400, detail)


@pytest.mark.parametrize(
    "host, expected",
    [("127.0.0.1", "http://127.0.0.1:8765"), ("::1", "http://[::1]:8765"),
     ("localhost", "http://localhost:8765")],
)
def test_cli_builds_a_valid_url_for_each_loopback_host(
    monkeypatch, host: str, expected: str
) -> None:
    from jarvis.app import terminal

    monkeypatch.setenv("JARVIS_SERVER_HOST", host)
    with terminal._get_api_client() as client:
        assert str(client.base_url).rstrip("/") == expected
