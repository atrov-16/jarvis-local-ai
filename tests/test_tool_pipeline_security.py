"""Regression tests for tool-pipeline correctness and execution security (Phase 14).

Tools are exercised through ToolExecutor.execute_step wherever the bug lived in the
integration (input validation, context merging, timeouts), in disposable workspaces.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from jarvis.approvals.broker import ApprovalBroker
from jarvis.approvals.models import ApprovalActionType, ProposedAction, RiskLevel
from jarvis.storage.connection import sqlite_connection
from jarvis.storage.migrations import run_migrations
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tasks import command_runner as cr
from jarvis.tasks.command_runner import CommandResult, CommandRunner
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tools.build_runner import BuildTool
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.filesystem import (
    DeleteFileTool,
    ListDirectoryTool,
    PatchFileTool,
    ReadFileTool,
    RestoreFileTool,
    WriteFileTool,
)
from jarvis.tools.generic_command import GenericCommandTool
from jarvis.tools.git import GitOperation, GitTool
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.tasks import GetTaskStatusTool
from jarvis.tools.test_runner import TestTool, find_project_python
from jarvis.tools.trash_manager import TrashManager
from jarvis.workspaces.registry import WorkspaceRegistry, dangerous_root_reason

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX paths/symlinks")


def ok_result(command: str = "cmd") -> CommandResult:
    return CommandResult(
        exit_code=0,
        stdout="",
        stderr="",
        execution_time=0.01,
        timeout_occurred=False,
        command=command,
        working_dir=".",
    )


@pytest.fixture
def workspace(tmp_path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("print('hello')\nvalue = 1\n")
    (ws / ".env").write_text("OPENROUTER_API_KEY=sk-secret\n")
    (ws / ".env.local").write_text("TOKEN=secret\n")
    (ws / ".env.example").write_text("OPENROUTER_API_KEY=\n")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n")
    (ws / ".jarvis").mkdir()
    (ws / ".jarvis" / "notes.txt").write_text("internal")
    return ws


@pytest.fixture
def workspaces(workspace) -> list[dict[str, Any]]:
    return [{"path": str(workspace)}]


@pytest.fixture
def mock_runner():
    runner = MagicMock(spec=CommandRunner)
    runner.run = AsyncMock(return_value=ok_result())
    return runner


@pytest.fixture
def executor(mock_runner) -> ToolExecutor:
    registry = ToolRegistry()
    for tool in (
        ReadFileTool(),
        WriteFileTool(),
        PatchFileTool(),
        DeleteFileTool(),
        RestoreFileTool(),
        ListDirectoryTool(),
        GetTaskStatusTool(),
        GitTool(mock_runner),
        TestTool(mock_runner),
        BuildTool(mock_runner),
        GenericCommandTool(mock_runner),
    ):
        registry.register(tool)
    return ToolExecutor(registry)


async def run_tool(executor, tool, inputs, workspaces, **extra) -> ToolResult:
    return await executor.execute_step(
        tool, json.dumps(inputs), workspaces=workspaces, task_id="task-ctx", **extra
    )


# --- 1. ToolExecutor integration contracts -----------------------------------------


async def test_patch_file_nested_models_survive_validation(executor, workspaces, workspace):
    result = await run_tool(
        executor,
        "patch_file",
        {"path": "src/app.py", "hunks": [{"search": "value = 1", "replace": "value = 2"}]},
        workspaces,
    )
    assert result.success, result.error
    assert "value = 2" in (workspace / "src" / "app.py").read_text()


async def test_get_task_status_context_collision(executor, tmp_path):
    db = tmp_path / "status.sqlite"
    async with sqlite_connection(db) as conn:
        await run_migrations(conn)
    uow = UnitOfWork(db)
    async with uow.begin() as unit:
        current = await unit.repositories.tasks.insert(title="Current", user_request="x")
        other = await unit.repositories.tasks.insert(title="Other", user_request="y")

    ctx = {"uow": uow, "task_id": current, "current_task_id": current, "workspaces": []}
    # No task_id supplied: the context's current task is used (previously TypeError).
    result = await executor.execute_step("get_task_status", "{}", **ctx)
    assert result.success, result.error
    assert result.data["title"] == "Current"
    # Explicit task_id supplied by the model overrides the context value.
    result = await executor.execute_step("get_task_status", json.dumps({"task_id": other}), **ctx)
    assert result.success, result.error
    assert result.data["title"] == "Other"


@pytest.mark.parametrize(
    "tool,inputs,check",
    [
        ("read_file", {"path": "src/app.py"}, lambda r, ws: "hello" in r.data),
        (
            "write_file",
            {"path": "out/new.txt", "content": "x"},
            lambda r, ws: (ws / "out" / "new.txt").read_text() == "x",
        ),
        (
            "list_directory",
            {"path": "."},
            lambda r, ws: {"src", ".env.example"} <= {i["name"] for i in r.data},
        ),
        ("delete_file", {"path": "src/app.py"}, lambda r, ws: not (ws / "src" / "app.py").exists()),
    ],
)
async def test_filesystem_tools_through_executor(
    executor, workspaces, workspace, tool, inputs, check
):
    result = await run_tool(executor, tool, inputs, workspaces)
    assert result.success, result.error
    assert check(result, workspace)


async def test_reserved_context_key_in_schema_is_refused():
    class BadInput(BaseModel):
        workspaces: list[str] = []

    class BadTool(BaseTool):
        def __init__(self):
            super().__init__(name="bad", description="", category=ToolCategory.READ_ONLY)

        def get_input_schema(self):
            return BadInput

        async def execute(self, **kwargs):
            return ToolResult(success=True)

    registry = ToolRegistry()
    registry.register(BadTool())
    result = await ToolExecutor(registry).execute_step(
        "bad", json.dumps({"workspaces": ["/"]}), workspaces=[]
    )
    assert not result.success
    assert "reserved context keys" in result.error


async def test_executor_uses_tool_execution_timeout():
    class SlowInput(BaseModel):
        pass

    class SlowTool(BaseTool):
        def __init__(self):
            super().__init__(
                name="slow", description="", category=ToolCategory.READ_ONLY, timeout_seconds=1
            )

        def get_input_schema(self):
            return SlowInput

        def execution_timeout(self, inputs):
            return 5  # e.g. an explicitly configured, longer command timeout

        async def execute(self, **kwargs):
            await asyncio.sleep(1.5)
            return ToolResult(success=True)

    registry = ToolRegistry()
    registry.register(SlowTool())
    result = await ToolExecutor(registry).execute_step("slow", "{}")
    assert result.success, result.error  # previously cut off at timeout_seconds=1


def test_command_tool_execution_timeout_covers_requested_timeout(mock_runner):
    tool = GenericCommandTool(mock_runner)
    assert tool.execution_timeout({"binary": "npm", "timeout": 900}) >= 910
    assert tool.execution_timeout({"binary": "npm", "timeout": None}) >= 310  # npm default 300
    assert TestTool(mock_runner).execution_timeout({}) > TestTool(mock_runner).timeout_seconds
    assert BuildTool(mock_runner).execution_timeout({}) > BuildTool(mock_runner).timeout_seconds


async def test_command_timeout_above_cap_is_rejected(executor, workspaces):
    result = await run_tool(executor, "command", {"binary": "ls", "timeout": 10**6}, workspaces)
    assert not result.success
    assert "Input validation failed" in result.error


# --- 2. Shared filesystem protection -----------------------------------------------

PROTECTED = [
    ".env",
    ".ENV",
    ".env.local",
    ".git/config",
    ".GIT/config",
    ".jarvis/notes.txt",
    "src/../.env",
    "src/.git/config",
]


@pytest.mark.parametrize("path", PROTECTED)
@pytest.mark.parametrize(
    "tool,extra",
    [
        ("read_file", {}),
        ("write_file", {"content": "pwned"}),
        ("patch_file", {"hunks": [{"search": "a", "replace": "b"}]}),
        ("delete_file", {}),
    ],
)
async def test_protected_paths_denied_for_every_operation(executor, workspaces, path, tool, extra):
    result = await run_tool(executor, tool, {"path": path, **extra}, workspaces)
    assert not result.success
    assert "protected system path" in result.error


async def test_protected_directory_cannot_be_listed_and_is_hidden(executor, workspaces):
    result = await run_tool(executor, "list_directory", {"path": ".git"}, workspaces)
    assert not result.success and "protected" in result.error
    listing = await run_tool(executor, "list_directory", {"path": "."}, workspaces)
    names = {i["name"] for i in listing.data}
    assert not names & {".env", ".env.local", ".git", ".jarvis"}
    assert ".env.example" in names  # templates hold no secrets


async def test_env_template_is_readable(executor, workspaces):
    result = await run_tool(executor, "read_file", {"path": ".env.example"}, workspaces)
    assert result.success


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "~/.ssh/id_rsa", "src/../../x"])
async def test_traversal_is_denied(executor, workspaces, path):
    result = await run_tool(executor, "read_file", {"path": path}, workspaces)
    assert not result.success
    assert "outside allowed workspaces" in result.error


@posix_only
async def test_symlink_to_protected_file_is_denied(executor, workspaces, workspace):
    (workspace / "innocent.txt").symlink_to(workspace / ".env")
    result = await run_tool(executor, "read_file", {"path": "innocent.txt"}, workspaces)
    assert not result.success
    assert "protected" in result.error


@posix_only
async def test_symlinked_directory_escape_is_denied(executor, workspaces, workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (workspace / "linkdir").symlink_to(outside, target_is_directory=True)
    for tool, extra in [("read_file", {}), ("write_file", {"content": "x"})]:
        result = await run_tool(executor, tool, {"path": "linkdir/secret.txt", **extra}, workspaces)
        assert not result.success
        assert "outside allowed workspaces" in result.error


async def test_disabled_workspace_grants_no_access(executor, workspace):
    result = await run_tool(
        executor, "read_file", {"path": "src/app.py"}, [{"path": str(workspace), "enabled": 0}]
    )
    assert not result.success


# --- Restore -------------------------------------------------------------------------


async def test_restore_round_trip_still_works(executor, workspaces, workspace):
    deleted = await run_tool(executor, "delete_file", {"path": "src/app.py"}, workspaces)
    assert deleted.success
    restored = await run_tool(
        executor,
        "restore_file",
        {"trash_id": deleted.data["trash_id"], "workspace_path": str(workspace)},
        workspaces,
    )
    assert restored.success, restored.error
    assert (workspace / "src" / "app.py").exists()


@pytest.mark.parametrize("destination", ["outside", "protected"])
def test_restore_revalidates_tampered_destination(workspace, tmp_path, destination):
    manager = TrashManager(workspace)
    entry_id = manager.trash(workspace / "src" / "app.py")
    target = tmp_path / "escaped.py" if destination == "outside" else workspace / ".git" / "hooks"
    meta = manager.metadata_dir / f"{entry_id}.json"
    data = json.loads(meta.read_text())
    data["original_path"] = str(target)
    meta.write_text(json.dumps(data))

    with pytest.raises(PermissionError):
        manager.restore(entry_id)
    assert not target.exists()
    assert (manager.entries_dir / entry_id).exists()  # nothing moved


def test_restore_rejects_path_like_trash_ids(workspace):
    with pytest.raises(ValueError, match="Invalid trash entry id"):
        TrashManager(workspace).restore("../../etc/passwd")


async def test_restore_requires_workspace_root(executor, workspaces, workspace):
    result = await run_tool(
        executor,
        "restore_file",
        {
            "trash_id": "00000000-0000-0000-0000-000000000000",
            "workspace_path": str(workspace / "src"),
        },
        workspaces,
    )
    assert not result.success
    assert "root of an allowed workspace" in result.error
    assert not (workspace / "src" / ".jarvis").exists()  # no stray trash dirs created


# --- 3. Git ----------------------------------------------------------------------------


async def test_git_diff_path_outside_workspace_is_rejected(executor, workspaces, mock_runner):
    for path in ("../other/file.py", "/etc/passwd", ".env"):
        result = await run_tool(executor, "git", {"operation": "diff", "path": path}, workspaces)
        assert not result.success
    mock_runner.run.assert_not_called()


async def test_git_diff_path_is_separated_and_hardened(
    executor, workspaces, workspace, mock_runner
):
    result = await run_tool(
        executor, "git", {"operation": "diff", "path": "src/app.py"}, workspaces
    )
    assert result.success, result.error
    args = mock_runner.run.call_args.args[1]
    assert args[args.index("--") + 1] == str(workspace / "src" / "app.py")
    assert "core.fsmonitor=false" in args and "--no-ext-diff" in args and "--no-textconv" in args


async def test_git_option_like_path_cannot_become_an_option(executor, workspaces, mock_runner):
    await run_tool(executor, "git", {"operation": "diff", "path": "--output=x"}, workspaces)
    for call in mock_runner.run.call_args_list:
        args = call.args[1]
        assert "--" in args
        assert not any(a.startswith("--output") for a in args[: args.index("--")])


async def test_git_status_runs_with_safe_options(mock_runner, workspaces):
    await GitTool(mock_runner).execute(operation=GitOperation.STATUS, workspaces=workspaces)
    assert mock_runner.run.call_args_list
    for call in mock_runner.run.call_args_list:
        assert call.args[1][:3] == ["--no-pager", "--no-optional-locks", "-c"]


# --- Test / build runners --------------------------------------------------------------


async def test_test_and_build_tools_require_approval():
    broker = ApprovalBroker(MagicMock(), MagicMock())
    action = ProposedAction(action_type=ApprovalActionType.TOOL, summary="s", action_json="{}")
    runner = MagicMock(spec=CommandRunner)
    for tool in (TestTool(runner), BuildTool(runner)):
        assert await broker.get_risk_level(action, tool.category) == RiskLevel.CRITICAL


@pytest.mark.parametrize(
    "field,value",
    [
        ("target", "--collect-only"),
        ("target", "-p"),
        ("pattern", "-x"),
        ("pattern", "a\nb"),
    ],
)
async def test_test_runner_rejects_option_injection(
    executor, workspaces, mock_runner, field, value
):
    result = await run_tool(executor, "test", {"framework": "pytest", field: value}, workspaces)
    assert not result.success
    assert "Invalid" in result.error
    mock_runner.run.assert_not_called()


async def test_test_runner_target_must_stay_in_workspace(executor, workspaces, mock_runner):
    for target in ("../elsewhere", ".git"):
        result = await run_tool(
            executor, "test", {"framework": "pytest", "target": target}, workspaces
        )
        assert not result.success
    mock_runner.run.assert_not_called()


async def test_build_target_option_injection_rejected(executor, workspaces, mock_runner):
    result = await run_tool(
        executor, "build", {"system": "make", "target": "-f/etc/evil"}, workspaces
    )
    assert not result.success
    mock_runner.run.assert_not_called()


async def test_test_runner_prefers_project_virtualenv(executor, workspaces, workspace, mock_runner):
    venv_python = workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    await run_tool(executor, "test", {"framework": "pytest"}, workspaces)
    assert mock_runner.run.call_args.args[0] == str(venv_python)


def test_python_lookup_falls_back_to_jarvis_interpreter(tmp_path):
    assert find_project_python(tmp_path) == (sys.executable, "jarvis")


# --- Generic command -------------------------------------------------------------------


@pytest.mark.parametrize(
    "binary,args",
    [
        ("find", [".", "-exec", "rm", "{}", ";"]),
        ("find", [".", "-delete"]),
        ("node", ["-e", "require('child_process').execSync('id')"]),
        ("node", ["--eval=1"]),
        ("npm", ["exec", "cowsay"]),
        ("pnpm", ["dlx", "x"]),
        ("make", ["--eval=all:;@id"]),
        ("gcc", ["-o/etc/evil", "a.c"]),
        ("grep", ["-r", "KEY", ".env"]),
        ("ls", ["~/.ssh"]),
    ],
)
async def test_dangerous_command_arguments_rejected(
    executor, workspaces, mock_runner, binary, args
):
    result = await run_tool(executor, "command", {"binary": binary, "args": args}, workspaces)
    assert not result.success
    assert "Security Error" in result.error
    mock_runner.run.assert_not_called()


@pytest.mark.parametrize(
    "binary,args",
    [
        ("npm", ["install"]),
        ("npm", ["run", "lint"]),
        ("find", [".", "-name", "*.py"]),
        ("grep", ["-rn", "TODO", "src"]),
        ("cargo", ["test", "--release"]),
        ("node", ["src/app.js"]),
    ],
)
async def test_legitimate_developer_commands_allowed(
    executor, workspaces, mock_runner, binary, args
):
    result = await run_tool(executor, "command", {"binary": binary, "args": args}, workspaces)
    assert result.success, result.error
    assert mock_runner.run.call_args.args[:2] == (binary, args)


# --- 4. Command containment --------------------------------------------------------------


def test_child_environment_is_allowlisted(monkeypatch):
    for key, value in {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LC_ALL": "C",
        "VIRTUAL_ENV": "/v",
        "GITHUB_TOKEN": "ghp_x",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "OPENROUTER_API_KEY": "sk",
        "NODE_OPTIONS": "--require=/tmp/evil.js",
        "NPM_TOKEN": "npm",
        "JARVIS_API_TOKEN": "j",
    }.items():
        monkeypatch.setenv(key, value)
    env = cr._child_environment({"EXTRA": "1", "JARVIS_API_TOKEN": "explicit"})
    assert {"PATH", "HOME", "LC_ALL", "VIRTUAL_ENV", "EXTRA"} <= set(env)
    assert not set(env) & {
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "OPENROUTER_API_KEY",
        "NODE_OPTIONS",
        "NPM_TOKEN",
        "JARVIS_API_TOKEN",
    }


async def test_secrets_not_visible_to_real_child_process(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    result = await CommandRunner().run(
        sys.executable,
        ["-c", "import os; print(os.environ.get('GITHUB_TOKEN', 'absent'))"],
        cwd=tmp_path,
    )
    assert result.stdout.strip() == "absent"


def test_windows_cmd_shim_is_resolved(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: True)
    monkeypatch.setattr(cr.shutil, "which", lambda name: r"C:\nodejs\npm.CMD")
    assert cr._resolve_executable("npm", ["install", "left-pad"]) == r"C:\nodejs\npm.CMD"


@pytest.mark.parametrize("arg", ["a&calc", "a|b", "%PATH%", 'x"y', "a\nb", "!x!", "a^b"])
def test_windows_batch_metacharacters_rejected(monkeypatch, arg):
    monkeypatch.setattr(cr, "_is_windows", lambda: True)
    monkeypatch.setattr(cr.shutil, "which", lambda name: r"C:\nodejs\npm.cmd")
    with pytest.raises(ValueError, match="unsafe for batch"):
        cr._resolve_executable("npm", ["install", arg])


def test_windows_exe_and_unresolved_commands(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: True)
    monkeypatch.setattr(cr.shutil, "which", lambda name: r"C:\Git\cmd\git.exe")
    assert cr._resolve_executable("git", ["log", "a&b"]) == r"C:\Git\cmd\git.exe"  # no cmd.exe
    monkeypatch.setattr(cr.shutil, "which", lambda name: None)
    assert cr._resolve_executable("missing", []) == "missing"


def test_posix_does_not_rewrite_command(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: False)
    assert cr._resolve_executable("npm", ["a&b"]) == "npm"


# --- Workspace roots ---------------------------------------------------------------------


@posix_only
@pytest.mark.parametrize(
    "root", ["/", "/usr", "/usr/local/project", "/etc", "/Users", "/home", "/opt"]
)
def test_dangerous_workspace_roots(root):
    assert dangerous_root_reason(Path(root).resolve()) is not None


def test_home_and_its_parents_rejected():
    home = Path.home().resolve()
    assert dangerous_root_reason(home) is not None
    assert dangerous_root_reason(home.parent) is not None


def test_project_directory_accepted(tmp_path):
    assert dangerous_root_reason((tmp_path / "project").resolve()) is None


async def test_registry_rejects_dangerous_roots_and_files(tmp_path):
    db = tmp_path / "ws.sqlite"
    async with sqlite_connection(db) as conn:
        await run_migrations(conn)
    registry = WorkspaceRegistry(UnitOfWork(db))
    with pytest.raises(ValueError, match="Refusing to register"):
        await registry.add("home", str(Path.home()))
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        await registry.add("file", str(a_file))
    project = tmp_path / "project"
    project.mkdir()
    assert await registry.add("project", str(project))
