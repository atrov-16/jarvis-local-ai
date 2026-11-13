# Phase 18 handoff (approval tiers)

Branch `phase18/approval-tiers`, draft PR #2 to `main`. Decisions and slices: the Phase 18 prompt
(grill session, settled; do not reopen) and `docs/gui-and-agent-loop-plan.md`.

## Done and pushed

- Slice 0 `16f99bd`: `CLAUDE.md` says Phase 17 is merged at `7829f08`, Phase 18 in progress.
- Slice 1 `2e9ae51`: a call that never runs (bad arguments, over the per-turn limit, denied) inserts a
  `step_failed` event, which the relay publishes as `task.tool_call_completed` with
  `success=False`. Code: `AgentLoop._store_tool_calls` (`jarvis/tasks/agent_loop.py`) and `decide_step`
  (`jarvis/tasks/decisions.py`). Tests: end of the "Bus events" section of
  `tests/test_task_stream_api.py`. Review item 6 is marked fixed in `docs/phase17-review.md`.
- State at `2e9ae51`: Windows suite 827 passed, 37 skipped; ruff 230 and mypy 106 (equal to
  `.github/static-analysis-baseline.json`).

## Next: slice 2 (path resolution), then 3 to 6 as in the prompt

Starting points, found while reading the code:

- The approval queue is `jarvis/tasks/queue.py` (there is no `jarvis/approvals/queue.py`);
  `jarvis/approvals/` holds `broker.py`. `tiers.py` goes in `jarvis/approvals/`.
- `is_outside = False` is hardcoded in `queue.py`; slice 2 replaces it.
- A task's steps run through `TaskQueue._execute_step`; the loop is `AgentLoop`
  (`self._agent_loop` on the queue). `app.state.task_queue._tool_executor._registry` is where tests
  register dummy tools (`tests/_agent_helpers.py`: `ScriptedModel`, `EchoTool`, `WriteTool`).
- `tests/test_task_stream_api.py` has the `client`, `app`, `tools`, `published` fixtures and the
  `script`, `wait_for`, `waiting_step` helpers; reuse them for API-level tests.
- Event types the relay forwards: `BUS_EVENT_FOR` in `jarvis/tasks/event_relay.py`. A new Auto audit
  event (decision 7) is stored in `task_events` and relayed only if added there.

## Environment gotchas

- `gh` is not on the Git Bash PATH. Use `C:\Program Files\GitHub CLI\gh.exe` from PowerShell; pass
  `--json` and parse with `ConvertFrom-Json` (`--jq` strings break in PowerShell quoting).
- ECC's GateGuard hook blocks the first Bash/Edit/Write of a session and the first edit of each file
  until you state the facts it asks for (call sites, existing tests, the user's instruction
  verbatim). Do a Grep for the call sites first. A blocked Edit in a parallel batch can leave a file
  half-edited: re-read before building on it.
- mypy does not narrow a variable asserted in one branch and used after the `if`; declare it
  `X | None = None` before the branch and assert where it is used.
- Do not start a long pytest and `sleep`-poll it; run it in the foreground (the full suite takes
  about 4 minutes) or use an `until` loop.
- `docs/next-session-prompt.md` and `.claude/` are untracked on purpose; leave them out of commits.

## Still to do after slice 6

QA on a scratch database, full suite and baselines, `docs/mac-test-checklist.md` entries for any
macos/posix tests, `supermatt:pr` body then `gh pr ready` (do not merge), one
`supermatt:code-review` since `7829f08` into `docs/phase18-review.md`.
