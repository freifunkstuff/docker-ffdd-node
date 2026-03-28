from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sysinfo


class SysinfoTests(unittest.TestCase):
    def test_require_valid_sysinfo_config_warns_for_missing_gps(self) -> None:
        messages: list[str] = []

        config = sysinfo.require_valid_sysinfo_config(
            defaults={},
            env={
                "NODE_CONTACT_EMAIL": "admin@example.org",
                "NODE_NAME": "stub-node",
                "NODE_COMMUNITY": "Dresden",
                "NODE_GPS_LATITUDE": "",
                "NODE_GPS_LONGITUDE": "",
            },
            logger=messages.append,
        )

        self.assertEqual(config["node"]["contact"]["email"], "admin@example.org")
        self.assertIn("config warning: NODE_GPS_LATITUDE: NODE_GPS_LATITUDE is required", messages)
        self.assertIn("config warning: NODE_GPS_LONGITUDE: NODE_GPS_LONGITUDE is required", messages)

    def test_require_valid_sysinfo_config_sets_leipzig_domain_default(self) -> None:
        config = sysinfo.require_valid_sysinfo_config(
            defaults={},
            env={
                "NODE_CONTACT_EMAIL": "admin@example.org",
                "NODE_NAME": "stub-node",
                "NODE_COMMUNITY": "Leipzig",
                "NODE_GPS_LATITUDE": "",
                "NODE_GPS_LONGITUDE": "",
            },
            log_warnings=False,
        )

        self.assertEqual(config["node"]["common"]["domain"], "freifunk-leipzig.de")

    def test_require_valid_sysinfo_config_sets_dresden_domain_default(self) -> None:
        config = sysinfo.require_valid_sysinfo_config(
            defaults={},
            env={
                "NODE_CONTACT_EMAIL": "admin@example.org",
                "NODE_NAME": "stub-node",
                "NODE_COMMUNITY": "Dresden",
                "NODE_GPS_LATITUDE": "",
                "NODE_GPS_LONGITUDE": "",
            },
            log_warnings=False,
        )

        self.assertEqual(config["node"]["common"]["domain"], "freifunk-dresden.de")

    def test_require_valid_sysinfo_config_keeps_explicit_domain(self) -> None:
        config = sysinfo.require_valid_sysinfo_config(
            defaults={},
            env={
                "NODE_CONTACT_EMAIL": "admin@example.org",
                "NODE_NAME": "stub-node",
                "NODE_COMMUNITY": "Leipzig",
                "NODE_DOMAIN": "custom.example.org",
                "NODE_GPS_LATITUDE": "",
                "NODE_GPS_LONGITUDE": "",
            },
            log_warnings=False,
        )

        self.assertEqual(config["node"]["common"]["domain"], "custom.example.org")

    def test_require_valid_sysinfo_config_can_suppress_warnings(self) -> None:
        messages: list[str] = []

        config = sysinfo.require_valid_sysinfo_config(
            defaults={},
            env={
                "NODE_CONTACT_EMAIL": "admin@example.org",
                "NODE_NAME": "stub-node",
                "NODE_COMMUNITY": "Dresden",
                "NODE_GPS_LATITUDE": "",
                "NODE_GPS_LONGITUDE": "",
            },
            log_warnings=False,
            logger=messages.append,
        )

        self.assertEqual(config["node"]["contact"]["email"], "admin@example.org")
        self.assertEqual(messages, [])

    def test_require_valid_sysinfo_config_requires_node_name(self) -> None:
        messages: list[str] = []

        with self.assertRaises(SystemExit):
            sysinfo.require_valid_sysinfo_config(
                defaults={},
                env={
                    "NODE_CONTACT_EMAIL": "admin@example.org",
                    "NODE_NAME": "",
                    "NODE_COMMUNITY": "Dresden",
                    "NODE_GPS_LATITUDE": "51.0",
                    "NODE_GPS_LONGITUDE": "13.0",
                },
                logger=messages.append,
            )

        self.assertIn("config error: NODE_NAME: NODE_NAME is required", messages)

    def test_render_stub_payload_uses_registered_node_id(self) -> None:
        nodes_payload = {
            "timestamp": "1",
            "node": {
                "id": "52001",
                "ip": "10.200.203.237",
                "community": "Dresden",
                "domain": "freifunk-dresden.de",
                "network_id": "0",
            },
            "bmxd": {
                "links": [],
                "gateways": {"selected": "", "preferred": "", "gateways": []},
                "originators": [],
                "info": [],
            },
        }

        with patch.object(sysinfo, "_read_os_release", return_value={"ID": "alpine", "VERSION_ID": "3.22", "PRETTY_NAME": "Alpine Linux"}), patch.object(sysinfo, "_safe_command", side_effect=["up 1 day", "Linux test", ""]), patch.object(sysinfo, "_read_nameservers", return_value=["10.200.200.21"]), patch.object(sysinfo, "_read_cpuinfo", return_value="Test CPU"), patch.object(sysinfo, "_read_meminfo", return_value={"MemTotal": "100 kB", "MemFree": "50 kB", "Buffers": "5 kB", "Cached": "10 kB"}), patch.object(sysinfo, "_read_cpu_stat", return_value="cpu 1 2 3 4"), patch.object(sysinfo, "_container_uptime_seconds", return_value=3600.0), patch.object(sysinfo, "_read_text", return_value=""), patch.object(sysinfo, "_derive_fastd_pubkey", return_value="pubkey"):
            payload = sysinfo.render_stub_payload(
                config={
                    "system": {"node_type": "server", "autoupdate": 0},
                    "node": {
                        "community": "Dresden",
                        "common": {"domain": "freifunk-dresden.de", "group_id": "", "network_id": "0"},
                        "gps": {"latitude": 51.0, "longitude": 13.0, "altitude": 0},
                        "contact": {"email": "admin@example.org", "name": "stub", "location": "", "note": ""},
                    },
                },
                state={"registration": {"node_id": 52001}},
                nodes_payload=nodes_payload,
            )

        self.assertEqual(payload["data"]["common"]["node"], "52001")
        self.assertEqual(payload["data"]["common"]["ip"], "10.200.203.237")
        self.assertEqual(payload["data"]["common"]["domain"], "freifunk-dresden.de")
        self.assertIn("firmware", payload["data"])
        self.assertIn("statistic", payload["data"])
        self.assertEqual(payload["data"]["backbone"]["fastd_pubkey"], "pubkey")
        self.assertIn("bmxd", payload["data"])
        self.assertEqual(payload["data"]["bmxd"]["links"], [])
        self.assertNotIn("originators", payload["data"]["bmxd"])

    def test_render_once_writes_json_and_nodes_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "sysinfo.json"
            nodes_output_path = Path(temp_dir) / "nodes.json"
            webroot = Path(temp_dir) / "www"
            config = {
                "system": {"node_type": "server", "autoupdate": 0},
                "node": {
                    "community": "Dresden",
                    "common": {"domain": "freifunk-dresden.de", "group_id": "", "network_id": "0"},
                    "gps": {"latitude": None, "longitude": None, "altitude": 0},
                    "contact": {"email": "admin@example.org", "name": "stub", "location": "", "note": ""},
                },
            }
            state = {"registration": {"node_id": 52001}}
            nodes_payload = {
                "timestamp": "1",
                "node": {
                    "id": "52001",
                    "ip": "10.200.203.237",
                    "community": "Dresden",
                    "domain": "freifunk-dresden.de",
                    "network_id": "0",
                },
                "bmxd": {
                    "links": [],
                    "gateways": {"selected": "", "preferred": "", "gateways": []},
                    "originators": [],
                    "info": [],
                },
            }

            with patch.object(sysinfo, "require_valid_sysinfo_config", return_value=config), patch.object(sysinfo, "load_state", return_value=state), patch.object(sysinfo, "_build_firmware_block", return_value={"version": "dockernode-test"}), patch.object(sysinfo, "_build_system_block", return_value={"node_type": "server", "autoupdate": 0}), patch.object(sysinfo, "_build_statistic_block", return_value={"gateway_usage": []}), patch.object(sysinfo, "build_nodes_payload", return_value=nodes_payload), patch.object(sysinfo, "_derive_fastd_pubkey", return_value=""):
                sysinfo.render_once(output_path, webroot, nodes_output_path)

            written = json.loads(output_path.read_text(encoding="utf-8"))
            written_nodes = json.loads(nodes_output_path.read_text(encoding="utf-8"))
            self.assertEqual(written["data"]["common"]["node"], "52001")
            self.assertEqual(written["data"]["contact"]["email"], "admin@example.org")
            self.assertIn("firmware", written["data"])
            self.assertIn("statistic", written["data"])
            self.assertNotIn("originators", written["data"]["bmxd"])
            self.assertIn("originators", written_nodes["bmxd"])
            self.assertTrue((webroot / "sysinfo.json").is_symlink())
            self.assertTrue((webroot / "sysinfo-json.cgi").is_symlink())
            self.assertTrue((webroot / "nodes.json").is_symlink())
            self.assertEqual((webroot / "sysinfo.json").resolve(), output_path)
            self.assertEqual((webroot / "nodes.json").resolve(), nodes_output_path)

    def test_publish_web_links_replaces_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "runtime" / "sysinfo.json"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("{}\n", encoding="utf-8")
            nodes_output_path = Path(temp_dir) / "runtime" / "nodes.json"
            nodes_output_path.write_text("{}\n", encoding="utf-8")
            webroot = Path(temp_dir) / "www"
            webroot.mkdir()
            (webroot / "sysinfo.json").write_text("old\n", encoding="utf-8")
            (webroot / "sysinfo-json.cgi").write_text("old\n", encoding="utf-8")
            (webroot / "nodes.json").write_text("old\n", encoding="utf-8")

            sysinfo.publish_web_links(output_path, nodes_output_path, webroot)

            self.assertTrue((webroot / "sysinfo.json").is_symlink())
            self.assertTrue((webroot / "sysinfo-json.cgi").is_symlink())
            self.assertTrue((webroot / "nodes.json").is_symlink())
            self.assertEqual((webroot / "sysinfo-json.cgi").resolve(), output_path)
            self.assertEqual((webroot / "nodes.json").resolve(), nodes_output_path)

    def test_write_json_atomic_sets_world_readable_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "sysinfo.json"

            sysinfo.write_json_atomic(output_path, {"version": "17", "data": {}})

            mode = output_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o644)

    def test_build_bmxd_block_returns_empty_without_client_data(self) -> None:
        with patch.object(sysinfo, "_safe_command", return_value=""):
            data = sysinfo._build_bmxd_block()

        self.assertEqual(data["links"], [])
        self.assertEqual(data["gateways"]["selected"], "")
        self.assertEqual(data["gateways"]["preferred"], "")
        self.assertEqual(data["gateways"]["gateways"], [])
        self.assertEqual(data["info"], [])
        self.assertNotIn("originators", data)

    def test_safe_command_returns_empty_when_binary_missing(self) -> None:
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            result = sysinfo._safe_command(["bmxd", "-c", "--links"])

        self.assertEqual(result, "")

    def test_build_system_block_handles_missing_bmxd_output(self) -> None:
        config = {"system": {"node_type": "server", "autoupdate": 0}}

        def fake_safe_command(command: list[str]) -> str:
            if command == ["uptime"]:
                return "up 1 day"
            if command == ["uname", "-a"]:
                return "Linux test"
            if command == ["bmxd", "-c", "status"]:
                return ""
            return ""

        with patch.object(sysinfo, "_safe_command", side_effect=fake_safe_command), patch.object(sysinfo, "_read_nameservers", return_value=["10.200.200.21"]), patch.object(sysinfo, "_read_cpuinfo", return_value="cpu"), patch.object(sysinfo, "os") as mock_os:
            mock_os.cpu_count.return_value = 2
            mock_os.environ.get.side_effect = lambda key, default=None: default
            mock_os.uname.return_value.machine = "x86_64"
            block = sysinfo._build_system_block(config)

        self.assertEqual(block["bmxd"], "")
        self.assertEqual(block["node_type"], "server")

    def test_build_bmxd_block_preserves_sysinfo_format(self) -> None:
        data = sysinfo._build_bmxd_block(
            {
                "links": [
                    {
                        "node": "1042",
                        "ip": "10.200.4.23",
                        "neighbor_ip": "10.201.200.11",
                        "interface": "tbb_fastd",
                        "rtq": "99",
                        "rq": "99",
                        "tq": "99",
                        "type": "backbone",
                    }
                ],
                "gateways": {
                    "selected": "10.200.200.21",
                    "preferred": "10.200.200.21",
                    "gateways": [
                        {
                            "node": "51020",
                            "ip": "10.200.200.21",
                            "best_next_hop": "10.201.200.21",
                            "brc": "94",
                            "community": "1",
                            "speed": "8MBit/8MBit",
                            "selected": True,
                            "preferred": True,
                            "usage": "13",
                        }
                    ],
                },
                "originators": [
                    {
                        "node": "1042",
                        "ip": "10.200.4.23",
                        "interface": "tbb_fastd",
                        "best_next_hop": "10.201.200.21",
                        "brc": "92",
                        "type": "backbone",
                    }
                ],
                "info": ["opt1", "opt2"],
            }
        )

        self.assertEqual(
            data["links"],
            [{"node": "1042", "ip": "10.200.4.23", "interface": "tbb_fastd", "rtq": "99", "rq": "99", "tq": "99", "type": "backbone"}],
        )
        self.assertEqual(data["gateways"]["selected"], "10.200.200.21")
        self.assertEqual(data["gateways"]["preferred"], "10.200.200.21")
        self.assertEqual(data["gateways"]["gateways"], [{"ip": "10.200.200.21"}])
        self.assertEqual(data["info"], ["opt1", "opt2"])
        self.assertNotIn("originators", data)

    def test_build_nodes_payload_parses_client_output(self) -> None:
        def fake_safe_command(command: list[str]) -> str:
            if command == ["bmxd", "-c", "--links"]:
                return "10.201.200.11 tbb_fastd 10.200.4.23 99 99 99\n"
            if command == ["bmxd", "-c", "--gateways"]:
                return "preferred gateway: 10.200.200.21\n=> 10.200.200.21 10.201.200.21 94, 1, 8MBit/8MBit\n10.200.200.68 10.201.200.68 88, 0, 8MBit/8MBit\n"
            if command == ["bmxd", "-c", "--originators"]:
                return "10.200.4.23 tbb_fastd 10.201.200.21 92 95 0:00:10:00 123 0\n"
            if command == ["bmxd", "-c", "options"]:
                return "opt1\nopt2\n"
            return ""

        with patch.object(sysinfo, "_safe_command", side_effect=fake_safe_command), patch.object(sysinfo, "_read_gateway_usage_map", return_value={"10.200.200.21": "13"}):
            data = sysinfo.build_nodes_payload(
                config={
                    "node": {
                        "community": "Dresden",
                        "common": {"domain": "freifunk-dresden.de", "network_id": "0"},
                    }
                },
                state={"registration": {"node_id": 52001}},
            )

        self.assertEqual(data["node"]["id"], "52001")
        self.assertEqual(data["node"]["ip"], "10.200.203.237")
        self.assertEqual(len(data["bmxd"]["links"]), 1)
        self.assertEqual(data["bmxd"]["links"][0]["ip"], "10.200.4.23")
        self.assertEqual(data["bmxd"]["links"][0]["neighbor_ip"], "10.201.200.11")
        self.assertEqual(data["bmxd"]["links"][0]["node"], "1042")
        self.assertEqual(data["bmxd"]["links"][0]["type"], "backbone")
        self.assertEqual(data["bmxd"]["gateways"]["selected"], "10.200.200.21")
        self.assertEqual(data["bmxd"]["gateways"]["preferred"], "10.200.200.21")
        self.assertEqual(
            data["bmxd"]["gateways"]["gateways"],
            [
                {
                    "node": "51020",
                    "ip": "10.200.200.21",
                    "best_next_hop": "10.201.200.21",
                    "brc": "94",
                    "community": "1",
                    "speed": "8MBit/8MBit",
                    "selected": True,
                    "preferred": True,
                    "usage": "13",
                },
                {
                    "node": "51067",
                    "ip": "10.200.200.68",
                    "best_next_hop": "10.201.200.68",
                    "brc": "88",
                    "community": "0",
                    "speed": "8MBit/8MBit",
                    "selected": False,
                    "preferred": False,
                    "usage": "0",
                },
            ],
        )
        self.assertEqual(
            data["bmxd"]["originators"],
            [
                {
                    "node": "1042",
                    "ip": "10.200.4.23",
                    "interface": "tbb_fastd",
                    "best_next_hop": "10.201.200.21",
                    "brc": "92",
                    "type": "backbone",
                }
            ],
        )
        self.assertEqual(data["bmxd"]["info"], ["opt1", "opt2"])

    def test_build_statistic_block_includes_gateway_usage_and_interfaces_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gw_file = Path(temp_dir) / "gateway_usage"
            gw_file.write_text("gw1: 123\ngw2: 456\n", encoding="utf-8")

            def fake_read_text(path: Path) -> str:
                path_str = str(path)
                if path == gw_file:
                    return gw_file.read_text(encoding="utf-8")
                if path_str.endswith("/statistics/rx_bytes"):
                    return "100\n"
                if path_str.endswith("/statistics/tx_bytes"):
                    return "200\n"
                return ""

            with patch.object(sysinfo, "GATEWAY_USAGE_PATH", gw_file), patch.object(sysinfo, "_read_meminfo", return_value={"MemTotal": "100 kB", "MemFree": "50 kB", "Buffers": "5 kB", "Cached": "10 kB"}), patch.object(sysinfo, "_read_cpu_stat", return_value="cpu 1 2 3 4"), patch.object(sysinfo, "_read_text", side_effect=fake_read_text):
                statistic = sysinfo._build_statistic_block()

        self.assertEqual(statistic["gateway_usage"], [{"gw1": "123"}, {"gw2": "456"}])
        self.assertIn("interfaces", statistic)
        self.assertIn("tbb_fastd_rx", statistic["interfaces"])
        self.assertIn("tbb_fastd_tx", statistic["interfaces"])
        self.assertNotIn("bmx_prime_rx", statistic["interfaces"])
        self.assertNotIn("eth0_rx", statistic["interfaces"])

    def test_build_statistic_block_prefers_cgroup_memory_values(self) -> None:
        def fake_read_text(path: Path) -> str:
            path_str = str(path)
            if path_str == "/sys/fs/cgroup/memory.current":
                return "2097152\n"
            if path_str == "/sys/fs/cgroup/memory.max":
                return "4194304\n"
            if path_str == "/proc/loadavg":
                return "0.00 0.00 0.00 1/1 1\n"
            return ""

        with patch.object(sysinfo, "_read_meminfo", return_value={"MemTotal": "100 kB", "MemFree": "50 kB", "Buffers": "5 kB", "Cached": "10 kB"}), patch.object(sysinfo, "_read_cpu_stat", return_value="cpu 1 2 3 4"), patch.object(sysinfo, "_read_gateway_usage", return_value=[]), patch.object(sysinfo, "_read_interface_stats", return_value={}), patch.object(sysinfo, "_read_text", side_effect=fake_read_text):
            statistic = sysinfo._build_statistic_block()

        self.assertEqual(statistic["meminfo_MemTotal"], "4096 kB")
        self.assertEqual(statistic["meminfo_MemFree"], "2048 kB")

    def test_build_statistic_block_uses_host_meminfo_without_finite_cgroup_limit(self) -> None:
        def fake_read_text(path: Path) -> str:
            path_str = str(path)
            if path_str == "/sys/fs/cgroup/memory.current":
                return "2097152\n"
            if path_str == "/sys/fs/cgroup/memory.max":
                return "max\n"
            if path_str == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                return "9223372036854771712\n"
            if path_str == "/proc/loadavg":
                return "0.00 0.00 0.00 1/1 1\n"
            return ""

        with patch.object(sysinfo, "_read_meminfo", return_value={"MemTotal": "100 kB", "MemFree": "50 kB", "Buffers": "5 kB", "Cached": "10 kB"}), patch.object(sysinfo, "_read_cpu_stat", return_value="cpu 1 2 3 4"), patch.object(sysinfo, "_read_gateway_usage", return_value=[]), patch.object(sysinfo, "_read_interface_stats", return_value={}), patch.object(sysinfo, "_read_text", side_effect=fake_read_text):
            statistic = sysinfo._build_statistic_block()

        self.assertEqual(statistic["meminfo_MemTotal"], "100 kB")
        self.assertEqual(statistic["meminfo_MemFree"], "50 kB")


if __name__ == "__main__":
    unittest.main()
