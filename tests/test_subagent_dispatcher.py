import asyncio
import unittest

from subagent_dispatcher import SubagentDispatcher


CHAIN = [
    {"url": "http://primary/v1/chat/completions",   "model": "m", "role": "primary"},
    {"url": "http://secondary/v1/chat/completions", "model": "m", "role": "secondary"},
    {"url": "http://fallback/v1/chat/completions",  "model": "m", "role": "fallback"},
]


class SubagentDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserve_returns_primary_when_idle(self):
        disp = SubagentDispatcher(CHAIN)
        ticket = await disp.reserve()
        self.assertEqual(await ticket.acquire(), "http://primary/v1/chat/completions")
        self.assertEqual(disp.in_flight["http://primary/v1/chat/completions"], 1)
        await ticket.release()
        self.assertEqual(disp.in_flight["http://primary/v1/chat/completions"], 0)

    async def test_reserve_falls_back_to_secondary_when_primary_busy(self):
        disp = SubagentDispatcher(CHAIN)
        first = await disp.reserve()
        await first.acquire()
        second = await disp.reserve()
        self.assertEqual(await second.acquire(), "http://secondary/v1/chat/completions")
        await first.release()
        await second.release()

    async def test_third_request_queues_when_both_busy(self):
        disp = SubagentDispatcher(CHAIN)
        a = await disp.reserve()
        b = await disp.reserve()
        await a.acquire()
        await b.acquire()
        c = await disp.reserve()
        self.assertEqual(len(disp.queue), 1)
        self.assertFalse(c._future.done())
        # Release primary → c should be assigned primary
        await a.release()
        self.assertEqual(await c.acquire(), "http://primary/v1/chat/completions")
        self.assertEqual(len(disp.queue), 0)
        await b.release()
        await c.release()

    async def test_release_prefers_primary_for_queued_waiter(self):
        disp = SubagentDispatcher(CHAIN)
        a = await disp.reserve()
        b = await disp.reserve()
        await a.acquire()
        await b.acquire()
        c = await disp.reserve()
        # Release secondary first — c should get secondary (the freed slot)
        await b.release()
        self.assertEqual(await c.acquire(), "http://secondary/v1/chat/completions")
        await a.release()
        await c.release()

    async def test_cancel_pending_removes_queued_ticket(self):
        disp = SubagentDispatcher(CHAIN)
        a = await disp.reserve()
        b = await disp.reserve()
        await a.acquire()
        await b.acquire()
        c = await disp.reserve()
        self.assertTrue(await c.cancel_pending())
        self.assertEqual(len(disp.queue), 0)
        # Releasing a doesn't hand the slot to the cancelled ticket
        await a.release()
        self.assertEqual(disp.in_flight["http://primary/v1/chat/completions"], 0)
        await b.release()

    async def test_fallback_url(self):
        disp = SubagentDispatcher(CHAIN)
        self.assertEqual(disp.fallback_url(), "http://fallback/v1/chat/completions")

    async def test_fallback_optional(self):
        disp = SubagentDispatcher(CHAIN[:2])  # no fallback entry
        self.assertIsNone(disp.fallback_url())

    async def test_requires_primary(self):
        with self.assertRaises(ValueError):
            SubagentDispatcher([
                {"url": "http://x/", "model": "m", "role": "secondary"},
            ])

    async def test_model_for_url(self):
        disp = SubagentDispatcher(CHAIN)
        self.assertEqual(disp.model_for("http://primary/v1/chat/completions"), "m")
        self.assertIsNone(disp.model_for("http://unknown/"))


class SubagentDispatcherCapacityTests(unittest.IsolatedAsyncioTestCase):
    CHAIN = [
        {"url": "http://primary/v1/chat/completions",   "model": "m", "role": "primary",   "capacity": 2},
        {"url": "http://secondary/v1/chat/completions", "model": "m", "role": "secondary"},
        {"url": "http://fallback/v1/chat/completions",  "model": "m", "role": "fallback"},
    ]

    async def test_primary_serves_up_to_capacity_before_secondary(self):
        disp = SubagentDispatcher(self.CHAIN)
        a = await disp.reserve()
        b = await disp.reserve()
        self.assertEqual(await a.acquire(), "http://primary/v1/chat/completions")
        self.assertEqual(await b.acquire(), "http://primary/v1/chat/completions")
        self.assertEqual(disp.in_flight["http://primary/v1/chat/completions"], 2)
        c = await disp.reserve()
        self.assertEqual(await c.acquire(), "http://secondary/v1/chat/completions")
        await a.release()
        await b.release()
        await c.release()

    async def test_fourth_request_queues_when_primary_full_and_secondary_busy(self):
        disp = SubagentDispatcher(self.CHAIN)
        a = await disp.reserve()
        b = await disp.reserve()
        c = await disp.reserve()
        await a.acquire()
        await b.acquire()
        await c.acquire()
        d = await disp.reserve()
        self.assertEqual(len(disp.queue), 1)
        self.assertFalse(d._future.done())
        await a.release()
        self.assertEqual(await d.acquire(), "http://primary/v1/chat/completions")
        await b.release()
        await c.release()
        await d.release()

    async def test_capacity_defaults_to_one(self):
        chain = [
            {"url": "http://p/", "model": "m", "role": "primary"},
            {"url": "http://s/", "model": "m", "role": "secondary"},
        ]
        disp = SubagentDispatcher(chain)
        self.assertEqual(disp.capacity["http://p/"], 1)
        self.assertEqual(disp.capacity["http://s/"], 1)

    async def test_capacity_floors_to_one(self):
        chain = [
            {"url": "http://p/", "model": "m", "role": "primary", "capacity": 0},
        ]
        disp = SubagentDispatcher(chain)
        self.assertEqual(disp.capacity["http://p/"], 1)


if __name__ == "__main__":
    unittest.main()
