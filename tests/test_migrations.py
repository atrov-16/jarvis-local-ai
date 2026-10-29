"""Regression tests for migration integrity (Phase 13).

All tests use disposable SQLite databases under pytest's tmp_path.
"""

from pathlib import Path

import pytest

from jarvis.storage import migrations as migrations_module
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import Migration, MigrationError, run_migrations
from jarvis.storage.repositories import StorageRepositories

ALL_MIGRATIONS = migrations_module._load_migrations()
LATEST = max(m.version for m in ALL_MIGRATIONS)
CHILD_TABLES = ("task_steps", "task_events", "approval_requests", "process_registry")


def _only(monkeypatch, migrations: list[Migration]) -> None:
    monkeypatch.setattr(migrations_module, "_load_migrations", lambda: list(migrations))


async def _counts(conn) -> dict[str, int]:
    counts = {}
    for table in ("tasks", *CHILD_TABLES):
        cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
        counts[table] = (await cursor.fetchone())[0]
    return counts


async def _versions(conn) -> list[int]:
    cursor = await conn.execute("SELECT version FROM schema_migrations ORDER BY version")
    return [row[0] for row in await cursor.fetchall()]


async def _fk_violations(conn) -> list:
    cursor = await conn.execute("PRAGMA foreign_key_check")
    return list(await cursor.fetchall())


async def _foreign_keys_on(conn) -> bool:
    cursor = await conn.execute("PRAGMA foreign_keys")
    return bool((await cursor.fetchone())[0])


async def _seed_pre_0015(conn) -> dict[str, object]:
    """Representative data at schema version 14: a parent/child task pair (self-referential
    FK), several steps, events, pending/decided approvals and a process record."""
    repos = StorageRepositories(conn)
    project_id = await repos.projects.insert(name="Upgrade project")
    parent = await repos.tasks.insert(
        title="Parent", user_request="parent", project_id=project_id, status="completed"
    )
    child = await repos.tasks.insert(
        title="Child",
        user_request="child",
        project_id=project_id,
        parent_task_id=parent,
        status="paused",
    )
    steps = []
    for i, status in enumerate(("completed", "completed", "waiting_for_approval")):
        steps.append(
            await repos.tasks.insert_step(
                task_id=child,
                step_index=i,
                title=f"Step {i}",
                tool_name="write_file",
                input_json='{"path": "a.txt"}',
                status=status,
            )
        )
    for step_id in steps:
        await repos.tasks.insert_event(
            task_id=child,
            step_id=step_id,
            event_type="step_started",
            message="start",
            severity="info",
            correlation_id="corr",
        )
    await repos.tasks.insert_event(task_id=parent, event_type="status_change", message="done")
    pending = await repos.approvals.insert(
        task_id=child,
        step_id=steps[2],
        action_type="tool",
        risk_level="high",
        summary="write",
        action_json="{}",
        action_hash="hash-1",
        context_id=project_id,
    )
    await repos.approvals.insert(
        task_id=parent,
        action_type="plan",
        risk_level="medium",
        summary="plan",
        action_json="{}",
        action_hash="hash-2",
        status="approved",
    )
    await repos.processes.register(
        id="proc-1",
        pid=4242,
        task_id=child,
        command_display="npm test",
        status="running",
        creation_time=123.5,
    )
    await conn.commit()

    cursor = await conn.execute("SELECT * FROM tasks ORDER BY id")
    tasks_before = [dict(r) for r in await cursor.fetchall()]
    return {
        "parent": parent,
        "child": child,
        "steps": steps,
        "pending": pending,
        "tasks_before": tasks_before,
    }


async def test_upgrade_from_v14_preserves_all_dependent_records(tmp_path: Path, monkeypatch):
    db = tmp_path / "upgrade.sqlite"
    async with sqlite_connection(db) as conn:
        _only(monkeypatch, [m for m in ALL_MIGRATIONS if m.version < 15])
        await run_migrations(conn)
        assert await _versions(conn) == list(range(1, 15))
        seeded = await _seed_pre_0015(conn)
        before = await _counts(conn)
        assert before == {
            "tasks": 2,
            "task_steps": 3,
            "task_events": 4,
            "approval_requests": 2,
            "process_registry": 1,
        }

        monkeypatch.undo()
        applied = await run_migrations(conn)

        assert applied == [v for v in range(15, LATEST + 1)]
        assert await _counts(conn) == before

        # IDs, relationships and timestamps are unchanged.
        cursor = await conn.execute("SELECT * FROM tasks ORDER BY id")
        assert [dict(r) for r in await cursor.fetchall()] == seeded["tasks_before"]
        cursor = await conn.execute(
            "SELECT parent_task_id FROM tasks WHERE id = ?", (seeded["child"],)
        )
        assert (await cursor.fetchone())[0] == seeded["parent"]
        cursor = await conn.execute(
            "SELECT id FROM task_steps WHERE task_id = ? ORDER BY step_index", (seeded["child"],)
        )
        assert [r[0] for r in await cursor.fetchall()] == seeded["steps"]
        cursor = await conn.execute(
            "SELECT status FROM approval_requests WHERE id = ?", (seeded["pending"],)
        )
        assert (await cursor.fetchone())[0] == "pending"
        cursor = await conn.execute(
            "SELECT pid, creation_time FROM process_registry WHERE id = 'proc-1'"
        )
        assert tuple(await cursor.fetchone()) == (4242, 123.5)

        # Integrity, indexes, constraints and enforcement are intact.
        assert await _fk_violations(conn) == []
        assert await _foreign_keys_on(conn)
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tasks'"
        )
        assert "idx_tasks_status" in {r[0] for r in await cursor.fetchall()}
        await conn.execute(
            "UPDATE tasks SET status = 'interrupted' WHERE id = ?", (seeded["child"],)
        )
        with pytest.raises(Exception, match="CHECK constraint"):
            await conn.execute("UPDATE tasks SET status = 'bogus' WHERE id = ?", (seeded["child"],))
        # Cascades still work after the rebuild (FK definitions survived the rename).
        await conn.execute("DELETE FROM tasks WHERE id = ?", (seeded["child"],))
        cursor = await conn.execute("SELECT COUNT(*) FROM task_steps")
        assert (await cursor.fetchone())[0] == 0


async def test_fresh_database_initialisation(tmp_path: Path):
    async with sqlite_connection(tmp_path / "fresh.sqlite") as conn:
        applied = await run_migrations(conn)
        assert applied == [m.version for m in ALL_MIGRATIONS]
        assert await _versions(conn) == applied
        assert await _counts(conn) == dict.fromkeys(("tasks", *CHILD_TABLES), 0)
        assert await _fk_violations(conn) == []
        assert await _foreign_keys_on(conn)


async def test_startup_is_idempotent_and_leaves_data_untouched(tmp_path: Path):
    db = tmp_path / "idempotent.sqlite"
    async with sqlite_connection(db) as conn:
        await run_migrations(conn)
        await _seed_pre_0015(conn)
        before = await _counts(conn)
    async with sqlite_connection(db) as conn:  # a second daemon start
        assert await run_migrations(conn) == []
        assert await _counts(conn) == before
        assert await _foreign_keys_on(conn)


async def test_already_recorded_0015_is_not_rerun(tmp_path: Path, monkeypatch):
    db = tmp_path / "recorded.sqlite"
    async with sqlite_connection(db) as conn:
        await run_migrations(conn)
        await _seed_pre_0015(conn)
        before = await _counts(conn)

        executed: list[int] = []
        real_apply = migrations_module._apply_migration

        async def spy(connection, migration):
            executed.append(migration.version)
            await real_apply(connection, migration)

        monkeypatch.setattr(migrations_module, "_apply_migration", spy)
        assert await run_migrations(conn) == []
        assert executed == []
        assert await _counts(conn) == before


async def test_failed_migration_rolls_back_and_can_be_retried(tmp_path: Path, monkeypatch):
    db = tmp_path / "failure.sqlite"
    async with sqlite_connection(db) as conn:
        await run_migrations(conn)
        await _seed_pre_0015(conn)
        before = await _counts(conn)

        broken = Migration(
            version=LATEST + 1,
            name="broken",
            sql=(
                "CREATE TABLE phase13_probe (id TEXT PRIMARY KEY);\n"
                "DELETE FROM task_steps;\n"
                "THIS IS NOT SQL;\n"
            ),
        )
        _only(monkeypatch, [*ALL_MIGRATIONS, broken])
        with pytest.raises(MigrationError, match="rolled back"):
            await run_migrations(conn)

        # Nothing from the failed migration persisted; version not recorded.
        assert LATEST + 1 not in await _versions(conn)
        assert await _counts(conn) == before
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'phase13_probe'"
        )
        assert (await cursor.fetchone())[0] == 0
        assert await _foreign_keys_on(conn)

        fixed = Migration(
            version=LATEST + 1,
            name="fixed",
            sql="CREATE TABLE phase13_probe (id TEXT PRIMARY KEY);",
        )
        _only(monkeypatch, [*ALL_MIGRATIONS, fixed])
        assert await run_migrations(conn) == [LATEST + 1]
        assert LATEST + 1 in await _versions(conn)


async def test_migration_introducing_fk_violation_is_rejected(tmp_path: Path, monkeypatch):
    async with sqlite_connection(tmp_path / "fk.sqlite") as conn:
        await run_migrations(conn)
        await _seed_pre_0015(conn)
        before = await _counts(conn)
        orphaning = Migration(version=LATEST + 1, name="orphan", sql="DELETE FROM tasks;")
        _only(monkeypatch, [*ALL_MIGRATIONS, orphaning])

        with pytest.raises(MigrationError, match="foreign-key violations"):
            await run_migrations(conn)
        assert await _counts(conn) == before
        assert LATEST + 1 not in await _versions(conn)


async def test_migration_with_transaction_control_is_rejected(tmp_path: Path, monkeypatch):
    async with sqlite_connection(tmp_path / "txn.sqlite") as conn:
        await run_migrations(conn)
        bad = Migration(
            version=LATEST + 1, name="commits", sql="CREATE TABLE x (id INT);\nCOMMIT;\n"
        )
        _only(monkeypatch, [*ALL_MIGRATIONS, bad])
        with pytest.raises(MigrationError, match="transaction control"):
            await run_migrations(conn)
        assert LATEST + 1 not in await _versions(conn)


def test_trigger_bodies_are_not_mistaken_for_transaction_control():
    for migration in ALL_MIGRATIONS:
        assert not migrations_module.TRANSACTION_CONTROL_PATTERN.search(migration.sql), (
            migration.name
        )
