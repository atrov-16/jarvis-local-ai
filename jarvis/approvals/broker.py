"""Approval broker for centralized security gatekeeping."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from jarvis.approvals.models import ApprovalActionType, ProposedAction, RiskLevel
from jarvis.core.event_bus import EventBus
from jarvis.storage.unit_of_work import UnitOfWork
from jarvis.tools.base import ToolCategory

LOG = logging.getLogger(__name__)


class ApprovalBroker:
    def __init__(self, uow: UnitOfWork, event_bus: EventBus) -> None:
        self._uow = uow
        self._event_bus = event_bus

    async def get_risk_level(
        self, 
        action: ProposedAction, 
        tool_category: ToolCategory | None = None,
        is_outside_workspace: bool = False
    ) -> RiskLevel:
        """Determine the risk level of an action."""
        if action.action_type == ApprovalActionType.PLAN:
            return RiskLevel.MEDIUM
            
        if action.action_type == ApprovalActionType.EXTERNAL:
            return RiskLevel.HIGH

        if tool_category == ToolCategory.READ_ONLY:
            return RiskLevel.HIGH if is_outside_workspace else RiskLevel.LOW
            
        if tool_category == ToolCategory.MUTATING:
            return RiskLevel.HIGH
            
        if tool_category == ToolCategory.DESTRUCTIVE:
            return RiskLevel.CRITICAL
            
        if tool_category == ToolCategory.SYSTEM:
            return RiskLevel.CRITICAL
            
        return RiskLevel.MEDIUM

    def compute_hash(self, action_type: str, action_json: str, context_id: str | None) -> str:
        """Generate a stable hash for an action payload."""
        payload = {
            "type": action_type,
            "json": action_json,
            "context": context_id or ""
        }
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def create_request(self, action: ProposedAction) -> str:
        """Create a new approval request."""
        action_hash = self.compute_hash(action.action_type, action.action_json, action.context_id)
        
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            approval_id = await unit.repositories.approvals.insert(
                task_id=action.task_id,
                step_id=action.step_id,
                action_type=action.action_type,
                risk_level=action.risk_level,
                summary=action.summary,
                action_json=action.action_json,
                action_hash=action_hash,
                context_id=action.context_id,
            )
            
            await unit.repositories.audit.insert(
                actor="system",
                action_type="approval.request_created",
                summary=f"Approval requested for {action.action_type}: {action.summary}",
                target=approval_id,
                details={
                    "risk_level": action.risk_level,
                    "action_type": action.action_type,
                },
                task_id=action.task_id,
                approval_request_id=approval_id,
            )
            
        await self._event_bus.publish("approval.created", {"id": approval_id, "summary": action.summary})
        return approval_id

    async def verify_hash(self, approval_id: str, action_json: str) -> bool:
        """Verify that the current action matches the approved hash."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            request = await unit.repositories.approvals.get(approval_id)
            if not request:
                return False
                
            current_hash = self.compute_hash(request["action_type"], action_json, request["context_id"])
            if current_hash != request["action_hash"]:
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="security.hash_mismatch",
                    summary=f"Security Alert: Hash mismatch for approval {approval_id}",
                    target=approval_id,
                    details={
                        "expected": request["action_hash"],
                        "actual": current_hash
                    },
                    task_id=request["task_id"],
                    approval_request_id=approval_id,
                )
                return False
                
            return request["status"] == "approved"

    async def approve(self, approval_id: str, decided_by: str = "user", reason: str | None = None) -> bool:
        """Mark a request as approved."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            success = await unit.repositories.approvals.update_status(
                approval_id, "approved", decided_by=decided_by, reason=reason
            )
            if success:
                request = await unit.repositories.approvals.get(approval_id)
                await unit.repositories.audit.insert(
                    actor=decided_by,
                    action_type="approval.approved",
                    summary=f"Approved action: {request['summary'] if request else approval_id}",
                    target=approval_id,
                    details={"reason": reason},
                    task_id=request["task_id"] if request else None,
                    approval_request_id=approval_id,
                )
                await self._event_bus.publish("approval.approved", {"id": approval_id})
            return success

    async def deny(self, approval_id: str, decided_by: str = "user", reason: str | None = None) -> bool:
        """Mark a request as denied."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            success = await unit.repositories.approvals.update_status(
                approval_id, "denied", decided_by=decided_by, reason=reason
            )
            if success:
                request = await unit.repositories.approvals.get(approval_id)
                await unit.repositories.audit.insert(
                    actor=decided_by,
                    action_type="approval.denied",
                    summary=f"Denied action: {request['summary'] if request else approval_id}",
                    target=approval_id,
                    details={"reason": reason},
                    task_id=request["task_id"] if request else None,
                    approval_request_id=approval_id,
                )
                await self._event_bus.publish("approval.denied", {"id": approval_id})
            return success
