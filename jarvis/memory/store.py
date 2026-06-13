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
    importance: float
    access_count: int
    last_retrieved_at: str | None
    source_ids: list[str]
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
        importance: float = 0.5,
        source_ids: list[str] | None = None,
    ) -> str:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            proposal_id = await unit.repositories.memory.propose_long_term(
                project_id=project_id,
                task_id=task_id,
                memory_type=memory_type,
                proposed_content=proposed_content,
                proposed_tags=proposed_tags,
                reason=reason,
                importance=importance,
                source_ids=source_ids,
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
        async with self._uow.begin() as unit:
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
                importance=float(proposal.get("importance", 0.5)),
                source_ids=proposal.get("source_ids", []),
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
        async with self._uow.begin() as unit:
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
        async with self._uow.begin() as unit:
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

    async def get_planner_context(
        self,
        query: str,
        *,
        project_id: str | None = None,
    ) -> tuple[str, list[str]]:
        """Retrieve, rank, and budget memories for planner context injection."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            raw_results = await unit.repositories.memory.search_long_term(
                query, project_id=project_id, limit=50
            )

        if not raw_results:
            return "", []

        now = datetime.now(UTC)
        ranked_results = []
        
        ranks = [float(r["rank"]) for r in raw_results]
        min_rank = min(ranks)
        max_rank = max(ranks)
        rank_range = max_rank - min_rank
        
        import math
        for r in raw_results:
            # Normalize S_fts to 0.1 - 1.0 (1.0 being the best/lowest rank)
            if rank_range == 0:
                s_fts = 1.0
            else:
                s_fts = 0.1 + 0.9 * ((max_rank - float(r["rank"])) / rank_range)

            importance = float(r.get("importance", 0.5))
            
            r_boost = 0.0
            last_retrieved_str = r.get("last_retrieved_at") or r.get("created_at")
            if last_retrieved_str:
                try:
                    last_retrieved = datetime.fromisoformat(str(last_retrieved_str))
                    days_diff = (now - last_retrieved).total_seconds() / 86400
                    r_boost = min(0.1, 0.1 * (1.0 / (1.0 + max(0, days_diff))))
                except ValueError:
                    pass
                    
            access_count = int(r.get("access_count", 0))
            f_boost = min(0.05, 0.05 * (math.log10(1 + access_count) / math.log10(100))) if access_count > 0 else 0.0
            
            p_boost = 1.2 if r.get("project_id") == project_id and project_id else 1.0
            
            final_score = s_fts * (1.0 + (importance - 0.5)) * p_boost * (1.0 + r_boost + f_boost)
            ranked_results.append((final_score, r))
            
        ranked_results.sort(key=lambda x: x[0], reverse=True)
        
        # Categorical Token Budgeting (approx 1 token = 4 chars)
        budget_decisions = 1600
        budget_reflections = 1200
        total_budget = 6000
        
        used_total = 0
        used_decisions = 0
        used_reflections = 0
        
        selected_memories = []
        memory_ids = []
        
        for _, r in ranked_results:
            m_type = str(r["memory_type"])
            content = str(r["content"])
            m_id = str(r["id"])
            
            entry_str = f"[{m_type.capitalize()}] {content}"
            entry_len = len(entry_str) + 1  # +1 for newline
            
            if used_total + entry_len > total_budget:
                continue
                
            if m_type == "decision":
                if used_decisions + entry_len > budget_decisions:
                    continue
                used_decisions += entry_len
            elif m_type == "reflection":
                if used_reflections + entry_len > budget_reflections:
                    continue
                used_reflections += entry_len
            
            used_total += entry_len
            selected_memories.append(entry_str)
            memory_ids.append(m_id)
            
        if not selected_memories:
            return "", []
            
        context_str = "### SYSTEM CONTEXT & MEMORIES\nPlease adhere to the following rules, facts, and user preferences when formulating your plan:\n\n"
        context_str += "\n".join(selected_memories)
        
        return context_str, memory_ids

    async def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            raw_results = await unit.repositories.memory.search_long_term(
                query, project_id=project_id, memory_type=memory_type, limit=limit
            )
            
            return [
                MemorySearchResult(
                    id=str(r["id"]),
                    project_id=str(r["project_id"]) if r["project_id"] else None,
                    memory_type=str(r["memory_type"]),
                    title=str(r["title"]) if r["title"] else None,
                    content=str(r["content"]),
                    tags=r["tags"], # type: ignore
                    source=str(r["source"]),
                    importance=float(r.get("importance", 0.5)),
                    access_count=int(r.get("access_count", 0)),
                    last_retrieved_at=str(r["last_retrieved_at"]) if r.get("last_retrieved_at") else None,
                    source_ids=r.get("source_ids", []), # type: ignore
                    relevance_score=float(r["rank"]),
                    created_at=str(r["created_at"]),
                    updated_at=str(r["updated_at"]),
                )
                for r in raw_results
            ]
