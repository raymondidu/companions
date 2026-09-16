"""Recording a callback must not stall every other request in the process.

MEASURED ON THE BOX, 2026-09-16 18:48: tradehouse_callbacks.json is
34,496,030 bytes across 71 signals and 956 position rows. record_callback does
a full _read at the top and a full _write at the bottom, both synchronous, and
companion_app called it INLINE inside `async def tradehouse_callback`. So every
inbound callback parsed, re-serialised and rewrote 34.5 MB on the event loop,
and nothing else in the process could be accepted or answered while it ran.

That is why /health had been unanswerable for days, why a container recreate
never helped (the ledger lives on the volume and survives one), and why the
dashboard sat at 90-97% CPU.

I BLAMED THE WRONG FUNCTION FIRST. My hypothesis was delivery_snapshot(), which
/health calls on every request. Measuring it said 585 ms -- nowhere near the
25 s timeouts -- and /health is a plain `def`, so FastAPI already runs it in a
threadpool. The probe printed AGGREGATION_IS_NOT_THE_COST and it was right.
Shipping that guess would have optimised a function that was not the problem.

THE CONCURRENCY TEST IS THE IMPORTANT ONE. The event loop was serialising these
read-modify-write cycles for free. Moving the work to a thread without a lock
would let two callbacks read the same state and have the second overwrite the
first, silently losing a real trade event -- strictly worse than a slow
dashboard. The lock is the fix; the thread alone is a regression.

Nothing here asserts verification, admission or recording behaviour. This is
about WHERE record_callback runs, not WHAT it does.
"""
import asyncio
import importlib
import sys
import time
import types
import unittest


def _module():
    if 'httpx' not in sys.modules:
        stub = types.ModuleType('httpx')

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        stub.AsyncClient = _Client
        sys.modules['httpx'] = stub
    return importlib.import_module('companion_tradehouse')


class RecordingDoesNotBlockTheLoop(unittest.TestCase):
    def setUp(self):
        self.mod = _module()
        self.original = self.mod.record_callback
        self.addCleanup(setattr, self.mod, 'record_callback', self.original)

    def test_the_wrapper_exists_and_is_a_coroutine_function(self):
        self.assertTrue(asyncio.iscoroutinefunction(self.mod.record_callback_async))

    def test_the_app_awaits_the_wrapper_and_never_calls_the_blocking_one(self):
        """Assert through the module the app imports, not through source text
        alone: a handler that imported the blocking name would defeat every
        other test here while they all still passed."""
        app_mod = importlib.import_module('companion_app')
        self.assertTrue(hasattr(app_mod, 'record_callback_async'))
        self.assertFalse(
            hasattr(app_mod, 'record_callback'),
            'companion_app still holds the blocking record_callback; the '
            'handler can call it and stall the loop again')

    def test_the_loop_is_never_starved_while_a_slow_record_is_in_flight(self):
        """THE POINT, and the first version of it was VACUOUS.

        It originally counted total ticks and asserted more than five. That
        passed against the blocking call too: gather lets the ticker finish its
        remaining ticks AFTER the block clears, so the total recovers and the
        test sees nothing. Reverting the fix left it green, which is the whole
        reason mutations get run before a guard is trusted.

        What actually distinguishes the two is the LONGEST GAP between ticks.
        Blocking the loop for 0.30 s produces one gap of ~0.30 s no matter how
        the rest recovers; running off-loop keeps every gap near the 0.01 s
        sleep. Measure the starvation, not the recovery.
        """
        self.mod.record_callback = lambda payload: (time.sleep(0.30), (True, 'RECORDED'))[1]
        stamps = []

        async def ticker():
            for _ in range(40):
                await asyncio.sleep(0.01)
                stamps.append(time.monotonic())

        async def drive():
            task = asyncio.ensure_future(ticker())
            await asyncio.sleep(0.05)          # let the ticker get going first
            await self.mod.record_callback_async({'x': 1})
            await task

        asyncio.run(drive())
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        worst = max(gaps) if gaps else 0.0
        self.assertLess(
            worst, 0.15,
            f'the event loop was starved for {worst:.3f}s while a callback was '
            'being recorded. A 34.5 MB rewrite on the loop is exactly this, and '
            'it is why /health could not answer.')

    def test_concurrent_records_are_serialised_and_none_is_lost(self):
        """Without the lock this is a lost-update race on trading truth.

        The stub reproduces read-modify-write with a real gap between the read
        and the write, which is exactly the shape of record_callback. If the two
        calls overlap, the second write clobbers the first and a genuine trade
        event disappears with no error anywhere.
        """
        state = {'events': []}
        overlaps = 0
        inside = 0

        def slow_record(payload):
            nonlocal overlaps, inside
            inside += 1
            if inside > 1:
                overlaps += 1
            snapshot = list(state['events'])      # read
            time.sleep(0.05)                      # the window a thread exposes
            snapshot.append(payload['id'])
            state['events'] = snapshot            # write
            inside -= 1
            return True, 'RECORDED'

        self.mod.record_callback = slow_record

        async def drive():
            await asyncio.gather(*(self.mod.record_callback_async({'id': i})
                                   for i in range(6)))

        asyncio.run(drive())
        self.assertEqual(overlaps, 0, 'two records ran at once; the lock is gone')
        self.assertEqual(
            sorted(state['events']), list(range(6)),
            f"lost a callback: {state['events']} -- a real trade event would "
            'have vanished with no error')

    def test_the_result_is_passed_through_unchanged(self):
        """The wrapper must not become a place where a refusal turns into an
        acceptance. Both tuple elements travel untouched."""
        for expected in ((True, 'RECORDED'), (False, 'INVALID_EVENT_TYPE'),
                         (True, 'DUPLICATE_EVENT')):
            with self.subTest(expected=expected):
                self.mod.record_callback = lambda payload, e=expected: e
                got = asyncio.run(self.mod.record_callback_async({'x': 1}))
                self.assertEqual(got, expected)

    def test_an_exception_still_propagates(self):
        """Swallowing here would turn a failed write into a 200 and tell the
        executor we stored something we did not."""
        def boom(payload):
            raise RuntimeError('disk full')

        self.mod.record_callback = boom
        with self.assertRaises(RuntimeError):
            asyncio.run(self.mod.record_callback_async({'x': 1}))

    def test_the_lock_is_released_after_a_failure(self):
        """A lock held by a crashed call would deadlock every later callback --
        a worse outage than the one being fixed."""
        def boom(payload):
            raise RuntimeError('disk full')

        self.mod.record_callback = boom
        with self.assertRaises(RuntimeError):
            asyncio.run(self.mod.record_callback_async({'x': 1}))

        self.mod.record_callback = lambda payload: (True, 'RECORDED')
        self.assertEqual(
            asyncio.run(self.mod.record_callback_async({'x': 2})),
            (True, 'RECORDED'))


if __name__ == '__main__':
    unittest.main()
