# Jarvis — project context for Claude Code

Jarvis is a local-first AI developer assistant: a FastAPI daemon on loopback, a Typer/Rich CLI,
SQLite storage, an LLM planner (OpenRouter primary, Ollama fallback), a tool registry, and
human approval before risky actions. Design docs: `docs/architecture-v1.md`,
`docs/architecture-review-phase-0.md`.

The author works on two machines: a **Windows laptop** (`C:\jarvis`) at home and a **Mac** at
college. GitHub (`atrov-16/jarvis-local-ai`) is the only sync between them. Commit and push before
switching machines; pull when starting.

## Where things stand (as of 2026-11-11)

- `main` is at `f6e4c25` ("Add task show command"). All current work is on branch
  `phase16/checkpoint-paused` (pushed), which was 21 commits ahead of `main` at `a23ca6b` and
  already contains the `rc-hardening` fixes from the October review. Don't merge it into `main`
  until a macOS run is green.
- Windows suite at `a23ca6b`: 641 passed, 37 skipped (the skips are macOS/POSIX-only tests).
  **macOS has not been run yet** on this branch.
- CI exists: `.github/workflows/ci.yml` (Windows and macOS, Python 3.12 and 3.13).
- ruff and mypy are held at the baseline in `.github/static-analysis-baseline.json`
  (247 / 112 at `a23ca6b`). Stay at or below it, and lower the numbers when you fix findings.
- Platform-specific tests use the markers in `tests/_platforms.py` (`windows`, `macos`, `posix`).
  macOS/POSIX-marked tests added since the last Mac run are listed in
  `docs/mac-test-checklist.md`.
- Phase 17 (the observe-act agent loop) is in progress on `phase16/checkpoint-paused` (draft PR
  #1). Done: slice 1 native tool calling, slice 2 non-blocking EventBus, slice 3 migration 0016
  (`tasks.final_answer`, `task_steps.tool_call_id`, `task_messages`). Next: slice 4 (the loop,
  replaces the planner), slice 5 (API/CLI/events). Plan: `docs/gui-and-agent-loop-plan.md`.
- The portability list below was written before Phase 16 and has not been re-audited. Check each
  item against the code before working on it.

## Current goal: make Jarvis work on both Windows and macOS

Work in this order:

1. Run the full suite on macOS; fix failures. Merge `phase16/checkpoint-paused` into `main` once
   green on both.
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
4. Then the big functional gap (below).

## Still open from the October review

- **Planner is one-shot.** `jarvis/tasks/planner.py` writes every step's tool input before any
  step runs; step outputs are never fed back, tool-less steps are no-ops, and tasks end with no
  answer to the user. Replace with an observe-act loop using native tool calling, approval per
  tool call, and a final response.
- **CLI approvals** (`jarvis/app/terminal.py`) never show `input_json` or a diff before approving.
- **EventBus** (`jarvis/core/event_bus.py`) awaits `queue.put` into bounded queues; one slow
  subscriber blocks every publisher. Drop or disconnect slow subscribers.
- `python-dotenv` is imported by the daemon but not declared in `pyproject.toml`.

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
