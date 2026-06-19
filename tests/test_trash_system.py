
import pytest

from jarvis.tools.filesystem import DeleteFileTool, RestoreFileTool
from jarvis.tools.trash_manager import TrashManager


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws

@pytest.fixture
def trash_manager(workspace):
    return TrashManager(workspace)

def test_trash_file(trash_manager, workspace):
    test_file = workspace / "test.txt"
    test_file.write_text("hello", encoding="utf-8")
    
    entry_id = trash_manager.trash(test_file)
    
    assert not test_file.exists()
    assert (trash_manager.entries_dir / entry_id).exists()
    assert (trash_manager.metadata_dir / f"{entry_id}.json").exists()
    
    entries = trash_manager.list_entries()
    assert len(entries) == 1
    assert entries[0].original_path == str(test_file.resolve())

def test_restore_file(trash_manager, workspace):
    test_file = workspace / "test.txt"
    test_file.write_text("hello", encoding="utf-8")
    
    entry_id = trash_manager.trash(test_file)
    trash_manager.restore(entry_id)
    
    assert test_file.exists()
    assert test_file.read_text(encoding="utf-8") == "hello"
    assert not (trash_manager.entries_dir / entry_id).exists()
    assert not (trash_manager.metadata_dir / f"{entry_id}.json").exists()

@pytest.mark.asyncio
async def test_delete_file_tool_protected_path(workspace):
    tool = DeleteFileTool()
    git_dir = workspace / ".git"
    git_dir.mkdir()
    
    workspaces = [{"path": str(workspace)}]
    result = await tool.execute(path=".git", workspaces=workspaces)
    
    assert not result.success
    assert "protected system path" in result.error

@pytest.mark.asyncio
async def test_delete_file_tool_workspace_root(workspace):
    tool = DeleteFileTool()
    workspaces = [{"path": str(workspace)}]
    
    # Try to delete the workspace root itself
    result = await tool.execute(path=".", workspaces=workspaces)
    
    assert not result.success
    assert "Cannot delete a workspace root directory" in result.error

@pytest.mark.asyncio
async def test_full_delete_restore_lifecycle(workspace):
    delete_tool = DeleteFileTool()
    restore_tool = RestoreFileTool()
    workspaces = [{"path": str(workspace)}]
    
    test_file = workspace / "delete_me.txt"
    test_file.write_text("goodbye", encoding="utf-8")
    
    # 1. Delete
    del_result = await delete_tool.execute(path="delete_me.txt", workspaces=workspaces)
    assert del_result.success
    trash_id = del_result.data["trash_id"]
    assert not test_file.exists()
    
    # 2. Restore
    res_result = await restore_tool.execute(
        trash_id=trash_id, 
        workspace_path=str(workspace), 
        workspaces=workspaces
    )
    assert res_result.success
    assert test_file.exists()
    assert test_file.read_text(encoding="utf-8") == "goodbye"
