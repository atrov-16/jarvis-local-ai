"""`grep -r` must not read files the file tools may never read.

Path arguments of the `command` tool are checked one by one, but `grep -rn KEY .` walks
the whole tree below an allowed directory and used to print `.env` and `.git/config`.
The unit tests run everywhere; the real-process tests run the real grep (macOS/Linux
always, Windows when Git for Windows' Unix tools are installed) against canary files.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jarvis.tasks.command_runner import CommandResult, CommandRunner
from jarvis.tools.generic_command import (
    GenericCommandTool,
    _grep_argument_error,
    _grep_protection_arguments,
    _harden_grep_arguments,
)

# --- Argument policy (no process is started) -----------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["-rn", "TODO", "."],
        ["-r", "--include=*.py", "TODO", "."],
        ["-e", "Regex", "-r", "."],
        ["-eRegex", "-r", "."],  # attached pattern: 'R' is part of the pattern, not a flag
        ["-rnC", "3", "TODO", "src"],
        ["--recursive", "TODO", "src"],
        ["-iw", "needle", "file.txt"],
    ],
)
def test_ordinary_grep_arguments_are_accepted(args):
    assert _grep_argument_error(args) is None


@pytest.mark.parametrize(
    "args,reason",
    [
        (["-R", "x", "."], "follows symlinks"),
        (["-nR", "x", "."], "follows symlinks"),
        (["-rnR", "x", "."], "follows symlinks"),
        (["-S", "x", "."], "follows symlinks"),
        (["--dereference-recursive", "x", "."], "follows symlinks"),
        (["--dereference-r", "x", "."], "follows symlinks"),  # getopt accepts prefixes
        (["--deref", "x", "."], "follows symlinks"),
        (["-r", "--", "x", "."], "end-of-options"),
        (["--", "-r"], "end-of-options"),
    ],
)
def test_grep_arguments_that_defeat_the_protection_are_refused(args, reason):
    assert reason in (_grep_argument_error(args) or "")


def test_protection_covers_every_protected_directory_and_env_file_in_any_case():
    protection = _grep_protection_arguments()
    assert "--exclude-dir=.[gG][iI][tT]" in protection
    assert "--exclude-dir=.[jJ][aA][rR][vV][iI][sS]" in protection
    assert "--exclude-dir=.[jJ][aA][rR][vV][iI][sS]_[bB][aA][cC][kK][uU][pP][sS]" in protection
    assert "--exclude=.[eE][nN][vV]" in protection
    assert "--exclude=.[eE][nN][vV].*" in protection


def test_protection_comes_first_and_last_so_a_caller_include_cannot_override_it():
    original = ["-rn", "--include=.env", "TODO", "."]
    hardened = _harden_grep_arguments(original)
    protection = _grep_protection_arguments()
    assert hardened[: len(protection)] == protection
    assert hardened[-len(protection):] == protection
    # Between the two copies: the caller's arguments, untouched and in order.
    assert hardened[len(protection): -len(protection) - 1] == original


# --- Through the tool (mocked runner) ------------------------------------------------------


@pytest.fixture
def runner():
    mocked = CommandRunner()
    mocked.run = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(0, "", "", 0.0, False, "grep", ".")
    )
    return mocked


@pytest.fixture
def workspaces(tmp_path):
    return [{"path": str(tmp_path)}]


async def test_tool_hardens_grep_before_running_it(runner, workspaces, unix_toolchain):
    result = await GenericCommandTool(runner).execute(
        binary="grep", args=["-rn", "TODO", "."], workspaces=workspaces
    )
    assert result.success, result.error
    sent = runner.run.call_args.args[1]
    assert sent[0].startswith("--exclude-dir=") and sent[-1].startswith("--exclude=")
    assert "--line-buffered" in sent


@pytest.mark.parametrize("args", [["-R", "x", "."], ["-r", "--", "x", "."]])
async def test_tool_refuses_unsafe_grep_without_running_it(
    runner, workspaces, unix_toolchain, args
):
    result = await GenericCommandTool(runner).execute(
        binary="grep", args=args, workspaces=workspaces
    )
    assert not result.success and "Security Error" in (result.error or "")
    runner.run.assert_not_called()


async def test_other_binaries_are_not_modified(runner, workspaces, unix_toolchain):
    await GenericCommandTool(runner).execute(
        binary="find", args=[".", "-name", "*.py"], workspaces=workspaces
    )
    assert runner.run.call_args.args[1] == [".", "-name", "*.py"]


# --- The real grep -------------------------------------------------------------------------


def _find_real_grep() -> str | None:
    """A real grep: on PATH, or (Windows) in Git for Windows' usr/bin next to git."""
    found = shutil.which("grep")
    if found:
        return found
    git = shutil.which("git")
    if sys.platform == "win32" and git:
        for parent in Path(git).resolve().parents:
            candidate = parent / "usr" / "bin" / "grep.exe"
            if candidate.is_file():
                return str(candidate)
    return None


REAL_GREP = _find_real_grep()
needs_grep = pytest.mark.skipif(REAL_GREP is None, reason="no grep installed on this machine")

CANARIES = {
    ".env": "canary-env",
    ".ENV.local": "canary-env-local",
    ".git/config": "canary-git",
    ".Jarvis/state.db": "canary-jarvis",
    ".jarvis_backups/old.txt": "canary-backup",
    "src/app.py": "canary-src",
    ".github/ci.yml": "canary-actions",  # starts with '.git' but is not protected
}
PROTECTED = {"canary-env", "canary-env-local", "canary-git", "canary-jarvis", "canary-backup"}


@pytest.fixture
def canary_workspace(tmp_path, monkeypatch):
    assert REAL_GREP is not None
    for relative, text in CANARIES.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n")
    monkeypatch.setenv("PATH", f"{Path(REAL_GREP).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return tmp_path


async def _run_grep(workspace: Path, args: list[str]):
    result = await GenericCommandTool(CommandRunner()).execute(
        binary="grep", args=args, workspaces=[{"path": str(workspace)}]
    )
    stdout = (result.data or {}).get("stdout", "") if result.data else ""
    return result, stdout


@needs_grep
@pytest.mark.parametrize(
    "args",
    [
        ["-rn", "canary", "."],
        ["-r", "canary", "."],
        ["--recursive", "canary", "."],
        ["-rn", "--include=*", "canary", "."],
        ["-rn", "--include=.e*", "canary", "."],  # opt back in with a glob, past the path check
        ["-rn", "--include=.*", "--include=config", "canary", "."],
    ],
)
async def test_recursive_grep_never_returns_protected_content(canary_workspace, args):
    result, stdout = await _run_grep(canary_workspace, args)
    leaked = sorted(word for word in PROTECTED if word in stdout)
    assert not leaked, f"protected content leaked through grep {args}: {leaked}"
    assert result.data is not None, result.error


@needs_grep
async def test_recursive_grep_still_searches_ordinary_files(canary_workspace):
    _, stdout = await _run_grep(canary_workspace, ["-rn", "canary", "."])
    assert "canary-src" in stdout
    assert "canary-actions" in stdout, "a similarly named, unprotected directory was skipped"


@needs_grep
async def test_grep_that_ends_with_an_option_expecting_a_value_cannot_unprotect(canary_workspace):
    # The trailing -e would swallow the first protective option as its pattern.
    _, stdout = await _run_grep(canary_workspace, ["-rn", "canary", ".", "-e"])
    assert not [word for word in PROTECTED if word in stdout]


@needs_grep
async def test_direct_access_to_a_protected_file_is_still_denied(canary_workspace):
    result, stdout = await _run_grep(canary_workspace, ["canary", ".env"])
    assert not result.success and "canary-env" not in stdout
    assert "Security Error" in (result.error or "")


async def test_naming_a_protected_file_in_an_include_option_is_refused(unix_toolchain, tmp_path):
    result = await GenericCommandTool(CommandRunner()).execute(
        binary="grep", args=["-rn", "--include=.ENV.local", "canary", "."],
        workspaces=[{"path": str(tmp_path)}],
    )
    assert not result.success and "protected" in (result.error or "")
