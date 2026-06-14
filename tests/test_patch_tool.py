import pytest
import os
from pathlib import Path
from jarvis.tools.patch_engine import PatchEngine, PatchHunk
from jarvis.tools.filesystem import PatchFileTool, PatchHunkInput, ToolResult

@pytest.fixture
def patch_engine():
    return PatchEngine()

def test_patch_engine_exact_match(patch_engine):
    content = "line1\nline2\nline3"
    hunks = [PatchHunk(search="line2", replace="line2_patched")]
    result = patch_engine.apply_hunks(content, hunks)
    assert result.success
    assert result.content == "line1\nline2_patched\nline3"

def test_patch_engine_multiple_hunks(patch_engine):
    content = "a\nb\nc"
    hunks = [
        PatchHunk(search="a", replace="A"),
        PatchHunk(search="c", replace="C")
    ]
    result = patch_engine.apply_hunks(content, hunks)
    assert result.success
    assert result.content == "A\nb\nC"

def test_patch_engine_not_found(patch_engine):
    content = "a\nb\nc"
    hunks = [PatchHunk(search="z", replace="Z")]
    result = patch_engine.apply_hunks(content, hunks)
    assert not result.success
    assert "not found" in result.error

def test_patch_engine_ambiguous(patch_engine):
    content = "a\na\na"
    hunks = [PatchHunk(search="a", replace="A")]
    result = patch_engine.apply_hunks(content, hunks)
    assert not result.success
    assert "ambiguous" in result.error

@pytest.mark.asyncio
async def test_patch_file_tool_success(tmp_path):
    # Setup
    workspace = tmp_path / "ws"
    workspace.mkdir()
    file_path = workspace / "test.txt"
    file_path.write_text("hello\nworld", encoding="utf-8")
    
    tool = PatchFileTool()
    workspaces = [{"path": str(workspace)}]
    
    # Execute
    hunks = [PatchHunkInput(search="world", replace="universe")]
    result = await tool.execute(
        path="test.txt", 
        hunks=hunks, 
        workspaces=workspaces
    )
    
    assert result.success
    assert file_path.read_text(encoding="utf-8") == "hello\nuniverse"
    # Ensure backup is cleaned up
    assert not list(workspace.glob(".jarvis_backups/*"))

@pytest.mark.asyncio
async def test_patch_file_tool_rollback_on_verification_failure(tmp_path, monkeypatch):
    # Setup
    workspace = tmp_path / "ws"
    workspace.mkdir()
    file_path = workspace / "test.txt"
    file_path.write_text("hello\nworld", encoding="utf-8")
    
    tool = PatchFileTool()
    workspaces = [{"path": str(workspace)}]
    
    # Mock the write_text to something that doesn't include the replacement
    # to trigger verification failure
    original_write = Path.write_text
    def sabotaged_write(self, content, encoding=None):
        return original_write(self, "sabotaged content", encoding=encoding)
    
    monkeypatch.setattr(Path, "write_text", sabotaged_write)
    
    # Execute
    hunks = [PatchHunkInput(search="world", replace="universe")]
    result = await tool.execute(
        path="test.txt", 
        hunks=hunks, 
        workspaces=workspaces
    )
    
    assert not result.success
    assert "Verification failed" in result.error
    # Check if rollback restored original content
    monkeypatch.undo() # Restore real write_text to read the file correctly if needed, 
                       # though read_text isn't mocked.
    assert file_path.read_text(encoding="utf-8") == "hello\nworld"
