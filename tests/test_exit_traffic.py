from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from exit_traffic import ExitTrafficLedger


def stamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Europe/Berlin')).timestamp()


class ExitTrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'exit-traffic.json'
        self.ledger = ExitTrafficLedger(self.path)
        self.t = stamp('2025-06-01T12:00:00')

    def sample(self, when, rx, tx=0, generation='namespace-1', name='exit0'):
        self.ledger.sample(when, {name: {'rx_bytes': rx, 'tx_bytes': tx}}, {name: generation})

    def test_baseline_reload_catchup_and_uint64(self):
        initial = (1 << 63) + 123
        self.sample(self.t, initial, 10)
        self.sample(self.t + 1, initial + 9, 15)
        self.ledger.flush(force=True)
        with patch('exit_traffic.time.time', return_value=self.t + 2):
            self.ledger = ExitTrafficLedger(self.path)
        self.sample(self.t + 2, initial + 20, 19)
        self.sample(self.t + 2, initial + 20, 19)
        self.assertEqual(self.ledger.export()['totals'], {'rx_bytes': 20, 'tx_bytes': 9})
        self.assertTrue(self.ledger.export()['days'][0]['partial'])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_generation_reset_unknown_and_missing_directions(self):
        self.sample(self.t, 100, 100)
        self.sample(self.t + 1, 120, 130)
        self.sample(self.t + 2, 1000, 1000, 'namespace-2')
        self.sample(self.t + 3, 2, 5, 'namespace-2')
        self.sample(self.t + 4, 100, 100, None)
        self.sample(self.t + 5, 200, 200, 'namespace-2')
        self.sample(self.t + 6, None, 210, 'namespace-2')
        self.sample(self.t + 7, 500, 220, 'namespace-2')
        self.assertEqual(self.ledger.export()['totals'], {'rx_bytes': 20, 'tx_bytes': 50})

    def test_invalid_values_not_coerced_and_names_bounded(self):
        self.sample(self.t, 10, 10)
        for i, invalid in enumerate([-1, True, float('nan'), float('inf'), 22.0, 1 << 64]):
            self.sample(self.t + i + 1, invalid, 11 + i)
        self.assertEqual(self.ledger.export()['totals'], {'rx_bytes': 0, 'tx_bytes': 6})
        counters = {f'e{i}': {'rx_bytes': 0, 'tx_bytes': 0} for i in range(20)}
        counters.update({'__proto__': {}, 'constructor': {}, 'bad/name': {}})
        self.ledger.sample(self.t + 20, counters, {k: 'g' for k in counters})
        self.assertEqual(len(self.ledger._state['baselines']), 8)

    def test_midnight_short_interval_integer_split(self):
        boundary = stamp('2025-06-02T00:00:00')
        self.sample(boundary - 0.5, 0)
        self.sample(boundary + 0.5, (1 << 63) + 2)
        days = self.ledger.export()['days']
        self.assertEqual([d['rx_bytes'] for d in days], [(1 << 62) + 1] * 2)

    def test_dst_and_long_gap_partial_endpoints(self):
        for start, end in [('2025-03-30T00:00:00', '2025-03-31T00:00:00'),
                           ('2025-10-26T00:00:00', '2025-10-27T00:00:00')]:
            ledger = ExitTrafficLedger(Path(self.tmp.name) / (start[:10] + '.json'))
            ledger.sample(stamp(start), {'e': {'rx_bytes': 0, 'tx_bytes': 0}}, {'e': 'g'})
            ledger.sample(stamp(end), {'e': {'rx_bytes': 100, 'tx_bytes': 50}}, {'e': 'g'})
            days = ledger.export()['days']
            self.assertEqual([d['rx_bytes'] for d in days], [0, 100])
            self.assertTrue(all(d['partial'] for d in days))

    def test_thirty_calendar_days_no_prehistory(self):
        self.sample(self.t, 100)
        self.assertEqual(len(self.ledger.export()['days']), 1)
        for i in range(1, 41):
            self.sample(self.t + i * 86400, 100 + i)
        out = self.ledger.export()
        self.assertEqual(len(out['days']), 30)
        self.assertEqual(out['days'][0]['date'], '2025-06-12')
        self.assertEqual(out['totals']['rx_bytes'], 40)

    def test_checkpoint_failure_atomic_dirty_and_retry_throttle(self):
        with patch('exit_traffic.time.monotonic', return_value=0):
            self.sample(self.t, 0)
        original = self.path.read_bytes()
        with patch('exit_traffic.time.monotonic', return_value=31), patch('exit_traffic.os.replace', side_effect=OSError('SECRET /private/path')):
            with self.assertRaisesRegex(OSError, '^traffic checkpoint failed$'):
                self.sample(self.t + 1, 10)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.ledger._dirty)
        with patch('exit_traffic.time.monotonic', return_value=32):
            self.sample(self.t + 2, 20)
        self.assertEqual(self.path.read_bytes(), original)
        with patch('exit_traffic.time.monotonic', return_value=62):
            self.ledger.flush()
        self.assertFalse(self.ledger._dirty)
        self.assertEqual(json.loads(self.path.read_text())['totals']['rx_bytes'], 20)
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 1)

    def test_corrupt_state_sanitized_not_overwritten(self):
        for data in [b'private-key SECRET', b'{}', b'x' * (256 * 1024 + 1), b'{"version":NaN}']:
            self.path.write_bytes(data)
            with self.assertRaisesRegex(ValueError, '^invalid traffic state$'):
                ExitTrafficLedger(self.path)
            self.assertEqual(self.path.read_bytes(), data)

    def test_schema_validation_and_timezone_mismatch(self):
        self.sample(self.t, 1)
        valid = json.loads(self.path.read_text())
        mutations = []
        for key, value in [('version', 2), ('timezone', 'UTC'), ('retention_days', 31),
                           ('totals', {'rx_bytes': True, 'tx_bytes': 0}),
                           ('baselines', {'__proto__': {}})]:
            altered = dict(valid)
            altered[key] = value
            mutations.append(altered)
        for altered in mutations:
            self.path.write_text(json.dumps(altered))
            with self.assertRaisesRegex(ValueError, '^invalid traffic state$'):
                ExitTrafficLedger(self.path)

    def test_gap_generation_change_marks_previous_day(self):
        boundary = stamp('2025-06-02T00:00:00')
        self.sample(boundary - 10, 100)
        self.sample(boundary - 5, 105)
        self.sample(boundary + 10, 999, generation='new')
        self.assertEqual(self.ledger.export()['totals']['rx_bytes'], 5)
        self.assertTrue(all(d['partial'] for d in self.ledger.export()['days']))

    def test_public_privacy_and_validation(self):
        self.ledger.sample(self.t, {'exit0': {'rx_bytes': 0, 'tx_bytes': 0, 'private_key': 'SECRET', 'client_ip': '192.0.2.1'}}, {'exit0': 'opaque-generation'})
        public = self.ledger.export()
        self.assertEqual(set(public), {'version', 'timezone', 'retention_days', 'started_at', 'updated_at', 'totals', 'days'})
        self.assertNotIn('opaque-generation', json.dumps(public))
        self.assertNotIn('SECRET', self.path.read_text())
        self.assertNotIn('192.0.2.1', self.path.read_text())
        public['totals']['rx_bytes'] = 999
        self.assertEqual(self.ledger.export()['totals']['rx_bytes'], 0)
        with self.assertRaises(ValueError):
            self.sample(self.t - 1, 10)
        with self.assertRaises(ValueError):
            self.sample(float('nan'), 10)


if __name__ == '__main__':
    unittest.main()
