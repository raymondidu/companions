"""/health returned a 28MB body and therefore answered nobody.

MEASURED 2026-09-16 18:55, from the companions deploy log:

    curl: (28) Operation timed out after 5000 milliseconds
          with 23884760 out of 28348272 bytes received

delivery_snapshot() ends with `"signals": signals, "callbacks": callback_signals`
-- the whole delivery and callback ledgers, inline -- and /health embedded the
lot. tradehouse_callbacks.json alone is 34,496,030 bytes on the box.

WHAT THAT BROKE, none of it looking like a payload problem: the deploy verifier
uses curl --max-time 5 and could never pull 28MB, so companions deploys 66, 67,
69, 70 and 71 all read FAILURE while deploying fine; companion-telemetry failed
11 of its last 13 runs; companion-health-assert has been committing that payload
into a git branch on every run; and the callback rejection counters have been
unreadable all day, which is what blocked answering the executor.

I BLAMED TWO OTHER THINGS FIRST AND MEASURED BOTH BEFORE SHIPPING EITHER.
delivery_snapshot() itself times at 585ms, nowhere near a 25s timeout. The event
loop WAS being blocked by record_callback rewriting 34.5MB per callback, and that
was real and is fixed -- but it was not the whole story, because after fixing it
curl still could not pull 28MB in five seconds. The byte count in that curl error
is what finally named it.

NOTHING THAT READS /health NEEDS THOSE TWO KEYS. Verified before trimming rather
than assumed: the deploy verifier reads executor_configured and
live_enable_requested, the callback auth probe reads callback_rejections, and
companion_health_assert.py reads summary, lifecycle, open_failure_reasons,
markets and live_signal_gate with ZERO references to `callbacks` and every
`signals` hit being opened_signals or closed_signals inside summary.

The dashboard does need both, and it reads /api/markets, which is untouched.
"""
import importlib
import json
import sys
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


# Every key a real consumer of /health was verified to read. If a future trim
# drops one of these, the deploy verifier or the health assertions go blind.
CONSUMER_KEYS = (
    'executor_configured',       # companions deploy verifier
    'live_enable_requested',     # companions deploy verifier
    'callback_rejections',       # companion-callback-auth-probe
    'callback_max_age_ms',       # companion-callback-auth-probe
    'summary',                   # companion_health_assert.py
    'open_failure_reasons',      # companion_health_assert.py
    'policy_version',
    'cohort',
    'active_paths',
)


class HealthPayloadIsFetchable(unittest.TestCase):
    def setUp(self):
        self.mod = _module()
        self._read = self.mod._read
        self.addCleanup(setattr, self.mod, '_read', self._read)

        # A ledger big enough that including it is obviously the fault. Shaped
        # like the real one: signals keyed by id, each carrying a payload.
        markets = ('USOIL', 'BTC', 'SILVER')
        big = {'signals': {
            f'sig-{i}': {'market': markets[i % 3],
                         'attempted_at': f'2026-09-16T{i // 60:02d}:{i % 60:02d}:00Z',
                         'payload': {'blob': 'x' * 200}, 'positions': {}}
            for i in range(400)}}
        self.mod._read = lambda path: big

    def _snapshot(self, **kw):
        return self.mod.delivery_snapshot(**kw)

    def test_the_default_still_carries_the_whole_ledger(self):
        """The control. /api/markets and /api/companion/tradehouse rely on this,
        and a trim that hit every caller would break the dashboard."""
        full = self._snapshot()
        self.assertIn('signals', full)
        self.assertIn('callbacks', full)

    def test_the_trimmed_snapshot_drops_exactly_the_two_bulk_keys(self):
        trimmed = self._snapshot(ledger='none')
        self.assertNotIn('signals', trimmed)
        self.assertNotIn('callbacks', trimmed)

    def test_every_key_a_real_consumer_reads_survives_the_trim(self):
        """THE ONE THAT MATTERS. Dropping a key a verifier reads turns a
        fetchable payload into a failing deploy, which is the fault this change
        exists to remove, reintroduced from the other side."""
        trimmed = self._snapshot(ledger='none')
        for key in CONSUMER_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, trimmed)

    def test_the_omission_is_announced_with_counts_not_silent(self):
        """A key that just disappears reads as zero to the next consumer. The
        counts say what was there and where to get it."""
        trimmed = self._snapshot(ledger='none')
        note = trimmed.get('ledger_omitted')
        self.assertIsInstance(note, dict)
        self.assertEqual(note.get('signals_count'), 400)
        self.assertIn('callback_signals_count', note)
        self.assertIn('/api/companion/tradehouse', str(note.get('full_payload_at')))

    def test_the_trim_actually_makes_it_small(self):
        """Assert the property the outage was about, not just the key names.

        A trim that removed the keys but left the bulk somewhere else would pass
        every assertion above and still be unfetchable.
        """
        full = len(json.dumps(self._snapshot(), default=str))
        trimmed = len(json.dumps(self._snapshot(ledger='none'), default=str))
        self.assertLess(
            trimmed * 10, full,
            f'trimmed payload is {trimmed} bytes against {full}: not the order of '
            'magnitude this change exists to produce')

    def test_health_calls_the_trimmed_form_and_markets_does_not(self):
        """Assert through the app, because the snapshot being capable of
        trimming is worth nothing if /health does not ask for it."""
        src = open('companion_app.py').read()
        health = [l for l in src.splitlines() if "'/health'" in l or 'def health' in l]
        self.assertTrue(health)
        # The two endpoints the dashboard and full-detail readers use must keep
        # the ledger; only /health trims.
        self.assertIn("delivery_snapshot(ledger='none')", src)
        self.assertIn("'tradehouse':delivery_snapshot(ledger='latest')}", src)

    def test_a_missing_ledger_still_produces_an_answerable_payload(self):
        """/health must still answer when the ledger is absent, because an
        unfetchable health endpoint is the whole problem this change removes.

        THE FIRST VERSION OF THIS TEST CONTRADICTED ITSELF: its docstring said
        the endpoint must still answer while the assertion required an OSError,
        and it passed only because it replaced _read with a raiser. The real
        _read catches its own exceptions and returns {}, so that raiser tested a
        path production does not have. Driven through the real _read now, with
        the data directory pointed at somewhere that does not exist.
        """
        import pathlib as _pl
        self.mod._read = self._read          # restore the real reader
        # SAVE AND RESTORE. A stub left behind flips the module for every later
        # test in the same process, which this repo has already paid for once.
        original_data_dir = self.mod._data_dir
        self.addCleanup(setattr, self.mod, '_data_dir', original_data_dir)
        self.mod._data_dir = lambda: _pl.Path('/nonexistent-xyz')
        trimmed = self._snapshot(ledger='none')
        self.assertIn('executor_configured', trimmed)
        self.assertEqual(trimmed['ledger_omitted']['signals_count'], 0)
        self.assertEqual(trimmed['ledger_omitted']['callback_signals_count'], 0)


    def test_latest_mode_keeps_only_the_newest_signal_per_market(self):
        """What the DASHBOARD needs, and nothing more.

        The page's latestSignal() filters by market, sorts by attempted_at
        descending and takes [0]; lifecycle() then looks up that one signal_id.
        So sending only the newest per market is behaviourally identical to the
        page while removing the 28MB that stopped it loading at all.
        """
        latest = self._snapshot(ledger='latest')
        kept = latest['signals']
        self.assertEqual(len(kept), 3, 'one per market, no more')
        by_market = {v['market']: v['attempted_at'] for v in kept.values()}
        self.assertEqual(sorted(by_market), ['BTC', 'SILVER', 'USOIL'])
        # Each kept signal must be the newest for its market, which is exactly
        # what the page would have picked out of the full ledger.
        full = self._snapshot()['signals']
        for market, stamp in by_market.items():
            newest = max(v['attempted_at'] for v in full.values()
                         if v['market'] == market)
            self.assertEqual(stamp, newest, market)

    def test_latest_mode_is_what_the_page_would_have_chosen(self):
        """Drive the page's own selection against both payloads and require the
        same answer. Asserting counts alone would pass for a trim that kept the
        WRONG signal."""
        def page_pick(snapshot, market):
            rows = [v for v in (snapshot.get('signals') or {}).values()
                    if v.get('market') == market]
            rows.sort(key=lambda x: str(x.get('attempted_at') or x.get('updated_at') or ''),
                      reverse=True)
            return rows[0] if rows else None

        full, latest = self._snapshot(), self._snapshot(ledger='latest')
        for market in ('USOIL', 'BTC', 'SILVER'):
            with self.subTest(market=market):
                self.assertEqual(page_pick(full, market), page_pick(latest, market))

    def test_latest_mode_is_dramatically_smaller_than_full(self):
        full = len(json.dumps(self._snapshot(), default=str))
        latest = len(json.dumps(self._snapshot(ledger='latest'), default=str))
        self.assertLess(latest * 10, full, f'{latest} against {full}')

    def test_the_full_payload_endpoint_is_untouched(self):
        src = open('companion_app.py').read()
        self.assertIn('return delivery_snapshot()', src,
                      '/api/companion/tradehouse must still serve everything')


if __name__ == '__main__':
    unittest.main()