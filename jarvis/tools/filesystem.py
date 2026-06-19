"""Filesystem tools for Jarvis."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jarvis.tools.base import BaseTool, ToolCategory, ToolResult
from jarvis.tools.patch_engine import PatchEngine, PatchHunk
from jarvis.tools.trash_manager import TrashManager

PROTECTED_PATHS = {".git", ".jarvis", ".env"}
...
class PatchHunkInput(BaseModel):
    search: str = Field(..., description="The exact block of code to find.")
    replace: str = Field(..., description="The block of code to replace it with.")


class PatchFileInput(BaseModel):
    path: str = Field(..., description="Path to the file to patch.")
    hunks: list[PatchHunkInput] = Field(..., description="List of search/replace blocks to apply.")


class PatchFileTool(BaseTool):
    """Tool for surgical file patching."""

    def __init__(self) -> None:
        super().__init__(
            name="patch_file",
            description="Applies surgical search-and-replace patches to a file.",
            category=ToolCategory.MUTATING,
        )
        self._engine = PatchEngine()

    def get_input_schema(self) -> type[BaseModel]:
        return PatchFileInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            path = _get_validated_path(kwargs["path"], workspaces)
            hunks = [PatchHunk(h.search, h.replace) for h in kwargs["hunks"]]
            
            if not path.is_file():
                return ToolResult(success=False, error=f"Not a file: {path}")

            # 1. Read Original
            original_content = path.read_text(encoding="utf-8")
            
            # 2. Create Pre-flight Backup
            backup_path = self._create_backup(path)
            
            try:
                # 3. Apply Patches in-memory
                result = self._engine.apply_hunks(original_content, hunks)
                if not result.success:
                    return ToolResult(success=False, error=result.error)
                
                # 4. Write New Content
                new_content = result.content
                if new_content is None:
                    return ToolResult(success=False, error="Patch engine returned no content.")
                    
                path.write_text(new_content, encoding="utf-8")
                
                # 5. Post-application Verification
                # We re-read and check if all 'replace' blocks are actually present
                verified_content = path.read_text(encoding="utf-8")
                for hunk in hunks:
                    if hunk.replace not in verified_content:
                        # Rollback
                        self._restore_backup(backup_path, path)
                        return ToolResult(
                            success=False, 
                            error="Verification failed: Patched content was not found in file after writing. Rolled back."
                        )
                
                return ToolResult(success=True, data=f"File patched successfully: {path}")
            
            except Exception as e:
                # Emergency Rollback
                self._restore_backup(backup_path, path)
                raise e
            finally:
                # Cleanup backup if it exists
                if backup_path and backup_path.exists():
                    backup_path.unlink()

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _create_backup(self, path: Path) -> Path:
        """Create a temporary backup of the file."""
        backup_dir = path.parent / ".jarvis_backups"
        backup_dir.mkdir(exist_ok=True)
        backup_path = backup_dir / f"{path.name}.{int(time.time() * 1000)}.bak"
        shutil.copy2(path, backup_path)
        return backup_path

    def _restore_backup(self, backup_path: Path, target_path: Path) -> None:
        """Restore a file from backup."""
        if backup_path and backup_path.exists():
            shutil.move(str(backup_path), str(target_path))


def _get_validated_path(target_path_str: str, workspaces: list[dict]) -> Path:
    """Resolve and validate that a path is within at least one workspace."""
    target_path = Path(target_path_str).expanduser()
    
    # Check if target_path is absolute or relative
    # If relative, we must decide which workspace it's relative to.
    # For simplicity in V1, if it's relative, we try to resolve it against each workspace.
    # But usually the LLM provides absolute paths it found via list_directory.
    
    for ws in workspaces:
        ws_root = Path(str(ws["path"])).resolve()
        
        # Try resolving relative to this workspace if not absolute
        if not target_path.is_absolute():
            potential_path = (ws_root / target_path).resolve()
        else:
            potential_path = target_path.resolve()
            
        if potential_path.is_relative_to(ws_root):
            return potential_path
            
    raise PermissionError(f"Path is outside allowed workspaces: {target_path_str}")


class ReadFileInput(BaseModel):
    path: str = Field(..., description="Path to the file to read.")


class ReadFileTool(BaseTool):
    """Tool for reading file contents."""

    def __init__(self) -> None:
        super().__init__(
            name="read_file",
            description="Reads the content of a file within a workspace.",
            category=ToolCategory.READ_ONLY,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return ReadFileInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            path = _get_validated_path(kwargs["path"], workspaces)
            
            if not path.is_file():
                return ToolResult(success=False, error=f"Not a file: {path}")
                
            content = path.read_text(encoding="utf-8")
            return ToolResult(success=True, data=content)
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class ListDirectoryInput(BaseModel):
    path: str = Field(".", description="Path to the directory to list.")


class ListDirectoryTool(BaseTool):
    """Tool for listing directory contents."""

    def __init__(self) -> None:
        super().__init__(
            name="list_directory",
            description="Lists files and folders within a workspace directory.",
            category=ToolCategory.READ_ONLY,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return ListDirectoryInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            path = _get_validated_path(kwargs["path"], workspaces)
            
            if not path.is_dir():
                return ToolResult(success=False, error=f"Not a directory: {path}")
                
            items = []
            for item in path.iterdir():
                items.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0
                })
            return ToolResult(success=True, data=items)
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class WriteFileInput(BaseModel):
    path: str = Field(..., description="Path to the file to write.")
    content: str = Field(..., description="Content to write to the file.")


class WriteFileTool(BaseTool):
    """Tool for writing file contents."""

    def __init__(self) -> None:
        super().__init__(
            name="write_file",
            description="Creates or overwrites a file within a workspace.",
            category=ToolCategory.MUTATING,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return WriteFileInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            path = _get_validated_path(kwargs["path"], workspaces)
            
            # Ensure parent directory exists
            path.parent.mkdir(parents=True, exist_ok=True)
            
            path.write_text(kwargs["content"], encoding="utf-8")
            return ToolResult(success=True, data=f"File written successfully: {path}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

class DeleteFileInput(BaseModel):
    path: str = Field(..., description="Path to the file or directory to delete.")
    recursive: bool = Field(False, description="Whether to delete a directory recursively.")


class DeleteFileTool(BaseTool):
    """Tool for safe file deletion via soft-delete."""

    def __init__(self) -> None:
        super().__init__(
            name="delete_file",
            description="Safely removes a file or directory by moving it to the trash.",
            category=ToolCategory.DESTRUCTIVE,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return DeleteFileInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            path = _get_validated_path(kwargs["path"], workspaces)
            task_id = kwargs.get("task_id")

            # 1. Protected Path Enforcement
            if path.name in PROTECTED_PATHS:
                return ToolResult(success=False, error=f"Access Denied: '{path.name}' is a protected system path.")
            
            # 2. Workspace Root Protection
            for ws in workspaces:
                if path.resolve() == Path(str(ws["path"])).resolve():
                    return ToolResult(success=False, error="Access Denied: Cannot delete a workspace root directory.")

            # 3. Directory check
            if path.is_dir() and not kwargs.get("recursive"):
                return ToolResult(success=False, error=f"'{path}' is a directory. Use recursive=True to delete.")

            # 4. Perform Soft-Delete
            # TrashManager needs to know which workspace root to use.
            ws_root = None
            for ws in workspaces:
                root = Path(str(ws["path"])).resolve()
                if path.resolve().is_relative_to(root):
                    ws_root = root
                    break
            
            if not ws_root:
                return ToolResult(success=False, error="Could not identify workspace root for trash.")

            trash_manager = TrashManager(ws_root)
            entry_id = trash_manager.trash(path, task_id=task_id)

            return ToolResult(
                success=True, 
                data={
                    "message": f"Successfully moved to trash: {path}",
                    "trash_id": entry_id
                }
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class RestoreFileInput(BaseModel):
    trash_id: str = Field(..., description="The unique ID of the trashed entry to restore.")
    workspace_path: str = Field(..., description="Path to the workspace root where the file was trashed.")


class RestoreFileTool(BaseTool):
    """Tool for restoring deleted files from trash."""

    def __init__(self) -> None:
        super().__init__(
            name="restore_file",
            description="Restores a previously deleted file or directory from the trash.",
            category=ToolCategory.MUTATING,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return RestoreFileInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            workspaces = kwargs.get("workspaces", [])
            ws_path = _get_validated_path(kwargs["workspace_path"], workspaces)
            
            trash_manager = TrashManager(ws_path)
            restored_path = trash_manager.restore(kwargs["trash_id"])
            
            return ToolResult(success=True, data=f"Successfully restored to: {restored_path}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))
