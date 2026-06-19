"""Core service for safe and asynchronous command execution."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jarvis.core.process_registry import ProcessRegistryService

LOG = logging.getLogger(__name__)


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
        
        # 1. Prepare Environment
        # We start with a clean copy and remove Jarvis-specific secrets
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
            
        forbidden_keys = {"JARVIS_API_TOKEN", "OPENROUTER_API_KEY", "OLLAMA_HOST"}
        for key in forbidden_keys:
            process_env.pop(key, None)

        # 2. Start Process
        # We use start_new_session=True (on Unix) or CREATE_NEW_PROCESS_GROUP (on Windows)
        # to ensure we can kill the entire process tree.
        # aiosqlite/asyncio handles OS differences mostly, but we use shell=False always.
        
        process = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=process_env,
            # Windows-specific: ensure we can terminate the process group
            creationflags=0x00000200 if os.name == "nt" else 0, 
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
            if self._process_registry and task_id:
                await self._process_registry.update_status(process_id, "timed_out")
            LOG.warning(f"Command '{cmd}' timed out after {timeout}s. Terminating process group.")
            self._terminate_process(process)
            # Try to grab whatever was in the pipes
            try:
                stdout_data, stderr_data = await asyncio.wait_for(process.communicate(), timeout=2.0)
            except Exception:
                pass
        except Exception as e:
            if self._process_registry and task_id:
                await self._process_registry.update_status(process_id, "terminated")
            LOG.exception(f"Unexpected error during command execution: {e}")
            self._terminate_process(process)
            raise
        finally:
            if self._process_registry and task_id:
                await self._process_registry.unregister_process(process_id)

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

    def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        """Safely terminate a process and its children."""
        try:
            if os.name == "nt":
                # Windows process group termination
                import subprocess
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
            else:
                # Unix process group termination
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception as e:
            LOG.debug(f"Failed to kill process group {process.pid}: {e}")
            # Fallback to single process kill
            try:
                process.kill()
            except Exception:
                pass
