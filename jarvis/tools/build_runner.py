"""Multi-framework build tool for compilation and bundling verification."""

from __future__ import annotations

import logging
import re
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Type

from pydantic import BaseModel, Field

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult

LOG = logging.getLogger(__name__)


class BuildSystem(str, Enum):
    """Supported build systems."""
    __test__ = False
    NPM = "npm"
    CARGO = "cargo"
    PYTHON = "python"
    MAKE = "make"
    AUTO = "auto"


class BuildToolInput(BaseModel):
    system: BuildSystem = Field(BuildSystem.AUTO, description="The build system to use.")
    target: str | None = Field(None, description="Optional specific build target (e.g., 'all' for make).")
    mode: str = Field("debug", description="Build mode (e.g., 'debug' or 'release' for cargo).")


class BuildTool(BaseTool):
    """Tool for running build commands and extracting structured summary data."""
    __test__ = False

    def __init__(self, runner: CommandRunner) -> None:
        super().__init__(
            name="build",
            description="Executes project build commands and verifies compilation.",
            category=ToolCategory.MUTATING,
            timeout_seconds=300, # Slow builds (e.g. cargo)
        )
        self._runner = runner

    def get_input_schema(self) -> Type[BaseModel]:
        return BuildToolInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            from jarvis.tools.filesystem import _get_validated_path
            cwd = _get_validated_path(".", workspaces)
            
            system = kwargs.get("system", BuildSystem.AUTO)
            if system == BuildSystem.AUTO:
                system = self._detect_build_system(cwd)
                if not system:
                    return ToolResult(success=False, error="Could not auto-detect build system. Please specify it explicitly.")

            # 1. Detect TypeScript
            is_typescript = (cwd / "tsconfig.json").exists()

            # 2. Prepare Command
            cmd, args = self._prepare_command(system, kwargs)
            
            # 3. Run Command
            task_id = kwargs.get("task_id")
            start_time = time.perf_counter()
            res = await self._runner.run(cmd, args, cwd=cwd, timeout=self.timeout_seconds, task_id=task_id)
            duration = time.perf_counter() - start_time
            
            # 4. Parse Output for warnings/errors
            stats = self._parse_output(system, res.stdout, res.stderr)
            
            # 5. Final Result
            result_data = {
                "system": system.value,
                "is_typescript": is_typescript,
                "success": res.exit_code == 0 and not res.timeout_occurred,
                "duration": duration,
                "warning_count": stats["warnings"],
                "error_count": stats["errors"],
                "summary": (
                    f"Build {'succeeded' if res.exit_code == 0 else 'failed'} in {duration:.2f}s. "
                    f"Warnings: {stats['warnings']}, Errors: {stats['errors']}."
                ),
                "raw_logs": res.stdout + "\n" + res.stderr
            }
            
            return ToolResult(
                success=result_data["success"],
                data=result_data,
                execution_time=duration,
                timeout_occurred=res.timeout_occurred
            )

        except Exception as e:
            LOG.exception("Build execution failed")
            return ToolResult(success=False, error=str(e))

    def _detect_build_system(self, cwd: Path) -> BuildSystem | None:
        """Identify the build system by probing for markers."""
        if (cwd / "package.json").exists():
            return BuildSystem.NPM
        if (cwd / "Cargo.toml").exists():
            return BuildSystem.CARGO
        if (cwd / "pyproject.toml").exists() or (cwd / "setup.py").exists():
            return BuildSystem.PYTHON
        if (cwd / "Makefile").exists() or (cwd / "makefile").exists():
            return BuildSystem.MAKE
        return None

    def _prepare_command(self, system: BuildSystem, inputs: dict) -> tuple[str, list[str]]:
        target = inputs.get("target")
        mode = inputs.get("mode", "debug")
        
        if system == BuildSystem.NPM:
            # npm run build
            args = ["run", "build"]
            if target:
                args.extend(["--", target])
            return "npm", args
            
        elif system == BuildSystem.CARGO:
            # cargo build
            args = ["build"]
            if mode == "release":
                args.append("--release")
            if target:
                args.extend(["--bin", target])
            return "cargo", args
            
        elif system == BuildSystem.PYTHON:
            # python -m build
            return sys.executable, ["-m", "build"]
            
        elif system == BuildSystem.MAKE:
            # make [target]
            args = []
            if target:
                args.append(target)
            return "make", args
            
        raise ValueError(f"Unsupported build system: {system}")

    def _parse_output(self, system: BuildSystem, stdout: str, stderr: str) -> dict[str, int]:
        """Simple regex-based parsing for warnings and errors."""
        full_output = stdout + "\n" + stderr
        stats = {"warnings": 0, "errors": 0}
        
        # 1. Framework-specific patterns
        if system == BuildSystem.CARGO:
            stats["errors"] = len(re.findall(r"^error:", full_output, re.MULTILINE))
            stats["warnings"] = len(re.findall(r"^warning:", full_output, re.MULTILINE))
        elif system == BuildSystem.NPM:
            # npm/webpack/tsc often use standard prefixes
            stats["errors"] = len(re.findall(r"error |FAILED|FATAL", full_output, re.IGNORECASE))
            stats["warnings"] = len(re.findall(r"warning |WARN", full_output, re.IGNORECASE))
        elif system == BuildSystem.MAKE:
            stats["errors"] = len(re.findall(r": error:|Stop\.", full_output))
            stats["warnings"] = len(re.findall(r": warning:", full_output))
        else:
            # Generic fallback
            stats["errors"] = len(re.findall(r"error[: ]", full_output, re.IGNORECASE))
            stats["warnings"] = len(re.findall(r"warning[: ]", full_output, re.IGNORECASE))
            
        return stats
