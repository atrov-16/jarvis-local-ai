"""Multi-framework test runner tool for autonomous validation."""

from __future__ import annotations

import logging
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult

LOG = logging.getLogger(__name__)


class TestFramework(str, Enum):
    """Supported test frameworks."""
    __test__ = False
    PYTEST = "pytest"
    UNITTEST = "unittest"
    NPM = "npm"
    CARGO = "cargo"
    AUTO = "auto"


class TestToolInput(BaseModel):
    framework: TestFramework = Field(TestFramework.AUTO, description="The test framework to use.")
    target: str | None = Field(None, description="Optional specific test file or directory.")
    pattern: str | None = Field(None, description="Optional pattern to match test names.")


class TestTool(BaseTool):
    """Tool for running automated tests with auto-detection and structured reporting."""
    __test__ = False

    def __init__(self, runner: CommandRunner) -> None:
        super().__init__(
            name="test",
            description="Runs automated tests and extracts structured failure data.",
            category=ToolCategory.READ_ONLY,
            timeout_seconds=120,
        )
        self._runner = runner

    def get_input_schema(self) -> type[BaseModel]:
        return TestToolInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            from jarvis.tools.filesystem import _get_validated_path
            cwd = _get_validated_path(".", workspaces)
            
            framework = kwargs.get("framework", TestFramework.AUTO)
            if framework == TestFramework.AUTO:
                framework = self._detect_framework(cwd)
                if not framework:
                    return ToolResult(success=False, error="Could not auto-detect test framework. Please specify it explicitly.")

            # 1. Prepare Command
            cmd, args = self._prepare_command(framework, kwargs)
            
            # 2. Run Command
            task_id = kwargs.get("task_id")
            res = await self._runner.run(cmd, args, cwd=cwd, timeout=self.timeout_seconds, task_id=task_id)
            
            # 3. Parse Output
            parsed_data = self._parse_output(framework, res.stdout, res.stderr)
            
            # 4. Final Result
            summary_text = (
                f"[Validation Result] {framework.value.upper()} | "
                f"Passed: {parsed_data['passed']}, Failed: {parsed_data['failed']}, "
                f"Skipped: {parsed_data['skipped']} | Duration: {parsed_data['duration'] or 'unknown'}"
            )
            
            result_data = {
                "framework": framework.value,
                "status": "passed" if parsed_data["failed"] == 0 and res.exit_code == 0 else "failed",
                "counts": {
                    "passed": parsed_data["passed"],
                    "failed": parsed_data["failed"],
                    "skipped": parsed_data["skipped"],
                },
                "failures": parsed_data["failures"],
                "duration": parsed_data["duration"],
                "summary": summary_text,
                "raw_logs": res.stdout + "\n" + res.stderr
            }
            
            return ToolResult(
                success=result_data["status"] == "passed",
                data=result_data
            )

        except Exception as e:
            LOG.exception("Test execution failed")
            return ToolResult(success=False, error=str(e))

    def _detect_framework(self, cwd: Path) -> TestFramework | None:
        """Probe the directory for test markers."""
        if (cwd / "package.json").exists():
            return TestFramework.NPM
        if (cwd / "Cargo.toml").exists():
            return TestFramework.CARGO
        if (cwd / "pytest.ini").exists() or (cwd / "pyproject.toml").exists() or (cwd / "conftest.py").exists():
            return TestFramework.PYTEST
        if (cwd / "tests").is_dir():
            return TestFramework.UNITTEST
        return None

    def _prepare_command(self, framework: TestFramework, inputs: dict) -> tuple[str, list[str]]:
        python_exe = sys.executable
        target = inputs.get("target")
        pattern = inputs.get("pattern")
        
        if framework == TestFramework.PYTEST:
            args = ["-m", "pytest", "-v", "--no-header"]
            if target: args.append(target)
            if pattern: args.extend(["-k", pattern])
            return python_exe, args
            
        elif framework == TestFramework.UNITTEST:
            args = ["-m", "unittest", "discover", "-v"]
            if target: args.extend(["-s", target])
            if pattern: args.extend(["-p", pattern])
            return python_exe, args
            
        elif framework == TestFramework.NPM:
            args = ["test"]
            if target or pattern:
                args.append("--")
                if target: args.append(target)
                if pattern: args.extend(["-g", pattern])
            return "npm", args
            
        elif framework == TestFramework.CARGO:
            args = ["test"]
            if target: args.append(target)
            return "cargo", args
            
        raise ValueError(f"Unsupported framework: {framework}")

    def _parse_output(self, framework: TestFramework, stdout: str, stderr: str) -> dict:
        """Extract counts and failures from test output."""
        data = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "duration": None,
            "failures": []
        }
        
        full_output = stdout + "\n" + stderr
        
        if framework == TestFramework.PYTEST:
            match = re.search(r"==+ (.*) in ([\d\.]+)s ==+", full_output)
            if match:
                summary_part, duration = match.groups()
                data["duration"] = f"{duration}s"
                pass_match = re.search(r"(\d+) passed", summary_part)
                fail_match = re.search(r"(\d+) failed", summary_part)
                skip_match = re.search(r"(\d+) skipped", summary_part)
                if pass_match: data["passed"] = int(pass_match.group(1))
                if fail_match: data["failed"] = int(fail_match.group(1))
                if skip_match: data["skipped"] = int(skip_match.group(1))
            
            fail_blocks = re.findall(r"_+ (test_\w+) _+\n(.*?)(?=\n_+ |$)", full_output, re.DOTALL)
            for name, content in fail_blocks[:5]:
                data["failures"].append({"test_name": name, "error": content.strip().splitlines()[-1]})

        elif framework == TestFramework.UNITTEST:
            ran_match = re.search(r"Ran (\d+) tests in ([\d\.]+)s", full_output)
            if ran_match:
                count, duration = ran_match.groups()
                data["duration"] = f"{duration}s"
                total = int(count)
                if "OK" in full_output:
                    data["passed"] = total
                else:
                    fail_match = re.search(r"FAILED \(.*failures=(\d+).*\)", full_output)
                    error_match = re.search(r"FAILED \(.*errors=(\d+).*\)", full_output)
                    skipped_match = re.search(r"FAILED \(.*skipped=(\d+).*\)", full_output)
                    data["failed"] = (int(fail_match.group(1)) if fail_match else 0) + (int(error_match.group(1)) if error_match else 0)
                    data["skipped"] = int(skipped_match.group(1)) if skipped_match else 0
                    data["passed"] = total - data["failed"] - data["skipped"]

        return data
