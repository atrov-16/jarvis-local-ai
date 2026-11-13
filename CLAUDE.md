# Jarvis — project context for Claude Code

Jarvis is a local-first AI developer assistant: a FastAPI daemon on loopback, a Typer/Rich CLI,
SQLite storage, an LLM planner (OpenRouter primary, Ollama fallback), a tool registry, and
human approval before risky actions. Design docs: `docs/architecture-v1.md`,
`docs/architecture-review-phase-0.md`.

The author works on two machines: a **Windows laptop** (`C:\jarvis`) at home and a **Mac** at
college. GitHub (`atrov-16/jarvis-local-ai`) is the only sync between them. Commit and push before
switching machines; pull when starting.

## Where things stand (as of 2026-11-13)

- `main` is at `7829f08`: Phases 16 and 17 are merged (PR #1). GitHub CI is green on Windows and
  macOS at `7829f08`. Phase 18 is in progress on `phase18/approval-tiers` (draft PR to `main`);
  merge it once CI is green on both platforms.
- Windows suite at `7829f08`: 824 passed, 37 skipped (the skips are macOS/POSIX-only tests).
  No local macOS run since Phase 17 began; CI is the only macOS signal.
- CI: `.github/workflows/ci.yml` (Windows and macOS, Python 3.12 and 3.13).
- ruff and mypy are held at the baseline in `.github/static-analysis-baseline.json`
  (230 / 106 at `7829f08`). Stay at or below it, and lower the numbers when you fix findings.
- Platform-specific tests use the markers in `tests/_platforms.py` (`windows`, `macos`, `posix`).
  macOS/POSIX-marked tests added since the last Mac run are listed in
  `docs/mac-test-checklist.md`.
- The agent loop (Phase 17) is in `jarvis/tasks/agent_loop.py`: the model calls tools turn by
  turn; a denied or failed call is reported back to the model. Every approve or deny goes through
  `jarvis/tasks/decisions.py`. Task events reach clients through `jarvis/tasks/event_relay.py`
  after the transaction commits. Review findings still open: `docs/phase17-review.md`.
- Phase 18 (approval tiers: Auto, Batch, Always ask): in progress. Decisions and slices are in
  `docs/gui-and-agent-loop-plan.md`. Phase 19 is the GUI, Phase 20 polish.
- The portability list below was written before Phase 16 and has not been re-audited. Check each
  item against the code before working on it.

## Current goal: make Jarvis work on both Windows and macOS

Work in this order:

1. Run the full suite on macOS; fix failures.
2. Keep CI green on both platforms (pytest, ruff and mypy on every push).
3. Known portability gaps:
   - **Line endings.** `write_file` / `patch_file` in `jarvis/tools/filesystem.py` use
     `read_text`/`write_text` in text mode, so on Windows every edit rewrites the whole file to
     CRLF. Read and write with `newline=""` and keep the file's original line-ending style. Add a
     `.gitattributes` (`* text=auto eol=lf`).
   - **Process checks.** `jarvis/core/process_utils.py` reads process start time from `/proc`,
     which macOS lacks, so the PID-reuse guard in orphan recovery is silently off on Mac. The
     Windows path in `jarvis/core/orphan_recovery.py` spawns PowerShell per process. Replace both
     (and `taskkill`) with `psutil`.
   - **Command allowlist.** `jarvis/tools/generic_command.py`: `grep`/`ls` usually don't exist on
     Windows and `find` is `find.exe` (different program, so the `find` denylist is meaningless
     there). Build the allowlist per platform at startup with `shutil.which` and tell the planner
     which tools exist.
   - **Untested Windows paths.** `.cmd` shim resolution and `taskkill` tree-kill in
     `jarvis/tasks/command_runner.py`, and Windows dangerous-root rejection in
     `jarvis/workspaces/registry.py`, have never run. Tests for symlinks and roots are
     `posix_only` with no Windows counterparts. Add Windows tests, including NTFS name tricks
     against the protected-path check (`.env::$DATA`, `.env` with trailing space or dot, 8.3
     names like `GIT~1`).
   - `taskkill` is called with blocking `subprocess.run` inside async code.
4. The big functional gap (the agent loop) is closed: Phase 17.

## Still open from the October review

Nothing. The one-shot planner, CLI approvals without args, the blocking EventBus and the
undeclared `python-dotenv` are all fixed (Phase 17 and `pyproject.toml`).

## Running it

```
uv sync --group dev
uv run pytest
uv run jarvis daemon
uv run jarvis status
```

Database: by default `%APPDATA%\Jarvis\memory.sqlite` (Windows) or
`~/Library/Application Support/Jarvis/memory.sqlite` (macOS). If an older database exists at
`.jarvis/dev/memory.sqlite`, the daemon refuses to create a new empty one; set
`JARVIS_DATABASE_PATH` in `.env` or move the file (with its `-wal`/`-shm` files). Each machine
has its own database and API token; they are not synced.

## Conventions

- Never weaken the approval or path-policy checks to make a test pass.
- Every behaviour change gets a regression test; anything platform-specific gets a test on that
  platform (skip on the other, never skip on both).
- Never read, print or commit `.env`, API tokens or the SQLite database.
- Commit with your own git identity (`git config user.name` / `user.email` set per repo,
  especially on shared college machines). Descriptive commit messages.
