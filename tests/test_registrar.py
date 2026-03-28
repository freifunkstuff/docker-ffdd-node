from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import registrar
from node_config import build_base_config


class RegistrarTests(unittest.TestCase):
    def test_require_valid_registrar_config_uses_defaults_for_blank_values(self) -> None:
        config = registrar.require_valid_registrar_config(
            defaults={
                "FASTD_PEERS": "vpn1.example.org:5002;deadbeef",
                "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                "INITIAL_NODE_ID": 52001,
            },
            env={
                "FASTD_PEERS": "",
                "NODE_REGISTRATION_URL": "",
                "REGISTRAR_INTERVAL": "3600",
            },
        )

        self.assertEqual(config["fastd"]["peers"], "vpn1.example.org:5002;deadbeef")
        self.assertIn("__NODE_REGISTER_KEY__", config["registrar"]["registration_url"])
        self.assertIn("__NODE_ID__", config["registrar"]["registration_url"])

    def test_require_valid_registrar_config_rejects_missing_placeholders(self) -> None:
        with self.assertRaises(SystemExit):
            registrar.require_valid_registrar_config(
                defaults={"FASTD_PEERS": "vpn1.example.org:5002;deadbeef"},
                env={
                    "FASTD_PEERS": "vpn1.example.org:5002;deadbeef",
                    "NODE_REGISTRATION_URL": "https://example.invalid/static",
                    "REGISTRAR_INTERVAL": "3600",
                },
            )

    def test_parse_peer_string_deduplicates_by_host_and_port(self) -> None:
        peers = registrar.parse_peer_string(
            "vpn1.example.org:5002;key1 vpn1.example.org:5002;otherkey vpn2.example.org:5002;key2"
        )

        self.assertEqual(
            peers,
            [
                {"host": "vpn1.example.org", "port": "5002", "key": "key1"},
                {"host": "vpn2.example.org", "port": "5002", "key": "key2"},
            ],
        )

    def test_requested_peer_string_prefers_environment(self) -> None:
        with patch.dict(os.environ, {"FASTD_PEERS": "env-peer:5002;key"}, clear=False):
            source, value = registrar.requested_peer_string({"fastd": {"peers": "default-peer:5002;key"}})

        self.assertEqual(source, "env")
        self.assertEqual(value, "env-peer:5002;key")

    def test_blank_preferred_gateway_stays_empty_not_none(self) -> None:
        config = registrar.require_valid_registrar_config(
            defaults={
                "FASTD_PEERS": "vpn1.example.org:5002;deadbeef",
                "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                "INITIAL_NODE_ID": 52001,
            },
            env={
                "FASTD_PEERS": "",
                "NODE_REGISTRATION_URL": "",
                "REGISTRAR_INTERVAL": "3600",
                "BMXD_PREFERRED_GATEWAY": "",
            },
        )

        self.assertEqual(config["bmxd"]["preferred_gateway"], "")

    def test_render_bmxd_runtime_normalizes_none_preferred_gateway(self) -> None:
        config = build_base_config()
        config["bmxd"]["preferred_gateway"] = None

        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "bmxd.env"
            with patch.object(registrar, "BMXD_ENV_FILE", env_file):
                changed = registrar.render_bmxd_runtime(config, 52001)
            content = env_file.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn('BMXD_PREFERRED_GATEWAY=""', content)
        self.assertNotIn('BMXD_PREFERRED_GATEWAY="None"', content)
        self.assertIn('BMXD_GATEWAY_USAGE_FILE="/data/statistic/gateway_usage"', content)


if __name__ == "__main__":
    unittest.main()
