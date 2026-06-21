"""Approval Center Service for Phase 10."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis.api.schemas import (
    ApprovalStats,
    BulkApprovalItem,
    BulkApprovalRequest,
    UnifiedApprovalItem,
)
from jarvis.approvals.broker import ApprovalBroker
from jarvis.memory.store import MemoryStore
from jarvis.storage.unit_of_work import UnitOfWork

LOG = logging.getLogger(__name__)

class ApprovalCenterService:
    def __init__(self, uow: UnitOfWork, approval_broker: ApprovalBroker, memory_store: MemoryStore) -> None:
        self._uow = uow
        self._approval_broker = approval_broker
        self._memory_store = memory_store

    async def list_pending(self, limit: int = 50, offset: int = 0) -> list[UnifiedApprovalItem]:
        """Aggregate all pending approvals and proposals."""
        items: list[UnifiedApprovalItem] = []
        
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            
            # 1. Fetch pending actions (tools, plans)
            actions = await unit.repositories.approvals.list_all(status="pending", limit=limit)
            for a in actions:
                items.append(UnifiedApprovalItem(
                    id=str(a["id"]),
                    type="action",
                    subtype=str(a["action_type"]),
                    summary=str(a["summary"]),
                    risk_level=str(a["risk_level"]),
                    status="pending",
                    task_id=str(a["task_id"]) if a.get("task_id") else None,
                    created_at=str(a["created_at"]),
                    metadata={"action_json": a["action_json"]}
                ))
            
            # 2. Fetch pending memory proposals
            proposals = await unit.repositories.memory.list_proposals(status="pending", limit=limit)
            for p in proposals:
                subtype = "fact"
                m_type = "memory"
                metadata = p.get("metadata", {})
                
                if metadata.get("type") == "consolidation_merge":
                    subtype = "merge"
                    m_type = "consolidation"
                elif metadata.get("type") == "consolidation_conflict":
                    subtype = "conflict"
                    m_type = "consolidation"
                else:
                    subtype = str(p["memory_type"])

                items.append(UnifiedApprovalItem(
                    id=str(p["id"]),
                    type=m_type,
                    subtype=subtype,
                    summary=str(p["reason"]),
                    risk_level="low" if m_type == "memory" else "medium",
                    status="pending",
                    task_id=str(p["task_id"]) if p.get("task_id") else None,
                    created_at=str(p["created_at"]),
                    metadata={
                        "proposed_content": p["proposed_content"],
                        "confidence_score": p.get("confidence_score", 1.0),
                        "importance": p.get("importance", 0.5)
                    }
                ))

        # Sort combined list by created_at DESC
        items.sort(key=lambda x: x.created_at, reverse=True)
        return items[offset : offset + limit]

    async def bulk_respond(self, request: BulkApprovalRequest, user_id: str = "user") -> dict[str, Any]:
        """Perform bulk approve/deny across different types."""
        results = []
        
        async def handle_item(item: BulkApprovalItem):
            try:
                if request.action == "approve":
                    if item.type == "action":
                        success = await self._approval_broker.approve(item.id, decided_by=user_id, reason=request.reason)
                        return {"id": item.id, "success": success}
                    elif item.type in ("memory", "consolidation"):
                        memory_id = await self._memory_store.approve(item.id)
                        return {"id": item.id, "success": True, "memory_id": memory_id}
                else:  # deny
                    if item.type == "action":
                        success = await self._approval_broker.deny(item.id, decided_by=user_id, reason=request.reason)
                        return {"id": item.id, "success": success}
                    elif item.type in ("memory", "consolidation"):
                        success = await self._memory_store.deny(item.id, reason=request.reason)
                        return {"id": item.id, "success": success}
            except Exception as e:
                LOG.error(f"Bulk {request.action} failed for {item.type} {item.id}: {e}")
                return {"id": item.id, "success": False, "error": str(e)}

        tasks = [handle_item(item) for item in request.items]
        for t in tasks:
            results.append(await t)
        
        return {
            "action": request.action,
            "results": results,
            "summary": {
                "total": len(request.items),
                "success": len([r for r in results if r.get("success")]),
                "failed": len([r for r in results if not r.get("success")])
            }
        }

    async def get_stats(self) -> ApprovalStats:
        """Get aggregate metrics for the approval system."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            
            action_stats = await unit.repositories.approvals.get_stats()
            
            # Simple aggregation for memory proposals
            assert unit.connection is not None
            cursor = await unit.connection.execute(
                "SELECT COUNT(*) as count FROM memory_proposals WHERE status = 'pending'"
            )
            row = await cursor.fetchone()
            memory_pending = int(row["count"]) if row else 0
            
            # Risk counts
            assert unit.connection is not None
            cursor = await unit.connection.execute(
                "SELECT risk_level, COUNT(*) as count FROM approval_requests WHERE status = 'pending' GROUP BY risk_level"
            )
            risk_rows = await cursor.fetchall()
            by_risk = {str(r["risk_level"]): int(r["count"]) for r in risk_rows}
            # Memories are always considered 'low' or 'medium' for v1 stats
            by_risk["low"] = by_risk.get("low", 0) + memory_pending

            total_decided = int(action_stats.get("approved", 0)) + int(action_stats.get("denied", 0))
            approval_rate = float(action_stats["approved"]) / total_decided if total_decided > 0 else None

            return ApprovalStats(
                pending_count=int(action_stats.get("pending", 0)) + memory_pending,
                avg_decision_time_sec=action_stats.get("avg_time"),
                by_risk=by_risk,
                approval_rate=approval_rate
            )
