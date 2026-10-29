"""Regression tests: command timeouts/cancellation must kill the child tree, never Jarvis."""

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jarvis.tasks import command_runner as cr
from jarvis.tasks.command_runner import CommandRunner

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
REPO_ROOT = Path(__file__).resolve().parents[1]


class RecordingRegistry:
    """Stands in for ProcessRegistryService and records lifecycle calls."""

    def __init__(self, fail_updates: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_updates = fail_updates

    async def register_process(self, id, pid, task_id, command_display, creation_time=None):
        self.calls.append(("register", id))

    async def update_status(self, id, status):
        self.calls.append(("update", id, status))
        if self.fail_updates:
            raise RuntimeError("registry unavailable")

    async def unregister_process(self, id):
        self.calls.append(("unregister", id))

    def statuses(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "update"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


async def _wait_for_file(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text()
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} was never written")


# Child that records its pid and its own grandchild's pid, then sleeps.
TREE_SCRIPT = textwrap.dedent(
    """
    import subprocess, sys, time
    out = sys.argv[1]
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(out, "w") as f:
        f.write(f"{__import__('os').getpid()} {grandchild.pid}")
    time.sleep(60)
    """
)


@posix_only
async def test_child_runs_in_its_own_process_group(tmp_path):
    result = await CommandRunner().run(
        sys.executable, ["-c", "import os; print(os.getpid(), os.getpgrp())"], cwd=tmp_path
    )
    child_pid, child_pgrp = map(int, result.stdout.split())
    assert child_pgrp == child_pid  # session/group leader
    assert child_pgrp != os.getpgrp()  # never the caller's group


@posix_only
def test_timeout_does_not_kill_the_calling_process(tmp_path):
    # Run the timeout inside a sacrificial process in its own session, so that a
    # regression can only kill that process, never this test runner.
    script = textwrap.dedent(
        f"""
        import asyncio, sys
        from pathlib import Path
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from jarvis.tasks.command_runner import CommandRunner
        async def main():
            res = await CommandRunner().run(
                sys.executable,
                ["-c", "import time; time.sleep(30)"],
                cwd=Path({str(tmp_path)!r}),
                timeout=1,
            )
            print("SURVIVED", res.timeout_occurred, res.exit_code, flush=True)
        asyncio.run(main())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"caller was killed (rc={proc.returncode}): {proc.stderr[-500:]}"
    assert "SURVIVED True" in proc.stdout


@posix_only
async def test_timeout_terminates_the_whole_child_tree(tmp_path):
    pid_file = tmp_path / "pids.txt"
    registry = RecordingRegistry()
    result = await CommandRunner(registry).run(
        sys.executable, ["-c", TREE_SCRIPT, str(pid_file)], cwd=tmp_path, timeout=2, task_id="t1"
    )

    assert result.timeout_occurred is True
    assert result.exit_code is not None and result.exit_code != 0
    child_pid, grandchild_pid = map(int, pid_file.read_text().split())
    assert _wait_dead(child_pid)
    assert _wait_dead(grandchild_pid), "grandchild survived the process-group kill"

    # Registry: registered -> timed_out -> unregistered (no record left claiming 'running').
    assert registry.calls[0][0] == "register"
    assert registry.statuses() == ["timed_out"]
    assert registry.calls[-1][0] == "unregister"


@posix_only
async def test_cancellation_terminates_child_and_propagates(tmp_path):
    pid_file = tmp_path / "pids.txt"
    registry = RecordingRegistry()
    task = asyncio.create_task(
        CommandRunner(registry).run(
            sys.executable,
            ["-c", TREE_SCRIPT, str(pid_file)],
            cwd=tmp_path,
            timeout=60,
            task_id="t1",
        )
    )
    child_pid, grandchild_pid = map(int, (await _wait_for_file(pid_file)).split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _wait_dead(child_pid)
    assert _wait_dead(grandchild_pid)
    assert registry.statuses() == ["terminated"]
    assert registry.calls[-1][0] == "unregister"


async def test_successful_command_registry_lifecycle(tmp_path):
    registry = RecordingRegistry()
    result = await CommandRunner(registry).run(
        sys.executable,
        ["-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        cwd=tmp_path,
        task_id="t1",
    )
    assert (result.exit_code, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")
    assert result.timeout_occurred is False
    assert registry.statuses() == ["completed"]
    assert [c[0] for c in registry.calls] == ["register", "update", "unregister"]


@posix_only
async def test_registry_failure_does_not_prevent_termination(tmp_path):
    pid_file = tmp_path / "pids.txt"
    registry = RecordingRegistry(fail_updates=True)
    result = await CommandRunner(registry).run(
        sys.executable, ["-c", TREE_SCRIPT, str(pid_file)], cwd=tmp_path, timeout=2, task_id="t1"
    )
    assert result.timeout_occurred is True
    child_pid, grandchild_pid = map(int, pid_file.read_text().split())
    assert _wait_dead(child_pid) and _wait_dead(grandchild_pid)
    assert registry.calls[-1][0] == "unregister"


async def test_terminate_is_safe_after_exit_and_when_repeated(tmp_path):
    runner = CommandRunner()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", **cr._spawn_isolation_kwargs()
    )
    await process.wait()
    # Already exited and reaped: repeated termination must not raise or signal anything else.
    runner._terminate_process(process)
    runner._terminate_process(process)


def test_posix_spawn_isolation_kwargs(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: False)
    assert cr._spawn_isolation_kwargs() == {"start_new_session": True}


def test_windows_spawn_isolation_kwargs(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: True)
    assert cr._spawn_isolation_kwargs() == {"creationflags": 0x00000200}


def test_windows_termination_uses_taskkill_tree(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: True)
    taskkill = MagicMock()
    monkeypatch.setattr(subprocess, "run", taskkill)
    killpg = MagicMock()
    monkeypatch.setattr(cr.os, "killpg", killpg, raising=False)

    process = MagicMock(pid=4321, returncode=None)
    CommandRunner()._terminate_process(process)

    taskkill.assert_called_once()
    assert taskkill.call_args[0][0] == ["taskkill", "/F", "/T", "/PID", "4321"]
    killpg.assert_not_called()
    process.kill.assert_called_once()


def test_windows_termination_skips_exited_process(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: True)
    taskkill = MagicMock()
    monkeypatch.setattr(subprocess, "run", taskkill)
    process = MagicMock(pid=4321, returncode=0)
    CommandRunner()._terminate_process(process)
    taskkill.assert_not_called()
    process.kill.assert_not_called()


@posix_only
def test_posix_termination_never_signals_own_process_group(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: False)
    killpg = MagicMock()
    monkeypatch.setattr(cr.os, "killpg", killpg)
    process = MagicMock(pid=os.getpgrp(), returncode=None)
    CommandRunner()._terminate_process(process)
    killpg.assert_not_called()


@posix_only
def test_posix_termination_targets_child_group(monkeypatch):
    monkeypatch.setattr(cr, "_is_windows", lambda: False)
    killpg = MagicMock()
    monkeypatch.setattr(cr.os, "killpg", killpg)
    process = MagicMock(pid=999_999, returncode=None)
    CommandRunner()._terminate_process(process)
    killpg.assert_called_once()
    assert killpg.call_args[0][0] == 999_999
