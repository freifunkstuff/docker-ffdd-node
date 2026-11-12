import base64
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gateway_store import GatewayStore, StoreError, validate_key, validate_node


def key(n):
    return base64.b64encode(bytes([n]) * 32).decode()


def claim(args):
    path, node = args
    return GatewayStore(path).enroll(node, key(1), 60000)[0]


class GatewayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 10000000
        self.store = GatewayStore(self.temp.name, clock=lambda: self.now)

    def test_validation_and_zero(self):
        self.assertEqual(validate_node("0"), 0)
        for node in (-1, 65280, 65536, True, "1x", " 1", "123456"):
            with self.assertRaises(StoreError):
                validate_node(node)
        for invalid in (key(1) + "\n", "x" + key(1), key(1)[:-1], "!" * 44, "A" * 42 + "B="):
            with self.assertRaises(StoreError):
                validate_key(invalid)
        self.assertEqual(self.store.enroll(0, key(1), 4)[0], "RequestAccepted")
        self.assertEqual(self.store.enroll(4, key(2), 4)[0], "RequestConflict")

    def test_conflicts_and_restricted_reconnect(self):
        self.assertEqual(self.store.enroll(1, key(1), 4, True)[0], "Restricted")
        self.store.enroll(1, key(1), 4)
        self.assertEqual(self.store.enroll(1, key(1), 4, True)[0], "RequestAlreadyRegistered")
        self.assertEqual(self.store.enroll(1, key(2), 4)[0], "RequestConflict")
        self.assertEqual(self.store.enroll(2, key(1), 4)[0], "RequestKeyConflict")
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_threads_and_processes(self):
        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(lambda _: self.store.enroll(1, key(1), 4)[0], range(30)))
        self.assertEqual(results.count("RequestAccepted"), 1)
        with ProcessPoolExecutor(4) as pool:
            results = list(pool.map(claim, [(self.temp.name, n) for n in range(2, 14)]))
        self.assertEqual(set(results), {"RequestKeyConflict"})
        self.assertEqual(len(self.store.snapshot()), 1)

    def test_known_reconnect_diagnostic_writes_at_most_once_per_minute(self):
        self.store.enroll(1, key(1), 4)
        created = self.store.snapshot()[0]["created_at"]
        with patch.object(self.store, "_write", wraps=self.store._write) as write:
            for _ in range(20):
                self.assertEqual(self.store.enroll(1, key(1), 4)[0], "RequestAlreadyRegistered")
            self.now += 59
            self.store.enroll(1, key(1), 4)
            write.assert_not_called()
            self.now += 2
            self.store.enroll(1, key(1), 4)
            self.assertEqual(write.call_count, 1)
            self.store.enroll(1, key(1), 4)
            self.now -= 120
            self.store.enroll(1, key(1), 4)
            self.assertEqual(write.call_count, 1)
        peer = self.store.snapshot()[0]
        self.assertEqual(peer["created_at"], created)
        self.assertIsNone(peer["last_handshake_at"])
        self.assertEqual(peer["last_request_at"], created + 61)

    def test_unchanged_runtime_sweeps_do_not_write(self):
        self.store.enroll(1, key(1), 4)
        with patch.object(self.store, "_write", wraps=self.store._write) as write:
            self.store.update_handshakes({})
            self.store.update_handshakes({key(1): 0})
            self.store.expire(dry_run=False)
            write.assert_not_called()
            self.store.update_handshakes({key(1): self.now - 1})
            self.assertEqual(write.call_count, 1)
            self.store.update_handshakes({key(1): self.now - 1})
            self.store.update_handshakes({key(1): self.now - 2})
            self.store.update_handshakes({key(1): 0})
            self.assertEqual(write.call_count, 1)

    def test_ttl_handshakes_and_revocation(self):
        self.store.enroll(1, key(1), 4)
        self.now += 29 * 86400
        self.store.enroll(1, key(1), 4)
        self.store.update_handshakes({key(1): self.now})
        self.store.update_handshakes({key(1): 0})
        self.now += 29 * 86400
        self.assertEqual(self.store.expired(), [])
        self.now += 2 * 86400
        self.assertEqual(len(self.store.expire()), 1)
        self.assertEqual(len(self.store.snapshot()), 1)
        self.store.expire(dry_run=False)
        self.assertEqual(self.store.enroll(1, key(1), 4)[0], "RequestAccepted")
        self.store.revoke(1)
        self.assertEqual(self.store.enroll(1, key(1), 4)[0], "RequestRevoked")
        self.assertEqual(self.store.enroll(2, key(1), 4)[0], "RequestRevoked")
        self.now += 50 * 86400
        self.assertEqual(self.store.expired(), [])

    def test_never_handshake_clock_backward_and_pinned(self):
        self.store.enroll(1, key(1), 4)
        self.assertEqual(self.store.expired(now=0), [])
        self.now += 31 * 86400
        self.assertEqual(len(self.store.expired()), 1)
        state = json.loads(self.store.path.read_text())
        state["peers"][0]["pinned"] = True
        self.store.path.write_text(json.dumps(state))
        self.assertEqual(self.store.expired(), [])

    def test_corruption_not_overwritten_and_capacity(self):
        store = GatewayStore(self.temp.name, max_peers=1)
        store.enroll(1, key(1), 4)
        self.assertEqual(store.enroll(2, key(2), 4)[0], "RequestLimitReached")
        store.path.write_text('{"version":2,"peers":[]}')
        with self.assertRaises(StoreError):
            store.enroll(2, key(2), 4)
        self.assertIn('"version":2', store.path.read_text())


if __name__ == "__main__":
    unittest.main()
