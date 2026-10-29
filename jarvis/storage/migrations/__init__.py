"""SQLite migration runner and SQL migration resources.

Each pending migration is applied atomically: its SQL and its `schema_migrations`
record are written in one transaction, so a crash or error leaves the database at the
previous version with no partial schema changes.

Foreign-key enforcement is switched off (outside any transaction, where SQLite honours
the pragma) while migrations run. Table rebuilds (create new table, copy rows, drop old
table, rename) are otherwise unsafe: with enforcement on, `DROP TABLE` performs an
implicit DELETE that fires `ON DELETE CASCADE` on every child table. That is how the
original runner lost task steps, events, approvals and process records in migration
0015. Referential integrity is instead verified with `PRAGMA foreign_key_check` before
each migration commits, and the previous enforcement setting is restored afterwards.
See docs/migration-0015-data-loss.md for databases already affected.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from importlib.resources import files

import aiosqlite

MIGRATION_NAME_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>.+)\.sql$")

# Statement-level transaction control would break the runner's per-migration
# transaction. (Trigger bodies' BEGIN ... END are not matched: their BEGIN is not at
# the start of a line and `END;` alone is allowed.)
TRANSACTION_CONTROL_PATTERN = re.compile(
    r"^\s*(BEGIN(\s+(DEFERRED|IMMEDIATE|EXCLUSIVE))?(\s+TRANSACTION)?"
    r"|COMMIT(\s+TRANSACTION)?|ROLLBACK(\s+TRANSACTION)?|END\s+TRANSACTION"
    r"|SAVEPOINT\b.*|RELEASE\b.*)\s*;",
    re.IGNORECASE | re.MULTILINE,
)


class MigrationError(RuntimeError):
    """A migration could not be applied; the database was left at its previous version."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


async def run_migrations(connection: aiosqlite.Connection) -> list[int]:
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        """
    )
    await connection.commit()

    applied = await _applied_versions(connection)
    pending = [m for m in _load_migrations() if m.version not in applied]
    if not pending:
        return []

    foreign_keys_were_on = await _foreign_keys_enabled(connection)
    await connection.execute("PRAGMA foreign_keys = OFF")
    if await _foreign_keys_enabled(connection):
        # Only possible if a transaction is open, where SQLite ignores the pragma.
        raise MigrationError(
            "Could not disable foreign-key enforcement for migrations; "
            "refusing to run table rebuilds with cascading deletes enabled."
        )

    applied_now: list[int] = []
    try:
        for migration in pending:
            await _apply_migration(connection, migration)
            applied_now.append(migration.version)
    finally:
        if foreign_keys_were_on:
            await connection.execute("PRAGMA foreign_keys = ON")
    return applied_now


async def _apply_migration(connection: aiosqlite.Connection, migration: Migration) -> None:
    label = f"{migration.version:04d}_{migration.name}"
    if TRANSACTION_CONTROL_PATTERN.search(migration.sql):
        raise MigrationError(
            f"Migration {label} contains transaction control statements; "
            "the runner manages the transaction."
        )

    violations_before = await _foreign_key_violations(connection)
    try:
        # executescript commits any pending transaction first, then runs the script.
        # Prefixing BEGIN leaves the migration's own transaction open afterwards so the
        # version record and integrity check join it.
        await connection.executescript("BEGIN IMMEDIATE;\n" + migration.sql)
        await connection.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, ?, datetime('now'))",
            (migration.version, migration.name),
        )
        introduced = await _foreign_key_violations(connection) - violations_before
        if introduced:
            raise MigrationError(
                f"Migration {label} would introduce foreign-key violations: {dict(introduced)}"
            )
        await connection.commit()
    except BaseException as e:
        if connection.in_transaction:
            await connection.rollback()
        if isinstance(e, MigrationError) or not isinstance(e, Exception):
            raise
        raise MigrationError(f"Migration {label} failed and was rolled back: {e}") from e


async def _foreign_keys_enabled(connection: aiosqlite.Connection) -> bool:
    cursor = await connection.execute("PRAGMA foreign_keys")
    row = await cursor.fetchone()
    return bool(row and row[0])


async def _foreign_key_violations(connection: aiosqlite.Connection) -> Counter[tuple[str, str]]:
    """Count violations per (child table, parent table).

    Counted rather than compared row by row because rebuilt tables may get new rowids.
    """
    cursor = await connection.execute("PRAGMA foreign_key_check")
    rows = await cursor.fetchall()
    return Counter((str(row[0]), str(row[2])) for row in rows)


async def _applied_versions(connection: aiosqlite.Connection) -> set[int]:
    cursor = await connection.execute("SELECT version FROM schema_migrations")
    rows = await cursor.fetchall()
    return {int(row["version"]) for row in rows}


def _load_migrations() -> list[Migration]:
    migration_dir = files("jarvis.storage.migrations")
    migrations: list[Migration] = []
    for resource in migration_dir.iterdir():
        match = MIGRATION_NAME_PATTERN.match(resource.name)
        if match is None:
            continue
        migrations.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=resource.read_text(encoding="utf-8"),
            )
        )
    return sorted(migrations, key=lambda migration: migration.version)
