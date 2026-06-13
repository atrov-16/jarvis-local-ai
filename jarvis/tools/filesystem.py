"""Filesystem tools for Jarvis."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Type

from pydantic import BaseModel, Field

from jarvis.tools.base import BaseTool, ToolCategory, ToolResult


def _get_validated_path(target_path_str: str, workspaces: List[dict]) -> Path:
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

    def get_input_schema(self) -> Type[BaseModel]:
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

    def get_input_schema(self) -> Type[BaseModel]:
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

    def get_input_schema(self) -> Type[BaseModel]:
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
