from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jarvis.core.event_bus import EventBus
from jarvis.core.orphan_recovery import OrphanRecoveryService
from jarvis.core.recovery import SystemRecoveryService
from jarvis.storage.unit_of_work import UnitOfWork


@pytest.fixture
def mock_orphan_recovery() -> AsyncMock:
    service = AsyncMock(spec=OrphanRecoveryService)
    service.recover.return_value = ["task_1"]
    return service


@pytest.fixture
def mock_event_bus() -> AsyncMock:
    return AsyncMock(spec=EventBus)


@pytest.fixture
def mock_uow() -> MagicMock:
    uow = MagicMock(spec=UnitOfWork)
    
    # Mock the async context manager for uow.begin()
    class MockUnit:
        def __init__(self) -> None:
            self.repositories = MagicMock()
            self.repositories.tasks = AsyncMock()
            self.connection = AsyncMock()
            
    mock_unit = MockUnit()
    
    # Setup cursor and fetchall for tasks
    mock_cursor = AsyncMock()
    mock_cursor.fetchall.return_value = [
        {"id": "task_1", "status": "running"},
        {"id": "task_2", "status": "planning"}
    ]
    mock_unit.connection.execute.return_value = mock_cursor
    
    class MockBegin:
        async def __aenter__(self):
            return mock_unit
            
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
    uow.begin.return_value = MockBegin()
    return uow


@pytest.fixture
def system_recovery(mock_uow: MagicMock, mock_event_bus: AsyncMock, mock_orphan_recovery: AsyncMock) -> SystemRecoveryService:
    return SystemRecoveryService(mock_uow, mock_event_bus, mock_orphan_recovery)


@pytest.mark.asyncio
@patch("jarvis.core.recovery.datetime")
async def test_run_startup_recovery(
    mock_datetime: MagicMock,
    system_recovery: SystemRecoveryService,
    mock_uow: MagicMock,
    mock_event_bus: AsyncMock,
    mock_orphan_recovery: AsyncMock
) -> None:
    from datetime import UTC
    
    # Setup mock datetime
    mock_now = MagicMock()
    mock_now.isoformat.return_value = "2026-06-20T00:00:00Z"
    mock_datetime.now.return_value = mock_now
    mock_datetime.UTC = UTC
    
    await system_recovery.run_startup_recovery()
    
    # 1. Orphan recovery should be called
    mock_orphan_recovery.recover.assert_called_once()
    
    # Get the mock_unit
    mock_unit = await mock_uow.begin().__aenter__()
    
    # 2. Both tasks should be updated to 'paused'
    assert mock_unit.repositories.tasks.update.call_count == 2
    mock_unit.repositories.tasks.update.assert_any_call("task_1", status="paused")
    mock_unit.repositories.tasks.update.assert_any_call("task_2", status="paused")
    
    # 3. Events should be inserted for both tasks
    assert mock_unit.repositories.tasks.insert_event.call_count == 2
    
    # Task 1 (Orphan terminated = True)
    mock_unit.repositories.tasks.insert_event.assert_any_call(
        task_id="task_1",
        event_type="status_change",
        message="Task recovered after daemon restart and marked as paused.",
        payload={
            "recovery_reason": "daemon_restart",
            "previous_status": "running",
            "recovered_at": "2026-06-20T00:00:00Z",
            "orphan_process_terminated": True
        }
    )
    
    # Task 2 (Orphan terminated = False)
    mock_unit.repositories.tasks.insert_event.assert_any_call(
        task_id="task_2",
        event_type="status_change",
        message="Task recovered after daemon restart and marked as paused.",
        payload={
            "recovery_reason": "daemon_restart",
            "previous_status": "planning",
            "recovered_at": "2026-06-20T00:00:00Z",
            "orphan_process_terminated": False
        }
    )
    
    # 4. EventBus should publish task.recovered for both
    assert mock_event_bus.publish.call_count == 2
    
    # Extract the events passed to publish
    calls = mock_event_bus.publish.call_args_list
    event_1 = calls[0][0][0]
    event_2 = calls[1][0][0]
    
    assert event_1.type == "task.recovered"
    assert event_1.payload["task_id"] == "task_1"
    assert event_1.payload["orphan_process_terminated"] is True
    
    assert event_2.type == "task.recovered"
    assert event_2.payload["task_id"] == "task_2"
    assert event_2.payload["orphan_process_terminated"] is False
