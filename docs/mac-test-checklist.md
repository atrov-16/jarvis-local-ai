# macOS test checklist

Tests marked `macos` or `posix` that have been added on the Windows laptop and have **not yet
run on a Mac**. They are skipped on Windows, so a green Windows run says nothing about them.

On the Mac: `git pull`, then `uv sync --group dev` and `uv run pytest -ra`. When the run is
green, clear the list below and note the commit it was green at.

Last green macOS run: none yet on `phase16/checkpoint-paused`.

## Added during Phase 17 (agent loop)

None yet.
