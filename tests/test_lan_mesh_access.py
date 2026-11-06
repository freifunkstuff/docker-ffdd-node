from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lan_mesh_access import LAN_MESH_ACCESS_CONFIG_SCHEMA, parse_allowed_sources, resolve_environment
import node_config


class LanMeshAccessTests(unittest.TestCase):
    def test_default_disabled(self):
        self.assertEqual(resolve_environment(defaults={}, env={}), {
            "lan_mesh_access": {"enabled": False, "sources": ""},
        })

    def test_boolean_schema(self):
        for text, expected in (("true", True), ("false", False), ("1", True), ("0", False)):
            with self.subTest(text=text):
                result = node_config.resolve_config(
                    schema=LAN_MESH_ACCESS_CONFIG_SCHEMA, defaults={},
                    env={"LAN_MESH_ACCESS_ENABLED": text}, base_values={},
                )
                self.assertEqual(result.errors, [])
                self.assertIs(result.values["lan_mesh_access"]["enabled"], expected)
        with self.assertRaises(SystemExit):
            resolve_environment(defaults={}, env={"LAN_MESH_ACCESS_ENABLED": "maybe"})

    def test_enabled_requires_sources(self):
        with self.assertRaisesRegex(ValueError, "required"):
            resolve_environment(defaults={}, env={"LAN_MESH_ACCESS_ENABLED": "true"})
        self.assertEqual(parse_allowed_sources(" \t", False), [])

    def test_trusted_private_and_public_sources(self):
        self.assertEqual(parse_allowed_sources("192.168.10.0/24\t172.16.0.1/32 8.8.8.0/24", True),
                         ["192.168.10.0/24", "172.16.0.1/32", "8.8.8.0/24"])

    def test_reject_invalid_or_noncanonical_cidrs(self):
        for source in ("garbage", "192.168.1.3/24", "192.168.1.1", "192.168.1.0/255.255.255.0", "::/0", "192.168.01.0/24"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_allowed_sources(source, True)

    def test_reject_unsafe_and_mesh_overlap(self):
        for source in ("0.0.0.0/0", "0.0.0.0/8", "127.0.0.1/32", "169.254.0.0/16",
                       "224.0.0.0/4", "255.255.255.255/32", "240.0.0.0/4",
                       "10.200.0.0/16", "10.201.0.1/32", "10.203.0.0/16", "10.0.0.0/8", "10.202.0.0/15"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                parse_allowed_sources(source, True)
        self.assertEqual(parse_allowed_sources("10.202.0.0/16", True), ["10.202.0.0/16"])

    def test_disabled_still_validates_nonempty_sources(self):
        with self.assertRaises(ValueError):
            parse_allowed_sources("0.0.0.0/0", False)

    def test_resolve_environment_uses_runtime_defaults_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            defaults = Path(directory) / "defaults.yaml"
            defaults.write_text("{}\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEFAULTS_FILE": str(defaults)}, clear=True):
                self.assertFalse(resolve_environment()["lan_mesh_access"]["enabled"])


if __name__ == "__main__":
    unittest.main()
