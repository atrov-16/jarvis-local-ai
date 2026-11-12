"""In-process async event bus.

`publish` never waits for a subscriber. Each subscriber has a bounded queue; when it is full the
subscriber is too slow, and the bus either drops the event for that subscriber or disconnects
it (its choice, see `OverflowPolicy`). Either way the loss is counted and logged, and every
other subscriber still gets the event. Events are published from inside database transactions
and request handlers, so one stalled consumer must not be able to stall them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from jarvis.core.events import Event, create_event

LOG = logging.getLogger(__name__)

# "drop": the event is discarded for that subscriber, which stays subscribed.
# "disconnect": the subscriber is removed; after it has read what it already had buffered,
#   `get()` raises SubscriptionClosedError, so a consumer that must not miss events (a GUI)
#   learns that it did and can resynchronise instead of carrying on with a silent gap.
OverflowPolicy = Literal["drop", "disconnect"]

# After the first drop, a subscriber that keeps falling behind is logged every Nth drop only.
_LOG_EVERY_NTH_DROP = 100


class SubscriptionClosedError(Exception):
    """The bus disconnected this subscription because its consumer fell behind."""


class EventSubscription:
    """A subscription to EventBus events."""

    def __init__(
        self,
        bus: EventBus,
        max_queue_size: int,
        name: str = "",
        on_overflow: OverflowPolicy = "drop",
    ) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=max_queue_size)
        self._closed = False
        self._disconnected = False
        self._on_overflow = on_overflow
        self.name = name or "subscriber"
        self.dropped = 0  # events this subscription missed because its queue was full

    @property
    def disconnected(self) -> bool:
        """True once the bus has removed this subscription for being too slow."""
        return self._disconnected

    async def get(self) -> Event:
        if self._disconnected and self._queue.empty():
            raise SubscriptionClosedError(
                f"Event subscription '{self.name}' was disconnected for falling behind "
                f"({self.dropped} event(s) missed)."
            )
        return await self._queue.get()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus.unsubscribe(self)

    def deliver(self, event: Event) -> bool:
        """Offer an event without ever waiting. Called by the bus; False if it was missed."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._missed(event)
            return False
        return True

    def _missed(self, event: Event) -> None:
        self.dropped += 1
        if self._on_overflow == "disconnect":
            self._disconnected = True
            self._bus.unsubscribe(self)
            LOG.warning(
                f"Event subscriber '{self.name}' fell behind (queue full at {event.type}); "
                "disconnected."
            )
        elif self.dropped == 1 or self.dropped % _LOG_EVERY_NTH_DROP == 0:
            LOG.warning(
                f"Event subscriber '{self.name}' is falling behind: {self.dropped} event(s) "
                f"dropped so far (latest {event.type})."
            )


class EventBus:
    """Simple in-process publish/subscribe event bus."""

    def __init__(self) -> None:
        self._subscribers: set[EventSubscription] = set()
        # Events missed by subscribers because their queue was full, over the bus's lifetime.
        self.dropped_events = 0

    def subscribe(
        self,
        max_queue_size: int = 100,
        *,
        name: str = "",
        on_overflow: OverflowPolicy = "drop",
    ) -> EventSubscription:
        subscription = EventSubscription(self, max_queue_size, name, on_overflow)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        self._subscribers.discard(subscription)

    async def publish(self, event_type: str, payload: dict[str, object] | None = None) -> Event:
        event = create_event(event_type, payload or {})
        # Nothing below awaits: delivery cannot be held up by a subscriber, nor yield to
        # other tasks halfway through a publish.
        for subscription in list(self._subscribers):
            if not subscription.deliver(event):
                self.dropped_events += 1
        return event
