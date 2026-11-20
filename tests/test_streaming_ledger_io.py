import json
import pytest
from pathlib import Path
from unittest.mock import patch

import companion_tradehouse as th


def test_unreadable_callback_history_is_not_replaced_with_empty_state(tmp_path, monkeypatch):
    path = tmp_path / 'tradehouse_callbacks.json'
    path.write_text('{"signals":')
    monkeypatch.setattr(th, '_callback_path', lambda: path)
    with pytest.raises(Exception):
        th._read(path)
    assert path.read_text() == '{"signals":'


def test_latest_summary_does_not_copy_event_history():
    class History(dict):
        def __deepcopy__(self, memo):
            raise AssertionError('copied historical event bodies')
    signal = {'last_event': 'OPENED', 'positions': {'p': {
        'broker_position_id': 'broker-1', 'events': History({'event-1': {'large': 'payload'}})}}}
    out = th._callback_summary(signal)
    assert out['last_event'] == 'OPENED'
    assert out['positions']['p']['event_count'] == 1
    assert out['positions']['p']['broker_position_id'] == 'broker-1'
    assert 'events' not in out['positions']['p']


def test_large_snapshot_roundtrip_uses_streams(tmp_path):
    path = tmp_path / 'ledger.json'
    value = {'signals': {str(i): {'events': {'e': {'message': '£ Ω ' * 1000,
             'price': 123.125, 'sequence': i, 'valid': True, 'reason': None}}}
             for i in range(100)}}
    with patch.object(Path, 'write_text', side_effect=AssertionError('whole-file write')):
        with patch.object(json, 'dumps', side_effect=AssertionError('whole-file encoding')):
            th._write(path, value)
    with patch.object(Path, 'read_text', side_effect=AssertionError('whole-file read')):
        assert th._read(path) == value
    assert not path.with_suffix('.json.tmp').exists()


def test_failed_stream_write_preserves_existing_snapshot(tmp_path):
    path = tmp_path / 'ledger.json'
    th._write(path, {'original': True})
    with patch.object(json, 'dump', side_effect=OSError('disk full')):
        try:
            th._write(path, {'replacement': True})
        except OSError:
            pass
    assert th._read(path) == {'original': True}
