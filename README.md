# Jarvis AI

> A modular, local-first AI assistant with persistent memory, secure tool execution, project awareness, and human-in-the-loop approvals.

![Python](https://img.shields.io/badge/Python-3.12+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-In%20Development-orange)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## Overview

Jarvis AI is a local-first AI assistant designed to automate developer workflows while maintaining transparency, security, and user control.

Unlike traditional AI assistants that execute actions autonomously, Jarvis follows a human-in-the-loop architecture where every high-risk action requires explicit user approval before execution.

The project focuses on modularity, safety, extensibility, and long-term memory while remaining lightweight enough to run locally.

---

## Features

### AI Planning

- Intelligent task planning using LLMs
- Multi-step task decomposition
- Structured execution pipeline
- Deterministic planning prompts

### Human Approval System

- Plan approval before execution
- Individual tool approval
- Risk-based permission system
- Secure approval workflow

### Memory

- Persistent SQLite memory
- Reflection engine
- Memory consolidation
- Long-term storage
- Context retrieval

### Project Awareness

- Multiple projects
- Workspace isolation
- Secure filesystem boundaries
- Project-specific task execution

### Tool Execution

- Modular tool architecture
- File system operations
- Extensible Tool Registry
- Timeout protection
- Input validation

### Reliability

- Automatic recovery after crashes
- Task persistence
- Event-driven architecture
- Robust error handling

---

# Architecture

```
                    User
                      │
                      ▼
               Terminal CLI
                      │
                      ▼
                FastAPI Daemon
                      │
        ┌─────────────┴─────────────┐
        │                           │
        ▼                           ▼
   Task Planner               Memory Engine
        │                           │
        ▼                           ▼
 Approval Broker            Reflection Engine
        │                           │
        ▼                           ▼
  Tool Executor            SQLite Database
        │
        ▼
 Registered Tools
        │
        ▼
 Filesystem / Commands
```

---

# Technology Stack

- Python
- FastAPI
- SQLite
- Pydantic
- HTTPX
- Typer CLI
- OpenRouter
- Ollama (fallback)
- Pytest

---

# Project Structure

```
jarvis/
│
├── api/
├── app/
├── approvals/
├── config/
├── core/
├── memory/
├── models/
├── projects/
├── storage/
├── tasks/
├── tools/
├── workspaces/
│
tests/
docs/
```

---

# Current Capabilities

✔ Persistent Memory

✔ Task Planning

✔ Tool Registry

✔ Secure File Operations

✔ Project Management

✔ Workspace Management

✔ Human Approval Workflow

✔ Recovery System

✔ OpenRouter Integration

✔ Ollama Fallback Support

---

# Installation

Clone the repository

```bash
git clone https://github.com/atrov-16/jarvis-local-ai.git

cd jarvis-local-ai
```

Create a virtual environment

```bash
python -m venv .venv
```

Activate it

Windows

```bash
.venv\Scripts\activate
```

Install dependencies

```bash
pip install -e .
```

---

# Configuration

Create a `.env` file in the project root.

Example:

```env
OPENROUTER_API_KEY=your_api_key_here
JARVIS_API_TOKEN=your_local_api_token
```

If `JARVIS_API_TOKEN` is not set, the daemon generates a token on first start and stores it in
the OS credential store (Windows Credential Manager; service `Jarvis`, account `API Token`),
where the CLI finds it. The token value is never written to the log. If the credential store is
unavailable, the daemon refuses to start and asks you to set `JARVIS_API_TOKEN`.

## Database location

By default the database is `memory.sqlite` in the per-user data directory, whichever directory
you start Jarvis from:

| Platform | Default database |
|---|---|
| Windows | `%APPDATA%\Jarvis\memory.sqlite` |
| macOS (development) | `~/Library/Application Support/Jarvis/memory.sqlite` |
| Linux (development) | `$XDG_DATA_HOME/Jarvis/memory.sqlite` (`~/.local/share/...`) |

To use another file, set `JARVIS_DATABASE_PATH`, or `database_path` in the `[memory]` section of
a config file (`--config`, or `JARVIS_CONFIG_PATH`). The environment variable wins. `~` and
environment variables are expanded (`%APPDATA%` on Windows, where `$` is left as is;
`$HOME` / `${VAR}` elsewhere). A relative path is resolved against the
working directory, so prefer an absolute one; the daemon logs a warning for relative paths. The
daemon logs the database it opens at startup, and `GET /v1/status` reports it under `storage`.

**Upgrading from an earlier version.** The default used to be `.jarvis\dev\memory.sqlite`
relative to the directory the daemon was started from (normally the repository root). If such a
database exists in the current directory or the repository root and the new default does not,
the daemon refuses to start rather than create a second, empty database. Nothing is moved
automatically. Either keep using the existing database:

```env
JARVIS_DATABASE_PATH=C:\path\to\jarvis-local-ai\.jarvis\dev\memory.sqlite
```

or stop the daemon and move `memory.sqlite`, together with any `memory.sqlite-wal` and
`memory.sqlite-shm` files beside it, into `%APPDATA%\Jarvis\`. Migrations are unchanged: the
database is upgraded in place on the next start, wherever it lives.

## Model providers

Requests go to `primary_provider` (default `openrouter`). After a transient failure (timeout,
rate limit, 5xx) or HTTP 402 (insufficient OpenRouter credits), the request is sent once to
`fallback_provider` (default `ollama`, at `http://localhost:11434`); set `fallback_provider = ""`
in `[models]` to disable the fallback. Authentication errors do not fall back. Background
reflection requests at most `reflection_max_tokens` output tokens (default 1024).

---

# Running Jarvis

Start the daemon

```bash
uv run jarvis daemon
```

Check status

```bash
uv run jarvis status
```

Submit a task

```bash
uv run jarvis task submit "Create a hello.py file that prints Hello World"
```

Approve the generated plan

```bash
uv run jarvis task approve <task-id>
```

Approve an individual tool

```bash
uv run jarvis task approve <task-id> --step <step-id>
```

---

# Security Model

Jarvis follows a human-in-the-loop philosophy.

High-risk actions such as:

- Writing files
- Running shell commands
- Modifying project data

require explicit approval before execution.

Filesystem operations are restricted to approved project workspaces.

---

# Design Principles

- Local-first
- Transparent execution
- Modular architecture
- Human oversight
- Secure by default
- Extensible components

---

# Roadmap

## Version 1.0

- Complete workspace linking
- Finish approval workflow
- Improved CLI
- Better planning prompts
- Enhanced documentation

## Future

- Desktop GUI
- Voice interface
- Semantic memory search
- Plugin system
- Multi-agent collaboration
- Docker deployment
- Web dashboard
- Cloud synchronization (optional)

---

# Testing

Run the complete test suite

```bash
pytest
```

---

# Inspiration

Jarvis draws inspiration from the fictional J.A.R.V.I.S. while focusing on practical software engineering principles rather than science fiction.

The goal is not simply to build another chatbot, but to create a trustworthy developer assistant capable of planning tasks, maintaining long-term memory, and safely interacting with the local machine.

---

# Author

**Atharva Tawde**

Computer Science Engineering Student

MIT World Peace University

GitHub:
https://github.com/atrov-16

---

# Contributing

Contributions, ideas, feature requests, and bug reports are always welcome.

Feel free to fork the repository and submit a pull request.

---

# License

This project is licensed under the MIT License.
