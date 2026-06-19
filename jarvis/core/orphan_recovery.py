"""Service for detecting and recovering orphaned processes."""

from __future__ import annotations

import logging
import os
import subprocess

from jarvis.core.process_registry import ProcessRegistryService

LOG = logging.getLogger(__name__)


class OrphanRecoveryService:
    """Scans and cleans up processes that survived a daemon crash."""

    def __init__(self, registry: ProcessRegistryService) -> None:
        self._registry = registry

    async def recover(self) -> list[str]:
        """Scan the registry and terminate surviving orphaned processes.
        
        Returns:
            list[str]: A list of task IDs that had orphaned processes terminated.
        """
        terminated_tasks: list[str] = []
        processes = await self._registry.list_processes()
        for p in processes:
            if p["status"] == "running":
                pid = p["pid"]
                creation_time = p.get("creation_time")
                if self._is_orphaned(pid, p["command_display"], creation_time):
                    LOG.info(f"Terminating orphaned process {pid} for task {p['task_id']}")
                    self._terminate(pid)
                    terminated_tasks.append(p["task_id"])
                
            # Clean up the record
            await self._registry.unregister_process(p["id"])
            
        return terminated_tasks

    def _is_orphaned(self, pid: int, expected_command: str, recorded_creation_time: float | None = None) -> bool:
        """Verify if the process is running and matches our expected command/creation time."""
        if recorded_creation_time is not None:
            from jarvis.core.process_utils import get_process_creation_time
            actual_creation_time = get_process_creation_time(pid)
            if actual_creation_time is None:
                return False # Process doesn't exist
            
            # If the creation times differ by more than 5 seconds, it's a reused PID
            if abs(actual_creation_time - recorded_creation_time) > 5.0:
                LOG.info(f"PID {pid} was reused (time delta {abs(actual_creation_time - recorded_creation_time):.1f}s). Skipping.")
                return False

        if os.name == "nt":
            return self._is_orphaned_windows(pid, expected_command)
        else:
            return self._is_orphaned_unix(pid, expected_command)

    def _is_orphaned_windows(self, pid: int, expected_command: str) -> bool:
        try:
            cmd = f'powershell -Command "Get-CimInstance Win32_Process -Filter \\"ProcessId = {pid}\\" | Select-Object -ExpandProperty CommandLine"'
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0 or not result.stdout.strip():
                return False
                
            actual_command = result.stdout.strip().lower()
            expected_parts = expected_command.lower().split()
            if not expected_parts:
                return False
            
            if expected_parts[0] in actual_command:
                return True
            return False
        except Exception as e:
            LOG.warning(f"Failed to verify Windows process {pid}: {e}")
            return False

    def _is_orphaned_unix(self, pid: int, expected_command: str) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
            
        try:
            cmdline_path = f"/proc/{pid}/cmdline"
            if os.path.exists(cmdline_path):
                with open(cmdline_path, "r") as f:
                    actual_command = f.read().replace('\0', ' ').strip().lower()
                    expected_parts = expected_command.lower().split()
                    if expected_parts and expected_parts[0] in actual_command:
                        return True
            else:
                result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
                actual_command = result.stdout.strip().lower()
                expected_parts = expected_command.lower().split()
                if expected_parts and expected_parts[0] in actual_command:
                    return True
            return False
        except Exception as e:
            LOG.warning(f"Failed to verify Unix process {pid}: {e}")
            return False

    def _terminate(self, pid: int) -> None:
        """Terminate the orphaned process and its process group."""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                import signal
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    os.kill(pid, signal.SIGKILL)
        except Exception as e:
            LOG.warning(f"Failed to terminate process {pid}: {e}")
