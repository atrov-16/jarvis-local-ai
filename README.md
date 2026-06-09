# Jarvis

Jarvis is a local-first Windows desktop AI assistant. Phase 0 builds only the project
foundation: configuration, secret handling, storage migrations, an internal EventBus, a
minimal local API, and a terminal stub.

## Phase 0 Commands

```powershell
uv run pytest
uv run ruff check .
uv run mypy jarvis
uv run jarvis --help
```

The V1 API is localhost-only. `/health` is unauthenticated; `/v1/*` routes require a
local API token.

