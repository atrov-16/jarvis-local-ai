from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "memory.sqlite"))
    app = create_app(
        config=config,
        secret_manager=SecretManager({"JARVIS_API_TOKEN": "test-token"}, use_keyring=False),
    )
    with TestClient(app) as c:
        yield c


def test_workspace_endpoints(client: TestClient, tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer test-token"}
    workspace_path = str(tmp_path / "w1")

    # Add workspace
    resp = client.post(
        "/v1/workspaces", json={"name": "W1", "path": workspace_path}, headers=headers
    )
    assert resp.status_code == 200
    w_id = resp.json()["id"]
    assert resp.json()["name"] == "W1"

    # List workspaces
    resp = client.get("/v1/workspaces", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["id"] == w_id

    # Delete workspace
    resp = client.delete(f"/v1/workspaces/{w_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify deleted
    resp = client.get("/v1/workspaces", headers=headers)
    assert len(resp.json()) == 0


def test_project_endpoints(client: TestClient) -> None:
    headers = {"Authorization": "Bearer test-token"}

    # Create project
    resp = client.post(
        "/v1/projects", json={"name": "P1", "description": "D1"}, headers=headers
    )
    assert resp.status_code == 200
    p_id = resp.json()["id"]
    assert resp.json()["name"] == "P1"

    # List projects
    resp = client.get("/v1/projects", headers=headers)
    assert len(resp.json()) == 1
    assert resp.json()[0]["id"] == p_id

    # Current project
    resp = client.get("/v1/projects/current", headers=headers)
    assert resp.json()["id"] is None

    resp = client.post("/v1/projects/current", json={"id": p_id}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == p_id

    resp = client.get("/v1/projects/current", headers=headers)
    assert resp.json()["id"] == p_id

    # Delete project
    resp = client.delete(f"/v1/projects/{p_id}", headers=headers)
    assert resp.status_code == 200

    # Verify current cleared
    resp = client.get("/v1/projects/current", headers=headers)
    assert resp.json()["id"] is None


def test_workspace_linking(client: TestClient, tmp_path: Path) -> None:
    headers = {"Authorization": "Bearer test-token"}
    workspace_path = str(tmp_path / "w1")

    w_resp = client.post(
        "/v1/workspaces", json={"name": "W1", "path": workspace_path}, headers=headers
    )
    p_resp = client.post("/v1/projects", json={"name": "P1"}, headers=headers)

    w_id = w_resp.json()["id"]
    p_id = p_resp.json()["id"]

    # Link
    resp = client.post(f"/v1/projects/{p_id}/workspaces/{w_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["linked"] is True

    # Unlink
    resp = client.delete(f"/v1/projects/{p_id}/workspaces/{w_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["unlinked"] is True


def test_status_includes_providers(client: TestClient) -> None:
    headers = {"Authorization": "Bearer test-token"}
    
    # We need to patch the check_availability on the instance stored in app.state
    from unittest.mock import AsyncMock, patch
    from jarvis.models.schemas import ProviderStatus

    mock_statuses = [
        ProviderStatus(name="openrouter", available=True),
        ProviderStatus(name="ollama", available=False, error="Connection refused"),
    ]

    # Access the app from the client
    app = client.app # type: ignore
    with patch.object(app.state.model_router, "check_availability", new_callable=AsyncMock) as mock_check:
        mock_check.return_value = mock_statuses
        
        resp = client.get("/v1/status", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        
        assert "providers" in data
        assert len(data["providers"]) == 2
        
        providers = {p["name"]: p for p in data["providers"]}
        assert providers["openrouter"]["available"] is True
        assert providers["ollama"]["available"] is False
        assert "Connection refused" in providers["ollama"]["error"]
