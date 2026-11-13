import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from gateway_api import GatewayApplication, make_server
from gateway_store import GatewayStore
try:
    from .test_gateway_store import key
except ImportError:
    from test_gateway_store import key


class GatewayAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.identity = Path(self.temp.name) / "identity.json"
        self.identity.write_text(json.dumps({"node": 9, "key": key(9), "port": 5003}))
        self.runtime = Path(self.temp.name) / "runtime.json"
        self.outgoing = Path(self.temp.name) / "outgoing.json"
        self.outgoing.write_text(json.dumps({"peers": []}))
        self.store = GatewayStore(self.temp.name)
        self.app = GatewayApplication(self.store, role="gateway", identity_file=self.identity,
                                      runtime_file=self.runtime, apply=lambda p: True,
                                      outgoing_file=self.outgoing)

    def query(self, node=1, publickey=None):
        return "/wg.cgi?" + urlencode({"node": node, "key": publickey or key(1)})

    def test_role_off_has_no_gateway_side_effects(self):
        with patch("gateway_api.GatewayStore") as constructor, patch("gateway_api.bounded_json") as reader:
            with patch.dict("os.environ", {"NODE_ROLE": "node", "GATEWAY_MAX_PEERS": "invalid"}):
                app = GatewayApplication()
                self.assertIsNone(app.store)
                self.assertEqual(app.handle("/wg.cgi"), (404, {"status": "NotFound"}))
            constructor.assert_not_called()
            reader.assert_not_called()

    def test_role_and_discovery(self):
        self.app.role = "node"
        self.assertEqual(self.app.handle("/wg.cgi"), (404, {"status": "NotFound"}))
        self.app.role = "gateway"
        self.assertEqual(self.app.handle("/wg.cgi")[1]["status"], "NotRestricted")
        self.app.restricted = True
        self.assertEqual(self.app.handle("/wg.cgi")[1]["status"], "Restricted")
        self.identity.unlink()
        self.assertEqual(self.app.handle("/wg.cgi")[1], {"status": "NotConfigured"})

    def test_enroll_echo_conflict_and_restricted(self):
        result = self.app.handle(self.query())[1]
        self.assertEqual(result["status"], "RequestAccepted")
        self.assertEqual(result["client"], {"node": 1, "key": key(1)})
        self.app.restricted = True
        self.assertEqual(self.app.handle(self.query())[1]["status"], "RequestAlreadyRegistered")
        self.assertEqual(self.app.handle(self.query(2, key(2)))[1]["status"], "Restricted")
        self.assertEqual(self.app.handle(self.query(1, key(2)))[1]["status"], "RequestConflict")
        self.assertEqual(self.app.handle(self.query(2))[1]["status"], "RequestKeyConflict")
        self.assertEqual(self.app.handle(self.query(9))[1]["status"], "RequestConflict")
        self.assertEqual(self.app.handle(self.query(2, key(9)))[1]["status"], "RequestKeyConflict")

    def test_outgoing_conflicts_do_not_persist_and_exact_pair_allowed(self):
        self.outgoing.write_text(json.dumps({"peers": [{"node": 1, "key": key(1)}]}))
        self.assertEqual(self.app.handle(self.query(1, key(2)))[1], {"status": "RequestConflict"})
        self.assertEqual(self.app.handle(self.query(2, key(1)))[1], {"status": "RequestKeyConflict"})
        self.assertEqual(self.store.snapshot(), [])
        self.assertEqual(self.app.handle(self.query())[1]["status"], "RequestAccepted")

    def test_restricted_static_pair_authorized_but_runtime_and_revoke_still_gate(self):
        self.app.restricted = True
        self.outgoing.write_text(json.dumps({"peers": [{"node": 1, "key": key(1)}]}))
        self.assertEqual(self.app.handle(self.query(1, key(2)))[1], {"status": "RequestConflict"})
        self.assertEqual(self.store.snapshot(), [])
        self.app.apply = lambda peer: False
        self.assertEqual(self.app.handle(self.query())[1], {"status": "RequestFailed"})
        self.assertEqual(len(self.store.snapshot()), 1)
        self.app.apply = lambda peer: True
        self.assertEqual(self.app.handle(self.query())[1]["status"], "RequestAlreadyRegistered")
        self.store.expire(now=self.store.snapshot()[0]["created_at"] + 31 * 86400, dry_run=False)
        self.assertEqual(self.app.handle(self.query())[1]["status"], "RequestAccepted")
        self.store.revoke(1)
        self.assertEqual(self.app.handle(self.query())[1], {"status": "RequestRevoked"})
        self.assertEqual(self.app.handle(self.query(2, key(2)))[1]["status"], "Restricted")

    def test_missing_or_corrupt_outgoing_fails_closed(self):
        for raw in (None, "not json", '{"peers":{}}', '{"peers":[{"node":1,"key":"bad"}]}'):
            if raw is None:
                self.outgoing.unlink()
            else:
                self.outgoing.write_text(raw)
            self.assertEqual(self.app.handle(self.query())[1], {"status": "RequestFailed"})
            self.assertEqual(self.store.snapshot(), [])

    def test_runtime_confirmation_no_false_success(self):
        self.app.apply = None
        self.app.wait_seconds = 0
        self.assertEqual(self.app.handle(self.query())[1], {"status": "RequestFailed"})
        self.runtime.write_text(json.dumps({"state": "ready", "timestamp": time.time(), "peers": {"1": {"key": key(2), "applied": True}}}))
        self.assertEqual(self.app.handle(self.query())[1], {"status": "RequestFailed"})
        self.runtime.write_text(json.dumps({"state": "ready", "timestamp": time.time(), "peers": {"1": {"key": key(1), "applied": True}}}))
        self.assertEqual(self.app.handle(self.query())[1]["status"], "RequestAlreadyRegistered")

    def test_runtime_nonready_or_stale_cannot_ack_retained_peers(self):
        self.app.apply = None
        self.app.wait_seconds = 0
        for state, age in ((None, 0), ('repairing', 0), ('applying', 0), ('retrying', 0), ('ready', 16)):
            now = time.time()
            self.runtime.write_text(json.dumps({'state': state, 'timestamp': now - age,
                'last_success_at': now - 3, 'repair_started_at': now - 2,
                'peers': {'1': {'key': key(1), 'applied': True}}}))
            self.assertEqual(self.app.handle(self.query())[1], {'status': 'RequestFailed'})

    def test_repair_confirms_only_prior_success_not_store_known_retry(self):
        self.app.apply = None
        self.app.wait_seconds = 0
        self.store.enroll(1, key(1), 9)
        now = time.time()
        repair = dict(state='repairing', timestamp=now, last_success_at=now - 1,
                      repair_started_at=now, peers={},
                      confirmed_peers={'1': {'key': key(1), 'applied': True}})
        self.runtime.write_text(json.dumps(repair))
        self.assertEqual(self.app.handle(self.query())[1]['status'], 'RequestAlreadyRegistered')
        for _ in range(3):
            self.assertEqual(self.app.handle(self.query(2, key(2)))[1], {'status': 'RequestFailed'})
        self.assertEqual({peer['node'] for peer in self.store.snapshot()}, {1, 2})
        self.assertEqual(self.app.handle(self.query(1, key(2)))[1], {'status': 'RequestConflict'})
        self.assertEqual(self.app.handle(self.query(3, key(1)))[1], {'status': 'RequestKeyConflict'})
        self.store.revoke(1)
        self.assertEqual(self.app.handle(self.query())[1], {'status': 'RequestRevoked'})
        self.runtime.write_text(json.dumps(dict(state='ready', timestamp=time.time(),
            peers={'2': {'key': key(2), 'applied': True}})))
        self.assertEqual(self.app.handle(self.query(2, key(2)))[1]['status'], 'RequestAlreadyRegistered')

    def test_repair_confirmation_expiry_and_cross_state_maps(self):
        self.app.apply = None
        self.app.wait_seconds = 0
        peers = {'1': {'key': key(1), 'applied': True}}
        now = time.time()
        repair = dict(state='repairing', timestamp=now, last_success_at=now - 1,
                      repair_started_at=now, peers={}, confirmed_peers=peers)
        for record in (dict(repair, repair_started_at=now - 10, last_success_at=now - 11),
                       dict(repair, last_success_at=now - 15),
                       dict(repair, state='ready'), dict(repair, state='applying'),
                       dict(repair, state='retrying'),
                       dict(repair, peers=peers, confirmed_peers={}),
                       dict(repair, confirmed_peers={'1': {'key': key(1), 'applied': 1}})):
            with self.subTest(record=record):
                self.runtime.write_text(json.dumps(record))
                self.assertEqual(self.app.handle(self.query())[1], {'status': 'RequestFailed'})

    def test_errors_limits_and_no_leaks(self):
        for target in ("/wg.cgi?node=1", self.query()+"&NODE=2", "/wg.cgi?node=65536&key=bad",
                       "/wg.cgi?node=1&key=bad", "/wg.cgi?private=secret", "/wg.cgi?node"):
            self.assertEqual(self.app.handle(target)[1], {"status": "RequestFailed"})
        self.assertEqual(self.app.handle("/wg.cgi", "POST")[0], 405)
        self.assertEqual(self.app.handle("/wg.cgi?" + "x"*3000)[0], 414)
        self.app.rate_limit = 1
        self.assertEqual(self.app.handle(self.query(2, key(2)))[0], 200)
        self.assertEqual(self.app.handle(self.query(3, key(3)))[0], 429)
        self.app.rate_limit = 120
        self.app.apply = lambda p: (_ for _ in ()).throw(RuntimeError("private path"))
        self.assertEqual(self.app.handle(self.query())[1], {"status": "RequestFailed"})

    def test_discovery_and_fleet_reconnect_bypass_new_claim_budget(self):
        self.identity.write_text(json.dumps({"node": 9, "key": key(255), "port": 5003}))
        for node in range(140):
            self.store.enroll(node + 100, key(node), 9)
        self.app.rate_limit = 1
        self.assertEqual(self.app.handle(self.query(400, key(200)))[0], 200)
        self.assertEqual(self.app.handle(self.query(401, key(201)))[0], 429)
        with patch.object(self.store, "snapshot", side_effect=AssertionError("discovery must not scan peers")):
            for _ in range(140):
                self.assertEqual(self.app.handle("/wg.cgi")[1]["status"], "NotRestricted")
        for node in range(140):
            self.assertEqual(self.app.handle(self.query(node + 100, key(node)))[1]["status"],
                             "RequestAlreadyRegistered")
        self.assertEqual(len(self.app.requests), 1)

    def test_case_and_loopback(self):
        target = "/wg.cgi?" + urlencode({"Node": 0, "Key": key(1)})
        self.assertEqual(self.app.handle(target)[1]["status"], "RequestAccepted")
        server = make_server(self.app, port=0)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
