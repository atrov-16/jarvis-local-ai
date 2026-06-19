"""Memory tools for Jarvis."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jarvis.memory.store import MemoryStore
from jarvis.tools.base import BaseTool, ToolCategory, ToolResult


class SearchMemoryInput(BaseModel):
    query: str = Field(..., description="The search query.")
    memory_type: str | None = Field(None, description="Optional memory type to filter by.")
    limit: int = Field(20, description="Maximum number of results.")


class SearchMemoryTool(BaseTool):
    """Tool for searching long-term memory."""

    def __init__(self) -> None:
        super().__init__(
            name="search_memory",
            description="Queries long-term memory and project notes.",
            category=ToolCategory.READ_ONLY,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return SearchMemoryInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            uow = kwargs.get("uow")
            if not uow:
                return ToolResult(success=False, error="UnitOfWork context missing")
                
            project_id = kwargs.get("project_id")
            store = MemoryStore(uow)
            
            results = await store.search(
                query=kwargs["query"],
                project_id=project_id,
                memory_type=kwargs.get("memory_type"),
                limit=kwargs.get("limit", 20)
            )
            
            # Convert MemorySearchResult to dict
            data = [
                {
                    "id": r.id,
                    "type": r.memory_type,
                    "title": r.title,
                    "content": r.content,
                    "tags": r.tags,
                    "importance": r.importance,
                    "source_ids": r.source_ids,
                    "relevance": r.relevance_score
                }
                for r in results
            ]
            return ToolResult(success=True, data=data)
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class CreateMemoryProposalInput(BaseModel):
    content: str = Field(..., description="The content of the memory.")
    memory_type: str = Field("fact", description="Type: fact, preference, decision, reflection, note.")
    tags: list[str] = Field(default_factory=list, description="Optional tags.")
    reason: str = Field(..., description="Why this memory is worth saving.")
    importance: float = Field(0.5, description="Base importance score from 0.0 to 1.0.")
    confidence_score: float = Field(1.0, description="Confidence score from 0.0 to 1.0.")
    source_ids: list[str] = Field(default_factory=list, description="IDs of tasks or memories that justify this proposal.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional source lineage metadata.")


class CreateMemoryProposalTool(BaseTool):
    """Tool for proposing new memories."""

    def __init__(self) -> None:
        super().__init__(
            name="create_memory_proposal",
            description="Submits a new long-term memory for user approval.",
            category=ToolCategory.MUTATING,
        )

    def get_input_schema(self) -> type[BaseModel]:
        return CreateMemoryProposalInput

    async def execute(self, **kwargs: Any) -> ToolResult:
        try:
            uow = kwargs.get("uow")
            if not uow:
                return ToolResult(success=False, error="UnitOfWork context missing")
                
            project_id = kwargs.get("project_id")
            task_id = kwargs.get("task_id")
            store = MemoryStore(uow)
            
            proposal_id = await store.propose(
                project_id=project_id,
                task_id=task_id,
                memory_type=kwargs["memory_type"],
                proposed_content=kwargs["content"],
                proposed_tags=kwargs.get("tags"),
                reason=kwargs["reason"],
                importance=kwargs.get("importance", 0.5),
                confidence_score=kwargs.get("confidence_score", 1.0),
                source_ids=kwargs.get("source_ids"),
                metadata=kwargs.get("metadata"),
            )

            
            return ToolResult(
                success=True, 
                data={"proposal_id": proposal_id, "message": "Memory proposal created successfully."}
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
