"""Security and boundary tests for Jarvis tools."""

import os

import pytest

from jarvis.tools.filesystem import ListDirectoryTool, ReadFileTool, WriteFileTool


@pytest.fixture
def workspace_root(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "allowed.txt").write_text("inside", encoding="utf-8")
    return ws

@pytest.fixture
def workspaces(workspace_root):
    return [{"path": str(workspace_root), "name": "test_ws"}]

@pytest.mark.asyncio
async def test_filesystem_confinement_success(workspaces):
    tool = ReadFileTool()
    result = await tool.execute(path="allowed.txt", workspaces=workspaces)
    assert result.success is True
    assert result.data == "inside"

@pytest.mark.asyncio
async def test_filesystem_confinement_absolute_success(workspace_root, workspaces):
    tool = ReadFileTool()
    abs_path = str(workspace_root / "allowed.txt")
    result = await tool.execute(path=abs_path, workspaces=workspaces)
    assert result.success is True
    assert result.data == "inside"

@pytest.mark.asyncio
async def test_filesystem_traversal_escape(workspaces, tmp_path):
    tool = ReadFileTool()
    # Try to escape via ../
    result = await tool.execute(path="../outside.txt", workspaces=workspaces)
    assert result.success is False
    assert "outside allowed workspaces" in result.error

@pytest.mark.asyncio
async def test_filesystem_absolute_escape(workspaces, tmp_path):
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")
    
    tool = ReadFileTool()
    result = await tool.execute(path=str(outside_file), workspaces=workspaces)
    assert result.success is False
    assert "outside allowed workspaces" in result.error

@pytest.mark.asyncio
async def test_write_file_confinement(workspaces, workspace_root):
    tool = WriteFileTool()
    result = await tool.execute(path="new_file.txt", content="hello", workspaces=workspaces)
    assert result.success is True
    assert (workspace_root / "new_file.txt").read_text() == "hello"

@pytest.mark.asyncio
async def test_write_file_escape(workspaces, tmp_path):
    tool = WriteFileTool()
    result = await tool.execute(path="../escape.txt", content="evil", workspaces=workspaces)
    assert result.success is False
    assert "outside allowed workspaces" in result.error

@pytest.mark.asyncio
async def test_symlink_escape(workspaces, workspace_root, tmp_path):
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")
    
    # Create a symlink inside the workspace pointing outside
    link_path = workspace_root / "link_to_outside"
    try:
        os.symlink(outside_file, link_path)
    except OSError:
        pytest.skip("Symlinks not supported on this platform/user")

    tool = ReadFileTool()
    # Even if accessed via the link, resolve() should find the real path and fail
    result = await tool.execute(path="link_to_outside", workspaces=workspaces)
    assert result.success is False
    assert "outside allowed workspaces" in result.error

@pytest.mark.asyncio
async def test_list_directory_confinement(workspaces):
    tool = ListDirectoryTool()
    result = await tool.execute(path=".", workspaces=workspaces)
    assert result.success is True
    assert any(item["name"] == "allowed.txt" for item in result.data)

@pytest.mark.asyncio
async def test_list_directory_escape(workspaces):
    tool = ListDirectoryTool()
    result = await tool.execute(path="..", workspaces=workspaces)
    assert result.success is False
    assert "outside allowed workspaces" in result.error
