
import pytest

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.build_runner import BuildSystem, BuildTool


@pytest.fixture
def runner():
    return CommandRunner()

@pytest.fixture
def build_tool(runner):
    return BuildTool(runner)

@pytest.mark.asyncio
async def test_detect_npm_build(build_tool, tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert build_tool._detect_build_system(tmp_path) == BuildSystem.NPM

@pytest.mark.asyncio
async def test_detect_cargo_build(build_tool, tmp_path):
    (tmp_path / "Cargo.toml").write_text("")
    assert build_tool._detect_build_system(tmp_path) == BuildSystem.CARGO

@pytest.mark.asyncio
async def test_detect_python_build(build_tool, tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    assert build_tool._detect_build_system(tmp_path) == BuildSystem.PYTHON

@pytest.mark.asyncio
async def test_detect_make_build(build_tool, tmp_path):
    (tmp_path / "Makefile").write_text("")
    assert build_tool._detect_build_system(tmp_path) == BuildSystem.MAKE

@pytest.mark.asyncio
async def test_detect_typescript(build_tool, tmp_path):
    # TypeScript is a metadata check, not a build system itself for detection
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "tsconfig.json").write_text("{}")
    
    # We need to run execute to check is_typescript in result data
    # but we'll mock the runner to avoid actually running npm
    from unittest.mock import AsyncMock

    from jarvis.tasks.command_runner import CommandResult
    
    build_tool._runner.run = AsyncMock(return_value=CommandResult(
        exit_code=0, stdout="", stderr="", execution_time=0.1, 
        timeout_occurred=False, command="npm run build", working_dir=str(tmp_path)
    ))
    
    workspaces = [{"path": str(tmp_path)}]
    result = await build_tool.execute(system=BuildSystem.NPM, workspaces=workspaces)
    
    assert result.success
    assert result.data["is_typescript"] is True

@pytest.mark.asyncio
async def test_parse_cargo_output(build_tool):
    stdout = """
   Compiling test v0.1.0
warning: unused variable: `x`
  --> src/main.rs:2:9
   |
 2 |     let x = 5;
   |         ^ help: if this is intentional, prefix it with an underscore: `_x`

error: expected `;`, found `}`
  --> src/main.rs:3:2
   |
 3 | }
   |  ^ help: add `;` here

error: could not compile `test` due to previous error; 1 warning emitted
"""
    stats = build_tool._parse_output(BuildSystem.CARGO, stdout, "")
    assert stats["errors"] == 2 # "error:" appears twice
    assert stats["warnings"] == 1

@pytest.mark.asyncio
async def test_build_tool_execution_success(build_tool, tmp_path):
    # Create a simple Makefile project
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "Makefile").write_text("all:\n\t@echo Building...")
    
    workspaces = [{"path": str(workspace)}]
    # Note: 'make' must be on the system path for this to pass locally
    # If not, we might need to skip or mock
    import shutil
    if not shutil.which("make"):
        pytest.skip("make not found on system path")
        
    result = await build_tool.execute(system=BuildSystem.MAKE, workspaces=workspaces)
    
    assert result.success
    assert result.data["system"] == "make"
    assert "succeeded" in result.data["summary"]
