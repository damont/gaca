"""Unit tests for the in-process event bus (the Redis-swap-ready pub/sub)."""
import asyncio

from gaca_api.bus import InProcessEventBus


def test_publish_reaches_subscriber():
    bus = InProcessEventBus()

    async def main():
        sub = bus.subscribe("c1")
        pending = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0.01)  # let the generator register its queue
        await bus.publish("c1", "hello")
        got = await asyncio.wait_for(pending, 1)
        await sub.aclose()
        return got

    assert asyncio.run(main()) == "hello"


def test_publish_without_subscriber_is_dropped():
    bus = InProcessEventBus()
    # Nobody listening -> ephemeral drop, no error (matches Redis pub/sub).
    asyncio.run(bus.publish("c1", "x"))


def test_two_subscribers_both_receive():
    bus = InProcessEventBus()

    async def main():
        s1, s2 = bus.subscribe("c"), bus.subscribe("c")
        t1 = asyncio.ensure_future(s1.__anext__())
        t2 = asyncio.ensure_future(s2.__anext__())
        await asyncio.sleep(0.01)
        await bus.publish("c", "hi")
        result = await asyncio.wait_for(asyncio.gather(t1, t2), 1)
        await s1.aclose()
        await s2.aclose()
        return result

    assert asyncio.run(main()) == ["hi", "hi"]


def test_unsubscribe_cleans_up_channel():
    bus = InProcessEventBus()

    async def main():
        sub = bus.subscribe("c")
        pending = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0.01)
        await bus.publish("c", "x")
        await asyncio.wait_for(pending, 1)
        await sub.aclose()  # runs the generator's finally -> discard queue
        return bus._subscribers.get("c")

    assert asyncio.run(main()) is None
