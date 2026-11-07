from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sysinfo


class OffloaderSysinfoTests(unittest.TestCase):
    def config(self, **env):
        return sysinfo.require_valid_sysinfo_config(
            defaults={}, env={"NODE_CONTACT_EMAIL": "admin@example.org", "NODE_NAME": "offloader", **env},
            log_warnings=False,
        )

    def test_node_type_default_and_override(self):
        self.assertEqual(self.config()["system"]["node_type"], "server")
        config = self.config(NODE_TYPE="node")
        with patch.object(sysinfo, "_safe_command", return_value=""), patch.object(sysinfo, "_read_text", return_value=""), patch.object(sysinfo, "_container_uptime_seconds", return_value=0):
            self.assertEqual(sysinfo._build_system_block(config)["node_type"], "node")

    def test_node_type_rejects_invalid_and_blank(self):
        for value in ("gateway", "", "mobile"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.config(NODE_TYPE=value)

    def test_node_type_defaults_file(self):
        config = sysinfo.require_valid_sysinfo_config(defaults={"NODE_TYPE": "node"}, env={"NODE_CONTACT_EMAIL": "a@b", "NODE_NAME": "a"}, log_warnings=False)
        self.assertEqual(config["system"]["node_type"], "node")

    def test_wg_tunnel_aggregate_excludes_underlay_and_deduplicates(self):
        runtime = {"WIREGUARD_PEERS": "a:1;k1;1;tbb_wg1 b:2;k2;2;tbb_wg2 c:3;k3;3;tbb_wg1 d:4;k4;4;tbbwg"}
        counters = {"tbb_fastd": ("7", "8"), "tbb_wg1": ("10", "20"), "tbb_wg2": ("30", "40"), "tbbwg": ("999", "999")}
        def read(path):
            iface = path.parts[-3]
            self.assertNotEqual(iface, "tbbwg")
            return counters.get(iface, ("", ""))[path.name == "tx_bytes"]
        with patch.object(sysinfo, "load_runtime_env", return_value=runtime), patch.object(sysinfo, "_read_text", side_effect=read):
            self.assertEqual(sysinfo._read_interface_stats(), {"tbb_fastd_rx": "7", "tbb_fastd_tx": "8", "tbb_wg_rx": "40", "tbb_wg_tx": "60"})

    def test_missing_wg_is_omitted_zero_is_kept(self):
        runtime = {"WIREGUARD_PEERS": "a:1;k1;1;tbb_wg1 b:2;k2;2;tbb_wg2"}
        with patch.object(sysinfo, "load_runtime_env", return_value=runtime), patch.object(sysinfo, "_read_text", side_effect=lambda p: "0" if "tbb_wg1" in p.parts else ""):
            self.assertEqual(sysinfo._read_interface_stats(), {"tbb_wg_rx": "0", "tbb_wg_tx": "0"})
        with patch.object(sysinfo, "load_runtime_env", return_value=runtime), patch.object(sysinfo, "_read_text", return_value=""):
            self.assertEqual(sysinfo._read_interface_stats(), {})

    def test_multus_interfaces_are_lan_but_backbone_stays_backbone(self):
        with patch.dict(sysinfo.os.environ, {"MESH_INTERFACES": "net1 custommesh tbb_wg42"}):
            links = sysinfo._parse_bmxd_links("10.201.1.2 net1 10.200.1.2 90 91 92")
            self.assertEqual(links[0]["type"], "lan")
            self.assertEqual(sysinfo._link_type_for_interface("custommesh"), "lan")
            self.assertEqual(sysinfo._link_type_for_interface("tbb_wg42"), "backbone")
            self.assertEqual(sysinfo._link_type_for_interface("unknown"), "")

    def test_public_key_only_no_secret_derivation(self):
        with patch.object(sysinfo, "load_runtime_env", return_value={"WIREGUARD_PUBLIC_KEY": "runtime-public"}), patch.object(sysinfo.subprocess, "run") as run:
            self.assertEqual(sysinfo._wireguard_pubkey({"wireguard": {"secret": "private"}}), "runtime-public")
            self.assertEqual(sysinfo._wireguard_pubkey({"wireguard": {"public_key": "state-public", "secret": "private"}}), "state-public")
            run.assert_not_called()

    def test_resource_snapshot_render_contract(self):
        snapshot = sysinfo.ResourceSnapshot(
            scope="container", sampled_at=123, cpu_count=8, cpu_capacity=2,
            memory_limit_bytes=512 * 1024**2, memory_current_bytes=128 * 1024**2,
            memory_remaining_bytes=384 * 1024**2, memory_cache_bytes=1024,
            uptime_seconds=900, cpu_total_seconds=12,
            sources={"cpu": "cgroup2:/secret/workload", "memory": "cgroup2:/secret/workload"})
        nodes = {"bmxd": {"links": [], "gateways": {"selected": "", "preferred": "", "gateways": []}, "info": []}}
        with patch.object(sysinfo, "_resource_snapshot", return_value=snapshot) as read, patch.object(sysinfo, "_safe_command", return_value=""), patch.object(sysinfo, "_read_text", return_value=""), patch.object(sysinfo, "_wireguard_pubkey", return_value=""):
            payload = sysinfo.render_stub_payload(self.config(), {}, nodes)
        read.assert_called_once_with(self.config())
        system = payload["data"]["system"]
        stats = payload["data"]["statistic"]
        resources = payload["data"]["resources"]
        self.assertEqual(system["cpucount"], "2")
        self.assertEqual(system["uptime"], "900.00")
        self.assertEqual(system["uptime_string"], "up 00:15")
        self.assertEqual(system["cpuinfo_scope"], "hardware-host")
        self.assertEqual(stats["meminfo_MemTotal"], "524288 kB")
        self.assertEqual(stats["meminfo_MemFree"], "393216 kB")
        self.assertEqual(stats["meminfo_Cached"], "1 kB")
        for key in ("cpu_load", "cpu_stat", "meminfo_Buffers"):
            self.assertNotIn(key, stats)
        self.assertEqual(resources["cpu_capacity"], 2)
        self.assertEqual(resources["cpu_total_seconds"], 12)
        self.assertEqual(resources["sources"]["cpu"], "cgroup2")
        self.assertNotIn("sampled_at", resources)
        self.assertNotIn("load_average", resources)

    def test_scope_override_and_native_auto(self):
        for scope in ("auto", "host", "container"):
            with self.subTest(scope=scope), patch.dict(sysinfo.os.environ, {} if scope == "auto" else {"SYSINFO_RESOURCE_SCOPE": scope}, clear=True), patch.object(sysinfo, "read_resources") as read:
                sysinfo._resource_snapshot()
                read.assert_called_once_with(scope=scope)

    def test_resource_scope_config_validation(self):
        for value in ("invalid", "", "HOST"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.config(SYSINFO_RESOURCE_SCOPE=value)
        self.assertEqual(self.config()["resources"]["scope"], "auto")

    def test_resource_scope_defaults_and_environment_precedence(self):
        for override, expected in (({}, "host"), ({"SYSINFO_RESOURCE_SCOPE": "container"}, "container")):
            with self.subTest(override=override):
                config = sysinfo.require_valid_sysinfo_config(
                    defaults={"SYSINFO_RESOURCE_SCOPE": "host"},
                    env={"NODE_CONTACT_EMAIL": "a@b", "NODE_NAME": "a", **override},
                    log_warnings=False)
                self.assertEqual(config["resources"]["scope"], expected)
                with patch.dict(sysinfo.os.environ, {"SYSINFO_RESOURCE_SCOPE": "auto"}), patch.object(sysinfo, "read_resources") as read:
                    sysinfo._resource_snapshot(config)
                    read.assert_called_once_with(scope=expected)
                with patch.object(sysinfo, "read_resources", return_value=sysinfo.ResourceSnapshot(scope=expected, sampled_at=0)) as read, patch.object(sysinfo, "_safe_command", return_value=""), patch.object(sysinfo, "_read_text", return_value=""):
                    sysinfo._build_system_block(config)
                    read.assert_called_once_with(scope=expected)

    def test_fractional_cpu_and_unknown_uptime(self):
        snapshot = sysinfo.ResourceSnapshot(scope="container", sampled_at=0, cpu_count=8, cpu_capacity=1.5)
        with patch.object(sysinfo, "_safe_command", return_value=""), patch.object(sysinfo, "_read_text", return_value=""):
            block = sysinfo._build_system_block(self.config(), snapshot)
        self.assertEqual(block["cpucount"], "2")
        self.assertNotIn("uptime", block)
        self.assertNotIn("uptime_string", block)
        self.assertIsNone(sysinfo._container_uptime_seconds(snapshot))
        self.assertIsNone(sysinfo._read_uptime_raw(snapshot))

    def test_host_snapshot_preserves_load_and_kernel_uptime(self):
        snapshot = sysinfo.ResourceSnapshot(scope="host", sampled_at=0, cpu_count=8,
            uptime_seconds=86400, load_average=(1., 2., 3.), memory_remaining_bytes=1024)
        with patch.object(sysinfo, "_safe_command", return_value=""), patch.object(sysinfo, "_read_text", return_value=""):
            system = sysinfo._build_system_block(self.config(), snapshot)
            stats = sysinfo._build_statistic_block(snapshot)
        self.assertEqual(system["cpucount"], "8")
        self.assertEqual(system["uptime"], "86400.00")
        self.assertEqual(stats["cpu_load"], "1.0 2.0 3.0")
        self.assertEqual(stats["meminfo_MemFree"], "1 kB")

    def test_cpu_model_priority(self):
        for text, expected in (("processor: 0\nmodel name: Test CPU\nhardware: Board", "Test CPU"), ("processor: 0\nHardware: ARM Board", "ARM Board"), ("Processor: ARMv7", "ARMv7")):
            with self.subTest(text=text), patch.object(sysinfo, "_read_text", return_value=text):
                self.assertEqual(sysinfo._read_cpuinfo(), expected)
