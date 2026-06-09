"""In-process async event bus."""

from __future__ import annotations

import asyncio

from jarvis.core.events import Event, create_event, ensure_json_payload


class EventSubscription:
    """A subscription to EventBus events."""

    def __init__(self, bus: EventBus, queue: asyncio.Queue[Event]) -> None:
        self._bus = bus
        self._queue = queue
        self._closed = False

    async def get(self) -> Event:
        return await self._queue.get()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.unsubscribe(self._queue)


class EventBus:
    """Simple in-process publish/subscribe event bus."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self, max_queue_size: int = 100) -> EventSubscription:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue_size)
        self._subscribers.add(queue)
        return EventSubscription(self, queue)

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, event_type: str, payload: dict[str, object] | None = None) -> Event:
        event = create_event(event_type, payload or {})
        for queue in list(self._subscribers):
            await queue.put(event)
        return event

    async def publish_event(self, event: Event) -> Event:
        ensure_json_payload(event.payload)
        for queue in list(self._subscribers):
            await queue.put(event)
        return event

