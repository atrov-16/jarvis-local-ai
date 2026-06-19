"""Base definitions for Jarvis tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel


class ToolCategory(str, Enum):
    """Categories for tool classification and policy enforcement."""
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    SYSTEM = "system"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ToolResult:
    """Standardized output from a tool execution."""
    success: bool
    data: Any = None
    error: str | None = None
    execution_time: float | None = None
    timeout_occurred: bool = False


class BaseTool(ABC):
    """Abstract base class for all Jarvis tools."""

    def __init__(
        self,
        name: str,
        description: str,
        category: ToolCategory,
        timeout_seconds: int = 60,
    ) -> None:
        self.name = name
        self.description = description
        self.category = category
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    def get_input_schema(self) -> type[BaseModel]:
        """Return the Pydantic model for this tool's input."""
        pass

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Perform the tool's core logic."""
        pass
