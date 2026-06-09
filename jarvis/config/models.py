"""Typed Jarvis configuration models."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class PathsConfig(BaseModel):
    config_dir: Path = Path(".jarvis/dev")


class ModelsConfig(BaseModel):
    primary_provider: str = "openrouter"
    fallback_provider: str = "ollama"
    default_model: str = "openai/gpt-4.1"
    local_model: str = "llama3.1"
    request_timeout_seconds: int = 120


class MemoryConfig(BaseModel):
    database_path: Path = Path(".jarvis/dev/memory.sqlite")
    short_term_retention_days: int = 30


class ApprovalsConfig(BaseModel):
    mode: str = "terminal_queue"
    reusable_rules_enabled: bool = False


class TasksConfig(BaseModel):
    max_active_tasks: int = 1
    default_command_timeout_seconds: int = 120


class SecurityConfig(BaseModel):
    localhost_only: bool = True
    api_token_enabled: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"


class JarvisConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    approvals: ApprovalsConfig = Field(default_factory=ApprovalsConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    def public_dict(self) -> dict[str, object]:
        """Return non-secret config suitable for API and CLI output."""
        return self.model_dump(mode="json")

