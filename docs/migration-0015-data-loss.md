# Migration 0015: task history loss on upgrade

## What happened

Migration `0015_add_interrupted_status.sql` rebuilds the `tasks` table to allow the
`interrupted` status (create `tasks_new`, copy rows, `DROP TABLE tasks`, rename).

The original migration runner applied it with `PRAGMA foreign_keys = ON`. In SQLite,
`DROP TABLE` on a parent table then performs an implicit `DELETE`, which fires
`ON DELETE CASCADE` on every child table. Upgrading a database from schema version 14
(or earlier) therefore:

- **kept every row in `tasks`**, but
- **deleted every row in `task_steps`, `task_events`, `approval_requests` and
  `process_registry`**.

If any task had a `parent_task_id`, the implicit `DELETE` instead failed with
`FOREIGN KEY constraint failed` part-way through. Because the old runner did not wrap
migrations in a transaction, that left a stray `tasks_new` table behind and every later
daemon start failed with `table tasks_new already exists`.

Fresh databases (created directly at the latest version) were never affected: they
had no rows to lose.

## What is fixed

The migration runner (`jarvis/storage/migrations/__init__.py`) now:

1. turns foreign-key enforcement off **outside** any transaction (SQLite ignores the
   pragma inside one), so table rebuilds cannot cascade;
2. applies each migration and its `schema_migrations` record in a single transaction;
3. runs `PRAGMA foreign_key_check` before committing and rolls back if the migration
   introduced violations;
4. restores the previous foreign-key setting.

The SQL of migration 0015 is unchanged and keeps version 15: the resulting schema is
identical, so databases that already recorded version 15 are not migrated again, and
databases that have not reached version 15 now upgrade without losing data.

## A forward migration cannot recover deleted rows

If your database already applied migration 0015 with the old runner, the deleted steps,
events, approvals and process records are gone from that file. No later migration can
reconstruct them. The only way back is a backup taken **before** the upgrade.

## Check whether a database was affected (read-only)

Stop the daemon first. Then open the database read-only (default location:
`.jarvis/dev/memory.sqlite`, relative to the directory the daemon was started from):

```sh
sqlite3 -readonly .jarvis/dev/memory.sqlite
```

```sql
-- When was version 15 applied?
SELECT applied_at FROM schema_migrations WHERE version = 15;

-- Tasks created before that moment that have been processed (not still 'queued')
-- but have no events at all. Every processed task records events, so these lost
-- their history in the upgrade.
SELECT t.id, t.status, t.title, t.created_at
FROM tasks t
WHERE t.status <> 'queued'
  AND datetime(t.created_at) < (SELECT applied_at FROM schema_migrations WHERE version = 15)
  AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id);
```

No rows means no processed task lost its history. For the stuck-startup case, check
for a leftover table: `SELECT name FROM sqlite_master WHERE name = 'tasks_new';`.

## Restore from a backup

1. Stop the Jarvis daemon.
2. Move the current database aside; do not delete it. Keep it until you have verified
   the restore, because tasks created after the upgrade exist only there.
   Move `memory.sqlite` together with any `memory.sqlite-wal` and `memory.sqlite-shm`
   files.
3. Copy the pre-upgrade backup into place as `memory.sqlite`. If the backup was taken
   while the daemon was running, include its `-wal` file, or produce a consistent copy
   with `sqlite3 backup.sqlite ".backup memory.sqlite"`.
4. Start the daemon with this version. The restored database is below version 15, so
   the fixed runner upgrades it with all task history intact.
5. Re-run the read-only checks above, then compare against the moved-aside file. Copy
   over any tasks created after the original upgrade manually if you need them.

If the daemon is stuck on `table tasks_new already exists` and you have no backup:
version 15 was not recorded and the original `tasks` table may still exist. Make a
copy of the database before trying any manual repair.

## Preventing this in future

Back up `memory.sqlite` before upgrading Jarvis. Migrations that rebuild a table must
not contain transaction control or `PRAGMA foreign_keys` statements: the runner
manages both, and rejects migrations that contain transaction statements.
