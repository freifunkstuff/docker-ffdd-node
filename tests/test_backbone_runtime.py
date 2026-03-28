from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backbone_runtime


class BackboneRuntimeTests(unittest.TestCase):
    def test_load_fastd_peers_reads_generated_peer_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            peer_dir = Path(temp_dir) / "peers"
            peer_dir.mkdir()
            (peer_dir / "connect_vpn1_5002.conf").write_text(
                'key "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";\nremote ipv4 "vpn1.example.org":5002;\n',
                encoding="utf-8",
            )
            env_file = Path(temp_dir) / "fastd.env"
            env_file.write_text('FASTD_INTERFACE="tbb_fastd"\n', encoding="utf-8")

            with patch.object(backbone_runtime, "FASTD_PEER_DIR", peer_dir), patch.object(backbone_runtime, "FASTD_ENV_FILE", env_file):
                peers = backbone_runtime.load_fastd_peers()

        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0].host, "vpn1.example.org")
        self.assertEqual(peers[0].port, "5002")
        self.assertEqual(peers[0].ifname, "tbb_fastd")

    def test_build_backbone_payload_combines_wireguard_and_fastd(self) -> None:
        live_peer = backbone_runtime.LivePeer(
            endpoint="89.58.15.39:51820",
            latest_handshake=995,
            transfer_rx=100,
            transfer_tx=200,
            persistent_keepalive=25,
        )

        with patch.object(
            backbone_runtime,
            "parse_configured_peers",
            return_value=(
                "tbbwg",
                {
                    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=": backbone_runtime.ConfiguredPeer(
                        host="vpn2.example.org",
                        port="51820",
                        public_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                        node="51020",
                        ifname="tbb_wg51020",
                    )
                },
            ),
        ), patch.object(backbone_runtime, "load_runtime_env", return_value={}), patch.object(
            backbone_runtime,
            "read_live_peers",
            return_value={"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=": live_peer},
        ), patch.object(
            backbone_runtime,
            "load_fastd_peers",
            return_value=[
                backbone_runtime.FastdPeer(
                    host="vpn1.example.org",
                    port="5002",
                    public_key="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    ifname="tbb_fastd",
                )
            ],
        ), patch.object(
            backbone_runtime,
            "read_fastd_connected_keys",
            return_value={"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},
        ):
            payload, current_live_peers = backbone_runtime.build_backbone_payload(now=1000, previous_live_peers={})

        self.assertEqual(payload["timestamp"], "1000")
        self.assertEqual(
            payload["peers"],
            [
                {
                    "type": "fastd",
                    "host": "vpn1.example.org",
                    "port": "5002",
                    "interface": "tbb_fastd",
                    "status": "connected",
                },
                {
                    "type": "wireguard",
                    "host": "vpn2.example.org",
                    "port": "51820",
                    "interface": "tbb_wg51020",
                    "status": "connected",
                },
            ],
        )
        self.assertEqual(current_live_peers["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="], live_peer)


if __name__ == "__main__":
    unittest.main()