from pathlib import Path
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import mesh_firewall


class ExitHookTests(unittest.TestCase):
    def test_enabled_hooks_precede_drop_and_preserve_controller_chains(self):
        existing='\n'.join('-N '+name for name in (*mesh_firewall.EXIT_HOOKS,'FFDD_EXIT_NAT'))
        ok=subprocess.CompletedProcess([],0,existing,'')
        with patch.dict(os.environ,{'EXIT_CONFIG_FILE':'/run/secrets/exits.yaml'}),patch.object(mesh_firewall,'_uplink_interfaces',return_value={'eth0'}),patch.object(mesh_firewall.subprocess,'run',return_value=ok) as run:
            mesh_firewall.apply_mesh_forwarding_guard(['mesh0'])
        transactions=[c.kwargs['input'] for c in run.call_args_list if 'input'in c.kwargs]
        text='\n'.join(transactions)
        self.assertIn('-A FFDD_NODE_FORWARD -j FFDD_EXIT_FORWARD',text)
        self.assertIn('-A FFDD_NODE_INPUT -j FFDD_EXIT_INPUT',text)
        self.assertIn('-A FFDD_NODE_OUTPUT -j FFDD_EXIT_OUTPUT',text)
        self.assertIn('-A FFDD_NODE_LAN_NAT -j FFDD_EXIT_NAT',text)
        for chain in (*mesh_firewall.EXIT_HOOKS,'FFDD_EXIT_NAT'):
            self.assertNotIn('-F '+chain+'\n',text)
        self.assertFalse(any(c.args[0][-2:]==['-N','FFDD_EXIT_FORWARD'] for c in run.call_args_list))

    def test_unconfigured_node_retains_existing_no_exit_policy(self):
        ok=subprocess.CompletedProcess([],0,'','')
        with patch.dict(os.environ,{'EXIT_CONFIG_FILE':''}),patch.object(mesh_firewall,'_uplink_interfaces',return_value={'eth0'}),patch.object(mesh_firewall.subprocess,'run',return_value=ok) as run:
            mesh_firewall.apply_mesh_forwarding_guard(['mesh0'])
        text='\n'.join(c.kwargs.get('input','') for c in run.call_args_list)
        self.assertNotIn('FFDD_EXIT_',text)
        self.assertIn('-A FFDD_NODE_FORWARD -j DROP',text)
