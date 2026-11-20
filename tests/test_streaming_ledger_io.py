import json
from pathlib import Path
from unittest.mock import patch

import companion_tradehouse as th


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
