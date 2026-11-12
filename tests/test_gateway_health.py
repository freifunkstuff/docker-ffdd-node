import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from gateway_health import runtime_ready, fresh_ready


class HealthTests(unittest.TestCase):
    def test_ready(self):
        self.assertTrue(runtime_ready({'state': 'ready', 'timestamp': 99}, 100))
        for stamp in (None, True, False, float('nan'), float('inf'), -1, 101, 84):
            self.assertFalse(runtime_ready({'state': 'ready', 'timestamp': stamp}, 100))

    def test_repair_lease(self):
        record = dict(state='repairing', timestamp=97, last_success_at=96, repair_started_at=97)
        self.assertTrue(runtime_ready(record, 100))
        self.assertFalse(fresh_ready(record, 100))
        self.assertFalse(runtime_ready(record, 107))
        self.assertFalse(runtime_ready(dict(record, last_success_at=85), 100))
        for field in ('timestamp', 'last_success_at', 'repair_started_at'):
            for value in (None, True, float('nan'), float('inf'), 101):
                self.assertFalse(runtime_ready(dict(record, **{field: value}), 100))
        self.assertFalse(runtime_ready(dict(record, last_success_at=98), 100))
        self.assertFalse(runtime_ready(dict(record, timestamp=96), 100))

    def test_other_states_fail_closed(self):
        for record in (None, [], {}, {'state': 'applying', 'timestamp': 100},
                       {'state': 'retrying', 'timestamp': 100}):
            self.assertFalse(runtime_ready(record, 100))
