import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from gateway_health import runtime_ready, fresh_ready, peer_confirmed


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

    def test_peer_confirmation_selects_only_state_authority(self):
        peers = {'1': {'key': 'public-key', 'applied': True}}
        ready = dict(state='ready', timestamp=99, peers=peers)
        repair = dict(state='repairing', timestamp=97, last_success_at=96,
                      repair_started_at=97, peers={}, confirmed_peers=peers)
        self.assertTrue(peer_confirmed(ready, 1, 'public-key', 100))
        self.assertTrue(peer_confirmed(repair, 1, 'public-key', 100))
        self.assertFalse(peer_confirmed(dict(ready, peers={}, confirmed_peers=peers), 1, 'public-key', 100))
        self.assertFalse(peer_confirmed(dict(repair, peers=peers, confirmed_peers={}), 1, 'public-key', 100))
        for state in ('applying', 'retrying', None, 'unknown'):
            self.assertFalse(peer_confirmed(dict(repair, state=state, peers=peers), 1, 'public-key', 100))
        for now in (107, 111):
            self.assertFalse(peer_confirmed(repair, 1, 'public-key', now))
        self.assertFalse(peer_confirmed(dict(repair, last_success_at=85), 1, 'public-key', 100))
        self.assertFalse(peer_confirmed(dict(ready, timestamp=84), 1, 'public-key', 100))

    def test_peer_confirmation_malformed_and_identity_fail_closed(self):
        repair = dict(state='repairing', timestamp=97, last_success_at=96,
                      repair_started_at=97, peers={})
        for peers in (None, [], True, {}, {'1': None}, {'1': []},
                      {1: {'key': 'public-key', 'applied': True}}):
            self.assertFalse(peer_confirmed(dict(repair, confirmed_peers=peers), 1, 'public-key', 100))
        for applied in (None, False, 1, 'true'):
            peers = {'1': {'key': 'public-key', 'applied': applied}}
            self.assertFalse(peer_confirmed(dict(repair, confirmed_peers=peers), 1, 'public-key', 100))
        record = dict(repair, confirmed_peers={'1': {'key': 'public-key', 'applied': True}})
        self.assertFalse(peer_confirmed(record, 2, 'public-key', 100))
        self.assertFalse(peer_confirmed(record, 1, 'wrong-key', 100))
        for field in ('timestamp', 'last_success_at', 'repair_started_at'):
            for value in (None, True, float('nan'), float('inf'), -1, 101):
                self.assertFalse(peer_confirmed(dict(record, **{field: value}), 1, 'public-key', 100))
        for malformed in (None, [], {}, True):
            self.assertFalse(peer_confirmed(malformed, 1, 'public-key', 100))

    def test_other_states_fail_closed(self):
        for record in (None, [], {}, {'state': 'applying', 'timestamp': 100},
                       {'state': 'retrying', 'timestamp': 100}):
            self.assertFalse(runtime_ready(record, 100))
