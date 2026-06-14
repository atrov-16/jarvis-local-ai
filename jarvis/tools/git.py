"""Git tool for repository-aware developer workflows."""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Type

from pydantic import BaseModel, Field

from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tasks.command_runner import CommandRunner


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

    def get_input_schema(self) -> Type[BaseModel]:
        return GitToolInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            operation = kwargs["operation"]
            
            # 1. Resolve and Validate Workspace
            # We use a default relative path "." if no path provided, 
            # but we need a validated path to anchor the Git commands.
            target_path_str = kwargs.get("path") or "."
            
            # Note: _get_validated_path is in filesystem.py, but we don't want circular imports.
            # For now, we'll implement a simple version or move the helper.
            # Given the constraints, I will use a local validation check.
            from jarvis.tools.filesystem import _get_validated_path
            cwd = _get_validated_path(".", workspaces)
            
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
            if operation == GitOperation.STATUS:
                return await self._do_status(cwd)
            elif operation == GitOperation.DIFF:
                return await self._do_diff(cwd, kwargs.get("path"))
            elif operation == GitOperation.LOG:
                return await self._do_log(cwd, kwargs.get("limit", 5))
            else:
                return ToolResult(success=False, error=f"Operation '{operation}' is reserved or not supported.")

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    async def _do_status(self, cwd: Path) -> ToolResult:
        # Branch detection
        branch_res = await self._runner.run("git", ["branch", "--show-current"], cwd=cwd)
        branch = branch_res.stdout.strip() or "DETACHED"

        # Porcelain status for counts
        status_res = await self._runner.run("git", ["status", "--porcelain=v1"], cwd=cwd)
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

    async def _do_diff(self, cwd: Path, file_path: str | None) -> ToolResult:
        args = ["diff", "--no-color", "--no-ext-diff"]
        if file_path:
            args.append(file_path)
            
        res = await self._runner.run("git", args, cwd=cwd)
        
        if not res.stdout and not res.stderr and res.exit_code == 0:
            return ToolResult(success=True, data={"diff": "", "message": "No changes found."})
            
        # Truncation is handled by to_summary, but we want structured data here
        output = res.stdout
        max_len = 5000
        if len(output) > max_len:
            output = output[:max_len] + f"\n\n... [Diff truncated, total length: {len(res.stdout)} chars] ..."

        return ToolResult(success=True, data={"diff": output})

    async def _do_log(self, cwd: Path, limit: int) -> ToolResult:
        limit = min(max(1, limit), 20)
        res = await self._runner.run(
            "git", 
            ["log", f"-n{limit}", "--oneline", "--no-color"], 
            cwd=cwd
        )
        
        if res.exit_code != 0:
            return ToolResult(success=False, error=f"Git log failed: {res.stderr}")
            
        return ToolResult(success=True, data={"log": res.stdout.strip()})
