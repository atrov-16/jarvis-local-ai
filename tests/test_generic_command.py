import pytest
import os
from pathlib import Path
from jarvis.tools.generic_command import GenericCommandTool, ALLOWED_BINARIES
from jarvis.tasks.command_runner import CommandRunner

@pytest.fixture
def runner():
    return CommandRunner()

@pytest.fixture
def command_tool(runner):
    return GenericCommandTool(runner)

@pytest.mark.asyncio
async def test_command_allowlist_enforcement(command_tool):
    # Try forbidden binary
    result = await command_tool.execute(binary="python", args=["--version"], workspaces=[])
    assert not result.success
    assert "Security Error: Binary 'python' is not on the allowlist" in result.error
    
    # Try non-existent but allowed binary (might fail due to file not found, but not security error)
    # We'll use 'ls' which should exist on most systems (even Windows via git bash or similar, 
    # but we'll mock to be safe)
    from unittest.mock import AsyncMock
    from jarvis.tasks.command_runner import CommandResult
    
    command_tool._runner.run = AsyncMock(return_value=CommandResult(
        exit_code=0, stdout="file.txt", stderr="", execution_time=0.1,
        timeout_occurred=False, command="ls", working_dir="/tmp"
    ))
    
    result = await command_tool.execute(binary="ls", workspaces=[{"path": "/tmp"}])
    assert result.success

@pytest.mark.asyncio
async def test_workspace_confinement_args(command_tool, tmp_path):
    # Setup workspace
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspaces = [{"path": str(workspace)}]
    
    # Valid arg
    result = await command_tool.execute(binary="ls", args=["file.txt"], workspaces=workspaces)
    # Binary might fail to actually find file.txt but should pass security check
    
    # Invalid arg (escaping)
    result = await command_tool.execute(binary="ls", args=["../../etc/passwd"], workspaces=workspaces)
    assert not result.success
    assert "Security Error" in result.error
    assert "escapes workspace" in result.error

@pytest.mark.asyncio
async def test_network_awareness_metadata(command_tool):
    # Mock runner for npm (network-aware)
    from unittest.mock import AsyncMock
    from jarvis.tasks.command_runner import CommandResult
    
    command_tool._runner.run = AsyncMock(return_value=CommandResult(
        exit_code=0, stdout="ok", stderr="", execution_time=0.1,
        timeout_occurred=False, command="npm install", working_dir="/tmp"
    ))
    
    result = await command_tool.execute(binary="npm", args=["install"], workspaces=[{"path": "/tmp"}])
    assert result.success
    assert result.data["has_network_access"] is True
    
    # Mock for ls (not network-aware)
    command_tool._runner.run = AsyncMock(return_value=CommandResult(
        exit_code=0, stdout="ok", stderr="", execution_time=0.1,
        timeout_occurred=False, command="ls", working_dir="/tmp"
    ))
    
    result = await command_tool.execute(binary="ls", workspaces=[{"path": "/tmp"}])
    assert result.success
    assert result.data["has_network_access"] is False
