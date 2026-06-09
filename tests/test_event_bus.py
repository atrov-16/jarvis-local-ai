from __future__ import annotations

import pytest

from jarvis.core.event_bus import EventBus


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

