"""Event models."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, object] = Field(default_factory=dict)


def create_event(event_type: str, payload: dict[str, object]) -> Event:
    ensure_json_payload(payload)
    return Event(type=event_type, payload=payload)


def ensure_json_payload(payload: dict[str, object]) -> None:
    try:
        json.dumps(payload)
    except TypeError as exc:
        raise ValueError("Event payload must be JSON-serializable.") from exc

