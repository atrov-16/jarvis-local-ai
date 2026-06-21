"""Trace Explorer Service for Phase 10."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from jarvis.api.schemas import TaskSummaryResponse, TaskTraceResponse, TraceEntry
from jarvis.logging.redaction import redact_text
from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelRequest
from jarvis.storage.unit_of_work import UnitOfWork

LOG = logging.getLogger(__name__)

SUMMARY_PROMPT_TEMPLATE = """You are the Trace Analyst of Jarvis.
Analyze the following chronological trace of a completed task and provide a concise (2-3 sentence) summary of what happened, the key decisions made, and the final outcome.

Task Request: %s
Status: %s

Trace:
%s

Provide ONLY the summary text. No preamble.
"""

class TraceService:
    def __init__(self, uow: UnitOfWork, model_router: ModelRouter | None = None) -> None:
        self._uow = uow
        self._model_router = model_router

    async def get_task_trace(self, task_id: str, include_system: bool = False) -> TaskTraceResponse:
        """Aggregate chronological trace from all repositories."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            
            # 1. Fetch Task and Steps
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise ValueError(f"Task not found: {task_id}")
            
            steps = await unit.repositories.tasks.list_steps(task_id)
            step_map = {s["id"]: s for s in steps}
            
            # 2. Fetch Events
            events = await unit.repositories.tasks.list_events(task_id)
            
            # 3. Fetch Audit Log
            cursor = await unit.connection.execute(
                "SELECT * FROM audit_log WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,)
            )
            audit_rows = await cursor.fetchall()
            
            # 4. Fetch Approvals
            cursor = await unit.connection.execute(
                "SELECT * FROM approval_requests WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,)
            )
            approval_rows = await cursor.fetchall()
            approval_map = {a["id"]: a for a in approval_rows}

            entries: list[TraceEntry] = []
            
            # Convert Events to TraceEntries
            for e in events:
                payload = json.loads(str(e.get("payload_json", "{}")))
                entries.append(TraceEntry(
                    timestamp=str(e["created_at"]),
                    type="event",
                    actor="agent",
                    severity=str(e.get("severity", "info")),
                    summary=str(e["message"]),
                    details=payload,
                    step_id=str(e.get("step_id")) if e.get("step_id") else None,
                    correlation_id=str(e.get("correlation_id")) if e.get("correlation_id") else None
                ))
            
            # Convert Audit rows to TraceEntries
            for a in audit_rows:
                details = json.loads(str(a.get("details_json", "{}")))
                entries.append(TraceEntry(
                    timestamp=str(a["created_at"]),
                    type="audit",
                    actor=str(a["actor"]),
                    severity="info",
                    summary=str(a["summary"]),
                    details=details,
                    correlation_id=str(a.get("approval_request_id")) if a.get("approval_request_id") else None
                ))
                
            # Convert Approvals to TraceEntries (redundant but helps with correlation)
            for app_id, app in approval_map.items():
                entries.append(TraceEntry(
                    timestamp=str(app["created_at"]),
                    type="approval",
                    actor="system",
                    severity="warning",
                    summary=f"Approval Request: {app['summary']}",
                    details={
                        "risk_level": app["risk_level"],
                        "action_type": app["action_type"],
                        "status": app["status"],
                        "decision_reason": app.get("decision_reason")
                    },
                    step_id=str(app.get("step_id")) if app.get("step_id") else None,
                    correlation_id=app_id
                ))

            # Deterministic Ordering: Timestamp ASC, then ID (if available) or Type
            # We use created_at from entries
            entries.sort(key=lambda x: (x.timestamp, x.type, x.summary))
            
            if not include_system:
                # Filter out low-level noise if requested
                entries = [e for e in entries if e.type != "audit" or e.actor != "system"]

            return TaskTraceResponse(task_id=task_id, entries=entries)

    async def get_task_summary(self, task_id: str, secrets: list[str] = []) -> TaskSummaryResponse:
        """Generate or retrieve a high-level summary of the task."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                raise ValueError(f"Task not found: {task_id}")
            
            metadata = task.get("metadata", {})
            if "summary" in metadata:
                return TaskSummaryResponse(
                    task_id=task_id,
                    status=str(task["status"]),
                    summary=redact_text(str(metadata["summary"]), secrets),
                    outcome=metadata.get("outcome"),
                    tokens_used=metadata.get("tokens_used"),
                    wall_time=metadata.get("wall_time")
                )

        # Generate summary using LLM if possible
        if not self._model_router:
            return TaskSummaryResponse(task_id=task_id, status=str(task["status"]), summary="LLM router not available for summary generation.")

        trace = await self.get_task_trace(task_id, include_system=False)
        trace_text = "\n".join([
            f"[{e.timestamp}] {e.actor}: {e.summary}" 
            for e in trace.entries
        ])
        
        try:
            messages = [
                Message(role="system", content=SUMMARY_PROMPT_TEMPLATE % (
                    task.get("user_request", "").replace("%", "%%"),
                    task["status"],
                    trace_text.replace("%", "%%")
                )),
                Message(role="user", content="Summarize this task.")
            ]
            
            request = ModelRequest(messages=messages, temperature=0.0)
            response = await self._model_router.complete(request)
            summary = response.message.content.strip()
            summary = redact_text(summary, secrets)
            
            # Cache the summary
            async with self._uow.begin() as unit:
                assert unit.repositories is not None
                metadata["summary"] = summary
                metadata["outcome"] = "success" if task["status"] == "completed" else "failed"
                # Wall time calculation
                if task["started_at"] and task["completed_at"]:
                    start = datetime.fromisoformat(str(task["started_at"]))
                    end = datetime.fromisoformat(str(task["completed_at"]))
                    metadata["wall_time"] = f"{(end - start).total_seconds():.1f}s"
                
                await unit.repositories.tasks.update(task_id, metadata_json=json.dumps(metadata))
            
            return TaskSummaryResponse(
                task_id=task_id,
                status=str(task["status"]),
                summary=redact_text(summary, secrets),
                outcome=metadata.get("outcome"),
                wall_time=metadata.get("wall_time")
            )
        except Exception as e:
            LOG.exception(f"Failed to generate summary for task {task_id}: {e}")
            return TaskSummaryResponse(task_id=task_id, status=str(task["status"]), summary=f"Error generating summary: {e}")
