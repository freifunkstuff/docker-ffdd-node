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
                "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                "INITIAL_NODE_ID": 52001,
            },
            env={
                "BACKBONE_PEERS": "",
                "NODE_REGISTRATION_URL": "",
                "REGISTRAR_INTERVAL": "3600",
            },
        )

        self.assertEqual(
            config["backbone"]["peers"],
            "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        self.assertIn("__NODE_REGISTER_KEY__", config["registrar"]["registration_url"])
        self.assertIn("__NODE_ID__", config["registrar"]["registration_url"])

    def test_require_valid_registrar_config_rejects_missing_placeholders(self) -> None:
        with self.assertRaises(SystemExit):
            registrar.require_valid_registrar_config(
                defaults={
                    "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                },
                env={
                    "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "NODE_REGISTRATION_URL": "https://example.invalid/static",
                    "REGISTRAR_INTERVAL": "3600",
                },
            )

    def test_parse_peer_string_deduplicates_by_type_host_and_port(self) -> None:
        peers, issues = registrar.parse_peer_string(
            "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef "
            "fastd;vpn1.example.org:5002;ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff "
            "wireguard;vpn2.example.org:51820;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52002"
        )

        self.assertEqual(
            peers,
            [
                {
                    "type": "fastd",
                    "host": "vpn1.example.org",
                    "port": "5002",
                    "raw": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "key": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                },
                {
                    "type": "wireguard",
                    "host": "vpn2.example.org",
                    "port": "51820",
                    "raw": "wireguard;vpn2.example.org:51820;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52002",
                    "key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "node": "52002",
                },
            ],
        )
        self.assertEqual(issues, [])

    def test_validate_requested_peers_keeps_incomplete_wireguard_peer_for_runtime_check(self) -> None:
        peers, issues = registrar.parse_peer_string("wireguard;vpn2.example.org:51820")

        usable, validation_issues = registrar.validate_requested_peers(peers)

        self.assertEqual(issues, [])
        self.assertEqual(validation_issues, [])
        self.assertEqual(usable[0]["metadata_missing"], "1")

    def test_wireguard_example_does_not_use_public_key_prefix(self) -> None:
        example = registrar._wireguard_example("vpn2le.freifunk-leipzig.de", "5003")

        self.assertEqual(
            example,
            "wireguard;vpn2le.freifunk-leipzig.de:5003;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52001 or wireguard;vpn2le.freifunk-leipzig.de:5003",
        )
        self.assertNotIn("PUBLIC_KEY_BASE64=", example)

    def test_resolve_requested_peers_accepts_api_match(self) -> None:
        peers = [
            {
                "type": "wireguard",
                "host": "vpn2.example.org",
                "port": "51820",
                "raw": "wireguard;vpn2.example.org:51820;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52002",
                "key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "node": "52002",
            }
        ]
        messages: list[str] = []

        with patch.object(
            registrar,
            "fetch_wireguard_peer_info",
            return_value={"key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "node": "52002", "port": "51820"},
        ):
            usable = registrar.resolve_requested_peers(peers, 52001, "local-public-key", logger=messages.append)

        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0]["ifname"], "tbb_wg52002")
        self.assertIn(
            "validated wireguard peer via API: wireguard;vpn2.example.org:51820;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52002",
            messages,
        )

    def test_resolve_requested_peers_discards_api_mismatch(self) -> None:
        peers = [
            {
                "type": "wireguard",
                "host": "vpn2.example.org",
                "port": "51820",
                "raw": "wireguard;vpn2.example.org:51820;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52002",
                "key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "node": "52002",
            }
        ]
        messages: list[str] = []

        with patch.object(
            registrar,
            "fetch_wireguard_peer_info",
            return_value={"key": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=", "node": "52003", "port": "51820"},
        ):
            usable = registrar.resolve_requested_peers(peers, 52001, "local-public-key", logger=messages.append)

        self.assertEqual(usable, [])
        self.assertTrue(any("config differs from API" in message for message in messages))

    def test_resolve_requested_peers_discards_api_lookup_failure(self) -> None:
        peers = [
            {
                "type": "wireguard",
                "host": "vpn2.example.org",
                "port": "51820",
                "raw": "wireguard;vpn2.example.org:51820;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52002",
                "key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "node": "52002",
            }
        ]
        messages: list[str] = []

        with patch.object(registrar, "fetch_wireguard_peer_info", return_value=None):
            usable = registrar.resolve_requested_peers(peers, 52001, "local-public-key", logger=messages.append)

        self.assertEqual(usable, [])
        self.assertTrue(any("wireguard peer error:" in message and "API lookup failed" in message for message in messages))

    def test_resolve_requested_peers_discards_missing_metadata_with_api_hint(self) -> None:
        peers = [
            {
                "type": "wireguard",
                "host": "vpn2.example.org",
                "port": "51820",
                "raw": "wireguard;vpn2.example.org:51820",
                "key": "",
                "node": "",
                "metadata_missing": "1",
            }
        ]
        messages: list[str] = []

        with patch.object(
            registrar,
            "fetch_wireguard_peer_info",
            return_value={"key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "node": "52002", "port": "51820"},
        ):
            usable = registrar.resolve_requested_peers(peers, 52001, "local-public-key", logger=messages.append)

        self.assertEqual(usable, [])
        self.assertIn(
            "wireguard peer error: vpn2.example.org:51820: missing public key/node; discarding peer; API reports wireguard;vpn2.example.org:51820;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52002",
            messages,
        )

    def test_require_valid_registrar_config_does_not_log_nonfatal_peer_discard(self) -> None:
        messages: list[str] = []

        with patch.object(registrar, "probe_wireguard_support", return_value=None):
            config = registrar.require_valid_registrar_config(
                defaults={
                    "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                    "INITIAL_NODE_ID": 52001,
                },
                env={
                    "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef wireguard;vpn2.example.org:51820",
                    "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                    "REGISTRAR_INTERVAL": "3600",
                },
                logger=messages.append,
            )

        self.assertEqual(config["fastd"]["port"], 5002)
        self.assertEqual(messages, [])

    def test_require_valid_registrar_config_checks_wireguard_support_when_needed(self) -> None:
        with patch.object(registrar, "probe_wireguard_support", return_value="kernel probe failed"):
            with self.assertRaises(SystemExit):
                registrar.require_valid_registrar_config(
                    defaults={
                        "BACKBONE_PEERS": "wireguard;vpn2.example.org:51820",
                        "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                        "INITIAL_NODE_ID": 52001,
                    },
                    env={
                        "BACKBONE_PEERS": "",
                        "NODE_REGISTRATION_URL": "",
                        "REGISTRAR_INTERVAL": "3600",
                    },
                )

    def test_require_valid_registrar_config_skips_wireguard_probe_for_fastd_only(self) -> None:
        with patch.object(registrar, "probe_wireguard_support", side_effect=AssertionError("should not be called")):
            config = registrar.require_valid_registrar_config(
                defaults={
                    "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                    "INITIAL_NODE_ID": 52001,
                },
                env={
                    "BACKBONE_PEERS": "",
                    "NODE_REGISTRATION_URL": "",
                    "REGISTRAR_INTERVAL": "3600",
                },
            )

        self.assertEqual(config["fastd"]["port"], 5002)

    def test_requested_peer_string_prefers_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"BACKBONE_PEERS": "fastd;env-peer:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
            clear=False,
        ):
            source, value = registrar.requested_peer_string(
                {
                    "backbone": {
                        "peers": "fastd;default-peer:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                    }
                }
            )

        self.assertEqual(source, "env")
        self.assertEqual(
            value,
            "fastd;env-peer:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )

    def test_ensure_secret_reports_existing_or_created_state(self) -> None:
        state: dict = {}

        with patch.object(registrar, "run_fastd", return_value="generated-fastd-secret"):
            first_secret, first_created = registrar.ensure_secret(state)
            second_secret, second_created = registrar.ensure_secret(state)

        self.assertEqual(first_secret, "generated-fastd-secret")
        self.assertTrue(first_created)
        self.assertEqual(second_secret, "generated-fastd-secret")
        self.assertFalse(second_created)

    def test_ensure_wireguard_secret_reports_existing_or_created_state(self) -> None:
        state: dict = {}

        with patch.object(registrar, "run_wg", return_value="generated-wireguard-secret"):
            first_secret, first_created = registrar.ensure_wireguard_secret(state)
            second_secret, second_created = registrar.ensure_wireguard_secret(state)

        self.assertEqual(first_secret, "generated-wireguard-secret")
        self.assertTrue(first_created)
        self.assertEqual(second_secret, "generated-wireguard-secret")
        self.assertFalse(second_created)

    def test_blank_preferred_gateway_stays_empty_not_none(self) -> None:
        config = registrar.require_valid_registrar_config(
            defaults={
                "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "NODE_REGISTRATION_URL": "https://example.invalid/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__",
                "INITIAL_NODE_ID": 52001,
            },
            env={
                "BACKBONE_PEERS": "",
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
        self.assertIn('BMXD_BACKBONE_INTERFACES="tbb_fastd"', content)

    def test_render_bmxd_runtime_uses_only_supplied_backbone_interfaces(self) -> None:
        config = build_base_config()

        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "bmxd.env"
            with patch.object(registrar, "BMXD_ENV_FILE", env_file):
                changed = registrar.render_bmxd_runtime(config, 52001, ["tbb_wg51020"])
            content = env_file.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertIn('BMXD_BACKBONE_INTERFACES="tbb_wg51020"', content)
        self.assertNotIn('BMXD_BACKBONE_INTERFACES="tbb_fastd', content)

    def test_render_fastd_runtime_clears_config_without_fastd_peers(self) -> None:
        config = build_base_config()

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir) / "fastd"
            peer_dir = runtime_dir / "peers"
            config_file = runtime_dir / "fastd.conf"
            env_file = runtime_dir / "fastd.env"
            peer_dir.mkdir(parents=True, exist_ok=True)
            config_file.write_text("old config\n", encoding="utf-8")
            (peer_dir / "peer1.conf").write_text("peer config\n", encoding="utf-8")

            with (
                patch.object(registrar, "FASTD_RUNTIME_DIR", runtime_dir),
                patch.object(registrar, "PEER_DIR", peer_dir),
                patch.object(registrar, "FASTD_CONFIG", config_file),
                patch.object(registrar, "FASTD_ENV_FILE", env_file),
            ):
                changed = registrar.render_fastd_runtime(config, "secret", [], 52001)

            content = env_file.read_text(encoding="utf-8")

        self.assertTrue(changed)
        self.assertFalse(config_file.exists())
        self.assertEqual(list(peer_dir.glob("*.conf")), [])
        self.assertIn('FASTD_INTERFACE="tbb_fastd"', content)


if __name__ == "__main__":
    unittest.main()
