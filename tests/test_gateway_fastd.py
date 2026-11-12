import contextlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts/gateway_fastd.py"
spec = importlib.util.spec_from_file_location("gateway_fastd", MODULE)
gf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gf)
KEY = "ab" * 32
OTHER = "cd" * 32


class GatewayFastdTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = {"NODE_ROLE": "gateway", "GATEWAY_FASTD_ROOT": str(root / "persistent"),
                    "GATEWAY_FASTD_CONNECTION_DIR": str(root / "run"), "PEER_KEY": KEY}

    def gateway(self, **extra):
        return gf.GatewayFastd(dict(self.env, **extra))

    def test_role_blocked(self):
        for role in ("node", "", "other"):
            self.assertEqual(gf.main(["verify"], dict(self.env, NODE_ROLE=role)), 1)
        env = dict(self.env)
        del env["NODE_ROLE"]
        self.assertEqual(gf.main(["verify"], env), 1)
        self.assertFalse(Path(self.env["GATEWAY_FASTD_ROOT"]).exists())

    def test_strict_keys_and_no_key_in_error(self):
        for key in ("../bad", KEY + "\n", '"' + KEY, "z" * 64, "", "a" * 63):
            output = io.StringIO()
            with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                self.assertEqual(gf.main(["verify"], dict(self.env, PEER_KEY=key)), 1)
            self.assertEqual(output.getvalue(), "gateway fastd event failed\n")
        self.assertTrue(self.gateway().verify(KEY.upper()))

    def test_restricted_known_and_unknown(self):
        restricted = self.gateway(GATEWAY_FASTD_RESTRICT="true")
        self.assertFalse(restricted.verify(KEY))
        self.assertTrue(self.gateway().verify(KEY))
        self.assertTrue(restricted.verify(KEY))
        self.assertFalse(restricted.verify(OTHER))

    def test_deny_priority_and_ip_privacy(self):
        path = Path(self.temp.name) / "policy.json"
        path.write_text(json.dumps({"allow_keys": [KEY], "deny_ips": ["198.51.100.1"]}))
        gateway = self.gateway(GATEWAY_FASTD_POLICY_FILE=str(path), PEER_ADDRESS="198.51.100.1")
        self.assertFalse(gateway.verify(KEY))
        path.write_text(json.dumps({"allow_ips": ["198.51.100.1"]}))
        self.assertTrue(gateway.verify(KEY))
        self.assertNotIn("198.51.100.1", gateway.state_path.read_text())
        path.write_text(json.dumps({"allow_keys": [KEY], "deny_keys": [KEY]}))
        self.assertFalse(gateway.verify(KEY))

    def test_atomic_permissions_and_no_private_material(self):
        gateway = self.gateway()
        self.assertTrue(gateway.verify(KEY))
        config = gateway._config(KEY)
        self.assertEqual(config.read_text(), 'key "' + KEY + '";\n')
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(gateway.state_path.stat().st_mode), 0o600)
        self.assertEqual(list(gateway.accepted.glob(".gateway-*")), [])
        files = [p for p in gateway.root.rglob("*") if p.is_file()]
        self.assertEqual({p.name for p in files}, {".lock", "state.json", config.name})
        self.assertFalse(any("secret" in p.read_text() or "private" in p.read_text() for p in files))

    def test_ephemeral_connections_and_reset(self):
        gateway = self.gateway(PEER_ADDRESS="198.51.100.2", INTERFACE="tbb_fastd")
        self.assertTrue(gateway.establish(KEY))
        self.assertEqual(gateway.counts(), {"accepted": 1, "connected": 1})
        self.assertEqual((gateway.connections / KEY).read_text(), "")
        gateway.disestablish(KEY)
        self.assertEqual(gateway.counts()["connected"], 0)
        gateway.establish(KEY)
        gateway.reset()
        self.assertEqual(gateway.counts(), {"accepted": 1, "connected": 0})
        self.assertTrue(gateway._config(KEY).exists())

    def test_persistent_revocation_no_open_reenroll(self):
        gateway = self.gateway()
        gateway.establish(KEY)
        gateway.revoke(KEY)
        self.assertFalse(self.gateway().verify(KEY))
        self.assertFalse(gateway._config(KEY).exists())
        self.assertTrue((gateway.connections / KEY).exists())
        self.assertEqual(gateway.counts(), {"accepted": 0, "connected": 1})
        gateway.disestablish(KEY)
        self.assertEqual(gateway.counts()["connected"], 0)
        gateway.unrevoke(KEY)
        self.assertTrue(gateway.verify(KEY))

    def test_capacity_and_default_no_aging(self):
        gateway = self.gateway(GATEWAY_FASTD_MAX_RECORDS="1")
        gateway.verify(KEY)
        state = gateway._state()
        state["records"][KEY]["created"] = 1
        state["records"][KEY]["lastSeen"] = 1
        gateway._save(state)
        self.assertTrue(self.gateway(GATEWAY_FASTD_RESTRICT="true").verify(KEY))
        self.assertEqual(gateway._state()["records"][KEY]["created"], 1)
        self.assertFalse(gateway.verify(OTHER))
        gateway.revoke(KEY)
        self.assertFalse(gateway.verify(OTHER))

    def test_self_key_rejected_without_identity_generation(self):
        path = Path(self.temp.name) / "public"
        path.write_text(KEY + "\n")
        self.assertFalse(self.gateway(GATEWAY_FASTD_OWN_PUBLIC_KEY_FILE=str(path)).verify(KEY))
        self.assertEqual(path.read_text(), KEY + "\n")


if __name__ == "__main__":
    unittest.main()
