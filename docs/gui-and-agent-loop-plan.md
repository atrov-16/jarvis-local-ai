# Jarvis GUI and agent loop: plan

Status: draft for review (2026-11-09). Nothing here is built yet.

## Goal

The user types a request and Jarvis does the work, running several steps in a row, and comes
back with an answer. The user only steps in when something is actually risky, and when they do,
it takes one click on a card that shows exactly what will happen. Nobody types task IDs or
approval IDs.

Complex tasks are not a goal. Low friction is.

## Why a GUI alone won't get there

Today the friction comes from the backend as much as from the terminal:

- **One-shot planner** (`jarvis/tasks/planner.py`). It writes every step's tool input up front,
  step outputs are never fed back, and the task ends with no answer. A GUI over this would still
  feel stiff.
- **Approval granularity.** You approve the plan, then individual steps, and in the CLI you
  can't see `input_json` or a diff before approving.

So the work is backend first, then UI.

## Approval policy (reduce clicks without weakening checks)

Path policy, protected paths and the approval broker stay as they are. What changes is which
actions are *asked* and how they are *grouped*:

| Tier | Examples | Behaviour |
|---|---|---|
| Auto | read file, list dir, search, grep inside the workspace | Runs, shows in the activity feed, no prompt |
| Batch | write or patch files in the workspace, allowlisted commands | One card per turn listing every action with diffs: **Approve all** / review individually. Optional "allow edits for this task" |
| Always ask | delete, anything outside the workspace, network, `git push`, high or critical risk | Its own card every time, never batched or remembered |

Protected paths (`.env`, `.git`, ...) are never auto-approved, in any tier. The tier mapping is
config the user can tighten, with defaults that are safe. Every auto-run action is still recorded
in the audit log.

## Phases

Numbering continues from the original build (phases 1–16).

**Phase 16 wrap-up (prerequisites)**
- Finish the Windows/macOS parity work on `phase16/checkpoint-paused` and merge.
- Fix memory search (FTS5 phrase-query bug in `search_long_term`).

**Phase 17: Agent loop (backend)**
- Replace the one-shot planner with an observe-act loop using native tool calling
  (OpenRouter, with an Ollama fallback).
- Feed each tool result back to the model and end with a final answer to the user.
- Step limit, cancel, and a resume-after-crash checkpoint.
- Stream every step over the existing `/v1/events` websocket.

**Phase 18: Approval tiers (backend)**
- Classify each tool call into Auto / Batch / Always-ask.
- Group the pending Batch calls of one turn into a single approval. The existing
  `/v1/approvals/bulk` and approval center are the starting point.
- Include the diff or preview in the approval payload.
- Regression tests: protected paths never auto-run, and Always-ask is never batched.

**Phase 19: GUI v1**
- **Chat:** type a request and see the answer.
- **Activity feed:** live list of what Jarvis is doing (read X, edited Y, ran Z), collapsible.
- **Approval tray:** cards with diff or command preview and Approve all / Deny / Review.
  Keyboard shortcuts.
- **History:** past tasks, with a re-run option.
- Workspace picker and daemon status.
- Styling: neutral greyscale placeholder tokens in one file. **The user picks the colours** when
  styling starts.

**Phase 20: Polish**
- Auto-start the daemon, a tray icon, desktop notifications when approval is needed.
- Optional desktop wrapper (see the tech choice below).
- Colours and theme applied from the user's choice.

## Tech choice

**Decided (2026-11-09): a Tauri desktop app.**

- The frontend is a web UI (framework still open: React with Vite, or Svelte; keep it small).
  It talks to the daemon over the existing loopback REST and `/v1/events` websocket API. It never
  touches SQLite directly.
- The Tauri shell starts the Python daemon if it isn't already running. A sidecar process or
  an "attach to running daemon" mode both work. It reads the API token from the per-user config
  location and never asks the user to paste it.
- Rendering uses the OS webview: WebView2 on Windows (built into Windows 11) and WKWebView on
  macOS.
- Toolchain: Rust (`rustup`) and Node are needed on **both** machines, and the Tauri CLI comes in
  through npm. CI gains a `tauri build` job on `windows-latest` and `macos-latest`.
- Code signing and notarisation (especially for macOS Gatekeeper) are deferred. Unsigned
  dev builds are fine for personal use.
- Keep the Tauri allowlist minimal: no shell or fs plugins for the frontend. All real work goes
  through the daemon so the approval and path-policy checks still apply.
- Alternatives considered: a browser tab served by the daemon (simplest), Electron (heavier) and
  PySide/Qt (dated UI toolkit).

## Memory and Obsidian (separate track)

- SQLite stays the source of truth for Jarvis memory.
- Optional export and import of memories as Markdown (frontmatter and `[[links]]`) into an
  Obsidian vault, so the user can browse and edit them.
- NotebookLM is a good tool for a human to research over documents. It isn't usable as an
  agent's live memory, because there is no API Jarvis could call that I can confirm.

## Open questions for the user

1. ~~Browser tab vs Tauri~~: Tauri (decided 2026-11-09).
2. Should "allow edits for this task" exist, or should batch approval be once per turn?
3. ~~Phase 16 wrap-up first, or the agent loop in parallel?~~: wrap-up first (done on Windows; macOS pending).
