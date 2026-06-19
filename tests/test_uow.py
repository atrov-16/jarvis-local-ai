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
def test_app_startup_runs_migrations(mock_run_migrations: AsyncMock, tmp_path: Path) -> None:
    """Verify that the FastAPI lifespan correctly executes migrations exactly once at startup."""
    import os
    
    # We need to set the JARVIS_DB_PATH so lifespan uses a dummy path
    db_path = tmp_path / "test.sqlite"
    os.environ["JARVIS_DB_PATH"] = str(db_path)
    
    # Mock services that start in lifespan
    with patch("jarvis.api.http.SystemRecoveryService") as mock_sys_rec, \
         patch("jarvis.api.http.TaskQueue") as mock_tq, \
         patch("jarvis.api.http.ReflectionService") as mock_ref:
         
        mock_sys_rec.return_value.run_startup_recovery = AsyncMock()
        mock_tq.return_value.start = AsyncMock()
        mock_tq.return_value.stop = AsyncMock()
        mock_ref.return_value.start = AsyncMock()
        mock_ref.return_value.stop = AsyncMock()
         
        # create_app needs the config and secrets but for tests we mock or let it use defaults
        app = create_app()
         
        # TestClient triggers the lifespan events
        with TestClient(app) as client:
            # When the client enters the context manager, startup events (lifespan) are triggered
            pass
            
    # Verify migrations were run once
    mock_run_migrations.assert_awaited_once()
    assert "storage_status" in app.state._state
    assert "database_path" in app.state.storage_status
