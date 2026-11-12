from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.api.http import create_app
from jarvis.config.models import JarvisConfig, MemoryConfig
from jarvis.config.secrets import SecretManager
from jarvis.core.event_bus import EventBus, EventSubscription, SubscriptionClosedError
from jarvis.core.events import create_event

TOKEN = "DUMMY-event-bus-token-41c7"


async def test_event_bus_publish_subscribe() -> None:
    bus = EventBus()
    subscription = bus.subscribe()

    await bus.publish("phase0.test", {"ok": True})
    event = await subscription.get()
    await subscription.close()

    assert event.type == "phase0.test"
    assert event.payload == {"ok": True}


async def test_event_bus_rejects_non_json_payload() -> None:
    bus = EventBus()

    with pytest.raises(ValueError):
        await bus.publish("phase0.bad", {"bad": object()})


async def _publish_many(bus: EventBus, count: int) -> None:
    for n in range(count):
        await bus.publish("test.n", {"n": n})


async def _drain(subscription: EventSubscription, count: int) -> list[object]:
    return [(await asyncio.wait_for(subscription.get(), 1)).payload["n"] for _ in range(count)]


# --- A stalled subscriber must not hold up publishers ---------------------------------------


async def test_a_stalled_subscriber_does_not_block_publish_or_other_subscribers() -> None:
    bus = EventBus()
    stalled = bus.subscribe(max_queue_size=2)  # nobody ever reads from it
    healthy = bus.subscribe(max_queue_size=100)

    # Before this change the third publish waited on the full queue for ever.
    await asyncio.wait_for(_publish_many(bus, 10), timeout=2)

    assert await _drain(healthy, 10) == list(range(10))  # all events, in order
    assert stalled.dropped == 8 and bus.dropped_events == 8  # 10 published, 2 fit
    assert healthy.dropped == 0


async def test_a_full_subscriber_keeps_the_oldest_events_and_recovers_once_it_reads() -> None:
    bus = EventBus()
    slow = bus.subscribe(max_queue_size=2)

    await _publish_many(bus, 5)
    assert await _drain(slow, 2) == [0, 1]  # nothing was reordered, the overflow was dropped

    await bus.publish("test.n", {"n": 99})  # room again: delivered normally
    assert await _drain(slow, 1) == [99]
    assert slow.dropped == 3 and not slow.disconnected


async def test_drop_policy_never_disconnects() -> None:
    bus = EventBus()
    slow = bus.subscribe(max_queue_size=1)

    await _publish_many(bus, 20)

    assert slow in bus._subscribers and not slow.disconnected
    assert await _drain(slow, 1) == [0]
    await bus.publish("test.n", {"n": 20})
    assert await _drain(slow, 1) == [20]  # still subscribed, still receives


async def test_publish_without_subscribers_still_returns_the_event() -> None:
    event = await EventBus().publish("test.alone", {"a": 1})

    assert event.type == "test.alone" and event.payload == {"a": 1}


# --- Disconnecting a subscriber that must not silently miss events --------------------------


async def test_disconnect_policy_removes_the_slow_subscriber_and_tells_it() -> None:
    bus = EventBus()
    slow = bus.subscribe(max_queue_size=2, name="gui", on_overflow="disconnect")
    healthy = bus.subscribe(max_queue_size=100)

    await asyncio.wait_for(_publish_many(bus, 5), timeout=2)

    assert slow.disconnected and slow not in bus._subscribers
    assert healthy in bus._subscribers
    assert await _drain(healthy, 5) == list(range(5))  # the others are unaffected
    # What it had buffered is still delivered, then it is told it missed events.
    assert await _drain(slow, 2) == [0, 1]
    with pytest.raises(SubscriptionClosedError, match="gui"):
        await asyncio.wait_for(slow.get(), 1)
    assert slow.dropped == 1  # it was removed at the first event it could not take
    assert bus.dropped_events == 1


async def test_a_disconnected_subscriber_receives_nothing_further() -> None:
    bus = EventBus()
    slow = bus.subscribe(max_queue_size=1, on_overflow="disconnect")

    await _publish_many(bus, 3)
    await _drain(slow, 1)
    await _publish_many(bus, 3)

    with pytest.raises(SubscriptionClosedError):
        await slow.get()
    assert bus.dropped_events == 1  # not counted again once it is gone


async def test_closing_a_subscription_unsubscribes_it_and_is_idempotent() -> None:
    bus = EventBus()
    subscription = bus.subscribe(max_queue_size=1)

    await subscription.close()
    await subscription.close()
    await _publish_many(bus, 5)

    assert len(bus._subscribers) == 0
    assert subscription.dropped == 0 and bus.dropped_events == 0


# --- Diagnostics ----------------------------------------------------------------------------


async def test_drops_are_logged_on_the_first_and_then_every_hundredth(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    bus.subscribe(max_queue_size=1, name="reflection")

    with caplog.at_level(logging.WARNING, logger="jarvis.core.event_bus"):
        await _publish_many(bus, 250)  # 1 delivered, 249 dropped: logged at drops 1, 100, 200

    messages = [r.getMessage() for r in caplog.records if r.name == "jarvis.core.event_bus"]
    assert len(messages) == 3
    assert all("reflection" in m for m in messages)
    assert "1 event(s) dropped" in messages[0]


async def test_disconnect_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    bus.subscribe(max_queue_size=1, name="websocket", on_overflow="disconnect")

    with caplog.at_level(logging.WARNING, logger="jarvis.core.event_bus"):
        await _publish_many(bus, 10)

    messages = [r.getMessage() for r in caplog.records if r.name == "jarvis.core.event_bus"]
    assert len(messages) == 1 and "disconnected" in messages[0]


# --- /v1/events: a client that falls behind is disconnected, not silently skipped -----------


class _BehindOnSubscribe(EventBus):
    """Hands the WebSocket its subscription already overflowed, as if the client had not been
    reading while events were published."""

    def subscribe(self, max_queue_size: int = 100, **kwargs):  # type: ignore[no-untyped-def]
        subscription = super().subscribe(max_queue_size=1, **kwargs)
        if kwargs.get("name") == "websocket":
            subscription.deliver(create_event("test.buffered", {"n": 1}))
            subscription.deliver(create_event("test.lost", {"n": 2}))  # does not fit
        return subscription


def test_websocket_client_that_fell_behind_is_closed_with_try_again_later(tmp_path: Path) -> None:
    app = create_app(
        config=JarvisConfig(memory=MemoryConfig(database_path=tmp_path / "events.sqlite")),
        secret_manager=SecretManager({"JARVIS_API_TOKEN": TOKEN}, use_keyring=False),
        event_bus=_BehindOnSubscribe(),
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}

    with TestClient(app) as client:
        with client.websocket_connect("/v1/events", headers=headers) as websocket:
            assert websocket.receive_json()["type"] == "test.buffered"  # what it had arrives
            with pytest.raises(WebSocketDisconnect) as excinfo:
                websocket.receive_json()

    assert excinfo.value.code == 1013
