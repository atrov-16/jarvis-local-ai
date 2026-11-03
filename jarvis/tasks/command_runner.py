"""Core service for safe and asynchronous command execution."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.core.process_registry import ProcessRegistryService

LOG = logging.getLogger(__name__)

# Windows CREATE_NEW_PROCESS_GROUP, so taskkill /T can target the child's tree.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
# Windows CREATE_SUSPENDED: the child joins its job object before it runs any code.
_CREATE_SUSPENDED = 0x00000004


def _is_windows() -> bool:
    return os.name == "nt"


def _spawn_isolation_kwargs() -> dict[str, object]:
    """Options that place the child in its own process group.

    The child must never share the daemon's process group: termination signals the
    whole group, and a shared group would take the daemon (or test runner) with it.
    On Windows the child is also created suspended, so that `_contain` can place it in a
    job object before it can start descendants of its own.
    """
    if _is_windows():
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED}
    # setsid() in the child: it becomes leader of a new session and process group
    # whose id equals its pid.
    return {"start_new_session": True}


# Upper bound for any caller-requested command timeout (seconds).
MAX_COMMAND_TIMEOUT = 3600

# Environment variables a child process may inherit. Everything else (provider API keys,
# GITHUB_TOKEN, cloud credentials, npm auth tokens, NODE_OPTIONS ...) is withheld.
_INHERITED_ENV_NAMES = frozenset({
    # Process basics
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LANGUAGE", "TZ",
    "TMPDIR", "TMP", "TEMP", "PWD", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "CI",
    # Windows
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
    "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER", "OS", "ALLUSERSPROFILE", "PUBLIC",
    # Toolchain locations (paths only, never credentials)
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYENV_ROOT", "CARGO_HOME", "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN", "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE", "GOFLAGS",
    "JAVA_HOME", "NVM_DIR", "NVM_BIN", "VOLTA_HOME", "PNPM_HOME", "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
})
_INHERITED_ENV_PREFIXES = ("LC_",)


def _child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a child: allowlisted host variables plus caller-supplied ones."""
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _INHERITED_ENV_NAMES or name.upper().startswith(_INHERITED_ENV_PREFIXES)
    }
    if extra:
        env.update(extra)
    # Never pass Jarvis's own secrets, even if a caller supplied them explicitly.
    for key in ("JARVIS_API_TOKEN", "OPENROUTER_API_KEY", "OLLAMA_HOST"):
        env.pop(key, None)
    return env


# Characters cmd.exe interprets even inside quotes when it re-parses a batch file's
# arguments. Rejected outright: batch-file argument escaping cannot be made safe.
_BATCH_UNSAFE_CHARS = frozenset('"&|<>^%!\r\n')

# Programs CreateProcess starts directly (.exe, .com) or through cmd.exe (.bat, .cmd).
_WINDOWS_PROGRAM_SUFFIXES = (".exe", ".com", ".bat", ".cmd")
_WINDOWS_BATCH_SUFFIXES = (".bat", ".cmd")
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


class ExecutableNotFoundError(FileNotFoundError):
    """The program is not installed, or not on the PATH Jarvis passes to commands."""


def find_executable(name: str, search_path: str | None) -> str | None:
    """Locate program `name` on `search_path` (a PATH value), never in the working directory.

    Only absolute PATH entries are searched. An empty or relative entry, and on Windows the
    implicit current-directory lookup of CreateProcess and `shutil.which`, would make the
    result depend on Jarvis's working directory, where a workspace could plant a program of
    the same name. On Windows only .exe, .com, .bat and .cmd files qualify, tried in
    PATHEXT order within each directory.
    """
    if not search_path:
        return None
    directories = []
    for entry in search_path.split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and os.path.isabs(entry):
            directories.append(entry)

    if not _is_windows():
        for directory in directories:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    suffix = os.path.splitext(name)[1].lower()
    if suffix:
        names = [name] if suffix in _WINDOWS_PROGRAM_SUFFIXES else []
    else:
        pathext = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
        extensions = [e.lower() for e in pathext.split(";") if e.strip()]
        names = [name + e for e in extensions if e in _WINDOWS_PROGRAM_SUFFIXES]
    for directory in directories:
        for candidate_name in names:
            candidate = os.path.join(directory, candidate_name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _resolve_executable(cmd: str, args: list[str], search_path: str | None = None) -> str:
    """Resolve the program to launch to an absolute path.

    A bare name is looked up on the child's PATH (see `find_executable`); a missing program
    raises ExecutableNotFoundError with a clear message instead of a bare OS error. Callers
    that pass a path (a virtualenv interpreter, sys.executable) must pass an absolute one.

    On Windows, CreateProcess only appends `.exe`, so `npm`, `yarn` and `pnpm` (shipped
    as `.cmd` shims) are only found through PATHEXT. A batch target is run by cmd.exe,
    which re-parses its command line, so arguments with shell metacharacters are refused
    rather than escaped.
    """
    if os.path.dirname(cmd):
        if not os.path.isabs(cmd):
            raise ValueError(f"Program path {cmd!r} must be absolute.")
        program = cmd
    else:
        if search_path is None:
            search_path = os.environ.get("PATH")
        found = find_executable(cmd, search_path)
        if found is None:
            raise ExecutableNotFoundError(
                f"Program '{cmd}' was not found on PATH; it may not be installed."
            )
        program = found

    if _is_windows():
        suffix = Path(program).suffix.lower()
        if suffix not in _WINDOWS_PROGRAM_SUFFIXES:
            raise ValueError(f"'{program}' is not a Windows program (.exe, .com, .bat or .cmd).")
        if suffix in _WINDOWS_BATCH_SUFFIXES:
            for value in (program, *args):
                if any(ch in _BATCH_UNSAFE_CHARS for ch in value):
                    raise ValueError(
                        f"Argument {value!r} contains characters that are unsafe for batch "
                        f"program {Path(program).name}."
                    )
    return program


@dataclass(frozen=True)
class CommandResult:
    """Structured output from a command execution."""
    exit_code: int | None
    stdout: str
    stderr: str
    execution_time: float
    timeout_occurred: bool
    command: str
    working_dir: str

    def to_summary(self, max_chars: int = 2000) -> str:
        """Provide a structured summary of the command result."""
        status = "Success" if self.exit_code == 0 else "Failed"
        if self.timeout_occurred:
            status = "Timed Out"
            
        summary = [
            f"Command: {self.command}",
            f"Status: {status} (Exit Code: {self.exit_code})",
            f"Working Dir: {self.working_dir}",
            f"Duration: {self.execution_time:.2f}s",
            "\n--- Standard Output ---",
            self.stdout[-max_chars:] if len(self.stdout) > max_chars else self.stdout,
            "\n--- Standard Error ---",
            self.stderr[-max_chars:] if len(self.stderr) > max_chars else self.stderr,
        ]
        return "\n".join(summary)


class CommandRunner:
    """Handles the lifecycle of asynchronous subprocesses with safety guards."""

    def __init__(self, process_registry: ProcessRegistryService | None = None) -> None:
        self._process_registry = process_registry

    async def run(
        self,
        cmd: str,
        args: list[str],
        cwd: Path,
        timeout: int = 60,
        env: dict[str, str] | None = None,
        task_id: str | None = None,
    ) -> CommandResult:
        """
        Execute a command asynchronously.
        
        Args:
            cmd: The binary to execute (e.g., 'git', 'pytest').
            args: List of command arguments.
            cwd: Working directory (must be validated before calling).
            timeout: Execution timeout in seconds.
            env: Optional environment variables to add.
            task_id: Optional ID of the task spawning this process.
            
        Returns:
            CommandResult containing outputs and metadata.
        """
        start_time = time.perf_counter()
        
        # 1. Prepare Environment: allowlisted variables only (no inherited secrets)
        process_env = _child_environment(env)
        program = _resolve_executable(cmd, args, process_env.get("PATH"))

        # 2. Start Process
        # The child gets its own session/process group (POSIX) or job object (Windows), so
        # the entire child tree can be terminated without touching Jarvis itself.
        # shell=False always.
        process, job = await self._start(program, args, cwd, process_env)

        process_id = str(uuid.uuid4())
        command_display = f"{cmd} {' '.join(args)}"
        
        if self._process_registry and task_id:
            from jarvis.core.process_utils import get_process_creation_time

            try:
                # The creation time lets orphan recovery tell this process apart from an
                # unrelated one that reuses the PID after a crash.
                await self._process_registry.register_process(
                    id=process_id,
                    pid=process.pid,
                    task_id=task_id,
                    command_display=command_display,
                    creation_time=get_process_creation_time(process.pid),
                )
            except BaseException:
                # Never leave an unregistered child running.
                self._terminate_process(process, job)
                await self._reap(process)
                self._release(process, job)
                raise

        timeout_occurred = False
        stdout_data = b""
        stderr_data = b""

        try:
            # 3. Wait for Completion
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), 
                timeout=timeout
            )
            stdout_data = stdout_bytes
            stderr_data = stderr_bytes  # Fix for BUG: stderr_bytes = stderr_bytes
            
            if self._process_registry and task_id:
                await self._process_registry.update_status(process_id, "completed")
                
        except TimeoutError:
            timeout_occurred = True
            LOG.warning(f"Command '{cmd}' timed out after {timeout}s. Terminating process group.")
            # Terminate first: a registry failure must never leave the child running.
            self._terminate_process(process, job)
            await self._safe_update_status(process_id, task_id, "timed_out")
            # Try to grab whatever was in the pipes
            try:
                stdout_data, stderr_data = await asyncio.wait_for(
                    process.communicate(), timeout=2.0
                )
            except Exception:
                pass
        except asyncio.CancelledError:
            LOG.warning(f"Command '{cmd}' cancelled. Terminating process group.")
            self._terminate_process(process, job)
            await self._reap(process)
            await self._safe_update_status(process_id, task_id, "terminated")
            raise
        except Exception as e:
            LOG.exception(f"Unexpected error during command execution: {e}")
            self._terminate_process(process, job)
            await self._reap(process)
            await self._safe_update_status(process_id, task_id, "terminated")
            raise
        finally:
            # Descendants the command left behind never outlive it.
            self._release(process, job)
            if self._process_registry and task_id:
                try:
                    await self._process_registry.unregister_process(process_id)
                except Exception as e:
                    LOG.error(f"Failed to unregister process {process_id}: {e}")

        execution_time = time.perf_counter() - start_time
        
        return CommandResult(
            exit_code=process.returncode,
            stdout=stdout_data.decode("utf-8", errors="replace"),
            stderr=stderr_data.decode("utf-8", errors="replace"),
            execution_time=execution_time,
            timeout_occurred=timeout_occurred,
            command=f"{cmd} {' '.join(args)}",
            working_dir=str(cwd),
        )

    async def _safe_update_status(self, process_id: str, task_id: str | None, status: str) -> None:
        if not (self._process_registry and task_id):
            return
        try:
            await self._process_registry.update_status(process_id, status)
        except Exception as e:
            LOG.error(f"Failed to record process {process_id} as {status}: {e}")

    @staticmethod
    async def _reap(process: asyncio.subprocess.Process) -> None:
        """Wait briefly for a killed child so it does not linger as a zombie."""
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except Exception:
            pass

    @staticmethod
    async def _start(
        program: str, args: list[str], cwd: Path, env: dict[str, str]
    ) -> tuple[asyncio.subprocess.Process, Any]:
        """Start the child isolated from Jarvis: its own process group, and on Windows
        its own kill-on-close job object (returned; None elsewhere)."""
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            **_spawn_isolation_kwargs(),  # type: ignore[arg-type]
        )
        job = _contain(process) if _is_windows() else None
        return process, job

    def _release(self, process: asyncio.subprocess.Process, job: Any) -> None:
        """After the command ended, terminate descendants still in its group or job."""
        if job is not None:
            job.close()  # Kill-on-close: anything still in the job ends with it.
            return
        if _is_windows() or sys.platform == "win32":
            return
        pgid = process.pid
        if process.returncode is None or pgid == os.getpgrp():
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # The group is already empty.

    def _terminate_process(self, process: asyncio.subprocess.Process, job: Any = None) -> None:
        """Terminate the child and its descendants, never Jarvis's own process group.

        Safe to call repeatedly and after the child has already exited.
        """
        if _is_windows():
            if job is not None and not job.closed:
                # The job holds the whole tree, including descendants whose parent exited.
                try:
                    job.terminate()
                except OSError as e:
                    LOG.debug(f"Failed to terminate job of process {process.pid}: {e}")
            elif process.returncode is None:
                import subprocess

                from jarvis.core.process_utils import system32_program

                subprocess.run(
                    [system32_program("taskkill.exe"), "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                )
        elif sys.platform != "win32":
            # The child was started with start_new_session=True, so its process group id
            # equals its pid. Signal that group directly instead of looking it up: if the
            # child has exited, getpgid() could fail or, worse, describe a different process.
            pgid = process.pid
            if pgid == os.getpgrp():
                LOG.error(f"Refusing to kill process group {pgid}: it is Jarvis's own group.")
            else:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Whole group already gone.
                except Exception as e:
                    LOG.debug(f"Failed to kill process group {pgid}: {e}")

        # Fallback / belt-and-braces for the direct child.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except Exception as e:
                LOG.debug(f"Failed to kill process {process.pid}: {e}")


def _contain(process: asyncio.subprocess.Process) -> Any:
    """Windows: place a just-created, suspended child in a kill-on-close job, then resume it.

    Descendants inherit the job, so terminating the job ends the whole tree, and if Jarvis
    dies Windows closes the job and kills the tree with it. If no job can be created the
    child still runs; taskkill /T remains the fallback. A child that cannot be resumed is
    killed: it would otherwise hang until its timeout.
    """
    from jarvis.core import _win32

    job: Any = None
    try:
        job = _win32.JobObject()
        job.assign(process.pid)
    except OSError as e:
        LOG.warning(f"Could not place process {process.pid} in a job object: {e}")
        if job is not None:
            job.close()
        job = None
    try:
        _win32.resume_process(process.pid)
    except Exception:
        if job is not None:
            job.close()
        else:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        raise
    return job
