# Phase 17 review (d25e5b3..c97393c)

Three axes (standards, spec, adversarial), one host reader each, run once. Coverage: host-only, no
external peer (`docs/agents/cross-model.json` does not exist), so there is no cross-family
agreement. Excluded from the diff: `docs/` and `uv.lock`. Nothing was run by the readers except one
read-only trigger. Windows suite at review time: 822 passed, 37 skipped; ruff 230 / mypy 106.

Validation: the P1 was reproduced by the reader and I re-read the code path before fixing it. The
P2 and P3 items were **not validated** by a second reader; treat them as the readers' claims.

Verdict: P1 fixed (below). No P0. Open: P2 and P3 items for you to decide. **Ready**, subject to CI
being green on Windows and macOS.

## Fixed (one commit each, test-first, pushed)

| Sev | Finding | Commit |
|---|---|---|
| P1 | The approval preview printed the model's text with terminal escape sequences intact (`jarvis/app/terminal.py` `_print_call`). A prompt-injected model could erase or redraw a line so the user approved something other than what was stored (e.g. `generic_command` args `["rm","-rf","/","\x1b[2K\x1b[1G","git status"]`). `describe_call` now shows every non-printable character as an escape (`\x1b`). Test: `test_control_characters_in_arguments_are_shown_not_sent_to_the_terminal`. | `ef5ec41` |

## Open: adversarial (P2/P3)

1. **P2: legacy plan-approval tasks are stranded** (`jarvis/tasks/decisions.py:191`,
   `decide = broker.approve if approve else broker.deny`). A pre-Phase-17 database with a task in
   `waiting_for_plan_approval` and a pending `plan` request: `/plan/approve` is gone, and approving
   the plan from the approval center returns `{"approved": true}` but moves nothing. The task stays
   until cancelled. Fix options: fail or cancel those tasks and expire their plan requests in
   migration 0016 or at startup, and reject `action_type == "plan"` with 409 in `decide_approval`.
2. **P2: a reply cut off by `max_tokens` is treated as valid** (`jarvis/tasks/agent_loop.py:279`
   `max_tokens=4096,`, `:293` `_finish_with_answer(...)`). `finish_reason` is never checked. A cut-off
   tool call is "not valid JSON" and the model repeats it until 15 turns are gone. A cut-off text
   reply is stored as the `final_answer` and the task completes. Fix: on `finish_reason == "length"`
   fail the task with a clear message, or tell the model to be shorter. Needs your call on which.
3. **P2: a task that failed on `max_turns` fails again at once if resumed**
   (`jarvis/tasks/agent_loop.py:263`, `turns_used = sum(1 for m in messages if m.role == "assistant")`).
   Already noted in `docs/phase17-handoff.md` as known. Fix: refuse resume for that failure with a
   4xx, or count turns from the resume point.
4. **P2: bus events can reach subscribers out of `seq` order** (`jarvis/storage/unit_of_work.py:80`
   `await self.connection.close()`, `:86` `await self._on_commit(events)`). The worker commits seq 5
   and is still closing its connection; an API scope commits seq 6, closes and publishes first. A
   client that keeps "the last `seq` I saw" as its cursor then skips 5 on `?since=6`. Fix: publish
   under one lock in `seq` order, or document the cursor as "highest contiguous `seq`" and have
   clients dedupe.
5. **P3: cancel can land after the last check but before the tool runs**
   (`jarvis/tasks/agent_loop.py:418` `if not await self._still_running(task_id):`, `:421`
   `outcome = await self._execute_step(task_id, step)`). A cancel between the two still runs an
   already-approved `write_file`. Fix: make the "mark running" transaction in `_execute_step`
   conditional on the task still being `running`. I would do this one before merge: it is a small
   change and it is the approval/cancel boundary.
   **Fixed before merge (a9430cb):** `_execute_step` now does a compare-and-set on the task
   (`running` → `running`) in the same transaction that marks the step running and writes
   `step_started`, and returns `StepOutcome.STOPPED` if the task was cancelled. The approval stays
   unconsumed. Regression test: `test_a_cancel_after_the_last_check_stops_the_call_before_it_runs`.

Not issues (checked by the reader): no approval bypass or double execution (approval is a one-time
compare-and-set, consumed in the same transaction as `step_started`); risk classification and path
policy unchanged; migration 0016 is additive; the reflection listener does not lose `task.completed`
to the drop policy.

## Open: spec (P2/P3)

6. **P2: calls that never run emit no terminal event** (fixed in Phase 18 slice 1: they now insert `step_failed`), so a client sees them stuck at "proposed"
   (`jarvis/tasks/agent_loop.py:379` `event_type="tool_call_proposed",`; `decisions.py` sets a denied
   call to `skipped` with only `approval_denied` / `status_change`; `BUS_EVENT_FOR` in
   `jarvis/tasks/event_relay.py:7` relays none of those as a call-completed event). Calls with bad
   arguments (`failed`) and calls over the per-turn limit (`skipped`) are the same. Fix: also insert
   a `step_failed` event when a step is stored or decided as `failed` or `skipped`; the relay already
   maps it to `task.tool_call_completed` with `success=False`.
7. **P3: a `write_file` preview shows no diff** (`jarvis/app/call_preview.py:57` `_write_lines`).
   The spec says "the diff for writes"; only `patch_file` gets one. A `write_file` over an existing
   file shows the new lines only, so the user cannot see what is lost. Fix: diff against the current
   file when the path resolves, or leave it for Phase 18.
8. **P3: doc drift.** `CLAUDE.md` says ruff/mypy "232 / 106 at `d25e5b3`"; the baseline file says 230 /
   106 (it was lowered since). Fix: change the CLAUDE.md number to 230 or drop it.

Scope the spec did not ask for (none harmful; keep or cut): `_raise_for_error_body` in
`jarvis/models/openrouter.py:189` reclassifies HTTP-200 error bodies; `llm_input_schema` in
`jarvis/tools/git.py:80` narrows the git operation enum offered to the model;
`jarvis/models/tool_calls.py` rejects NaN/Infinity and deep nesting; tool results sent to the model
are cut at 20,000 characters (`MAX_TOOL_RESULT_CHARS`); the events endpoint has a `limit` capped at
500 (`MAX_EVENTS_PER_REQUEST`).

## Open: standards (judgement calls)

No documented-standard violations. Approval and path-policy checks are not weakened; no secrets; no
platform-specific code was added; ruff and mypy are at baseline.

Test gaps (P2):
- The deleted `test_failure_persisting_result_fails_task_instead_of_sticking` has no replacement.
  Its subject (a tool returning an unserialisable result must fail the task, not leave it `running`)
  still goes through `_process_task`. Add an agent-loop test with a tool returning `data=object()`.
- `jarvis/tasks/agent_loop.py:90` and `:101` (title truncation and the `PLACEHOLDER_TITLE` fallback)
  are untested. Add a 200-character request, a blank request, and a call with no string argument.
- `jarvis/tasks/decisions.py:90` (`raise DecisionError(409, "A newer approval request exists for this step.")`,
  the stale-request guard) is untested. Create a second request for the step, then decide the first.

Smells and small items (P3):
- `jarvis/api/http.py:165` `max_turns=jarvis_config.tasks.max_turns,`: only `make_queue(max_turns=3)`
  is tested, so a break in the config wiring would go unnoticed.
- `tests/test_approval_drives_task.py:133` `time.sleep(1.5)` before a negative assertion depends on
  timing. Poll until the worker has had one cycle instead.
- The same `wait_for` / `script` / `app` / `client` fixtures are copied in
  `tests/test_approval_drives_task.py:57`, `tests/test_task_approvals.py:52` and
  `tests/test_task_stream_api.py:92`. Move them to `tests/_agent_helpers.py`.
- `jarvis/api/http.py:506`, `:519`, `:679`: the same `raise HTTPException(status_code=e.status_code, detail=e.detail) from None`;
  `approve_request` and `deny_request` differ only by a flag.
- Step-prefix resolution exists three times in `jarvis/app/terminal.py` (`_find_step` at `:557`,
  and again at `:790` and `:830`). Reuse `_find_step`.
- Two sources of truth for the defaults: `jarvis/tasks/agent_loop.py:44` `DEFAULT_MAX_TURNS = 15`
  (and the per-turn call limit) versus `jarvis/config/models.py:47`.
- `jarvis/tasks/decisions.py:106` and `:127`: the variable `granted` holds an event type, including
  for a denial. Rename to `event_type`.
- `jarvis/tasks/queue.py:75` `tool_registry=tool_executor._registry,` reads a private attribute. Pass
  the registry in.
- `jarvis/storage/repositories.py:811` builds `insert_step` SQL dynamically so that old-schema test
  seeds keep working (test-driven production code).
- `tests/test_agent_loop.py:6` points to `test_agent_loop_api.py`, which does not exist; the tests
  are in `test_approval_drives_task.py` and `test_task_stream_api.py`.
