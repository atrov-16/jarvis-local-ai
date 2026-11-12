# Phase 17 handoff: what is done, what slice 5 still needs

Branch `phase16/checkpoint-paused` (draft PR #1). Last commit when this was written: `c6f444c`.
Windows suite green: 776 passed, 37 skipped. ruff 232 / mypy 106 (baseline file matches).
macOS not run locally (CI passed on macOS at `d25e5b3`, before Phase 17). No macOS/posix-marked
tests were added in slices 1-4, so `docs/mac-test-checklist.md` has nothing to list.

## Done

| Slice | Commit | What |
|---|---|---|
| 1 | `9032e02` | Native tool calling in the providers; `ToolCall`/`ToolChoice`; `ToolRegistry.to_openai_tools()` |
| 2 | `701f5ae` | Non-blocking `EventBus` (drop or disconnect slow subscribers, counted) |
| 3 | `0804434` | Migration 0016: `tasks.final_answer`, `task_steps.tool_call_id`, `task_messages`; repository methods |
| 4 | `c6f444c` | `jarvis/tasks/agent_loop.py` replaces the planner |

Decisions the user made for slice 4: a denied call is reported to the model and the task
continues; `final_answer` and `GET /v1/tasks/{id}/summary` coexist; 15 turns / 5 calls per turn.

## How the loop works (what slice 5 builds on)

- `TaskQueue` (`jarvis/tasks/queue.py`) = worker + `_execute_step` (risk, approval, one-time
  consumption) + `_fail_task` / `_interrupt_task`. `AgentLoop.run(task_id)` drives one task.
- States reused: `queued -> running -> paused` (a step is `waiting_for_approval`) `-> queued` (on
  approve or deny) `-> ... -> completed | failed | cancelled | interrupted`. A paused task holds no
  worker slot. `waiting_for_plan_approval` is kept in the enum only so old rows load.
- Step statuses used: `pending`, `approved`, `running`, `waiting_for_approval`, `completed`,
  `failed`, `skipped`. A denied call is `skipped` with error text `the user denied this call.
  Reason: ...`; calls beyond the per-turn limit are `skipped` too; a tool failure is `failed`.
  None of these fail the task: the model gets one `tool` message per call
  ("Error: ...", "Not run: ...", or the tool's JSON output).
- Task events go to `task_events` (types: `status_change`, `tool_call_proposed`, `step_started`,
  `step_completed`, `step_failed`, `approval_requested`, `approval_granted`, `approval_denied`,
  `final_answer`). Bus events published today: `task.completed`, `memory.retrieved`,
  `approval.created/approved/denied`, `task.recovered`.
- `max_turns` counts assistant messages in `task_messages`, so it carries across a resume (a task
  that failed on `max_turns` fails again at once if resumed; raise the config value first).
- A queued task that has steps but no `task_messages` (made by the retired planner) fails with a
  clear message.

## Slice 5 (not started): spec and notes

From `docs/next-session-prompt.md`:

1. **Response models** (`jarvis/api/schemas.py`): `final_answer` is already on `TaskResponse`.
   Still to add on `TaskStepResponse`: `tool_call_id`, `risk_level` (from the step's approval
   request) and the full `input_json` (already there as stored text; check nothing truncates it).
2. **Events published after commit**: `task.status_changed`, `tool_call_proposed`,
   `tool_call_started`, `tool_call_completed`, `approval_required`, `final_answer`. Publish only
   after the transaction commits (see how `task.completed` is done in `_finish_with_answer`).
   Subscribers are non-blocking now (slice 2), so publishing is cheap.
3. **`GET /v1/tasks/{id}/events?since=`** for catch-up after a dropped WebSocket (the WebSocket is
   closed with code 1013 when a client falls behind). `task_events` has no sequence column and
   `list_events` orders by `created_at`, which can tie. Use the table's implicit `rowid` as the
   cursor (no migration needed), or add a column if you prefer an explicit one.
4. **`POST /v1/approvals/{id}/approve` (and deny) must drive the step and task transitions**, as
   `POST /v1/tasks/{task_id}/steps/{step_id}/approve|deny` already do in `jarvis/api/http.py`
   (`_load_step_awaiting_decision`, then approve, `transition_step`, `transition paused -> queued`,
   events). Move that logic into one shared helper so both routes behave identically. The
   approval center and `POST /v1/approvals/bulk` also only flip the approval row today; check them.
   Keep: an approval authorises exactly one execution attempt; only `pending` requests can be
   approved; an approved-but-consumed request must be rejected.
5. **CLI** (`jarvis/app/terminal.py`): `task show` and `task approve` print the call's arguments
   (or the diff for writes) before approving. `task approve` without `--step` already approves the
   single waiting step (slice 4); keep that, and show what is being approved first.
6. Tests for each of the above; update CLAUDE.md's Phase 17 line; then `gh pr ready 1` and stop.
   Do not merge the PR.

## Practical notes for the next session

- Test helpers: `tests/_agent_helpers.py` (`ScriptedModel`, `tool_reply`, `text_reply`, dummy
  tools, `wait_for_task_status`). Queue-level tests run `queue._process_task` directly; API tests
  swap `app.state.model_router.complete` for `ScriptedModel.complete`. The worker polls every 1s.
- A hook in this environment asks for "facts" before the first edit or write of each file in a
  session; state the callers/schemas and the instruction, then retry the same call.
- `CLAUDE.md`'s first "Where things stand" bullets (about `main` at `f6e4c25` and the branch being
  21 commits ahead) are stale: `main` is `d25e5b3` (Phase 16, merged). Only the Phase 17 line was
  asked to be kept current, so the rest was left alone.
- `docs/next-session-prompt.md` and `.claude/` are still untracked.
