"""Generic command execution tool for developer workflows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from jarvis.tasks.command_runner import MAX_COMMAND_TIMEOUT, CommandRunner
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tools.path_policy import authorize_path, resolve_in_workspace

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


# Arguments that turn an allowlisted developer tool into a general code-execution or
# destructive primitive. Matched exactly or as a `--flag=value` / attached prefix.
DANGEROUS_ARGUMENTS: dict[str, frozenset[str]] = {
    "find": frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
                       "-fprintf", "-fls"}),
    "node": frozenset({"-e", "--eval", "-p", "--print", "-r", "--require", "--import",
                       "--loader", "--experimental-loader", "--inspect", "--inspect-brk"}),
    "npm": frozenset({"exec", "x", "explore", "publish", "unpublish", "adduser", "login",
                      "logout", "token", "owner", "access", "config", "set"}),
    "yarn": frozenset({"dlx", "exec", "publish", "npm", "config", "login", "logout"}),
    "pnpm": frozenset({"dlx", "exec", "publish", "config", "login", "logout"}),
    "make": frozenset({"--eval", "-E"}),
    "go": frozenset({"-exec", "-toolexec"}),
    "cargo": frozenset({"--config", "-Z"}),
    "gcc": frozenset({"-wrapper", "-fplugin", "-specs"}),
    "g++": frozenset({"-wrapper", "-fplugin", "-specs"}),
    "cmake": frozenset({"-P", "-E"}),
}


def _dangerous_argument(binary: str, arg: str) -> bool:
    denied = DANGEROUS_ARGUMENTS.get(binary)
    if not denied:
        return False
    head = arg.split("=", 1)[0]
    if head in denied:
        return True
    # Attached short-option values, e.g. `-e"code"` or `-Zunstable`.
    return any(d.startswith("-") and not d.startswith("--") and len(d) == 2 and arg.startswith(d)
               for d in denied)


def _path_candidates(arg: str) -> list[str]:
    """Strings within an argument that may name a filesystem path."""
    candidates = [arg]
    if "=" in arg:
        candidates.append(arg.split("=", 1)[1])
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
        candidates.append(arg[2:])  # Attached value, e.g. -o/etc/x or -I../include
    return [
        c for c in candidates
        if c and ("/" in c or "\\" in c or ".." in c or c.startswith("~") or ":" in c[:5]
                  or c.lstrip(".").casefold().startswith(("env", "git", "jarvis")))
    ]


class GenericCommandInput(BaseModel):
    binary: str = Field(..., description="The binary to execute (must be on the allowlist).")
    args: list[str] = Field(default_factory=list, description="List of arguments for the command.")
    timeout: int | None = Field(
        None, ge=1, le=MAX_COMMAND_TIMEOUT, description="Optional custom timeout in seconds."
    )


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

    def get_input_schema(self) -> type[BaseModel]:
        return GenericCommandInput

    def execution_timeout(self, inputs: dict[str, Any]) -> float:
        # Let the runner's own timeout (which returns partial output) fire first.
        allowed = ALLOWED_BINARIES.get(inputs.get("binary") or "")
        command_timeout = inputs.get("timeout") or (allowed.default_timeout if allowed else 0)
        return max(self.timeout_seconds, command_timeout + 10)

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
            cwd = resolve_in_workspace(".", workspaces).path

            for i, arg in enumerate(args):
                # 3. Arguments that would allow arbitrary code execution or deletion
                if _dangerous_argument(binary_name, arg):
                    return ToolResult(
                        success=False,
                        error=f"Security Error: Argument {i+1} ('{arg}') is not permitted for '{binary_name}'.",
                    )
                # 4. Path arguments must stay inside the workspace and off protected paths
                for candidate in _path_candidates(arg):
                    try:
                        authorize_path(candidate, workspaces)
                    except (PermissionError, ValueError) as e:
                        return ToolResult(success=False, error=f"Security Error: Argument {i+1} ('{arg}') escapes workspace: {e}")

            # 5. Determine Timeout
            timeout = kwargs.get("timeout") or allowed.default_timeout

            # 6. Run Command
            task_id = kwargs.get("task_id")
            res = await self._runner.run(
                binary_name, 
                args, 
                cwd=cwd, 
                timeout=timeout,
                task_id=task_id
            )
            
            # 7. Final Result
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
