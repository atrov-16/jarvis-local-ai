"""Process management against real operating-system processes (no mocked OS calls).

Every test starts genuine child processes. Platform-specific tests are skipped only on the
platform that lacks the mechanism under test (POSIX process groups, Windows job objects).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from jarvis.core import process_utils as pu
from jarvis.core.orphan_recovery import OrphanRecoveryService
from jarvis.tasks import command_runner as cr
from jarvis.tasks.command_runner import CommandRunner

WINDOWS = sys.platform == "win32"
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX sessions and process groups")
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows job objects and kernel32")

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]

# A child that starts a grandchild, records both PIDs, optionally exits first, then sleeps.
TREE = textwrap.dedent(
    """
    import os, subprocess, sys, time
    out, orphan_grandchild = sys.argv[1], sys.argv[2] == "orphan"
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(out, "w") as f:
        f.write(f"{os.getpid()} {grandchild.pid}")
    if orphan_grandchild:
        sys.exit(0)  # the parent exits; the grandchild keeps running
    time.sleep(60)
    """
)


def _spawn() -> subprocess.Popen[bytes]:
    flags: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        if WINDOWS
        else {"start_new_session": True}
    )
    return subprocess.Popen(SLEEPER, **flags)  # type: ignore[call-overload]


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


async def _wait_for_file(path: Path, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} was never written")


@pytest.fixture
def child():
    process = _spawn()
    try:
        yield process
    finally:
        process.kill()
        process.wait()


# --- Creation time and PID reuse protection ----------------------------------------------


def test_creation_time_of_a_real_child_matches_its_start(child):
    started = time.time()
    created = pu.get_process_creation_time(child.pid)
    assert created is not None
    # Created before we looked, after the fixture started it (generous bounds for CI).
    assert started - 30 < created <= started + 1


def test_creation_time_is_stable_and_distinguishes_processes(child):
    first = pu.get_process_creation_time(child.pid)
    time.sleep(0.2)
    assert pu.get_process_creation_time(child.pid) == first
    # The test process itself started earlier than the child.
    own = pu.get_process_creation_time(os.getpid())
    assert own is not None and first is not None and own < first


def test_creation_time_works_for_the_current_process():
    assert pu.get_process_creation_time(os.getpid()) is not None
    assert pu.is_process_running(os.getpid())


def test_creation_time_of_missing_or_invalid_pids_is_none():
    for pid in (0, -1, 2**22 + 12345):
        assert pu.get_process_creation_time(pid) is None
        assert pu.is_process_running(pid) is False


def test_exited_process_is_not_reported_as_running(child):
    assert pu.is_process_running(child.pid)
    child.kill()
    child.wait()
    assert pu.get_process_creation_time(child.pid) is None
    assert pu.is_process_running(child.pid) is False


@posix_only
def test_zombie_is_not_reported_as_running(child):
    # Killed but not yet reaped: the PID still exists in the table as a zombie.
    child.kill()
    assert _wait_for(lambda: not pu.is_process_running(child.pid))
    assert pu.get_process_creation_time(child.pid) is None
    child.wait()


def test_is_same_process_rejects_a_different_creation_time(child):
    created = pu.get_process_creation_time(child.pid)
    assert created is not None
    assert pu.is_same_process(child.pid, created)
    # A PID reused by another process has another creation time, even if only slightly.
    assert not pu.is_same_process(child.pid, created - 1.0)
    assert not pu.is_same_process(child.pid, created + 1.0)
    assert not pu.is_same_process(child.pid, created - 0.5)


# --- Orphan recovery with real processes ---------------------------------------------------


class _Registry:
    def __init__(self, records):
        self.records = records
        self.unregistered = []

    async def list_processes(self):
        return self.records

    async def unregister_process(self, id):
        self.unregistered.append(id)


def _record(pid, creation_time, status="running"):
    return {
        "id": f"r{pid}", "status": status, "pid": pid, "task_id": f"t{pid}",
        "command_display": "python -c sleep", "creation_time": creation_time,
    }


async def test_recovery_terminates_a_real_orphan_it_can_identify(child):
    registry = _Registry([_record(child.pid, pu.get_process_creation_time(child.pid))])
    terminated = await OrphanRecoveryService(registry).recover()
    assert terminated == [f"t{child.pid}"]
    assert child.wait(timeout=10) is not None  # it died
    assert registry.unregistered == [f"r{child.pid}"]


async def test_recovery_never_terminates_a_process_that_reused_the_pid(child):
    # The recorded creation time belongs to some earlier process that had this PID.
    registry = _Registry([_record(child.pid, pu.get_process_creation_time(child.pid) - 3600)])
    assert await OrphanRecoveryService(registry).recover() == []
    assert child.poll() is None, "an unrelated process with a reused PID was killed"
    assert registry.unregistered == [f"r{child.pid}"]  # the stale record is still cleaned up


async def test_recovery_never_terminates_a_record_without_creation_time(child):
    registry = _Registry([_record(child.pid, None)])
    assert await OrphanRecoveryService(registry).recover() == []
    assert child.poll() is None


async def test_recovery_ignores_records_whose_process_is_gone(child):
    created = pu.get_process_creation_time(child.pid)
    child.kill()
    child.wait()
    registry = _Registry([_record(child.pid, created)])
    assert await OrphanRecoveryService(registry).recover() == []
    assert registry.unregistered == [f"r{child.pid}"]


async def test_recovery_terminates_the_orphan_descendants_too(tmp_path):
    pid_file = tmp_path / "pids"
    flags: dict[str, object] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        if WINDOWS
        else {"start_new_session": True}
    )
    child = subprocess.Popen(
        [sys.executable, "-c", TREE, str(pid_file), "stay"], **flags  # type: ignore[call-overload]
    )
    try:
        _, grandchild = map(int, (await _wait_for_file(pid_file)).split())
        registry = _Registry([_record(child.pid, pu.get_process_creation_time(child.pid))])
        assert await OrphanRecoveryService(registry).recover()
        child.wait(timeout=10)
        assert _wait_for(lambda: not pu.is_process_running(grandchild)), "grandchild survived"
    finally:
        child.kill()
        child.wait()


@posix_only
def test_terminate_process_tree_never_signals_a_shared_group():
    # A child in *our* process group is not a session leader, so only that PID is signalled.
    sibling = subprocess.Popen(SLEEPER)
    try:
        assert os.getpgid(sibling.pid) == os.getpgrp()
        pu.terminate_process_tree(sibling.pid)
        sibling.wait(timeout=10)
        assert os.getpgrp() > 0  # still here: the test runner's group was not signalled
    finally:
        sibling.kill()
        sibling.wait()


# --- Command execution: timeout, cancellation, descendants ----------------------------------


async def test_timeout_kills_child_and_grandchild(tmp_path):
    pid_file = tmp_path / "pids"
    result = await CommandRunner().run(
        sys.executable, ["-c", TREE, str(pid_file), "stay"], cwd=tmp_path, timeout=3
    )
    assert result.timeout_occurred
    child, grandchild = map(int, pid_file.read_text().split())
    assert _wait_for(lambda: not pu.is_process_running(child))
    assert _wait_for(lambda: not pu.is_process_running(grandchild)), "grandchild survived timeout"


async def test_cancellation_kills_child_and_grandchild(tmp_path):
    pid_file = tmp_path / "pids"
    task = asyncio.create_task(
        CommandRunner().run(
            sys.executable, ["-c", TREE, str(pid_file), "stay"], cwd=tmp_path, timeout=60
        )
    )
    child, grandchild = map(int, (await _wait_for_file(pid_file)).split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _wait_for(lambda: not pu.is_process_running(child))
    assert _wait_for(lambda: not pu.is_process_running(grandchild)), "grandchild survived cancel"


async def test_grandchild_left_behind_by_a_finished_command_is_cleaned_up(tmp_path):
    """The command exits normally but leaves a descendant: it must not outlive the command.

    The grandchild inherits the stdout pipe, so the runner waits for it; the timeout ends that,
    and the descendant is killed with the tree.
    """
    pid_file = tmp_path / "pids"
    result = await CommandRunner().run(
        sys.executable, ["-c", TREE, str(pid_file), "orphan"], cwd=tmp_path, timeout=3
    )
    _, grandchild = map(int, pid_file.read_text().split())
    assert result.timeout_occurred or result.exit_code == 0
    assert _wait_for(lambda: not pu.is_process_running(grandchild)), "descendant leaked"


@posix_only
async def test_posix_child_leads_its_own_session_and_group(tmp_path):
    result = await CommandRunner().run(
        sys.executable,
        ["-c", "import os; print(os.getpid(), os.getpgrp(), os.getsid(0))"],
        cwd=tmp_path,
    )
    pid, pgrp, sid = map(int, result.stdout.split())
    assert pid == pgrp == sid
    assert pgrp != os.getpgrp()


@windows_only
async def test_windows_child_is_in_a_job_and_runs_normally(tmp_path):
    result = await CommandRunner().run(
        sys.executable, ["-c", "import sys; print('ok'); sys.exit(7)"], cwd=tmp_path
    )
    assert (result.stdout.strip(), result.exit_code) == ("ok", 7)


@windows_only
def test_job_object_kills_a_process_that_escaped_its_parent():
    """The case taskkill /T cannot handle: the intermediate parent has already exited."""
    from jarvis.core import _win32

    job = _win32.JobObject()
    try:
        launcher = subprocess.Popen(
            [
                sys.executable, "-c",
                "import subprocess, sys; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "print(p.pid, flush=True)",
            ],
            stdout=subprocess.PIPE,
            creationflags=0x00000004,  # CREATE_SUSPENDED
        )
        job.assign(launcher.pid)
        _win32.resume_process(launcher.pid)
        grandchild = int(launcher.stdout.readline())
        launcher.wait(timeout=10)  # the parent is gone; the grandchild remains
        assert pu.is_process_running(grandchild)
        assert job.active_processes() >= 1
        job.terminate()
        assert _wait_for(lambda: not pu.is_process_running(grandchild)), "job did not kill it"
    finally:
        job.close()


@windows_only
def test_closing_the_job_kills_its_processes():
    from jarvis.core import _win32

    job = _win32.JobObject()
    process = subprocess.Popen(SLEEPER, creationflags=0x00000004)
    job.assign(process.pid)
    _win32.resume_process(process.pid)
    job.close()  # kill-on-close: what happens if Jarvis itself dies
    assert process.wait(timeout=10) is not None


# --- Executable resolution against real files -------------------------------------------------


async def test_missing_program_gives_a_clear_error(tmp_path):
    with pytest.raises(cr.ExecutableNotFoundError, match="not found on PATH"):
        await CommandRunner().run("definitely-not-installed-xyz", [], cwd=tmp_path)


def test_working_directory_is_never_searched_for_programs(tmp_path, monkeypatch):
    name = "jarvis-planted.exe" if WINDOWS else "jarvis-planted"
    planted = tmp_path / name
    planted.write_text("")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert cr.find_executable("jarvis-planted", os.pathsep.join([".", ""])) is None
    assert cr.find_executable("jarvis-planted", str(tmp_path)) is not None  # only if on PATH


def test_relative_path_entries_are_ignored(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    name = "tool.exe" if WINDOWS else "tool"
    (tmp_path / "bin" / name).write_text("")
    (tmp_path / "bin" / name).chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert cr.find_executable("tool", "bin") is None


@windows_only
async def test_cmd_shim_runs_with_safe_arguments_and_rejects_unsafe_ones(tmp_path):
    shim = tmp_path / "shim.cmd"
    shim.write_text("@echo off\r\necho got:%1\r\n")
    env = {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    result = await CommandRunner().run("shim", ["hello"], cwd=tmp_path, env=env)
    assert "got:hello" in result.stdout
    for bad in ("a&calc", "a|b", "%PATH%", "x^y", "!x!"):
        with pytest.raises(ValueError, match="unsafe for batch"):
            await CommandRunner().run("shim", [bad], cwd=tmp_path, env=env)
