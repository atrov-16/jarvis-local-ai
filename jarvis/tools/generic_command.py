"""Generic command execution tool for developer workflows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Type

from pydantic import BaseModel, Field

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllowedBinary:
    """Metadata for a binary allowed to be run via GenericCommandTool."""
    name: str
    has_network_access: bool
    default_timeout: int = 60


# Strict allowlist of binaries for developer workflows
ALLOWED_BINARIES: dict[str, AllowedBinary] = {
    "node": AllowedBinary("node", has_network_access=False),
    "npm": AllowedBinary("npm", has_network_access=True, default_timeout=300),
    "yarn": AllowedBinary("yarn", has_network_access=True, default_timeout=300),
    "pnpm": AllowedBinary("pnpm", has_network_access=True, default_timeout=300),
    "cargo": AllowedBinary("cargo", has_network_access=True, default_timeout=300),
    "go": AllowedBinary("go", has_network_access=True, default_timeout=300),
    "make": AllowedBinary("make", has_network_access=False, default_timeout=300),
    "gcc": AllowedBinary("gcc", has_network_access=False, default_timeout=120),
    "g++": AllowedBinary("g++", has_network_access=False, default_timeout=120),
    "cmake": AllowedBinary("cmake", has_network_access=False, default_timeout=120),
    "grep": AllowedBinary("grep", has_network_access=False),
    "find": AllowedBinary("find", has_network_access=False),
    "ls": AllowedBinary("ls", has_network_access=False),
}


class GenericCommandInput(BaseModel):
    binary: str = Field(..., description="The binary to execute (must be on the allowlist).")
    args: list[str] = Field(default_factory=list, description="List of arguments for the command.")
    timeout: int | None = Field(None, description="Optional custom timeout in seconds.")


class GenericCommandTool(BaseTool):
    """Tool for running allowed system binaries within a workspace."""

    def __init__(self, runner: CommandRunner) -> None:
        super().__init__(
            name="command",
            description="Executes allowed system binaries for developer tasks.",
            category=ToolCategory.SYSTEM, # Always critical
            timeout_seconds=60,
        )
        self._runner = runner

    def get_input_schema(self) -> Type[BaseModel]:
        return GenericCommandInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            binary_name = kwargs["binary"]
            args = kwargs.get("args", [])
            
            # 1. Binary Allowlist Check
            if binary_name not in ALLOWED_BINARIES:
                return ToolResult(
                    success=False, 
                    error=f"Security Error: Binary '{binary_name}' is not on the allowlist. "
                          f"Allowed: {', '.join(sorted(ALLOWED_BINARIES.keys()))}"
                )
            
            allowed = ALLOWED_BINARIES[binary_name]
            
            # 2. Workspace Confinement
            workspaces = kwargs.get("workspaces", [])
            from jarvis.tools.filesystem import _get_validated_path
            cwd = _get_validated_path(".", workspaces)
            
            # 3. Path Argument Validation
            # Any argument that looks like an absolute path or relative upward path
            # must be within the workspace.
            for i, arg in enumerate(args):
                if arg.startswith("/") or arg.startswith("C:\\") or arg.startswith("../") or "..\\" in arg:
                    try:
                        # Attempt to validate if it looks like a path
                        _get_validated_path(arg, workspaces)
                    except (PermissionError, ValueError) as e:
                        return ToolResult(success=False, error=f"Security Error: Argument {i+1} ('{arg}') escapes workspace: {e}")

            # 4. Determine Timeout
            timeout = kwargs.get("timeout") or allowed.default_timeout

            # 5. Run Command
            task_id = kwargs.get("task_id")
            res = await self._runner.run(
                binary_name, 
                args, 
                cwd=cwd, 
                timeout=timeout,
                task_id=task_id
            )
            
            # 6. Final Result
            result_data = {
                "command": res.command,
                "exit_code": res.exit_code,
                "has_network_access": allowed.has_network_access,
                "timeout_occurred": res.timeout_occurred,
                "summary": res.to_summary(max_chars=1000),
                "stdout": res.stdout,
                "stderr": res.stderr,
            }
            
            return ToolResult(
                success=res.exit_code == 0 and not res.timeout_occurred,
                data=result_data,
                execution_time=res.execution_time,
                timeout_occurred=res.timeout_occurred
            )

        except Exception as e:
            LOG.exception(f"Generic command execution failed: {e}")
            return ToolResult(success=False, error=str(e))
