"""Memory store service for Phase 4."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jarvis.storage.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class MemorySearchResult:
    id: str
    project_id: str | None
    memory_type: str
    title: str | None
    content: str
    tags: list[str]
    source: str
    relevance_score: float
    created_at: str
    updated_at: str


class MemoryStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def propose(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        memory_type: str,
        proposed_content: str,
        proposed_tags: list[str] | None = None,
        reason: str,
    ) -> str:
        async with self._uow as unit:
            assert unit.repositories is not None
            proposal_id = await unit.repositories.memory.propose_long_term(
                project_id=project_id,
                task_id=task_id,
                memory_type=memory_type,
                proposed_content=proposed_content,
                proposed_tags=proposed_tags,
                reason=reason,
            )
            await unit.repositories.audit.insert(
                actor="system",
                action_type="memory.propose",
                summary=f"Proposed new {memory_type} memory",
                target=proposal_id,
                details={"memory_type": memory_type, "reason": reason},
            )
            return proposal_id

    async def approve(self, proposal_id: str, title: str | None = None) -> str:
        async with self._uow as unit:
            assert unit.repositories is not None
            proposal = await unit.repositories.memory.get_proposal(proposal_id)
            if not proposal:
                raise ValueError(f"Proposal not found: {proposal_id}")
            
            if proposal["status"] != "pending":
                raise ValueError(f"Proposal is already {proposal['status']}")

            # 1. Update status
            now = datetime.now(UTC).isoformat()
            await unit.repositories.memory.update_proposal_status(
                proposal_id, "approved", decided_at=now
            )

            # 2. Promote to long-term
            memory_id = await unit.repositories.memory.insert_long_term(
                project_id=proposal["project_id"],
                task_id=proposal["task_id"],
                memory_type=proposal["memory_type"],
                title=title or proposal.get("title"),
                content=proposal["proposed_content"],
                tags=proposal["proposed_tags"],
                source="proposal_promotion",
            )

            # 3. Audit
            await unit.repositories.audit.insert(
                actor="user",
                action_type="memory.approve",
                summary=f"Approved and promoted memory proposal: {proposal_id}",
                target=memory_id,
                details={"proposal_id": proposal_id},
            )
            return memory_id

    async def deny(self, proposal_id: str, reason: str | None = None) -> bool:
        async with self._uow as unit:
            assert unit.repositories is not None
            proposal = await unit.repositories.memory.get_proposal(proposal_id)
            if not proposal:
                return False
            
            updated = await unit.repositories.memory.update_proposal_status(proposal_id, "denied")
            if updated:
                await unit.repositories.audit.insert(
                    actor="user",
                    action_type="memory.deny",
                    summary=f"Denied memory proposal: {proposal_id}",
                    target=proposal_id,
                    details={"reason": reason},
                )
            return updated

    async def delete_memory(self, memory_id: str) -> bool:
        async with self._uow as unit:
            assert unit.repositories is not None
            memory = await unit.repositories.memory.get_long_term(memory_id)
            if not memory:
                return False

            deleted = await unit.repositories.memory.delete_long_term(memory_id)
            if deleted:
                await unit.repositories.audit.insert(
                    actor="user",
                    action_type="memory.delete",
                    summary=f"Deleted long-term memory: {memory_id}",
                    target=memory_id,
                )
            return deleted

    async def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        async with self._uow as unit:
            assert unit.repositories is not None
            raw_results = await unit.repositories.memory.search_long_term(
                query, project_id=project_id, memory_type=memory_type, limit=limit
            )
            
            return [
                MemorySearchResult(
                    id=r["id"],
                    project_id=r["project_id"],
                    memory_type=r["memory_type"],
                    title=r["title"],
                    content=r["content"],
                    tags=r["tags"],
                    source=r["source"],
                    relevance_score=float(r["rank"]),
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
                for r in raw_results
            ]
