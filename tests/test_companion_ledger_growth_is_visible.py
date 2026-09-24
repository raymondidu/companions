"""The companion dashboard grows without bound and nothing said so.

Two readings of the same container, from the production host:

    2026-09-21 08:27    258 MB RSS     5,256 callbacks verified
    2026-09-24 10:31  1,151 MB RSS    50,385 callbacks verified

~20 KB of resident memory per callback, monotonic. At 06:53 on 2026-09-24 the
kernel OOM-killed a uvicorn process on that host while host memory read 61.5%
and swap was zero, so no percentage threshold saw it coming and none ever will.

Nothing in /health, /api/markets or any telemetry line reported the size of the
thing that was growing. That is the house fault: a number that exists, decides
whether the box survives, and is surfaced nowhere.

These pin the reporting, not a retention policy. Nothing is pruned yet, because
the two things that grow are not equivalent -- dedupe_event_ids is bookkeeping,
stored_event_payloads holds the OPEN_FAILED details the handoff contract says to
preserve -- and which one to drop is the owner's call once the numbers say which
is the bulk.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import companion_tradehouse as th


def ledger(signals=1, positions=1, events=3, event_ids=10):
    out = {'signals': {}, 'event_ids': {}}
    for s in range(signals):
        rows = {}
        for p in range(positions):
            rows['th-%d-%d' % (s, p)] = {
                'tradehouse_id': 'th-%d-%d' % (s, p),
                'events': {'ev-%d-%d-%d' % (s, p, e): {'event': 'OPENED'} for e in range(events)},
            }
        out['signals']['BTC-%d' % s] = {'positions': rows}
    for i in range(event_ids):
        out['event_ids']['ev-%d' % i] = {'recorded_at': '2026-09-24T10:00:00+00:00'}
    return out


class LedgerFootprint(unittest.TestCase):

    def test_it_counts_every_stored_event_not_just_signals(self):
        """Signal count is nearly flat -- 191 generated in a week -- while the
        event payloads under them are what actually grew 10x. Reporting only the
        signal count would have shown a healthy-looking number all the way into
        the OOM."""
        out = th.ledger_footprint(ledger(signals=2, positions=3, events=5, event_ids=40))
        self.assertEqual(out['callback_signals'], 2)
        self.assertEqual(out['callback_positions'], 6)
        self.assertEqual(out['stored_event_payloads'], 30)
        self.assertEqual(out['dedupe_event_ids'], 40)

    def test_the_two_prunable_things_are_reported_separately(self):
        """One is safe to drop and one is execution evidence. A single total
        cannot tell you which you would be deleting."""
        out = th.ledger_footprint(ledger(events=1, event_ids=99))
        self.assertIn('stored_event_payloads', out)
        self.assertIn('dedupe_event_ids', out)
        self.assertNotEqual(out['stored_event_payloads'], out['dedupe_event_ids'])
        self.assertIn('safe to drop', out['note'])

    def test_it_does_not_re_read_the_file_it_is_measuring(self):
        """Parsing a 34MB ledger a second time to report how expensive parsing it
        is would have doubled the cost of the thing being measured. The caller
        that already has it passes it in."""
        calls = []
        real_read = th._read

        def counting_read(path):
            calls.append(path)
            return real_read(path)

        th._read = counting_read
        try:
            th.ledger_footprint(ledger())
        finally:
            th._read = real_read
        self.assertEqual(calls, [], 'ledger_footprint re-read the ledger it was handed')

    def test_a_standalone_caller_still_works(self):
        out = th.ledger_footprint(None)
        self.assertIn('callback_signals', out)
        self.assertIn('callbacks_bytes', out)

    def test_a_garbage_ledger_does_not_take_the_dashboard_down(self):
        """/health is the instrument that gets read when the box is unwell. It
        must not be the thing that breaks."""
        for junk in ({}, {'signals': None}, {'signals': {'a': 'not-a-dict'}},
                     {'signals': {'a': {'positions': {'b': None}}}}):
            out = th.ledger_footprint(junk)
            self.assertEqual(out['stored_event_payloads'], 0, junk)

    def test_bytes_per_event_is_absent_rather_than_zero_when_unknown(self):
        """Dividing by an empty ledger would print 0 bytes per event, which reads
        as 'nothing is stored' rather than 'nothing is known'."""
        out = th.ledger_footprint({'signals': {}, 'event_ids': {}})
        self.assertNotIn('bytes_per_stored_event', out)

    def test_health_carries_the_footprint_even_with_the_ledger_omitted(self):
        """/health omits the ledger because it made the body 28 MB. The size of
        the thing that was omitted is exactly what a reader needs at that moment,
        so it must survive the trim."""
        snapshot = th.delivery_snapshot(ledger='none')
        self.assertIn('ledger_footprint', snapshot)
        self.assertIn('stored_event_payloads', snapshot['ledger_footprint'])
        self.assertNotIn('signals', snapshot)


if __name__ == '__main__':
    unittest.main()
