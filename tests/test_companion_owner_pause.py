"""The owner said "Pause all scanning" on 2026-09-16. This proves companions is off.

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
    saved = {k: os.environ.get(k) for k in
             ('COMPANION_EXECUTOR_BASE_URL', 'COMPANION_EXECUTOR_SECRET')}
    os.environ['COMPANION_EXECUTOR_BASE_URL'] = 'https://executor.invalid'
    os.environ['COMPANION_EXECUTOR_SECRET'] = 'test-secret-name-only'
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


class CompanionPauseIsInForce(unittest.TestCase):
    def test_the_pause_is_on(self):
        self.assertTrue(
            th.COMPANION_SCANNING_PAUSED,
            'COMPANION_SCANNING_PAUSED is False; the owner paused scanning on '
            '2026-09-16 and only he lifts it',
        )

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


class ThePauseIsReversible(unittest.TestCase):
    """CONTROL. Without this the suite would pass against a hardcoded refusal."""

    def tearDown(self):
        th.COMPANION_SCANNING_PAUSED = True

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
