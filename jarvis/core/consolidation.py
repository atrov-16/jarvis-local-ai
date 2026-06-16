"""Memory Consolidation Engine for Jarvis Phase 9."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from jarvis.models.router import ModelRouter
from jarvis.models.schemas import ModelRequest, Message
from jarvis.memory.store import MemoryStore
from jarvis.storage.unit_of_work import UnitOfWork

if TYPE_CHECKING:
    from jarvis.memory.store import MemorySearchResult

LOG = logging.getLogger(__name__)

CONSOLIDATION_PROMPT = """You are the Memory Consolidation Brain of Jarvis.
Your goal is to analyze a cluster of related memories and identify potential duplicates or contradictions.

Cluster context:
{cluster_context}

Rules:
1. Detect Near-Duplicates: If two memories convey nearly the same fact or preference, propose a MERGE.
2. Detect Contradictions: If two memories provide conflicting information (e.g., "likes tabs" vs "likes spaces"), propose a CONFLICT.
3. For Merges: Propose a single unified memory content that preserves the detail of both.
4. For Conflicts: Clearly explain the contradiction. Do NOT resolve it.
5. Assign a merge_score or conflict_score (0.0 to 1.0) representing your confidence.
6. Preserve all original tags and add new ones if relevant.

Output ONLY a valid JSON object matching the requested schema.

Schema:
{{
  "actions": [
    {{
      "type": "merge" | "conflict",
      "target_ids": ["id1", "id2"],
      "proposed_content": "Unified content (only for merge)",
      "reason": "Why this merge or conflict was identified",
      "score": 0.95
    }}
  ]
}}
"""

class ConsolidationAction(BaseModel):
    type: str = Field(..., pattern="^(merge|conflict)$")
    target_ids: list[str]
    proposed_content: str | None = None
    reason: str
    score: float = Field(..., ge=0.0, le=1.0)


class ConsolidationResult(BaseModel):
    actions: list[ConsolidationAction]


class ConsolidationService:
    def __init__(
        self,
        uow: UnitOfWork,
        model_router: ModelRouter,
        memory_store: MemoryStore
    ) -> None:
        self._uow = uow
        self._model_router = model_router
        self._memory_store = memory_store
        self._semaphore = asyncio.Semaphore(1) # Run only one consolidation at a time

    async def consolidate_all(self, project_id: str | None = None) -> int:
        """Run consolidation across the entire memory store or a specific project."""
        async with self._semaphore:
            LOG.info(f"Starting memory consolidation for project: {project_id or 'Global'}")
            
            # 1. Fetch all active memories
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                memories = await unit.repositories.memory.list_long_term(project_id=project_id, status="active", limit=1000)
            
            if len(memories) < 2:
                LOG.info("Insufficient memories for consolidation.")
                return 0

            # 2. Heuristic Clustering: Group by memory_type and high-level tag overlap
            clusters = self._cluster_memories(memories)
            LOG.info(f"Identified {len(clusters)} potential clusters for analysis.")

            proposal_count = 0
            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                
                proposal_count += await self._analyze_cluster(cluster, project_id)
            
            return proposal_count

    def _cluster_memories(self, memories: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Group memories using simple heuristics to avoid O(N^2) LLM calls."""
        clusters: dict[str, list[dict[str, Any]]] = {}
        
        for m in memories:
            m_type = m["memory_type"]
            # Cluster by type + primary tag (if any) or just type
            tags = m.get("tags", [])
            primary_tag = tags[0] if tags else "untagged"
            cluster_key = f"{m_type}:{primary_tag}"
            
            if cluster_key not in clusters:
                clusters[cluster_key] = []
            clusters[cluster_key].append(m)
            
        return [c for c in clusters.values() if len(c) > 1]

    async def _analyze_cluster(self, cluster: list[dict[str, Any]], project_id: str | None) -> int:
        """Analyze a single cluster for duplicates and contradictions using LLM."""
        cluster_context = "\n".join([
            f"ID: {m['id']} | Content: {m['content']} | Tags: {m.get('tags', [])}"
            for m in cluster
        ])

        try:
            messages = [
                Message(role="system", content=CONSOLIDATION_PROMPT.format(cluster_context=cluster_context)),
                Message(role="user", content="Analyze this cluster for merges or conflicts."),
            ]
            
            request = ModelRequest(messages=messages, temperature=0.0)
            response = await self._model_router.complete(request)
            content = response.message.content.strip()
            
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
                
            result = ConsolidationResult.model_validate_json(content)
            
            proposal_count = 0
            for action in result.actions:
                if action.type == "merge":
                    await self._propose_merge(action, cluster, project_id)
                elif action.type == "conflict":
                    await self._propose_conflict(action, cluster, project_id)
                proposal_count += 1
                
            return proposal_count
        except Exception as e:
            LOG.exception(f"Cluster analysis failed: {e}")
            return 0

    async def _propose_merge(self, action: ConsolidationAction, cluster: list[dict[str, Any]], project_id: str | None) -> None:
        """Propose merging multiple memories into a new one."""
        targets = [m for m in cluster if m["id"] in action.target_ids]
        if not targets or not action.proposed_content:
            return

        # Combine source lineage
        source_ids = []
        all_tags = set()
        for t in targets:
            source_ids.extend(t.get("source_ids", []))
            source_ids.append(t["id"]) # Trace back to merged ones
            all_tags.update(t.get("tags", []))

        metadata = {
            "type": "consolidation_merge",
            "merge_score": action.score,
            "merged_ids": action.target_ids,
            "original_sources": list(set(source_ids))
        }

        await self._memory_store.propose(
            project_id=project_id,
            memory_type=targets[0]["memory_type"],
            proposed_content=action.proposed_content,
            proposed_tags=list(all_tags),
            reason=action.reason,
            importance=max(float(t.get("importance", 0.5)) for t in targets),
            confidence_score=action.score,
            source_ids=list(set(source_ids)),
            metadata=metadata
        )

    async def _propose_conflict(self, action: ConsolidationAction, cluster: list[dict[str, Any]], project_id: str | None) -> None:
        """Propose flagging a contradiction for human review."""
        targets = [m for m in cluster if m["id"] in action.target_ids]
        if not targets:
            return

        # We don't create a new memory for conflict, we propose flagging existing ones
        # For V1, we'll use a special proposal type or metadata
        metadata = {
            "type": "consolidation_conflict",
            "conflict_score": action.score,
            "conflicting_ids": action.target_ids,
            "reason": action.reason
        }

        # Propose as a 'reflection' type with 'conflict' tag for visibility
        await self._memory_store.propose(
            project_id=project_id,
            memory_type="reflection",
            proposed_content=f"CONFLICT DETECTED: {action.reason}",
            proposed_tags=["conflict", "needs-review"],
            reason=action.reason,
            importance=0.9, # High importance for conflicts
            confidence_score=action.score,
            source_ids=action.target_ids,
            metadata=metadata
        )
        
        # Also flag the memories in the DB if we had a bulk update, 
        # but the approval flow usually handles this. 
        # For now, we rely on the proposal to notify the user.
