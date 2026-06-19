"""Memory Browser Service for Phase 10."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from jarvis.api.schemas import (
    LineageNode,
    MemoryDetailResponse,
    MemoryHealthMetrics,
    MemoryProposalResponse,
    MemoryResponse,
)
from jarvis.memory.store import MemoryStore
from jarvis.storage.unit_of_work import UnitOfWork

LOG = logging.getLogger(__name__)

class MemoryBrowserService:
    def __init__(self, uow: UnitOfWork, memory_store: MemoryStore) -> None:
        self._uow = uow
        self._memory_store = memory_store

    async def list_memories(
        self, 
        project_id: str | None = None, 
        status: str | None = "active",
        memory_type: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0
    ) -> list[MemoryResponse]:
        """List or search memories with pagination and filtering."""
        if q:
            results = await self._memory_store.search(
                q, 
                project_id=project_id, 
                memory_type=memory_type, 
                status=status, 
                limit=limit, 
                offset=offset
            )
            return [
                MemoryResponse(
                    id=r.id,
                    project_id=r.project_id,
                    memory_type=r.memory_type,
                    title=r.title,
                    content=r.content,
                    tags=r.tags,
                    source=r.source,
                    status=r.status,
                    importance=r.importance,
                    confidence_score=r.confidence_score,
                    access_count=r.access_count,
                    last_retrieved_at=r.last_retrieved_at,
                    source_ids=r.source_ids,
                    metadata=r.metadata,
                    created_at=r.created_at,
                    updated_at=r.updated_at
                )
                for r in results
            ]
        
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            raw_memories = await unit.repositories.memory.list_long_term(
                project_id=project_id,
                status=status,
                memory_type=memory_type,
                limit=limit,
                offset=offset
            )
            return [MemoryResponse(**m) for m in raw_memories]

    async def get_memory_detail(self, memory_id: str, lineage_depth: int = 3) -> MemoryDetailResponse:
        """Get detailed memory information including metrics and lineage."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            raw_memory = await unit.repositories.memory.get_long_term(memory_id)
            if not raw_memory:
                raise ValueError(f"Memory not found: {memory_id}")
            
            memory = MemoryResponse(**raw_memory)
            
            # 1. Health Metrics
            metadata = raw_memory.get("metadata", {})
            metrics = MemoryHealthMetrics(
                access_count=int(raw_memory.get("access_count", 0)),
                confidence_score=float(raw_memory.get("confidence_score", 1.0)),
                importance=float(raw_memory.get("importance", 0.5)),
                merge_count=len(metadata.get("merged_ids", [])),
                conflict_count=len(metadata.get("conflicting_ids", [])),
                last_retrieved_at=str(raw_memory.get("last_retrieved_at")) if raw_memory.get("last_retrieved_at") else None
            )
            
            # 2. Lineage Tree
            lineage = await self._build_lineage(raw_memory.get("source_ids", []), depth=lineage_depth)
            
            return MemoryDetailResponse(
                **memory.model_dump(),
                metrics=metrics,
                lineage=lineage
            )

    async def _build_lineage(self, source_ids: list[str], depth: int) -> list[LineageNode]:
        """Recursively build lineage provenance tree."""
        if depth <= 0 or not source_ids:
            return []
            
        nodes = []
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            for s_id in source_ids:
                # Check Tasks
                task = await unit.repositories.tasks.get(s_id)
                if task:
                    nodes.append(LineageNode(
                        id=s_id,
                        type="task",
                        summary=task["title"],
                        timestamp=task["created_at"],
                        metadata={"user_request": task["user_request"]}
                    ))
                    continue
                
                # Check Memories (Merges)
                parent_memory = await unit.repositories.memory.get_long_term(s_id)
                if parent_memory:
                    children = await self._build_lineage(parent_memory.get("source_ids", []), depth - 1)
                    nodes.append(LineageNode(
                        id=s_id,
                        type="memory",
                        summary=parent_memory["content"][:100],
                        timestamp=parent_memory["created_at"],
                        metadata={"status": parent_memory["status"]},
                        children=children
                    ))
                    continue
                
                # Check Proposals
                proposal = await unit.repositories.memory.get_proposal(s_id)
                if proposal:
                    nodes.append(LineageNode(
                        id=s_id,
                        type="proposal",
                        summary=proposal["proposed_content"][:100],
                        timestamp=proposal["created_at"],
                        metadata={"status": proposal["status"]}
                    ))
                    continue
        
        return nodes

    async def resolve_conflict(
        self, 
        memory_id: str, 
        action: str, 
        winner_id: str | None = None,
        conflicting_ids: list[str] = [],
        reason: str | None = None
    ) -> bool:
        """Resolve a flagged contradiction between memories."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            
            if action == "pick_winner":
                if not winner_id:
                    raise ValueError("winner_id is required for pick_winner action.")
                
                # Restore winner to active
                await unit.repositories.memory.update_long_term(winner_id, status="active")
                
                # Archive others
                for c_id in conflicting_ids:
                    if c_id != winner_id:
                        await unit.repositories.memory.update_long_term(c_id, status="archived", archived_at=datetime.now(UTC).isoformat())
                
                await unit.repositories.audit.insert(
                    actor="user",
                    action_type="memory.resolve",
                    summary=f"Resolved conflict by picking winner: {winner_id}",
                    target=winner_id,
                    details={"action": action, "reason": reason, "archived": [id for id in conflicting_ids if id != winner_id]}
                )
            
            elif action == "ignore":
                # Restore all to active despite contradiction
                for c_id in conflicting_ids:
                    await unit.repositories.memory.update_long_term(c_id, status="active")
                
                await unit.repositories.audit.insert(
                    actor="user",
                    action_type="memory.resolve",
                    summary="Ignored conflict, restored all memories to active.",
                    target=memory_id,
                    details={"action": action, "reason": reason}
                )
                
            return True

    async def list_proposals(self, project_id: str | None = None, status: str = "pending", limit: int = 50, offset: int = 0) -> list[MemoryProposalResponse]:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            raw = await unit.repositories.memory.list_proposals(project_id=project_id, status=status, limit=limit, offset=offset)
            return [MemoryProposalResponse(**p) for p in raw]

    async def get_proposal(self, proposal_id: str) -> MemoryProposalResponse:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            p = await unit.repositories.memory.get_proposal(proposal_id)
            if not p:
                raise ValueError(f"Proposal not found: {proposal_id}")
            return MemoryProposalResponse(**p)
