from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager


def _test_app(tmp_path: Path) -> TestClient:
    config = JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "memory.sqlite"))
    app = create_app(
        config=config,
        secret_manager=SecretManager({"JARVIS_API_TOKEN": "test-token"}, use_keyring=False),
    )
    return TestClient(app)


def test_health_is_unauthenticated(tmp_path: Path) -> None:
    with _test_app(tmp_path) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_status_rejects_missing_or_invalid_token(tmp_path: Path) -> None:
    with _test_app(tmp_path) as client:
        missing = client.get("/v1/status")
        invalid = client.get("/v1/status", headers={"Authorization": "Bearer wrong"})

    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_status_accepts_valid_token(tmp_path: Path) -> None:
    with _test_app(tmp_path) as client:
        response = client.get("/v1/status", headers={"Authorization": "Bearer test-token"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["secrets"]["api_token_configured"] is True


def test_public_config_contains_no_secrets(tmp_path: Path) -> None:
    with _test_app(tmp_path) as client:
        response = client.get(
            "/v1/config/public",
            headers={"X-Jarvis-Api-Token": "test-token"},
        )

    body = response.json()
    assert response.status_code == 200
    assert "test-token" not in str(body)
    assert body["security"]["api_token_enabled"] is True
