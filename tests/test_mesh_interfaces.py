from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mesh_interfaces as mesh
import registrar
from node_config import MESH_CONFIG_SCHEMA, build_base_config, node_addresses, resolve_config


class MeshInterfacesTests(unittest.TestCase):
    def setUp(self):
        self.name = "mesh1062"
        self.ip = node_addresses(1062)["nonprimary_ip"]
        self.link = {"ifindex": 42, "mtu": 1500, "flags": []}
        self.addresses = []
        self.routes = [{"dst": "default", "dev": "internet0"}]

    def query(self, *args):
        if "route" in args:
            return self.routes if "-4" in args else []
        if "link" in args:
            if self.link is None:
                raise subprocess.CalledProcessError(1, ["ip", *args])
            return [self.link]
        return [{"addr_info": self.addresses}]

    def ensure(self, **kwargs):
        return mesh.ensure_mesh_interfaces([self.name], 1062, **kwargs)

    def test_schema_default_and_whitespace_names(self):
        config = resolve_config(schema=MESH_CONFIG_SCHEMA, defaults={}, env={}).values
        self.assertEqual(config["mesh"]["interfaces"], "")
        self.assertEqual(mesh.parse_mesh_interfaces(" eth1\tmesh1062\neth1 "), ["eth1", "mesh1062"])

    def test_invalid_and_reserved_names(self):
        for name in ("lo", "bmx_prime", "tbb_fastd", "tbbwg", "tbb_wg51020", "-bad", "x;id", "x/2", "a" * 16):
            with self.subTest(name=name), self.assertRaises(ValueError):
                mesh.parse_mesh_interfaces(name)
        with self.assertRaises(ValueError):
            mesh.parse_mesh_interfaces("custom", backbone_interfaces=["custom"])

    def test_empty_feature_has_no_network_calls(self):
        with patch.object(mesh, "_ip") as command, patch.object(mesh, "_json") as query:
            result = mesh.ensure_mesh_interfaces([], 1062)
        self.assertTrue(result.ready)
        self.assertFalse(result.changed)
        command.assert_not_called()
        query.assert_not_called()

    def test_firmware_address_mtu_and_non_destructive_commands(self):
        with patch.object(mesh, "_json", side_effect=self.query), patch.object(mesh, "_ip") as command:
            result = self.ensure()
        self.assertTrue(result.ready)
        calls = [call.args for call in command.call_args_list]
        self.assertEqual(calls, [
            ("link", "set", "dev", self.name, "mtu", "1200", "up"),
            ("addr", "replace", f"{self.ip}/16", "broadcast", "10.255.255.255", "dev", self.name),
        ])

    def test_existing_link_without_ipv4_record_is_initialized(self):
        def query(*args):
            if "addr" in args:
                return []
            return self.query(*args)
        with patch.object(mesh, "_json", side_effect=query), patch.object(mesh, "_ip") as command:
            result = self.ensure()
        self.assertTrue(result.ready)
        self.assertIn(("addr", "replace", f"{self.ip}/16", "broadcast", "10.255.255.255", "dev", self.name),
                      [c.args for c in command.call_args_list])

    def test_already_ready_is_idempotent(self):
        self.link.update(mtu=1200, flags=["UP"])
        self.addresses = [{"local": self.ip, "prefixlen": 16, "broadcast": "10.255.255.255"}]
        previous = {self.name: {"ifindex": 42, "address": self.ip, "status": "ready"}}
        with patch.object(mesh, "_json", side_effect=self.query), patch.object(mesh, "_ip") as command:
            result = self.ensure(previous=previous)
        self.assertTrue(result.ready)
        self.assertFalse(result.changed)
        command.assert_not_called()

    def test_missing_then_recreated_is_readdressed(self):
        previous = {self.name: {"ifindex": 41, "address": self.ip, "status": "ready"}}
        self.link = None
        with patch.object(mesh, "_json", side_effect=self.query), patch.object(mesh, "_ip") as command:
            missing = self.ensure(previous=previous)
        self.assertFalse(missing.ready)
        self.assertIn("waiting", missing.state[self.name]["status"])
        command.assert_not_called()
        self.link = {"ifindex": 43, "mtu": 1500, "flags": []}
        with patch.object(mesh, "_json", side_effect=self.query), patch.object(mesh, "_ip") as command:
            repaired = self.ensure(previous=missing.state)
        self.assertTrue(repaired.ready)
        self.assertTrue(repaired.changed)
        self.assertEqual(repaired.state[self.name]["ifindex"], 43)
        self.assertIn(("addr", "replace", f"{self.ip}/16", "broadcast", "10.255.255.255", "dev", self.name),
                      [c.args for c in command.call_args_list])

    def test_readdress_only_removes_recorded_address(self):
        old = "10.201.1.1"
        self.addresses = [{"local": old, "prefixlen": 16}, {"local": "192.0.2.5", "prefixlen": 24}]
        previous = {self.name: {"ifindex": 42, "address": old, "status": "ready"}}
        with patch.object(mesh, "_json", side_effect=self.query), patch.object(mesh, "_ip") as command:
            self.ensure(previous=previous)
        deleted = [c.args for c in command.call_args_list if c.args[:2] == ("addr", "del")]
        self.assertEqual(deleted, [("addr", "del", old + "/16", "dev", self.name)])

    def test_uplink_conflict_is_refused_without_mutation(self):
        for routes in ([{"dst": "default", "dev": self.name}],
                       [{"dst": "default", "nexthops": [{"dev": self.name}]}]):
            self.routes = routes
            with patch.object(mesh, "_json", side_effect=self.query), patch.object(mesh, "_ip") as command:
                result = self.ensure()
            self.assertFalse(result.ready)
            self.assertIn("uplink conflict", result.state[self.name]["status"])
            command.assert_not_called()

    def test_render_keeps_external_interfaces_separate_from_backbone(self):
        config = build_base_config()
        config["mesh"] = {"interfaces": "eth1 mesh1062"}
        with tempfile.TemporaryDirectory() as directory, patch.object(registrar, "BMXD_ENV_FILE", Path(directory) / "bmxd.env"):
            registrar.render_bmxd_runtime(config, 1062, ["tbb_fastd"])
            content = registrar.BMXD_ENV_FILE.read_text()
        self.assertIn('BMXD_MESH_INTERFACES="eth1 mesh1062"', content)
        self.assertIn('BMXD_BACKBONE_INTERFACES="tbb_fastd"', content)

    def test_forwarding_guard_failure_is_waiting_and_never_activates_interface(self):
        config = build_base_config()
        config["mesh"] = {"interfaces": self.name}
        with tempfile.TemporaryDirectory() as directory, patch.object(registrar, "BMXD_RUNTIME_DIR", Path(directory)), \
                patch.object(registrar, "apply_mesh_forwarding_guard", side_effect=OSError("guard unavailable")), \
                patch.object(registrar, "ensure_mesh_interfaces") as ensure:
            marker = Path(directory) / "service-applied.sha256"
            marker.write_text("old")
            result = registrar.reconcile_external_mesh(config, 1062, ["tbb_fastd"])
            self.assertFalse(result.ready)
            self.assertIn("forwarding guard failed", result.state[self.name]["status"])
            self.assertFalse(marker.exists())
            ensure.assert_not_called()

    def test_registrar_config_rejects_primary_and_accepts_generic_names(self):
        defaults = {
            "BACKBONE_PEERS": "fastd;vpn.example.org:5002;" + "a" * 64,
            "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
        }
        for name in ("eth1", "mesh1062", "mesh0"):
            config = registrar.require_valid_registrar_config(defaults=defaults, env={"MESH_INTERFACES": name})
            self.assertEqual(registrar.configured_mesh_interfaces(config), [name])
        with self.assertRaises(SystemExit):
            registrar.require_valid_registrar_config(defaults=defaults, env={"MESH_INTERFACES": "bmx_prime"})

    def test_registrar_poll_invalidates_ack_and_restarts_after_repair(self):
        config = build_base_config()
        config["mesh"] = {"interfaces": self.name}
        with tempfile.TemporaryDirectory() as directory, patch.object(registrar, "BMXD_RUNTIME_DIR", Path(directory)), \
                patch.object(mesh, "_json", side_effect=self.query), patch.object(mesh, "_ip"), \
                patch.object(registrar, "apply_mesh_forwarding_guard"), \
                patch.object(registrar, "reconcile_service") as service:
            marker = Path(directory) / "service-applied.sha256"
            marker.write_text("old")
            self.link = None
            result = registrar.reconcile_external_mesh(config, 1062, ["tbb_fastd"])
            self.assertFalse(result.ready)
            self.assertFalse(marker.exists())
            registrar.poll_external_mesh(config)
            service.assert_not_called()
            self.link = {"ifindex": 43, "mtu": 1500, "flags": []}
            registrar.poll_external_mesh(config)
            service.assert_called_once()
            state = json.loads((Path(directory) / "mesh-interfaces.json").read_text())
            self.assertEqual(state["interfaces"][self.name]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
