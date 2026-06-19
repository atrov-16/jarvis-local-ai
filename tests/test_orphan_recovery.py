import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from jarvis.core.orphan_recovery import OrphanRecoveryService
from jarvis.core.process_registry import ProcessRegistryService


@pytest.fixture
def mock_registry() -> AsyncMock:
    registry = AsyncMock(spec=ProcessRegistryService)
    return registry


@pytest.fixture
def orphan_service(mock_registry: AsyncMock) -> OrphanRecoveryService:
    return OrphanRecoveryService(mock_registry)


@pytest.mark.asyncio
async def test_recover_no_running_processes(orphan_service: OrphanRecoveryService, mock_registry: AsyncMock) -> None:
    mock_registry.list_processes.return_value = [
        {"id": "1", "status": "completed", "pid": 1234, "task_id": "t1", "command_display": "test"}
    ]
    await orphan_service.recover()
    assert mock_registry.unregister_process.call_count == 1
    mock_registry.unregister_process.assert_called_with("1")


@pytest.mark.asyncio
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._is_orphaned")
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate")
async def test_recover_running_not_orphaned(
    mock_terminate: MagicMock,
    mock_is_orphaned: MagicMock,
    orphan_service: OrphanRecoveryService,
    mock_registry: AsyncMock
) -> None:
    mock_registry.list_processes.return_value = [
        {"id": "2", "status": "running", "pid": 5678, "task_id": "t2", "command_display": "test", "creation_time": 100.0}
    ]
    mock_is_orphaned.return_value = False
    
    await orphan_service.recover()
    
    mock_is_orphaned.assert_called_with(5678, "test", 100.0)
    mock_terminate.assert_not_called()
    mock_registry.unregister_process.assert_called_with("2")


@pytest.mark.asyncio
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._is_orphaned")
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate")
async def test_recover_running_is_orphaned(
    mock_terminate: MagicMock,
    mock_is_orphaned: MagicMock,
    orphan_service: OrphanRecoveryService,
    mock_registry: AsyncMock
) -> None:
    mock_registry.list_processes.return_value = [
        {"id": "3", "status": "running", "pid": 9999, "task_id": "t3", "command_display": "test", "creation_time": 200.0}
    ]
    mock_is_orphaned.return_value = True
    
    await orphan_service.recover()
    
    mock_is_orphaned.assert_called_with(9999, "test", 200.0)
    mock_terminate.assert_called_with(9999)
    mock_registry.unregister_process.assert_called_with("3")


@patch("jarvis.core.process_utils.get_process_creation_time")
def test_is_orphaned_pid_reused(mock_get_ctime: MagicMock, orphan_service: OrphanRecoveryService) -> None:
    # Recorded time is 100.0. Actual OS creation time is 200.0 (>5s difference).
    mock_get_ctime.return_value = 200.0
    
    result = orphan_service._is_orphaned(1234, "test", recorded_creation_time=100.0)
    assert result is False
    mock_get_ctime.assert_called_with(1234)


@patch("jarvis.core.process_utils.get_process_creation_time")
def test_is_orphaned_process_missing(mock_get_ctime: MagicMock, orphan_service: OrphanRecoveryService) -> None:
    # Process no longer exists (OS returns None)
    mock_get_ctime.return_value = None
    
    result = orphan_service._is_orphaned(1234, "test", recorded_creation_time=100.0)
    assert result is False
    mock_get_ctime.assert_called_with(1234)

@patch("jarvis.core.process_utils.get_process_creation_time")
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._is_orphaned_windows")
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._is_orphaned_unix")
@patch("jarvis.core.orphan_recovery.os")
def test_is_orphaned_process_matched(
    mock_os: MagicMock,
    mock_unix: MagicMock,
    mock_windows: MagicMock,
    mock_get_ctime: MagicMock, 
    orphan_service: OrphanRecoveryService
) -> None:
    # Recorded time is 100.0. Actual time is 102.0 (<= 5s difference).
    mock_get_ctime.return_value = 102.0
    mock_os.name = "nt"
    mock_windows.return_value = True
    
    result = orphan_service._is_orphaned(1234, "test", recorded_creation_time=100.0)
    assert result is True
    mock_windows.assert_called_with(1234, "test")
