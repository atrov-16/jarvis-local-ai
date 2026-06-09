# Architecture Review and Phase 0 Plan

Status: Review output  
Reviewed document: `docs/architecture-v1.md`  
Date: 2026-06-10

## 1. Review Summary

The V1 architecture is directionally strong. The big decisions are consistent with the product goal: a local-first Windows assistant with a persistent daemon, terminal-first client, conservative approvals, explicit projects/workspaces, provider-agnostic models, and durable state.

Before implementation begins, the architecture should be tightened in a few places:

- Add a shared storage/database component instead of letting memory own SQLite implicitly.
- Enable local API authentication in V1, not as an optional future enhancement.
- Add persistent app state for current project and daemon/client state.
- Add write/delete file tools to the V1 tool plan, because file modification is a stated V1 capability.
- Freeze approval action payloads so an approved action cannot be changed before execution.
- Add an internal event bus component for WebSocket and task events.
- Reorder or clarify phases so model routing exists before model-dependent memory proposals and planning behavior.

## 2. Critical Changes Before Implementation

### 2.1 Add a Shared Storage Layer

Current issue:

- The schema covers memory, projects, workspaces, tasks, approvals, commands, and audit logs.
- The module layout has `memory/sqlite_store.py`, but no shared `storage` or `database` package.

Risk:

- Components may each open SQLite independently and duplicate transaction/migration logic.
- Cross-component operations such as "approve action, update task, write audit log" need a single transactional boundary.

Recommended change:

Add:

```text
jarvis/storage/
  __init__.py
  connection.py
  migrations.py
  repositories.py
  unit_of_work.py
```

Phase 0 should implement this before component-specific persistence.

### 2.2 Enable Local API Token in V1

Current issue:

- The architecture says localhost-only access is the V1 security boundary and the API token is optional.

Risk:

- Any local process could call the API.
- Read-only actions are automatic inside approved workspaces, so an unauthenticated local API could expose files, memories, project state, and logs.

Recommended change:

Enable a local API token by default in V1.

Rules:

- Bind to `127.0.0.1`.
- Require token for all `/v1/*` HTTP endpoints and WebSocket connections.
- Allow unauthenticated `GET /health` only.
- Store token as a secret, not in `config.toml`.
- Terminal client reads token through the SecretManager or a protected local runtime file.

### 2.3 Add Persistent App State

Current issue:

- Project switching is explicit, but the schema does not persist the current project.
- Runtime state like active daemon instance, current project, and client-visible preferences is not modeled.

Recommended change:

Add:

```sql
CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Suggested keys:

```text
current_project_id
daemon_instance_id
last_started_at
```

### 2.4 Add Immutable Action Payloads to Approvals

Current issue:

- `approval_requests.details_json` stores display details, but the document does not explicitly require immutable action payloads or hashes.

Risk:

- The user might approve one command or file action, but a later mutation could execute something different.

Recommended change:

Extend `approval_requests`:

```sql
action_json TEXT NOT NULL,
action_hash TEXT NOT NULL
```

Execution must verify the action hash before running.

### 2.5 Add File Write/Delete Tools to V1

Current issue:

- The architecture says file creation, modification, and deletion are V1 capabilities that require approval.
- The initial native tool list only includes `file.read`, `file.list`, and `file.search`.

Recommended change:

Add V1 native tools:

```text
file.write
file.patch
file.delete
```

All must require approval.

### 2.6 Add an Internal Event Bus

Current issue:

- WebSocket events are defined, but no component owns event publishing/subscription.

Recommended change:

Add:

```text
jarvis/core/event_bus.py
```

Responsibilities:

- Publish events from task queue, approvals, tools, models, memory, projects, and workspaces.
- Let WebSocket clients subscribe.
- Keep event format stable.
- Optionally persist important events through `task_events` and `audit_log`.

### 2.7 Clarify Phase Ordering Around Model-Dependent Features

Current issue:

- Phase 3 includes memory proposals.
- Phase 4 adds model routing.
- In practice, meaningful "This seems important. Save it?" proposals require a model or a simple interim heuristic.

Recommended change:

Either:

- Move Model Router before Memory V1, or
- Split Memory V1 into storage/search first, then model-generated proposals after Model Router.

Recommended order:

```text
Phase 3: Model Router
Phase 4: Memory storage/search and memory proposal workflow
Phase 5: Task queue and planning
```

## 3. Useful Simplifications

These are not blockers, but they will reduce early friction.

### 3.1 Defer `approval_rules` Implementation Details

Keep the table or migration placeholder if desired, but do not implement reusable rule evaluation in V1. Represent it as a disabled future table only.

### 3.2 Keep API Surface Thin in Phase 0 and Phase 1

The full API list is fine as a target, but Phase 0/1 only need:

```text
GET /health
GET /v1/status
GET /v1/config/public
GET /v1/events
```

Other routes can wait until their components exist.

### 3.3 Use FTS5 for Keyword Memory Search

Plain `LIKE` search will work briefly but degrade quickly. If SQLite FTS5 is available, use it for:

```text
short_term_context
long_term_memory
project_notes
task_events
```

This still counts as keyword search and does not force semantic embeddings.

### 3.4 Add Indexes Early

Add indexes for common lookups:

```text
tasks(status, priority, created_at)
task_steps(task_id, step_index)
approval_requests(status, created_at)
short_term_context(project_id, task_id, created_at)
long_term_memory(project_id, task_id, created_at)
audit_log(task_id, created_at)
```

### 3.5 Add Output Limits for Commands

Command execution should capture stdout/stderr, but Phase 0/8 should plan for:

- Maximum captured bytes.
- Truncation metadata.
- Secret redaction.
- Timeout handling.
- Process cancellation.

## 4. Missing Components to Add to the Architecture

Add these modules or explicit responsibilities:

```text
jarvis/storage/
  Shared SQLite connection, migrations, transactions, repositories.

jarvis/core/event_bus.py
  Internal pub/sub for API streaming and task status.

jarvis/core/context_builder.py
  Assembles model context from task, project, memory, tools, and policy.

jarvis/core/policies.py
  Shared policy types for action risk, permissions, and client visibility.

jarvis/logging/
  Structured logs, redaction, and operational diagnostics.
```

The `context_builder` is important because model prompts should not be assembled ad hoc inside the agent. It gives one place to enforce memory inclusion, project context, tool specs, and safety instructions.

## 5. Recommended Decisions on Open Questions

### Local API Token

Decision: enable in V1.

Reason: automatic read-only access and memory search make unauthenticated localhost access too permissive.

### Default OpenRouter Model

Decision: make it configurable and avoid hardcoding a fragile default in code.

Suggested config default:

```toml
default_model = "openai/gpt-4.1"
```

Implementation should tolerate the configured model being unavailable and report provider/model status clearly.

### Default Ollama Model

Decision: configurable, with `llama3.1` as a reasonable initial placeholder.

Implementation should not assume the model is installed. It should check Ollama availability and list/report missing local model clearly.

### Database Location

Decision:

- Development default: repo-local `.jarvis/dev/memory.sqlite`.
- Packaged/user default: `%APPDATA%\Jarvis\memory.sqlite`.
- Tests: temporary directory.

Reason: this keeps early development inspectable without polluting user app data.

### Workspace Registration Confirmation

Decision: explicit command is enough in V1 if the terminal shows the path and asks for confirmation before registering.

Reason: workspace registration grants automatic read access, so it deserves a clear confirmation even though it is user-initiated.

### Plan Approval for File Writes and Commands

Decision:

- Require plans for multi-step tasks, code/project work, and automation.
- For a single simple file write or command, action approval alone is enough.

Reason: avoids making the system tedious while preserving transparency for real tasks.

## 6. Revised Phase Order

Recommended V1 implementation order:

```text
Phase 0: Project foundation
Phase 1: Daemon/API shell and terminal connection
Phase 2: Storage, app state, projects, workspaces, audit baseline
Phase 3: Model router and provider status
Phase 4: Memory storage/search/proposals
Phase 5: Task queue and planning
Phase 6: Approval broker
Phase 7: Native tools and file access
Phase 8: Command execution
Phase 9: Hardening
```

## 7. Phase 0 Implementation Plan

Phase 0 goal:

Create a clean Python project skeleton with configuration, storage foundations, migrations, tests, and an empty daemon that can start and stop.

Phase 0 should not implement the agent brain, model calls, tool execution, or command execution.

### 7.1 Repository Setup

Create:

```text
pyproject.toml
README.md
.gitignore
.env.example
docs/
jarvis/
tests/
```

Recommended package skeleton:

```text
jarvis/
  __init__.py
  app/
    __init__.py
    daemon.py
    terminal.py
  api/
    __init__.py
    http.py
    schemas.py
    websocket.py
  config/
    __init__.py
    manager.py
    models.py
    secrets.py
  core/
    __init__.py
    event_bus.py
    events.py
  logging/
    __init__.py
    redaction.py
    setup.py
  storage/
    __init__.py
    connection.py
    migrations.py
    repositories.py
    unit_of_work.py
```

Defer these directories until later phases or create empty packages only:

```text
approvals/
execution/
memory/
models/
projects/
tools/
workspaces/
```

### 7.2 Dependency Setup

Use a standard `pyproject.toml`.

Recommended runtime dependencies:

```text
fastapi
uvicorn[standard]
pydantic
pydantic-settings
typer
rich
httpx
aiosqlite
platformdirs
keyring
```

Recommended development dependencies:

```text
pytest
pytest-asyncio
ruff
mypy
types-keyring
```

Recommended Python version:

```text
Python 3.11+
```

### 7.3 Configuration Foundation

Implement:

- `JarvisConfig` Pydantic model.
- Config file discovery.
- Defaults for development.
- TOML load support.
- Environment overrides for simple settings.
- Public config serialization that excludes secrets.

Initial config sections:

```text
server
paths
models
memory
approvals
tasks
security
logging
```

Phase 0 acceptance:

- Config loads with defaults when no config file exists.
- Config can load from an explicit file path.
- Public config output excludes secret values.

### 7.4 Secret Foundation

Implement a stub-safe `SecretManager`.

Phase 0 behavior:

- Read API token and OpenRouter key from environment variables.
- Define Windows Credential Manager/keyring methods, but allow them to be no-op until later if needed.
- Never print secret values.

Initial secret keys:

```text
JARVIS_API_TOKEN
OPENROUTER_API_KEY
```

Phase 0 acceptance:

- Missing secrets produce clear status values, not crashes.
- Secret values are redacted in logs and config output.

### 7.5 Storage Foundation

Implement:

- SQLite connection factory.
- Migration runner.
- Schema version table.
- App state table.
- Audit log table.
- Basic transaction/unit-of-work helper.

Phase 0 migration:

```text
0001_initial_foundation.sql
```

Minimum tables:

```text
schema_migrations
app_state
audit_log
```

Phase 0 acceptance:

- Database file is created.
- Migrations run once.
- Re-running migrations is safe.
- App state can be read/written.
- Audit entry can be inserted.

### 7.6 Event Bus Foundation

Implement:

- In-process async event bus.
- Event model with type, id, timestamp, payload.
- Subscribe/publish API.

Phase 0 acceptance:

- Events can be published and received in tests.
- Event payloads are JSON-serializable.

### 7.7 API Shell

Implement minimal FastAPI app:

```text
GET /health
GET /v1/status
GET /v1/config/public
GET /v1/events
```

Security:

- Bind default host to `127.0.0.1`.
- Require API token for `/v1/*`.
- Leave `/health` open.

Phase 0 acceptance:

- App can be created in tests.
- Health endpoint returns OK.
- Status endpoint returns daemon/config/storage status.
- Auth failure returns a clear 401.

### 7.8 Terminal Stub

Implement a minimal Typer app:

```text
jarvis daemon
jarvis status
jarvis config show
```

Phase 0 acceptance:

- CLI imports cleanly.
- `status` can call the local daemon when available.
- If daemon is unavailable, CLI shows a friendly message.

### 7.9 Logging and Redaction

Implement:

- Structured logging setup.
- Redaction helper for known secret values.
- Basic log level config.

Phase 0 acceptance:

- Logs do not include configured secrets.
- Tests cover redaction.

### 7.10 Tests

Create tests for:

```text
config loading
public config redaction
secret manager environment fallback
storage migrations
app state read/write
audit insert
event bus publish/subscribe
API health/status/auth
CLI import smoke test
```

Recommended test layout:

```text
tests/
  test_config.py
  test_secrets.py
  test_storage.py
  test_event_bus.py
  test_api.py
  test_cli.py
```

### 7.11 Phase 0 Completion Criteria

Phase 0 is complete when:

- The repository has a valid Python package skeleton.
- Config loads with safe defaults.
- Secrets are separated from config.
- SQLite migrations run.
- `app_state` and `audit_log` are usable.
- Event bus works in-process.
- FastAPI app exposes health/status/public config/events.
- `/v1/*` routes require a local API token.
- Typer CLI can query status.
- Tests pass.
- No model calls, file writes, command execution, or tool execution are implemented yet.

## 8. Suggested Next Architecture Edits

Before Phase 0 code starts, update `docs/architecture-v1.md` to include:

- `jarvis/storage` in the module layout.
- `app_state` table.
- `action_json` and `action_hash` on approval requests.
- `file.write`, `file.patch`, and `file.delete` in native tools.
- Internal `EventBus`.
- API token enabled by default.
- Revised phase order.

