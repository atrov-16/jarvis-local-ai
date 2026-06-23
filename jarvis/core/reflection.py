"""Reflection Engine for Jarvis Phase 9."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from jarvis.core.event_bus import EventBus
from jarvis.memory.store import MemoryStore
from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelRequest
from jarvis.storage.unit_of_work import UnitOfWork

if TYPE_CHECKING:
    pass

LOG = logging.getLogger(__name__)

REFLECTION_PROMPT = """You are the Reflection Brain of Jarvis, a local-first AI assistant.
Your goal is to analyze a completed task trace and identify high-signal "Fact" or "Preference" memories that should be saved for future tasks.

Memories types to extract:
1. Fact: Durable knowledge about the user's environment, project, or tools (e.g., "The project uses Python 3.12", "The main entry point is main.py").
2. Preference: Patterns in how the user wants things done (e.g., "The user prefers detailed commit messages", "The user likes code documented with docstrings").

Rules:
1. Be conservative. Only propose memories you are highly confident in.
2. Avoid duplicates. If the memory is already covered in the provided context, skip it.
3. Keep content concise and atomic. One fact or preference per entry.
4. Assign a confidence_score (0.0 to 1.0).
5. Output ONLY a valid JSON object matching the requested schema.
6. For V1, DO NOT propose "Decision" memories. Focus ONLY on Fact and Preference.

Trace Context:
{trace_context}

Existing Memories:
{existing_memories}

Schema:
{{
  "proposals": [
    {{
      "memory_type": "fact" | "preference",
      "content": "The memory content",
      "reason": "Why this is important to save",
      "confidence_score": 0.95,
      "tags": ["tag1", "tag2"],
      "importance": 0.7
    }}
  ]
}}
"""

class ReflectionProposal(BaseModel):
    memory_type: str
    content: str
    reason: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5


class ReflectionResult(BaseModel):
    proposals: list[ReflectionProposal]


class TraceAggregator:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def aggregate(self, task_id: str) -> str:
        """Build a chronological trace of the task execution."""
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            if not task:
                return "Task not found."

            steps = await unit.repositories.tasks.list_steps(task_id)
            events = await unit.repositories.tasks.list_events(task_id)
            
            trace = f"Task: {task['title']}\n"
            trace += f"Request: {task['user_request']}\n\n"
            
            trace += "--- Execution Steps ---\n"
            for step in steps:
                trace += f"Step {step['step_index']}: {step['title']} ({step['status']})\n"
                if step.get("tool_name"):
                    trace += f"  Tool: {step['tool_name']}\n"
                    trace += f"  Input: {step['input_json']}\n"
                if step.get("output_json"):
                    trace += f"  Output: {step['output_json']}\n"
                if step.get("error"):
                    trace += f"  Error: {step['error']}\n"
                trace += "\n"

            trace += "--- Audit Log ---\n"
            # Fetch related audit entries
            assert unit.connection is not None
            cursor = await unit.connection.execute(
                "SELECT * FROM audit_log WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,)
            )
            audit_rows = await cursor.fetchall()
            for row in audit_rows:
                row_dict = dict(row)
                trace += f"[{row_dict['created_at']}] {row_dict['actor']}: {row_dict['action_type']} - {row_dict['summary']}\n"
                if row_dict.get("details_json"):
                    trace += f"  Details: {row_dict['details_json']}\n"

            return trace


class ReflectionService:
    def __init__(
        self, 
        uow: UnitOfWork, 
        event_bus: EventBus, 
        model_router: ModelRouter,
        memory_store: MemoryStore
    ) -> None:
        self._uow = uow
        self._event_bus = event_bus
        self._model_router = model_router
        self._memory_store = memory_store
        self._listener_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._aggregator = TraceAggregator(uow)
        self._semaphore = asyncio.Semaphore(2)  # Throttling: max 2 concurrent reflections

    async def start(self) -> None:
        if self._listener_task is None:
            self._listener_task = asyncio.create_task(self._listener_loop())
            LOG.info("ReflectionService started.")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._listener_task:
            self._listener_task.cancel()
            self._listener_task = None
        LOG.info("ReflectionService stopped.")

    async def _listener_loop(self) -> None:
        subscription = self._event_bus.subscribe(max_queue_size=100)
        try:
            while not self._stop_event.is_set():
                event = await subscription.get()
                if event.type == "task.completed":
                    task_id = str(event.payload.get("task_id"))
                    project_id = str(event.payload.get("project_id")) if event.payload.get("project_id") else None
                    if task_id:
                        # Process in background with throttling
                        asyncio.create_task(self._safe_reflect(task_id, project_id))
        except asyncio.CancelledError:
            pass
        finally:
            await subscription.close()

    async def _safe_reflect(self, task_id: str, project_id: str | None, retries: int = 2) -> None:
        """Throttled wrapper for reflection with retry logic."""
        async with self._semaphore:
            for attempt in range(retries + 1):
                try:
                    await self.reflect_on_task(task_id, project_id)
                    return
                except Exception as e:
                    if attempt < retries:
                        wait = 2 ** attempt
                        LOG.warning(f"Reflection attempt {attempt + 1} failed for {task_id}, retrying in {wait}s: {e}")
                        await asyncio.sleep(wait)
                    else:
                        LOG.exception(f"Reflection failed for task {task_id} after {retries} retries.")

    async def reflect_on_task(self, task_id: str, project_id: str | None = None) -> None:
        """Analyze a task and propose memories if it meets thresholds."""
        LOG.info(f"Checking reflection thresholds for task {task_id}")
        
        if not await self._is_nontrivial(task_id):
            LOG.info(f"Task {task_id} is trivial, skipping reflection.")
            return

        trace_context = await self._aggregator.aggregate(task_id)
        
        # Targeted Context Retrieval: Search for memories related to the task request and trace
        # Instead of empty string "", we use the task's request and tool names for better signal
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            task = await unit.repositories.tasks.get(task_id)
            search_query = task["user_request"] if task else ""
            
        existing_context, _ = await self._memory_store.get_planner_context(search_query, project_id=project_id)
        
        messages = [
            Message(role="system", content=REFLECTION_PROMPT.format(
                trace_context=trace_context,
                existing_memories=existing_context or "None"
            )),
            Message(role="user", content="Analyze the trace and propose any new Fact or Preference memories."),
        ]
        
        request = ModelRequest(messages=messages, temperature=0.0, max_tokens=4096)
        response = await self._model_router.complete(request)
        content = response.message.content.strip()
        
        # JSON extraction
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
            
        result = ReflectionResult.model_validate_json(content)
        
        for proposal in result.proposals:
            if proposal.memory_type.lower() not in ("fact", "preference"):
                LOG.warning(f"Reflector proposed disallowed memory type: {proposal.memory_type}")
                continue

            # Lightweight Deduplication: Check if a similar memory exists
            if await self._is_duplicate(proposal.content, project_id):
                LOG.info(f"Skipping duplicate proposal: {proposal.content[:50]}...")
                continue

            await self._memory_store.propose(
                project_id=project_id,
                task_id=task_id,
                memory_type=proposal.memory_type.lower(),
                proposed_content=proposal.content,
                proposed_tags=proposal.tags,
                reason=proposal.reason,
                importance=proposal.importance,
                confidence_score=proposal.confidence_score,
                source_ids=[task_id],
                metadata={
                    "engine": "jarvis-reflection-v1",
                    "reflected_at": datetime.now(UTC).isoformat(),
                    "task_id": task_id,
                    "search_query": search_query
                }
            )
            LOG.info(f"Proposed {proposal.memory_type} from task {task_id}")

    async def _is_duplicate(self, content: str, project_id: str | None) -> bool:
        """Check for near-duplicates using simple keyword overlap or exact match."""
        # V1: Simple search and check first 5 results for high overlap
        results = await self._memory_store.search(content, project_id=project_id, limit=5)
        for r in results:
            # Exact or near-exact match (case-insensitive)
            if content.lower().strip() == r.content.lower().strip():
                return True
            # High overlap check (very naive but effective for many LLM-generated facts)
            if len(content) > 20 and r.content.lower() in content.lower():
                return True
        return False

    async def _is_nontrivial(self, task_id: str) -> bool:
        """
        Thresholds for non-trivial tasks:
        1. At least 1 tool call.
        2. OR more than 2 steps.
        """
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            steps = await unit.repositories.tasks.list_steps(task_id)
            
            if len(steps) > 2:
                return True
                
            for step in steps:
                if step.get("tool_name"):
                    return True
                    
            return False
