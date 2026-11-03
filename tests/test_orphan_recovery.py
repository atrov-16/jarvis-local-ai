from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
@patch("jarvis.core.process_utils.is_process_running", return_value=False)
@patch("jarvis.core.process_utils.is_identity_unverifiable", return_value=False)
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._is_orphaned")
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate")
async def test_recover_running_not_orphaned(
    mock_terminate: MagicMock,
    mock_is_orphaned: MagicMock,
    mock_unverifiable: MagicMock,
    mock_running: MagicMock,
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
def test_is_orphaned_process_matched(
    mock_get_ctime: MagicMock, orphan_service: OrphanRecoveryService
) -> None:
    # Recorded 100.0, observed 100.01: the same process (identical native source).
    mock_get_ctime.return_value = 100.01
    assert orphan_service._is_orphaned(1234, "test", recorded_creation_time=100.0) is True


@patch("jarvis.core.process_utils.is_process_running", return_value=True)
@patch("jarvis.core.process_utils.get_process_creation_time", return_value=102.0)
def test_two_seconds_apart_is_a_different_process(
    mock_get_ctime: MagicMock, mock_running: MagicMock, orphan_service: OrphanRecoveryService
) -> None:
    # The old 5 s tolerance would have treated this reused PID as the original process.
    assert orphan_service._is_orphaned(1234, "test", recorded_creation_time=100.0) is False


@patch("jarvis.core.process_utils.is_process_running", return_value=True)
def test_record_without_creation_time_is_never_terminated(
    mock_running: MagicMock, orphan_service: OrphanRecoveryService
) -> None:
    # Without a creation time the PID cannot be told apart from an unrelated process.
    assert orphan_service._is_orphaned(1234, "test", None) is False


# --- Records that must not be discarded ------------------------------------------------------


def _running_record(**overrides):
    record = {
        "id": "9", "status": "running", "pid": 4242, "task_id": "t9",
        "command_display": "test", "creation_time": 100.0,
    }
    record.update(overrides)
    return record


@pytest.mark.asyncio
@patch("jarvis.core.process_utils.is_process_running", return_value=True)
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate")
async def test_live_process_without_creation_time_keeps_its_record(
    mock_terminate: MagicMock,
    mock_running: MagicMock,
    orphan_service: OrphanRecoveryService,
    mock_registry: AsyncMock,
) -> None:
    mock_registry.list_processes.return_value = [_running_record(creation_time=None)]
    assert await orphan_service.recover() == []
    mock_terminate.assert_not_called()
    mock_registry.unregister_process.assert_not_called()


@pytest.mark.asyncio
@patch("jarvis.core.process_utils.is_process_running", return_value=False)
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate")
async def test_dead_process_without_creation_time_is_cleaned_up(
    mock_terminate: MagicMock,
    mock_running: MagicMock,
    orphan_service: OrphanRecoveryService,
    mock_registry: AsyncMock,
) -> None:
    mock_registry.list_processes.return_value = [_running_record(creation_time=None)]
    assert await orphan_service.recover() == []
    mock_registry.unregister_process.assert_called_once_with("9")


@pytest.mark.asyncio
@patch("jarvis.core.process_utils.is_identity_unverifiable", return_value=True)
@patch("jarvis.core.process_utils.is_same_process", return_value=False)
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate")
async def test_process_the_os_will_not_identify_keeps_its_record(
    mock_terminate: MagicMock,
    mock_same: MagicMock,
    mock_unverifiable: MagicMock,
    orphan_service: OrphanRecoveryService,
    mock_registry: AsyncMock,
) -> None:
    # e.g. access denied while reading the creation time of a live process.
    mock_registry.list_processes.return_value = [_running_record()]
    assert await orphan_service.recover() == []
    mock_terminate.assert_not_called()
    mock_registry.unregister_process.assert_not_called()


@pytest.mark.asyncio
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate", return_value=False)
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._is_orphaned", return_value=True)
async def test_failed_termination_keeps_the_record_and_reports_no_task(
    mock_is_orphaned: MagicMock,
    mock_terminate: MagicMock,
    orphan_service: OrphanRecoveryService,
    mock_registry: AsyncMock,
) -> None:
    mock_registry.list_processes.return_value = [_running_record()]
    assert await orphan_service.recover() == []
    mock_registry.unregister_process.assert_not_called()


@pytest.mark.asyncio
@patch("jarvis.core.process_utils.is_process_running", return_value=True)
@patch("jarvis.core.process_utils.is_identity_unverifiable", return_value=False)
@patch("jarvis.core.process_utils.is_same_process", return_value=False)
@patch("jarvis.core.orphan_recovery.OrphanRecoveryService._terminate")
async def test_provably_reused_pid_is_cleaned_up_without_a_kill(
    mock_terminate: MagicMock,
    mock_same: MagicMock,
    mock_unverifiable: MagicMock,
    mock_running: MagicMock,
    orphan_service: OrphanRecoveryService,
    mock_registry: AsyncMock,
) -> None:
    mock_registry.list_processes.return_value = [_running_record()]
    assert await orphan_service.recover() == []
    mock_terminate.assert_not_called()
    mock_registry.unregister_process.assert_called_once_with("9")


@patch("jarvis.core.orphan_recovery._EXIT_POLL_INTERVAL", 0)
@patch("jarvis.core.process_utils.is_process_running", side_effect=[True, True, False])
@patch("jarvis.core.process_utils.terminate_process_tree")
def test_terminate_waits_until_the_process_is_gone(
    mock_kill: MagicMock, mock_running: MagicMock, orphan_service: OrphanRecoveryService
) -> None:
    assert orphan_service._terminate(1234) is True


@patch("jarvis.core.orphan_recovery._EXIT_POLL_INTERVAL", 0)
@patch("jarvis.core.orphan_recovery._EXIT_POLLS", 3)
@patch("jarvis.core.process_utils.is_process_running", return_value=True)
@patch("jarvis.core.process_utils.terminate_process_tree", side_effect=OSError("denied"))
def test_terminate_reports_failure_when_the_process_survives(
    mock_kill: MagicMock, mock_running: MagicMock, orphan_service: OrphanRecoveryService
) -> None:
    assert orphan_service._terminate(1234) is False
