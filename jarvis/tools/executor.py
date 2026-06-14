"""Service for safe and validated tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from pydantic import ValidationError

from jarvis.approvals.broker import ApprovalBroker
from jarvis.tools.base import ToolResult
from jarvis.tools.registry import ToolRegistry

LOG = logging.getLogger(__name__)


class ToolExecutor:
    """Orchestrates tool lookup, validation, and execution."""

    def __init__(self, registry: ToolRegistry, approval_broker: ApprovalBroker | None = None) -> None:
        self._registry = registry
        self._approval_broker = approval_broker

    async def execute_step(
        self, 
        tool_name: str, 
        input_json: str | None = None,
        approval_request_id: str | None = None,
        **context: Any
    ) -> ToolResult:
        """
        Execute a tool with the provided JSON input and context.
        
        Args:
            tool_name: The registered name of the tool.
            input_json: A JSON string containing tool arguments.
            approval_request_id: Optional ID of the approval request for this action.
            context: Additional environmental data (e.g., workspaces, unit_of_work).
            
        Returns:
            ToolResult containing success status and data/error.
        """
        try:
            # 1. Resolve tool
            try:
                tool = self._registry.get(tool_name)
            except KeyError:
                return ToolResult(success=False, error=f"Tool not found: {tool_name}")

            # 2. Security Verification (Action Hash)
            if approval_request_id and self._approval_broker:
                # We verify the hash of the action_json against the approved request.
                # If it doesn't match or isn't approved, we block.
                verified = await self._approval_broker.verify_hash(
                    approval_request_id, input_json or "{}"
                )
                if not verified:
                    LOG.error(f"Security Block: Hash mismatch or unapproved action for tool {tool_name}")
                    return ToolResult(
                        success=False, 
                        error=f"Security verification failed for approval {approval_request_id}."
                    )

            # 3. Parse input
            inputs: dict[str, Any] = {}
            if input_json:
                try:
                    inputs = json.loads(input_json)
                except json.JSONDecodeError as e:
                    return ToolResult(success=False, error=f"Invalid JSON input: {e}")

            # 4. Validate input
            schema = tool.get_input_schema()
            try:
                validated_inputs = schema(**inputs).model_dump()
            except ValidationError as e:
                return ToolResult(success=False, error=f"Input validation failed: {e}")

            # 5. Execute with Timeout
            LOG.info(f"Executing tool '{tool_name}' (category: {tool.category.value}, timeout: {tool.timeout_seconds}s)")
            
            start_time = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    tool.execute(**validated_inputs, **context),
                    timeout=tool.timeout_seconds
                )
                execution_time = time.perf_counter() - start_time
                
                # Update result with execution time
                return ToolResult(
                    success=result.success,
                    data=result.data,
                    error=result.error,
                    execution_time=execution_time,
                    timeout_occurred=False
                )
            except asyncio.TimeoutError:
                execution_time = time.perf_counter() - start_time
                LOG.warning(f"Tool '{tool_name}' timed out after {tool.timeout_seconds}s")
                return ToolResult(
                    success=False,
                    error=f"Tool execution timed out after {tool.timeout_seconds} seconds.",
                    execution_time=execution_time,
                    timeout_occurred=True
                )

        except Exception as e:
            LOG.exception(f"Unexpected error executing tool '{tool_name}': {e}")
            return ToolResult(success=False, error=f"Internal tool error: {str(e)}")
