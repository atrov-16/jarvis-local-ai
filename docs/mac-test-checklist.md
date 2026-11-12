# macOS test checklist

Tests marked `macos` or `posix` that have been added on the Windows laptop and have **not yet
run on a Mac**. They are skipped on Windows, so a green Windows run says nothing about them.

On the Mac: `git pull`, then `uv sync --group dev` and `uv run pytest -ra`. When the run is
green, clear the list below and note the commit it was green at.

Last green macOS run: GitHub CI (Windows and macOS, Python 3.12 and 3.13) passed on macOS at
`d25e5b3` (Phase 16, merged to `main`). That is the CI run only; the list below has not been run
by hand on a Mac.

## Added during Phase 17 (agent loop)

None. Phase 17 added no macos/posix-marked tests (Windows skips stayed at 37, same as at `d25e5b3`).
