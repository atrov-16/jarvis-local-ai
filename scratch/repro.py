import asyncio
import aiosqlite
from pathlib import Path

async def reset_db(db_path: Path):
    if db_path.exists():
        db_path.unlink()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        await db.execute("INSERT INTO test (val) VALUES ('init')")
        await db.commit()

async def repro_2_missing_wal(db_path: Path):
    print("\n--- Repro 2: Missing WAL Mode (Writer blocked by Reader) ---")
    await reset_db(db_path)
    
    # timeout=0.1 means fail immediately if locked
    reader = await aiosqlite.connect(db_path, timeout=0.1, isolation_level="DEFERRED")
    writer = await aiosqlite.connect(db_path, timeout=0.1, isolation_level="DEFERRED")
    
    await reader.execute("BEGIN")
    await reader.execute("SELECT * FROM test")
    print("[Reader] Read lock acquired. Holding...")
    
    try:
        await writer.execute("BEGIN IMMEDIATE")
        print("[Writer] SUCCESS (Should not happen without WAL!)")
    except Exception as e:
        print(f"[Writer] FAILED as expected: {type(e).__name__} - {e}")
        
    await reader.close()
    await writer.close()

async def repro_5_nested_deadlock(db_path: Path):
    print("\n--- Repro 5: Nested API Transactions Deadlock ---")
    await reset_db(db_path)
    
    conn1 = await aiosqlite.connect(db_path, timeout=1.0, isolation_level="DEFERRED")
    conn2 = await aiosqlite.connect(db_path, timeout=1.0, isolation_level="DEFERRED")
    
    try:
        await conn1.execute("BEGIN DEFERRED")
        await conn1.execute("UPDATE test SET val = 'outer' WHERE id = 1")
        print("[Outer] Updated row. Write lock held.")
        
        # Now the inner transaction tries to write (like broker.approve)
        print("[Inner] Attempting to write on separate connection (Nested Transaction)...")
        await conn2.execute("BEGIN DEFERRED")
        await conn2.execute("UPDATE test SET val = 'inner' WHERE id = 1")
        print("[Inner] SUCCESS (Should not happen!)")
    except Exception as e:
        print(f"[Inner] FAILED as expected: {type(e).__name__} - {e}")
        
    await conn1.close()
    await conn2.close()

async def repro_6_concurrent_writes(db_path: Path):
    print("\n--- Repro 6: Concurrent Database Writes in bulk_respond ---")
    await reset_db(db_path)
    
    async def write_task(task_id: int):
        conn = await aiosqlite.connect(db_path, timeout=0.5, isolation_level="DEFERRED")
        try:
            await conn.execute("BEGIN DEFERRED")
            # Force read-lock first (aiosqlite DEFERRED defaults to this)
            await conn.execute("SELECT * FROM test")
            await asyncio.sleep(0.1) # Simulate some work
            # Attempt to upgrade to write lock
            await conn.execute(f"UPDATE test SET val = 'task_{task_id}' WHERE id = 1")
            await conn.commit()
            print(f"[Task {task_id}] SUCCESS")
        except Exception as e:
            print(f"[Task {task_id}] FAILED as expected: {type(e).__name__} - {e}")
        finally:
            await conn.close()
            
    print("[bulk_respond] Launching 5 concurrent writes via asyncio.gather...")
    await asyncio.gather(*(write_task(i) for i in range(5)))

async def main():
    db_path = Path("scratch_repro.db")
    await repro_2_missing_wal(db_path)
    await repro_5_nested_deadlock(db_path)
    await repro_6_concurrent_writes(db_path)
    if db_path.exists():
        db_path.unlink()

if __name__ == "__main__":
    asyncio.run(main())
