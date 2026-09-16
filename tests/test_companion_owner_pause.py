"""The owner pause is a MECHANISM, and this drives it both ways.

The owner paused scanning on 2026-09-16 and lifted it the same day: "let all go
live now and start sending trades". An earlier version of this file asserted
COMPANION_SCANNING_PAUSED was True, which pinned that morning's state and went
red the moment he changed his mind -- a test that fails because the owner made a
decision is watching nothing. What must hold either way is the MECHANISM: while
the flag is set nothing reaches the post, while it is clear a qualifying signal
does, and the fail-closed gates above the post answer for themselves in BOTH
positions.

COMPANIONS IS NOT PAPER-ONLY, whatever its status payload says. scan_one emits
'tradehouse_delivery': False and 'live_authority': False as literal labels in
its output dict, while companion_runner.py calls deliver_selected_signal on
EVERY scan and companion_tradehouse posts to the executor with an
x-executor-secret header. A field reporting a delivery path as off while the
code delivers is the instrument lying about the one thing it exists to report,
and it is why this pause is asserted against the POST and not against that flag.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import companion_tradehouse as th


def _reaches_the_post(market):
    """A signal that clears EVERY gate, so it lands on the pause and nowhere else.

    The first version of this helper passed an empty profiles dict and a null
    created_at. That never reached the post at all -- it died on WRONG_COHORT --
    and the test only looked like it worked because the pause was the first
    check in the function. Once the pause moved onto the post, where it belongs,
    the fixture was exposed as testing nothing. Every field below is here
    because omitting it short-circuits an earlier refusal.
    """
    path = th.ACTIVE_PATHS[market]
    now = datetime.now(timezone.utc).isoformat()
    return {
        'market': market,
        'profiles': {path: {'research_policy': {'cohort': th.COHORT}}},
        'live_candidate': {'direction': 'LONG', 'created_at': now},
        'setup_key': 'pause-fixture',
        'live_gate': 'PASS',
    }


def _deliver(market, **overrides):
    fx = _reaches_the_post(market)
    fx.update(overrides)
    # COMPANION_DATA_DIR is not optional here, and leaving it out is the second
    # fixture bug this file has had. Without it the module writes its delivery
    # state under /app/companion-data, which does not exist on a CI runner:
    # the reversibility control got PermissionError '/app' there while passing
    # locally, purely because the local shell runs as root. Every other routing
    # test in this repo points it at a tmp dir for exactly this reason.
    tmp = tempfile.mkdtemp(prefix='companion-pause-')
    keys = ('COMPANION_EXECUTOR_BASE_URL', 'COMPANION_EXECUTOR_SECRET',
            'COMPANION_DATA_DIR')
    saved = {k: os.environ.get(k) for k in keys}
    os.environ['COMPANION_EXECUTOR_BASE_URL'] = 'https://executor.invalid'
    os.environ['COMPANION_EXECUTOR_SECRET'] = 'test-secret-name-only'
    os.environ['COMPANION_DATA_DIR'] = tmp
    try:
        return asyncio.run(th.deliver_selected_signal(
            fx['market'], fx['profiles'],
            live_candidate=fx['live_candidate'],
            setup_key=fx['setup_key'], live_gate=fx['live_gate'],
        ))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


class ThePauseStopsEveryLivePathWhenItIsSet(unittest.TestCase):
    """Driven from a FORCED True, so it holds whichever way the flag ships."""

    def setUp(self):
        self._saved = th.COMPANION_SCANNING_PAUSED
        th.COMPANION_SCANNING_PAUSED = True

    def tearDown(self):
        th.COMPANION_SCANNING_PAUSED = self._saved

    def test_the_flag_is_a_real_boolean(self):
        self.assertIsInstance(th.COMPANION_SCANNING_PAUSED, bool)

    def test_delivery_refuses_before_it_can_reach_the_executor(self):
        """Every live path, including the ones that would otherwise qualify."""
        for market in list(th.ACTIVE_PATHS):
            with self.subTest(market=market):
                out = _deliver(market)
                self.assertFalse(out['sent'])
                self.assertEqual(
                    out['status'], 'PAUSED_BY_OWNER',
                    'a signal that cleared every gate was not stopped by the '
                    'pause; it reached %r' % out['status'],
                )

    def test_every_fail_closed_gate_still_answers_for_itself(self):
        """The pause sits ON the post, not at the top, and this is why.

        Placed first it returned PAUSED_BY_OWNER for these too, which makes
        fail-closed trading gates unreachable -- softening them. They still have
        to hold the day the pause is lifted, so they must keep answering.
        """
        out = asyncio.run(th.deliver_selected_signal('NOT_A_MARKET', {}))
        self.assertEqual(out['status'], 'REFUSED_INSTRUMENT')

        market = next(iter(th.ACTIVE_PATHS))
        bad = _deliver(market, live_candidate={
            'direction': 'BUY',
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(
            bad['status'], 'INVALID_DIRECTION',
            'the pause swallowed a fail-closed direction check',
        )

    def test_the_fail_closed_gates_answer_with_the_pause_LIFTED_too(self):
        """The reason the pause sits on the post and not at the top.

        These are trading-authority gates. They have to hold when the owner is
        live, which is precisely when nothing else is stopping a bad signal.
        Asserting them only while paused proves the cheap half.
        """
        saved = th.COMPANION_SCANNING_PAUSED
        th.COMPANION_SCANNING_PAUSED = False
        self.addCleanup(lambda: setattr(th, 'COMPANION_SCANNING_PAUSED', saved))

        out = asyncio.run(th.deliver_selected_signal('NOT_A_MARKET', {}))
        self.assertEqual(out['status'], 'REFUSED_INSTRUMENT')

        market = next(iter(th.ACTIVE_PATHS))
        bad = _deliver(market, live_candidate={
            'direction': 'BUY',
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(
            bad['status'], 'INVALID_DIRECTION',
            'a fail-closed direction check stopped answering once the pause was '
            'lifted, which is exactly when it matters',
        )

        stale = _deliver(market, live_candidate={
            'direction': 'LONG',
            'created_at': '2020-01-01T00:00:00+00:00',
        })
        self.assertEqual(
            stale['status'], 'STALE_SIGNAL',
            'the staleness gate stopped answering once the pause was lifted',
        )


class ThePauseIsReversible(unittest.TestCase):
    """CONTROL. Without this the suite would pass against a hardcoded refusal."""

    def setUp(self):
        self._saved = th.COMPANION_SCANNING_PAUSED

    def tearDown(self):
        # RESTORE what was there, never hardcode a value: a tearDown that sets
        # True would silently flip the module for every later test the day the
        # owner ships it False.
        th.COMPANION_SCANNING_PAUSED = self._saved

    def test_the_fixture_never_writes_outside_a_temp_dir(self):
        """Pins the fix for a CI-only failure this file actually caused.

        Unset, COMPANION_DATA_DIR resolves to /app/companion-data, and the
        control below walks far enough into the delivery path to create it.
        That is fine as root, which is why it passed locally, and it is
        PermissionError on a CI runner. Asserting the resolved path keeps the
        difference visible instead of leaving it to whoever runs the suite.
        """
        tmp = tempfile.mkdtemp(prefix='companion-pause-probe-')
        saved = os.environ.get('COMPANION_DATA_DIR')
        try:
            os.environ.pop('COMPANION_DATA_DIR', None)
            self.assertEqual(str(th._data_dir()), '/app/companion-data')
            os.environ['COMPANION_DATA_DIR'] = tmp
            self.assertEqual(str(th._data_dir()), tmp)
        finally:
            if saved is None:
                os.environ.pop('COMPANION_DATA_DIR', None)
            else:
                os.environ['COMPANION_DATA_DIR'] = saved
            shutil.rmtree(tmp, ignore_errors=True)

    def test_clearing_it_lets_a_qualifying_signal_past_the_pause(self):
        th.COMPANION_SCANNING_PAUSED = False
        out = _deliver(next(iter(th.ACTIVE_PATHS)))
        self.assertNotEqual(
            out['status'], 'PAUSED_BY_OWNER',
            'the pause still blocks after being cleared, so it is a brick',
        )


class TheContractIsUntouched(unittest.TestCase):
    def test_the_active_paths_are_still_exactly_the_agreed_two(self):
        self.assertEqual(
            th.ACTIVE_PATHS,
            {'USOIL': 'BALANCED_CLEAN', 'BTC': 'GOLD_M30_LOCAL_STRUCTURE'},
        )
        self.assertEqual(th.COHORT, 'EXNESS_SURVIVAL_V1')


if __name__ == '__main__':
    unittest.main()
