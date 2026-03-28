from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import wireguard_status


class WireguardStatusTests(unittest.TestCase):
    def test_parse_configured_peers_reads_wireguard_env_format(self) -> None:
        interface, peers = wireguard_status.parse_configured_peers(
            {
                "WIREGUARD_INTERFACE": "tbbwg",
                "WIREGUARD_PEERS": (
                    "vpn1.example.org:5003;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;51020;tbb_wg51020 "
                    "vpn2.example.org:5004;BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=;51021;tbb_wg51021"
                ),
            }
        )

        self.assertEqual(interface, "tbbwg")
        self.assertEqual(sorted(peers), [
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=",
        ])
        self.assertEqual(peers["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="].host, "vpn1.example.org")
        self.assertEqual(peers["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="].ifname, "tbb_wg51020")

    def test_load_runtime_env_parses_shell_style_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "wireguard.env"
            env_file.write_text(
                'WIREGUARD_INTERFACE="tbbwg"\nWIREGUARD_PEERS="vpn1.example.org:5003;AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;51020;tbb_wg51020"\n',
                encoding="utf-8",
            )

            values = wireguard_status.load_runtime_env(env_file)

        self.assertEqual(values["WIREGUARD_INTERFACE"], "tbbwg")
        self.assertIn("vpn1.example.org:5003", values["WIREGUARD_PEERS"])

    def test_determine_status_prefers_recent_handshake(self) -> None:
        live_peer = wireguard_status.LivePeer(
            endpoint="vpn1.example.org:5003",
            latest_handshake=900,
            transfer_rx=10,
            transfer_tx=5,
            persistent_keepalive=25,
        )

        status = wireguard_status.determine_status(live_peer, None, now=1000, stale_after=180)

        self.assertEqual(status, "connected")

    def test_determine_status_uses_transfer_delta_for_connected(self) -> None:
        previous = wireguard_status.LivePeer(
            endpoint="vpn1.example.org:5003",
            latest_handshake=100,
            transfer_rx=10,
            transfer_tx=5,
            persistent_keepalive=25,
        )
        current = wireguard_status.LivePeer(
            endpoint="vpn1.example.org:5003",
            latest_handshake=100,
            transfer_rx=20,
            transfer_tx=5,
            persistent_keepalive=25,
        )

        status = wireguard_status.determine_status(current, previous, now=1000, stale_after=180)

        self.assertEqual(status, "connected")

    def test_determine_status_marks_old_quiet_peer_as_stale(self) -> None:
        previous = wireguard_status.LivePeer(
            endpoint="vpn1.example.org:5003",
            latest_handshake=100,
            transfer_rx=20,
            transfer_tx=5,
            persistent_keepalive=25,
        )
        current = wireguard_status.LivePeer(
            endpoint="vpn1.example.org:5003",
            latest_handshake=100,
            transfer_rx=20,
            transfer_tx=5,
            persistent_keepalive=25,
        )

        status = wireguard_status.determine_status(current, previous, now=1000, stale_after=180)

        self.assertEqual(status, "stale")

    def test_determine_status_marks_missing_peer_as_disconnected(self) -> None:
        status = wireguard_status.determine_status(None, None, now=1000, stale_after=180)

        self.assertEqual(status, "disconnected")

    def test_wait_for_runtime_config_waits_until_env_exists(self) -> None:
        exists_values = iter([False, False, True])
        sleeps: list[int] = []

        with patch.object(wireguard_status, "WIREGUARD_ENV_FILE") as env_file, patch.object(
            wireguard_status.time,
            "sleep",
            side_effect=lambda value: sleeps.append(value),
        ):
            env_file.exists.side_effect = lambda: next(exists_values)
            wireguard_status.wait_for_runtime_config(interval=3)

        self.assertEqual(sleeps, [3, 3])

    def test_run_loop_logs_initial_peer_state(self) -> None:
        peer_key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        logs: list[str] = []

        with patch.object(
            wireguard_status,
            "snapshot_config",
            return_value=(
                "tbbwg",
                {
                    peer_key: wireguard_status.ConfiguredPeer(
                        host="vpn1.example.org",
                        port="5003",
                        public_key=peer_key,
                        node="51020",
                        ifname="tbb_wg51020",
                    )
                },
            ),
        ), patch.object(
            wireguard_status,
            "read_live_peers",
            return_value={
                peer_key: wireguard_status.LivePeer(
                    endpoint="89.58.15.39:5003",
                    latest_handshake=995,
                    transfer_rx=100,
                    transfer_tx=200,
                    persistent_keepalive=25,
                )
            },
        ), patch.object(wireguard_status, "wait_for_runtime_config", return_value=None), patch.object(wireguard_status, "log", side_effect=logs.append), patch.object(
            wireguard_status.time,
            "time",
            return_value=1000,
        ), patch.object(
            wireguard_status.time,
            "sleep",
            side_effect=SystemExit(0),
        ):
            with self.assertRaises(SystemExit):
                wireguard_status.run_loop(interval=10, stale_after=180)

        self.assertIn("watching 1 wireguard peer(s) on tbbwg", logs)
        self.assertEqual(sum("status connected" in line for line in logs), 1)


if __name__ == "__main__":
    unittest.main()