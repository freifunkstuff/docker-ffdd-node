from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mesh_firewall as firewall


class LanMeshFirewallTests(unittest.TestCase):
    def apply(self, enabled=True):
        ok = subprocess.CompletedProcess(['iptables'], 0, '', '')
        with patch.object(firewall, '_uplink_interfaces', return_value={'internet0'}), patch.object(firewall, '_primary_uplink', return_value='internet0'), patch.object(firewall.subprocess, 'run', return_value=ok) as run:
            options = {'lan_sources': ['192.168.1.0/24'], 'node_ip': '10.200.3.149'} if enabled else {}
            firewall.apply_mesh_forwarding_guard(['mesh0'], **options)
        return [call.kwargs['input'] for call in run.call_args_list if 'input' in call.kwargs]

    def test_default_off_has_no_authorization_or_nat(self):
        transactions = self.apply(False)
        combined = '\n'.join(transactions)
        self.assertNotIn('--set-xmark', combined)
        self.assertNotIn('-j SNAT', combined)
        self.assertNotIn('-o internet0 -j ACCEPT', combined)
        self.assertIn('-F FFDD_NODE_LAN_NAT', combined)
        self.assertIn('-F FFDD_NODE_LAN_AUTH', combined)

    def test_authorization_requires_original_trusted_uplink_request(self):
        transactions = self.apply()
        auth = next(text for text in transactions if text.startswith('*mangle'))
        self.assertIn('-i internet0 -s 192.168.1.0/24 -d 10.200.0.0/16', auth)
        self.assertIn('--ctstate NEW --ctdir ORIGINAL', auth)
        self.assertIn('-j CONNMARK --set-xmark 0x40000000/0x40000000', auth)
        self.assertNotIn('-i mesh0 -s', auth)
        self.assertTrue(transactions[-1].startswith('*mangle'), 'new connections authorized only after filter and SNAT')

    def test_forward_and_return_are_marked_and_scope_limited(self):
        text = next(t for t in self.apply() if t.startswith('*filter') and '-i internet0' in t)
        self.assertIn('-i internet0 -o mesh0 -s 192.168.1.0/24 -d 10.200.0.0/16', text)
        self.assertIn('--ctdir ORIGINAL -m connmark --mark', text)
        self.assertIn('-i mesh0 -o internet0 -d 192.168.1.0/24 -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -m connmark --mark', text)
        self.assertNotIn('-i mesh0 -o internet0 -j ACCEPT', text)
        self.assertIn('-A FFDD_NODE_FORWARD -j DROP', text)
        self.assertIn('-A FFDD_NODE_OUTPUT -o internet0 -d 192.168.1.0/24', text)
        self.assertIn('-A FFDD_NODE_OUTPUT -s 10.200.0.0/15 -j DROP', text)

    def test_snat_is_only_for_authorized_source_to_fixed_mesh_network(self):
        nat = next(t for t in self.apply() if t.startswith('*nat'))
        self.assertIn('-o mesh0 -s 192.168.1.0/24 -d 10.200.0.0/16 -m connmark --mark 0x40000000/0x40000000 -j SNAT --to-source 10.200.3.149', nat)
        self.assertNotIn('MASQUERADE', nat)
        self.assertNotIn('-o internet0', nat)

    def test_ipv6_is_not_opened_by_lan_feature(self):
        v6 = self.apply()[0]
        self.assertIn('-A FFDD_NODE_FORWARD -j DROP', v6)
        self.assertNotIn('-j ACCEPT', v6)

    def test_invalid_sources_or_primary_ip_fail_before_rules_are_modified(self):
        for sources, node_ip in [(['0.0.0.0/0'], '10.200.3.149'), (['10.200.0.0/16'], '10.200.3.149'), (['192.168.1.0/24'], None), (['192.168.1.0/24'], '192.168.1.1')]:
            with self.subTest(sources=sources, node_ip=node_ip), patch.object(firewall, '_uplink_interfaces', return_value={'internet0'}), patch.object(firewall.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    firewall.apply_mesh_forwarding_guard(['mesh0'], lan_sources=sources, node_ip=node_ip)
                run.assert_not_called()

    def test_primary_interface_follows_lowest_metric_main_default(self):
        routes = [{'dst': 'default', 'dev': 'backup0', 'metric': 100}, {'dst': 'default', 'dev': 'internet0', 'metric': 10}]
        with patch.object(firewall.subprocess, 'run', return_value=subprocess.CompletedProcess(['ip'], 0, json.dumps(routes), '')):
            self.assertEqual(firewall._primary_uplink(), 'internet0')

    def test_missing_or_ambiguous_default_is_rejected(self):
        for routes in [[], [{'dst': 'default', 'dev': 'a'}, {'dst': 'default', 'dev': 'b'}], [{'dst': 'default', 'nexthops': [{'dev': 'a'}, {'dev': 'b'}]}], [{'dst': 'default', 'dev': 'lo'}]]:
            with self.subTest(routes=routes), patch.object(firewall.subprocess, 'run', return_value=subprocess.CompletedProcess(['ip'], 0, json.dumps(routes), '')):
                with self.assertRaises(ValueError):
                    firewall._primary_uplink()


if __name__ == '__main__':
    unittest.main()
