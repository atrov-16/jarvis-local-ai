"""Git tool for repository-aware developer workflows."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tools.path_policy import authorize_path, resolve_in_workspace

# Global options for every invocation: a repository's own config (which a cloned,
# untrusted project controls) must not be able to run programs during status/diff/log.
GIT_SAFE_OPTIONS = [
    "--no-pager",
    "--no-optional-locks",
    "-c", "core.fsmonitor=false",
    "-c", "core.pager=cat",
    "-c", "diff.external=",
]


class GitOperation(str, Enum):
    STATUS = "status"
    DIFF = "diff"
    LOG = "log"
    # Reserved for future use
    ADD = "add"
    COMMIT = "commit"
    CHECKOUT = "checkout"
    MERGE = "merge"
    PUSH = "push"
    PULL = "pull"


# The operations `GitTool.execute` performs; the rest of `GitOperation` is reserved.
SUPPORTED_OPERATIONS = (GitOperation.STATUS, GitOperation.DIFF, GitOperation.LOG)


def _narrow_operation_enum(node: Any, supported: list[str]) -> int:
    """Replace the `GitOperation` enum, wherever the schema states it, with `supported`.
    Returns how many enums were replaced."""
    all_operations = [operation.value for operation in GitOperation]
    replaced = 0
    if isinstance(node, dict):
        if node.get("enum") == all_operations:
            node["enum"] = list(supported)
            replaced += 1
        for value in node.values():
            replaced += _narrow_operation_enum(value, supported)
    elif isinstance(node, list):
        for value in node:
            replaced += _narrow_operation_enum(value, supported)
    return replaced


class GitToolInput(BaseModel):
    operation: GitOperation = Field(..., description="The Git operation to perform.")
    path: str | None = Field(None, description="Optional file or directory path for diff.")
    limit: int = Field(5, description="Number of log entries to retrieve (max 20).", ge=1, le=20)


class GitTool(BaseTool):
    """Tool for read-only Git repository operations."""

    def __init__(self, runner: CommandRunner) -> None:
        super().__init__(
            name="git",
            description="Performs read-only Git operations like status, diff, and log.",
            category=ToolCategory.READ_ONLY,
        )
        self._runner = runner

    def get_input_schema(self) -> type[BaseModel]:
        return GitToolInput

    def llm_input_schema(self) -> dict[str, Any]:
        """Advertise only the operations `execute` performs. `GitOperation` also names
        reserved write operations; offering those to the model invites calls that can only
        fail."""
        schema = GitToolInput.model_json_schema()
        supported = [operation.value for operation in SUPPORTED_OPERATIONS]
        narrowed = _narrow_operation_enum(schema, supported)
        if narrowed != 1:
            raise RuntimeError(
                f"Expected exactly one GitOperation enum in the git tool schema, found {narrowed}."
            )
        return schema

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            operation = kwargs["operation"]
            
            # 1. Resolve and Validate Workspace
            # We use a default relative path "." if no path provided, 
            # but we need a validated path to anchor the Git commands.
            target_path_str = kwargs.get("path") or "."
            
            cwd = resolve_in_workspace(".", workspaces).path
            
            if not (cwd / ".git").exists():
                # Check parents if not at root
                is_repo = False
                for parent in cwd.parents:
                    if (parent / ".git").exists():
                        is_repo = True
                        break
                if not is_repo:
                    return ToolResult(success=False, error="Target path is not part of a Git repository.")

            # 2. Dispatch Operation
            task_id = kwargs.get("task_id")
            if operation == GitOperation.STATUS:
                return await self._do_status(cwd, task_id)
            elif operation == GitOperation.DIFF:
                diff_path = None
                if kwargs.get("path"):
                    # Must be inside the workspace and not protected; passed after `--` so
                    # it can never be read as an option.
                    diff_path = str(authorize_path(kwargs["path"], workspaces).path)
                return await self._do_diff(cwd, diff_path, task_id)
            elif operation == GitOperation.LOG:
                return await self._do_log(cwd, kwargs.get("limit", 5), task_id)
            else:
                return ToolResult(success=False, error=f"Operation '{operation}' is reserved or not supported.")

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    async def _git(self, args: list[str], cwd: Path, task_id: str | None):
        return await self._runner.run("git", [*GIT_SAFE_OPTIONS, *args], cwd=cwd, task_id=task_id)

    async def _do_status(self, cwd: Path, task_id: str | None = None) -> ToolResult:
        # Branch detection
        branch_res = await self._git(["branch", "--show-current"], cwd, task_id)
        if branch_res.exit_code != 0:
            error = f"Git status failed: {branch_res.stderr.strip()}"
            return ToolResult(success=False, error=error)
        branch = branch_res.stdout.strip() or "DETACHED"

        # Porcelain status for counts
        status_res = await self._git(["status", "--porcelain=v1"], cwd, task_id)
        if status_res.exit_code != 0:
            error = f"Git status failed: {status_res.stderr.strip()}"
            return ToolResult(success=False, error=error)
        lines = status_res.stdout.splitlines()
        
        modified = 0
        untracked = 0
        staged = 0
        for line in lines:
            if line.startswith("??"):
                untracked += 1
            elif line[1] in ("M", "D"): # Unstaged
                modified += 1
            elif line[0] in ("M", "A", "D", "R", "C"): # Staged
                staged += 1

        is_dirty = len(lines) > 0
        
        summary = {
            "branch": branch,
            "is_dirty": is_dirty,
            "modified_count": modified,
            "untracked_count": untracked,
            "staged_count": staged,
            "summary_text": f"Branch: {branch} | State: {'Dirty' if is_dirty else 'Clean'} ({modified} mod, {untracked} untracked, {staged} staged)"
        }
        
        return ToolResult(success=True, data=summary)

    async def _do_diff(self, cwd: Path, file_path: str | None, task_id: str | None = None) -> ToolResult:
        args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"]
        if file_path:
            args.extend(["--", file_path])
            
        res = await self._git(args, cwd, task_id)

        if res.exit_code != 0:
            return ToolResult(success=False, error=f"Git diff failed: {res.stderr.strip()}")

        if not res.stdout and not res.stderr and res.exit_code == 0:
            return ToolResult(success=True, data={"diff": "", "message": "No changes found."})
            
        # Truncation is handled by to_summary, but we want structured data here
        output = res.stdout
        max_len = 5000
        if len(output) > max_len:
            output = output[:max_len] + f"\n\n... [Diff truncated, total length: {len(res.stdout)} chars] ..."

        return ToolResult(success=True, data={"diff": output})

    async def _do_log(self, cwd: Path, limit: int, task_id: str | None = None) -> ToolResult:
        limit = min(max(1, limit), 20)
        res = await self._git(["log", f"-n{limit}", "--oneline", "--no-color"], cwd, task_id)
        
        if res.exit_code != 0:
            return ToolResult(success=False, error=f"Git log failed: {res.stderr}")
            
        return ToolResult(success=True, data={"log": res.stdout.strip()})
