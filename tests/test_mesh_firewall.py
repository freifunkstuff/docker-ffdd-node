from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mesh_firewall


class MeshFirewallTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(mesh_firewall, "_uplink_interfaces", return_value={"eth0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def apply(self, existing=""):
        ok = subprocess.CompletedProcess(["iptables"], 0, existing, "")
        with patch.object(mesh_firewall.subprocess, "run", return_value=ok) as run:
            mesh_firewall.apply_mesh_forwarding_guard(["mesh0", "eth42"])
        transactions = {(call.args[0][0] if call.kwargs["input"].startswith("*filter") else call.args[0][0] + ':' + call.kwargs["input"].splitlines()[0][1:]): call.kwargs["input"] for call in run.call_args_list if "input" in call.kwargs}
        return run, transactions

    def test_default_route_interface_cannot_become_a_mesh_forwarding_target(self):
        with patch.object(mesh_firewall.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "default-route uplink"):
                mesh_firewall.apply_mesh_forwarding_guard(["eth0"])
        run.assert_not_called()

    def test_first_apply_is_atomic_and_allows_only_mesh_backbone_transit(self):
        run, transactions = self.apply()
        transaction = transactions["iptables-restore"]
        self.assertIn("-I FORWARD 1 -j FFDD_NODE_FORWARD", transaction)
        self.assertIn("-A FFDD_NODE_FORWARD -i mesh0 -o tbb+ -j ACCEPT", transaction)
        self.assertIn("-A FFDD_NODE_FORWARD -i tbb+ -o eth42 -j ACCEPT", transaction)
        self.assertIn("-A FFDD_NODE_FORWARD -i mesh0 -o eth42 -j ACCEPT", transaction)
        self.assertIn("-A FFDD_NODE_FORWARD -j DROP\n", transaction)
        for text in transactions.values():
            self.assertNotIn("MASQUERADE", text)
            for parent in ("INPUT", "OUTPUT", "FORWARD"):
                self.assertNotIn(f"-F {parent}\n", text)
                self.assertNotIn(f"-P {parent}", text)
        for call in run.call_args_list:
            if "input" in call.kwargs:
                self.assertIn("--noflush", call.args[0])
                self.assertTrue(call.kwargs["check"])

    def test_web_and_diagnostics_available_on_mesh_but_uplink_address_not_exposed(self):
        _, transactions = self.apply()
        text = transactions["iptables-restore"]
        self.assertIn("-i mesh0 -d 10.200.0.0/15 -p tcp --dport 80 -j ACCEPT", text)
        self.assertIn("-i mesh0 -d 10.200.0.0/15 -p icmp -j ACCEPT", text)
        self.assertIn("-i mesh0 -d 10.255.255.255/32 -p udp --dport 4305:4307 -j ACCEPT", text)
        self.assertIn("-i mesh0 -d 255.255.255.255/32 -p udp --dport 4305:4307 -j ACCEPT", text)
        self.assertIn("-A FFDD_NODE_INPUT -i mesh0 -j DROP", text)
        self.assertNotIn("-i mesh0 -p tcp --dport 80 -j ACCEPT", text)
        self.assertIn("-A FFDD_NODE_INPUT -j RETURN", text)
        self.assertNotIn("--dport 22 -j ACCEPT", text)

    def test_mesh_output_cannot_reach_private_lan_and_mesh_sources_cannot_leak_to_uplink(self):
        _, transactions = self.apply()
        text = transactions["iptables-restore"]
        self.assertIn("-A FFDD_NODE_OUTPUT -o mesh0 -d 10.200.0.0/15 -j RETURN", text)
        self.assertIn("-A FFDD_NODE_OUTPUT -o mesh0 -j DROP", text)
        self.assertIn("-A FFDD_NODE_OUTPUT -s 10.200.0.0/15 -j DROP", text)
        self.assertIn("-A FFDD_NODE_OUTPUT -o lo -j RETURN", text)
        self.assertIn("-A FFDD_NODE_OUTPUT -j RETURN", text)

    def test_ipv6_mesh_is_fail_closed(self):
        _, transactions = self.apply()
        text = transactions["ip6tables-restore"]
        self.assertIn("-A FFDD_NODE_INPUT -i mesh0 -j DROP", text)
        self.assertIn("-A FFDD_NODE_OUTPUT -o mesh0 -j DROP", text)
        self.assertIn("-A FFDD_NODE_FORWARD -j DROP", text)
        self.assertNotIn("-j ACCEPT", text)

    def test_reload_does_not_duplicate_jumps(self):
        existing = "\n".join(f"-A {parent} -j {chain}" for parent, chain in [
            ("INPUT", mesh_firewall.INPUT_CHAIN), ("OUTPUT", mesh_firewall.OUTPUT_CHAIN), ("FORWARD", mesh_firewall.CHAIN),
        ])
        _, transactions = self.apply(existing)
        for text in (transactions["iptables-restore"], transactions["ip6tables-restore"]):
            self.assertNotIn("-I INPUT", text)
            self.assertNotIn("-I OUTPUT", text)
            self.assertNotIn("-I FORWARD", text)
            self.assertIn("-F FFDD_NODE_FORWARD", text)

    def test_refresh_restores_first_rule_precedence_without_flushing_parent(self):
        existing = "-A FORWARD -j ACCEPT\n-A FORWARD -j FFDD_NODE_FORWARD\n"
        _, transactions = self.apply(existing)
        for text in (transactions["iptables-restore"], transactions["ip6tables-restore"]):
            self.assertIn("-D FORWARD -j FFDD_NODE_FORWARD", text)
            self.assertIn("-I FORWARD 1 -j FFDD_NODE_FORWARD", text)
            self.assertNotIn("-D FORWARD -j ACCEPT", text)
            self.assertNotIn("-F FORWARD", text)

    def test_backbone_only_node_also_cannot_share_ordinary_uplink(self):
        ok = subprocess.CompletedProcess(["iptables"], 0, "", "")
        with patch.object(mesh_firewall.subprocess, "run", return_value=ok) as run:
            mesh_firewall.apply_mesh_forwarding_guard([])
        text = next(call.kwargs["input"] for call in run.call_args_list if call.args[0][0] == 'iptables-restore' and call.kwargs.get('input', '').startswith('*filter'))
        self.assertIn("-i tbb+ -o tbb+ -j ACCEPT", text)
        self.assertIn("-A FFDD_NODE_FORWARD -j DROP", text)
        self.assertNotIn("-o eth0 -j ACCEPT", text)

    def test_invalid_or_reserved_names_cannot_inject_rules(self):
        for name in ["mesh0 -j ACCEPT", "tbb_fastd", "lo", "tbb_wg123", "bmx_prime", "bad+name"]:
            with self.subTest(name=name), patch.object(mesh_firewall.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    mesh_firewall.apply_mesh_forwarding_guard([name])
                run.assert_not_called()

    def test_startup_quiesces_mesh_before_policy_and_never_uplink(self):
        with patch.object(mesh_firewall.subprocess, "run", return_value=subprocess.CompletedProcess(["ip"], 0, '[{"ifindex":2}]', '')) as run, patch.object(mesh_firewall, "apply_mesh_forwarding_guard") as apply:
            mesh_firewall.prepare_mesh_startup(["mesh0"])
        self.assertEqual(run.call_args.args[0], ["ip", "link", "set", "dev", "mesh0", "down"])
        apply.assert_called_once_with(["mesh0"])
        with patch.object(mesh_firewall.subprocess, "run") as run, patch.object(mesh_firewall, "apply_mesh_forwarding_guard") as apply:
            with self.assertRaisesRegex(ValueError, "uplink"):
                mesh_firewall.prepare_mesh_startup(["eth0"])
        run.assert_not_called()
        apply.assert_not_called()

    def test_startup_keeps_mesh_down_when_policy_application_fails(self):
        with patch.object(mesh_firewall.subprocess, "run", return_value=subprocess.CompletedProcess(["ip"], 0, '[{"ifindex":2}]', '')) as run, patch.object(mesh_firewall, "apply_mesh_forwarding_guard", side_effect=RuntimeError("denied")):
            with self.assertRaises(RuntimeError):
                mesh_firewall.prepare_mesh_startup(["mesh0"])
        self.assertTrue(any(call.args[0][-1] == "down" for call in run.call_args_list))
        self.assertFalse(any(call.args[0][-1] == "up" for call in run.call_args_list))

    def test_firewall_failure_propagates_for_fail_closed_startup(self):
        with patch.object(mesh_firewall.subprocess, "run", side_effect=subprocess.CalledProcessError(2, "iptables")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                mesh_firewall.apply_mesh_forwarding_guard(["mesh0"])
        run.assert_called_once()
        ok = subprocess.CompletedProcess(["iptables"], 0, "", "")
        with patch.object(mesh_firewall.subprocess, "run", side_effect=[ok, subprocess.CalledProcessError(1, "iptables-restore")]):
            with self.assertRaises(subprocess.CalledProcessError):
                mesh_firewall.apply_mesh_forwarding_guard(["mesh0"])


if __name__ == "__main__":
    unittest.main()
