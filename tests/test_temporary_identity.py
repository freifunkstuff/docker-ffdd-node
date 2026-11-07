from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import node_identity
import node_config
import registrar
import sysinfo


class TemporaryIdentityTests(unittest.TestCase):
    def config(self, **env):
        return registrar.require_valid_registrar_config(defaults={}, env={
            "NODE_REGISTRATION_MODE": "temporary", "BACKBONE_ENABLED": "0", **env,
        })

    def test_ffdd_fixed_digest_and_seconds(self):
        # abc -> 900150983cd24fb0d6963f7d28e17f72 -> 915983
        self.assertEqual(node_identity.temporary_node_id(123.99, "abc"), 906)
        self.assertEqual(node_identity.temporary_node_id(0, "abc"), 983)
        with patch.object(node_identity.hashlib, "md5", return_value=Mock(hexdigest=lambda: "a0b1c2d3e4f5a6b7")):
            self.assertEqual(node_identity.temporary_node_id(5, "ignored"), 961)

    def test_container_pid1_lifetime_uses_resource_provider(self):
        with patch.object(node_identity, "read_resources", return_value=Mock(uptime_seconds=750.75)) as read:
            self.assertEqual(node_identity.container_uptime_seconds(), 750.75)
            read.assert_called_once_with(scope="container")
        with patch.object(node_identity, "read_resources", return_value=Mock(uptime_seconds=None)):
            with self.assertRaisesRegex(RuntimeError, "PID 1"):
                node_identity.container_uptime_seconds()

    def test_persistence_and_separate_registered_identity(self):
        state = {"registration": {"node_id": 12345, "register_key": "keep"}, "fastd": {"secret": "keep-fastd"}}
        original = copy.deepcopy(state)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NODE_YAML": str(Path(directory) / "node.yaml")}):
            with patch.object(node_identity.subprocess, "run", return_value=Mock(stdout="abc")), patch.object(node_identity, "container_uptime_seconds", return_value=123.9):
                self.assertEqual(node_identity.ensure_temporary_node_id(state), 906)
            node_config.save_state(state)
            restarted = node_config.load_state()
            with patch.object(node_identity.subprocess, "run", side_effect=AssertionError("must not recalculate")):
                self.assertEqual(node_identity.ensure_temporary_node_id(restarted), 906)
            self.assertEqual(restarted["registration"], original["registration"])
            self.assertEqual(restarted["fastd"], original["fastd"])
            self.assertEqual(node_identity.effective_node_id({}, restarted), 12345)

    def test_temporary_checkconfig_ignores_registry_and_disabled_peers(self):
        with patch.object(registrar, "build_registration_ssl_context", side_effect=AssertionError("unused TLS")), patch.object(registrar, "requested_peer_string", side_effect=AssertionError("unused peers")), patch.object(registrar, "probe_wireguard_support", side_effect=AssertionError("unused WG")):
            config = self.config(NODE_REGISTRATION_CA_FILE="/missing", NODE_REGISTRATION_URL="bad", INITIAL_NODE_ID="bad", BACKBONE_PEERS="bad")
        self.assertEqual(config["backbone"]["enabled"], 0)

    def test_temporary_default_fastd_and_registry_short_circuit(self):
        config = registrar.require_valid_registrar_config(defaults={
            "BACKBONE_PEERS": "fastd;vpn.example.org:5002;" + "a" * 64,
        }, env={"NODE_REGISTRATION_MODE": "temporary"})
        self.assertEqual(config["backbone"]["enabled"], 1)
        with patch.object(registrar, "build_registration_url", side_effect=AssertionError("Registry forbidden")):
            self.assertEqual(registrar.register_once(config, {"temporary": {"node_id": 950}}), (950, ""))

    def test_default_registered_and_mode_validation(self):
        config = registrar.require_valid_registrar_config(defaults={}, env={"BACKBONE_ENABLED": "0", "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__"})
        self.assertEqual(node_identity.registration_mode(config), "registered")
        with self.assertRaises(SystemExit):
            self.config(NODE_REGISTRATION_MODE="automatic")
        with self.assertRaises(SystemExit):
            registrar.require_valid_registrar_config(defaults={}, env={"BACKBONE_ENABLED": "0"})

    def test_no_registry_request_or_peer_api_and_empty_runtime(self):
        config = self.config()
        state = {"temporary": {"node_id": 906}, "registration": {"node_id": 12345, "register_key": "keep"}, "fastd": {"secret": "secret"}, "wireguard": {"secret": "wg-secret"}}
        original = copy.deepcopy(state)
        with ExitStack() as stack:
            for name in ("build_registration_url", "build_registration_ssl_context", "fetch_wireguard_peer_info", "requested_peer_string", "ensure_register_key"):
                stack.enter_context(patch.object(registrar, name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(registrar, "load_state", return_value=state))
            stack.enter_context(patch.object(registrar, "save_state"))
            stack.enter_context(patch.object(registrar, "derive_wireguard_public_key", return_value="public"))
            fastd = stack.enter_context(patch.object(registrar, "render_fastd_runtime", return_value=False))
            wg = stack.enter_context(patch.object(registrar, "reconcile_wireguard_runtime", return_value=(False, [])))
            mesh = stack.enter_context(patch.object(registrar, "reconcile_external_mesh"))
            stack.enter_context(patch.object(registrar, "render_bmxd_runtime", return_value=False))
            self.assertEqual(registrar.run_iteration(False, True, config)[0], 906)
            self.assertEqual(fastd.call_args.args[2:], ([], 906))
            self.assertEqual(wg.call_args.args[2:4], ([], 906))
            self.assertEqual(mesh.call_args.args[1:], (906, []))
        self.assertEqual(state, original)

    def test_sysinfo_reports_effective_id_and_mode(self):
        config = sysinfo.require_valid_sysinfo_config(defaults={}, env={"NODE_REGISTRATION_MODE": "temporary", "NODE_CONTACT_EMAIL": "a@example.org", "NODE_NAME": "test"})
        state = {"registration": {"node_id": 12345}, "temporary": {"node_id": 906}}
        self.assertEqual(sysinfo._build_node_meta(config, state)["node"], "906")
        with patch.object(sysinfo, "_collect_bmxd_nodes_data", return_value={}):
            nodes = sysinfo.build_nodes_payload(config, state)
        self.assertEqual(nodes["node"]["registration_mode"], "temporary")
        with patch.object(sysinfo, "_build_bmxd_block", return_value={}), patch.object(sysinfo, "_build_statistic_block", return_value={}), patch.object(sysinfo, "_build_system_block", return_value={}):
            payload = sysinfo.render_stub_payload(config, state, nodes)
        self.assertEqual(payload["data"]["common"]["node"], "906")
        self.assertEqual(payload["data"]["common"]["registration_mode"], "temporary")


if __name__ == "__main__":
    unittest.main()
