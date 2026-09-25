"""The 40 MB callback ledger was parsed several times a minute, forever.

record_callback did a full _read at the top and a full _write at the bottom;
delivery_snapshot did its own _read of the same file on every /health,
/api/markets and /api/companion/tradehouse. The file is 40,009,220 bytes across
29,312 stored event payloads.

Measured on the production box, same container:

    2026-09-21 08:27    258 MB RSS     5,256 callbacks verified
    2026-09-24 10:31  1,151 MB RSS    50,385 callbacks verified

On 2026-09-24 at 06:53 the kernel OOM-killed a uvicorn on that host while host
memory read 61.5% and swap was zero, so no percentage threshold saw it coming.

NOTHING IS DELETED TO FIX IT. The size of the data was never the fault. These
pin the mechanism: parsed once, written on a debounce, and every accepted event
durable in a write-ahead log in between, so a crash between snapshots loses
nothing. TRADEHOUSE_EXECUTION_HANDOFF.md section 6 says the OPEN_FAILED payloads
must be preserved, and the last test here proves they still are.
"""

import json
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import companion_tradehouse as th


def event(n, signal='BTC-1', pos='th-1', kind='POSITION_UPDATE'):
    row = {
        'signal_id': signal, 'event_id': 'ev-%d' % n, 'tradehouse_id': pos,
        'event_type': kind, 'event_sequence': n,
        'payload_bulk': 'x' * 64,
    }
    if kind == 'OPENED':
        row['broker_position_id'] = 'bp-%d' % n
    return row


class LedgerIsParsedOnce(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get('COMPANION_DATA_DIR')
        os.environ['COMPANION_DATA_DIR'] = self.tmp.name
        self._real_data_dir = th._data_dir
        th._data_dir = lambda: Path(self.tmp.name)
        self._reset_ledger()

    def tearDown(self):
        # RESTORE EVERY FIXTURE. A stub left behind here once made a later,
        # unrelated test fail for a reason nobody could find.
        th._data_dir = self._real_data_dir
        if self._env is None:
            os.environ.pop('COMPANION_DATA_DIR', None)
        else:
            os.environ['COMPANION_DATA_DIR'] = self._env
        self._reset_ledger()
        self.tmp.cleanup()

    def _reset_ledger(self):
        th._LEDGER.update({'state': None, 'fingerprint': None, 'dirty': 0,
                           'flushed_at': 0.0, 'wal_lines': 0, 'load_error': None})

    def _count_reads(self):
        """Count real parses of the callback file, not calls to _read at large."""
        calls = []
        real = th._read

        def counting(path):
            if Path(path).name == 'tradehouse_callbacks.json':
                calls.append(path)
            return real(path)

        th._read = counting
        self.addCleanup(lambda: setattr(th, '_read', real))
        return calls

    # ---- the mechanism ----

    def test_fifty_callbacks_parse_the_ledger_once(self):
        """THE WHOLE POINT. It used to be one parse per callback."""
        reads = self._count_reads()
        for n in range(1, 51):
            ok, reason = th.record_callback(event(n))
            self.assertTrue(ok, (n, reason))
        self.assertEqual(len(reads), 1, 'the ledger was parsed %d times' % len(reads))

    def test_reading_health_does_not_parse_the_ledger_again(self):
        """delivery_snapshot paid its own 40 MB parse on every request."""
        th.record_callback(event(1))
        reads = self._count_reads()
        for _ in range(10):
            th.delivery_snapshot(ledger='none')
        self.assertEqual(len(reads), 0, 'a /health read re-parsed the ledger')

    def test_the_snapshot_write_is_debounced(self):
        """Rewriting 40 MB per callback is 27 GB an hour at the observed rate."""
        writes = []
        real = th._write

        def counting(path, value):
            if Path(path).name == 'tradehouse_callbacks.json':
                writes.append(path)
            return real(path, value)

        th._write = counting
        self.addCleanup(lambda: setattr(th, '_write', real))
        for n in range(1, 21):
            th.record_callback(event(n))
        self.assertLess(len(writes), 20,
                        'every callback still wrote the whole snapshot')

    # ---- durability: nothing may be lost ----

    def test_an_event_accepted_before_a_flush_survives_a_crash(self):
        """The debounce is only safe because the WAL makes it safe. Simulate a
        hard kill by dropping the in-memory ledger without flushing."""
        th.record_callback(event(1, kind='OPENED'))
        self.assertGreater(th._LEDGER['dirty'], 0, 'nothing was left unflushed to test')
        self._reset_ledger()                      # the crash
        with th._LEDGER_LOCK:
            state = th._load_ledger()             # the restart
        self.assertIn('ev-1', state.get('event_ids', {}),
                      'an accepted callback was lost between snapshots')

    def test_replay_is_idempotent(self):
        """TradeHouse retries up to 20 times; a replay that double-counted would
        corrupt the lifecycle as surely as losing one would."""
        for n in range(1, 4):
            th.record_callback(event(n))
        self._reset_ledger()
        with th._LEDGER_LOCK:
            first = dict(th._load_ledger().get('event_ids', {}))
        self._reset_ledger()
        with th._LEDGER_LOCK:
            second = dict(th._load_ledger().get('event_ids', {}))
        self.assertEqual(sorted(first), sorted(second))
        self.assertEqual(sorted(first), ['ev-1', 'ev-2', 'ev-3'])

    def test_a_torn_final_wal_line_does_not_stop_startup(self):
        """A WAL is written without fsync on purpose, so a torn tail is an
        expected shape. Refusing to start because of one would turn a saved
        event into an outage."""
        th.record_callback(event(1))
        with th._wal_path().open('a', encoding='utf-8') as handle:
            handle.write('{"signal_id": "BTC-1", "event_id": "ev-2", "even')
        self._reset_ledger()
        with th._LEDGER_LOCK:
            state = th._load_ledger()
        self.assertIn('ev-1', state.get('event_ids', {}))
        self.assertNotIn('ev-2', state.get('event_ids', {}))

    def test_a_flush_clears_the_wal(self):
        """A WAL that is never truncated is just a second unbounded ledger."""
        th.record_callback(event(1))
        with th._LEDGER_LOCK:
            th._flush_ledger(force=True)
        self.assertFalse(th._wal_path().exists(), 'the WAL survived a flush')
        self.assertEqual(th._LEDGER['dirty'], 0)

    def test_replay_re_validates_rather_than_trusting_the_file(self):
        """A WAL line is a file on disk, and a file on disk is not a promise.
        A hand-edited line must not enter the ledger just because it is JSON."""
        th.record_callback(event(1))
        with th._wal_path().open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'signal_id': 'X', 'event_id': 'ev-bad',
                                     'tradehouse_id': 'th-1',
                                     'event_type': 'NOT_A_REAL_EVENT',
                                     'event_sequence': 99}) + '\n')
        self._reset_ledger()
        with th._LEDGER_LOCK:
            state = th._load_ledger()
        self.assertNotIn('ev-bad', state.get('event_ids', {}))

    def test_the_replay_path_does_not_write_to_the_wal_it_is_reading(self):
        """_record_into is shared by the live path and the replay path. If the
        WAL append lived inside it, every restart would rewrite its own input."""
        source = Path(th.__file__).read_text(encoding='utf-8')
        body = source[source.index('def _record_into'):source.index('def _apply_callback')]
        self.assertNotIn('_append_wal', body)
        self.assertNotIn('_flush_ledger', body)

    # ---- nothing is deleted ----

    def test_every_stored_event_payload_survives(self):
        """The handoff contract says the OPEN_FAILED details must be preserved.
        This change is about the mechanism, never the data."""
        th.record_callback(event(1, kind='OPENED'))
        th.record_callback(event(2, kind='OPEN_FAILED'))
        th.record_callback(event(3, kind='CLOSED'))
        self._reset_ledger()
        foot = th.ledger_footprint()
        self.assertEqual(foot['stored_event_payloads'], 3)
        self.assertEqual(foot['dedupe_event_ids'], 3)

    def test_the_footprint_reports_what_is_not_yet_on_disk(self):
        """Reporting only the snapshot would under-report by exactly the events
        most at risk."""
        th.record_callback(event(1))
        foot = th.ledger_footprint()
        self.assertIn('unflushed_events', foot)
        self.assertIn('wal_bytes', foot)
        self.assertTrue(foot['ledger_parsed_in_memory'])

    # ---- the hazard this change introduces ----

    def test_a_reader_and_a_writer_do_not_collide(self):
        """One shared copy across two worker threads is a hazard this change
        CREATES, not one it inherits. Iterating the ledger in /health while
        record_callback mutates it raises "dictionary changed size during
        iteration".

        THE FIRST VERSION OF THIS TEST WAS VACUOUS AND A MUTATION PROVED IT.
        Removing the lock from delivery_snapshot left it green. The writer
        reused one signal_id, so the `signals` dict never changed SIZE -- and a
        size change is precisely what raises. It hammered 120 events at a dict
        that stayed one key wide and called that a race.

        So the writer now inserts a NEW signal on every event, and the ledger is
        pre-loaded wide enough that the reader is genuinely mid-walk when it
        happens. Verified to go red with the lock removed.
        """
        for n in range(1, 250):
            th.record_callback(event(n, signal='SEED-%d' % n, pos='th-%d' % n))

        errors = []
        stop = threading.Event()

        def write():
            try:
                n = 10_000
                while not stop.is_set():
                    n += 1
                    th.record_callback(event(n, signal='NEW-%d' % n, pos='th-%d' % n))
            except Exception as exc:          # noqa: BLE001 - the test IS the catch
                errors.append(('write', repr(exc)))

        def read():
            try:
                for _ in range(400):
                    th.delivery_snapshot(ledger='none')
            except Exception as exc:          # noqa: BLE001
                errors.append(('read', repr(exc)))
            finally:
                stop.set()

        threads = [threading.Thread(target=write), threading.Thread(target=read)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        stop.set()
        self.assertEqual(errors, [])

    def test_touching_the_ledger_without_the_lock_fails_immediately(self):
        """THE SOURCE GUARD BELOW WAS TOO WEAK AND CI PROVED IT.

        delivery_snapshot held the lock across `callbacks = _load_ledger()` and
        then walked the shared ledger outside it. The substring check passed --
        `with _LEDGER_LOCK:` really did precede the call -- and the reader raised
        "dictionary changed size during iteration" on the runner anyway, on a
        commit that was green here twenty minutes earlier.

        A race is timing. An assertion at the point of misuse is not. _load_ledger
        now refuses to run unlocked, so the next person who reaches for the
        ledger without the lock finds out in the first test they run rather than
        once in a while in production.
        """
        with self.assertRaises(RuntimeError) as caught:
            th._load_ledger()
        self.assertIn('_LEDGER_LOCK', str(caught.exception))

    def test_the_returned_ledger_is_a_copy_not_a_live_reference(self):
        """FastAPI serialises the payload long after the lock is released, so a
        live reference into the shared ledger races exactly the way the unlocked
        walk did."""
        th.record_callback(event(1, signal='BTC-1'))
        # 'full', not 'latest': 'latest' keys off the markets in the DELIVERY
        # file, which is empty in this fixture, so nothing travelled and the
        # test skipped itself. A skipped test proves nothing.
        # BOTH MODES THAT CARRY THE LEDGER, not just one. A mutation that made
        # 'latest' hand out a live reference passed while only 'full' was
        # checked, which is the same shape as the guard this file already
        # deleted for being green on the bug.
        #
        # 'latest' keys off the markets in the DELIVERY file, so that has to
        # exist or the mode returns nothing and the check is vacuous.
        th._write(th._state_path(), {'signals': {'BTC-1': {
            'market': 'BTC', 'signal_id': 'BTC-1',
            'attempted_at': '2026-09-25T00:00:00+00:00'}}})
        with th._LEDGER_LOCK:
            live = th._load_ledger()['signals']
        for mode in ('full', 'latest'):
            returned = (th.delivery_snapshot(ledger=mode) or {}).get('callbacks') or {}
            self.assertTrue(returned,
                            'no callback signals travelled in %s, so nothing was '
                            'checked' % mode)
            for sid, row in returned.items():
                self.assertIsNot(row, live.get(sid),
                                 '%s handed out a live reference to %s' % (mode, sid))

    # REMOVED: test_both_read_sites_take_the_lock.
    #
    # It asserted that `with _LEDGER_LOCK:` appeared somewhere before
    # `_load_ledger()` in each reader. That was true while delivery_snapshot
    # walked the shared ledger OUTSIDE the lock, so it passed on the exact
    # commit CI failed, and after the fix it started failing on the wrapper's
    # own docstring. A guard that is green on the bug and red on the fix is
    # worse than no guard.
    #
    # test_touching_the_ledger_without_the_lock_fails_immediately replaces it
    # and is strictly stronger: it checks the property at runtime, at the point
    # of misuse, rather than looking for a string near a call.


if __name__ == '__main__':
    unittest.main()
