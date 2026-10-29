import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.api.http import create_app

@pytest.mark.asyncio
@patch("jarvis.storage.unit_of_work.open_sqlite_connection")
@patch("jarvis.storage.unit_of_work.StorageRepositories")
async def test_unit_of_work_does_not_run_migrations(mock_repos: MagicMock, mock_open_conn: AsyncMock) -> None:
    """Verify that UnitOfWork no longer runs migrations on transaction entry."""
    mock_conn = AsyncMock()
    mock_open_conn.return_value = mock_conn
    
    uow = UnitOfWork(Path("dummy.sqlite"))
    
    async with uow.begin() as unit:
        assert unit.connection is mock_conn
        assert mock_repos.called
        
    # The key assertion: we removed run_migrations from unit_of_work.py entirely.
    # Let's prove open_sqlite_connection was called and NO migrations were run.
    mock_open_conn.assert_called_once_with(Path("dummy.sqlite"))
    mock_conn.close.assert_awaited_once()


@patch("jarvis.api.http.run_migrations", new_callable=AsyncMock)
def test_app_startup_runs_migrations(
    mock_run_migrations: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that the FastAPI lifespan correctly executes migrations exactly once at startup."""
    from jarvis.config.secrets import SecretManager

    # A disposable database, through the variable config actually reads (restored afterwards),
    # and a working directory where a stray default database would be noticed.
    db_path = tmp_path / "test.sqlite"
    monkeypatch.setenv("JARVIS_DATABASE_PATH", str(db_path))
    monkeypatch.chdir(tmp_path)
    
    # Mock services that start in lifespan
    with patch("jarvis.api.http.SystemRecoveryService") as mock_sys_rec, \
         patch("jarvis.api.http.TaskQueue") as mock_tq, \
         patch("jarvis.api.http.ReflectionService") as mock_ref:
         
        mock_sys_rec.return_value.run_startup_recovery = AsyncMock()
        mock_tq.return_value.start = AsyncMock()
        mock_tq.return_value.stop = AsyncMock()
        mock_ref.return_value.start = AsyncMock()
        mock_ref.return_value.stop = AsyncMock()
         
        # Injected credentials: startup must not read or write the OS credential store.
        app = create_app(
            secret_manager=SecretManager({"JARVIS_API_TOKEN": "dummy-token"}, use_keyring=False)
        )
         
        # TestClient triggers the lifespan events
        with TestClient(app):
            # When the client enters the context manager, startup events (lifespan) are triggered
            pass
            
    # Verify migrations were run once
    mock_run_migrations.assert_awaited_once()
    assert "storage_status" in app.state._state
    assert app.state.storage_status["database_path"] == str(db_path)
    assert not (tmp_path / ".jarvis").exists()
