"""Core service for safe and asynchronous command execution."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.core.process_registry import ProcessRegistryService

LOG = logging.getLogger(__name__)

# Windows CREATE_NEW_PROCESS_GROUP, so taskkill /T can target the child's tree.
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _is_windows() -> bool:
    return os.name == "nt"


def _spawn_isolation_kwargs() -> dict[str, object]:
    """Options that place the child in its own process group.

    The child must never share the daemon's process group: termination signals the
    whole group, and a shared group would take the daemon (or test runner) with it.
    """
    if _is_windows():
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}
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


def _resolve_executable(cmd: str, args: list[str]) -> str:
    """Resolve the program to launch.

    On Windows, CreateProcess only appends `.exe`, so `npm`, `yarn` and `pnpm` (shipped
    as `.cmd` shims) are not found by name. Resolve through PATH/PATHEXT instead. A
    batch target is run by cmd.exe, which re-parses its arguments, so arguments with
    shell metacharacters are refused rather than escaped.
    """
    if not _is_windows():
        return cmd
    resolved = shutil.which(cmd)
    if resolved is None:
        return cmd
    if Path(resolved).suffix.lower() in (".cmd", ".bat"):
        for arg in args:
            if any(ch in _BATCH_UNSAFE_CHARS for ch in arg):
                raise ValueError(
                    f"Argument {arg!r} contains characters that are unsafe for batch "
                    f"program {Path(resolved).name}."
                )
    return resolved


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
        program = _resolve_executable(cmd, args)

        # 2. Start Process
        # The child gets its own session/process group (POSIX) or process group (Windows)
        # so the entire child tree can be terminated without touching Jarvis itself.
        # shell=False always.
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=process_env,
            **_spawn_isolation_kwargs(),  # type: ignore[arg-type]
        )

        process_id = str(uuid.uuid4())
        command_display = f"{cmd} {' '.join(args)}"
        
        if self._process_registry and task_id:
            from jarvis.core.process_utils import get_process_creation_time
            creation_time = get_process_creation_time(process.pid)
            
            await self._process_registry.register_process(
                id=process_id,
                pid=process.pid,
                task_id=task_id,
                command_display=command_display,
                creation_time=creation_time,
            )

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
            self._terminate_process(process)
            await self._safe_update_status(process_id, task_id, "timed_out")
            # Try to grab whatever was in the pipes
            try:
                stdout_data, stderr_data = await asyncio.wait_for(process.communicate(), timeout=2.0)
            except Exception:
                pass
        except asyncio.CancelledError:
            LOG.warning(f"Command '{cmd}' cancelled. Terminating process group.")
            self._terminate_process(process)
            await self._reap(process)
            await self._safe_update_status(process_id, task_id, "terminated")
            raise
        except Exception as e:
            LOG.exception(f"Unexpected error during command execution: {e}")
            self._terminate_process(process)
            await self._reap(process)
            await self._safe_update_status(process_id, task_id, "terminated")
            raise
        finally:
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

    def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        """Terminate the child and its descendants, never Jarvis's own process group.

        Safe to call repeatedly and after the child has already exited.
        """
        if _is_windows():
            if process.returncode is None:
                import subprocess
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True
                )
        else:
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
