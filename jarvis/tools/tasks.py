"""Task and introspection tools for Jarvis."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jarvis.tools.base import BaseTool, ToolCategory, ToolResult


class GetTaskStatusInput(BaseModel):
    task_id: str | None = Field(None, description="The ID of the task. Defaults to current task.")


class GetTaskStatusTool(BaseTool):
    """Tool for checking task progress."""

    def __init__(self) -> None:
        super().__init__(
            name="get_task_status",
            description="Retrieves status and output of previously executed task steps.",
            category=ToolCategory.READ_ONLY,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return GetTaskStatusInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            uow = kwargs.get("uow")
            if not uow:
                return ToolResult(success=False, error="UnitOfWork context missing")
                
            task_id = kwargs.get("task_id") or kwargs.get("current_task_id")
            if not task_id:
                return ToolResult(success=False, error="No task ID provided or found in context.")
            
            async with uow.begin() as unit:
                assert unit.repositories is not None
                task = await unit.repositories.tasks.get(task_id)
                if not task:
                    return ToolResult(success=False, error=f"Task not found: {task_id}")
                    
                steps = await unit.repositories.tasks.list_steps(task_id)
                
                data = {
                    "id": task["id"],
                    "title": task["title"],
                    "status": task["status"],
                    "steps": [
                        {
                            "index": s["step_index"],
                            "title": s["title"],
                            "status": s["status"],
                            "output": s["output_json"],
                            "error": s["error"]
                        }
                        for s in steps
                    ]
                }
                return ToolResult(success=True, data=data)
        except Exception as e:
            return ToolResult(success=False, error=str(e))
