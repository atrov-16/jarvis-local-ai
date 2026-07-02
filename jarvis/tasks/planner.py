"""Planner service for breaking down user requests into discrete steps."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from jarvis.models.router import ModelRouter
from jarvis.models.schemas import Message, ModelRequest
from jarvis.tools.registry import ToolRegistry


class PlannedStep(BaseModel):
    title: str = Field(..., description="Short summary of the step.")
    description: str = Field(..., description="Detailed explanation of what this step is intended to do.")
    tool_name: str | None = Field(None, description="The name of the native tool to execute.")
    input_json: str | None = Field(None, description="The arguments for the tool as a JSON string.")
    requires_approval: bool = Field(False, description="Whether this step requires human sign-off.")


class PlannedTask(BaseModel):
    title: str = Field(..., description="A short, descriptive title for the overall task.")
    steps: list[PlannedStep] = Field(..., description="The sequence of steps to fulfill the task.")


PLANNER_PROMPT = """You are the Planning Brain of Jarvis, a local-first AI assistant.
Your goal is to break down a user's request into a sequence of discrete, executable steps.

Rules:
1. Be concise but thorough.
2. If the request is simple (e.g., "Hello"), a single step is enough.
3. For complex tasks, break them down logically (e.g., Read -> Analyze -> Modify -> Verify).
4. Identify which steps are risky and require approval (e.g., file writes, command execution).
5. Output ONLY a valid JSON object matching the requested schema.
6. Decision Precedence: If [Decision] memories are provided in the context, treat them as absolute, non-negotiable rules.
7. Conflict Resolution: If injected memories conflict, prefer [Decision] over [Preference] or [Fact], and prefer (Project) scoped memories over (Global) ones.
8. Use ONLY the tools listed below.
9. Do not invent tool names.
10. If no listed tool is appropriate, leave `tool_name` null.

Schema:
{
  "title": "Task summary",
  "steps": [
    {
      "title": "Step summary",
      "description": "Step detail",
      "tool_name": "Optional tool name",
      "input_json": "Optional tool input JSON string",
      "requires_approval": true/false
    }
  ]
}
"""

class Planner:
    def __init__(self, model_router: ModelRouter, tool_registry: ToolRegistry) -> None:
        self._model_router = model_router
        self._tool_registry = tool_registry

    async def create_plan(self, user_request: str, memory_context: str = "") -> PlannedTask:
        """Use the LLM to generate a plan for the user request."""
        system_prompt = PLANNER_PROMPT
        
        # Inject tool list
        tools_info = []
        for name, tool in self._tool_registry._tools.items():
            schema = tool.get_input_schema().model_json_schema()
            tools_info.append(
                f"- Name: {name}\n  Category: {tool.category.value}\n  Description: {tool.description}\n  Schema: {json.dumps(schema)}"
            )
        
        system_prompt += "\n\nAvailable Tools:\n" + "\n".join(tools_info)
        
        if memory_context:
            system_prompt += f"\n\n{memory_context}"

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=f"Request: {user_request}\n\nPlan:"),
        ]
        
        request = ModelRequest(
            messages=messages,
            temperature=0.0,  # Deterministic planning
            max_tokens=2048,
        )

        
        response = await self._model_router.complete(request)
        content = response.message.content.strip()
        
        # Basic JSON extraction if LLM wraps it in code blocks
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
            
        try:
            plan_dict = json.loads(content)
            return PlannedTask(**plan_dict)
        except (json.JSONDecodeError, ValueError) as e:
            # Fallback or re-raise with context
            raise ValueError(f"Failed to parse planner output as JSON: {e}\nRaw output: {content}")
