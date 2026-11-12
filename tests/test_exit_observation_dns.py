import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from exit_observation import read_exit_counters, read_node_primary
from exit_dns import dns_configuration


class ExitObservationDnsTests(unittest.TestCase):
    def test_counters_use_only_valid_profile_interfaces_and_real_zeros(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);status=root/'exits.json';sysfs=root/'sys'
            interface='wgx0123456789';base=sysfs/interface/'statistics';base.mkdir(parents=True)
            (base/'rx_bytes').write_text('0');(base/'tx_bytes').write_text('123')
            status.write_text(json.dumps({'exits':[{'name':'test','interface':interface,'privateKey':'do-not-copy'},{'name':'bad','interface':'../../data'}]}))
            self.assertEqual(read_exit_counters(status,sysfs),{'test':{'rx_bytes':0,'tx_bytes':123}})

    def test_identity_comes_from_published_metadata_not_new_registration(self):
        with tempfile.TemporaryDirectory() as d:
            env=Path(d)/'bmxd.env';env.write_text('BMXD_PRIMARY_IP="10.200.3.149"\n')
            with patch.dict(os.environ,{'BMXD_ENV_FILE':str(env),'EXIT_NODE_IP':''}):
                self.assertEqual(read_node_primary(),'10.200.3.149')
                env.write_text('BMXD_PRIMARY_IP="192.168.1.2"\n');self.assertIsNone(read_node_primary())

    def test_dns_is_exit_bound_no_system_resolver_and_only_when_reachable(self):
        profile={'name':'test','active':True,'state':'ready','reachable':True,'interface':'wgx0123456789','dns':['10.64.0.1']}
        status={'enabled':True,'exits':[profile]}
        config=dns_configuration(status,'10.200.3.149')
        self.assertIn('no-resolv',config)
        self.assertIn('server=10.64.0.1@wgx0123456789',config)
        self.assertIn('listen-address=127.0.0.1,10.200.3.149',config)
        self.assertNotIn('0.0.0.0',config)
        profile['reachable']=False;self.assertIsNone(dns_configuration(status,'10.200.3.149'))
        profile['reachable']=True;profile['interface']='eth0'
        with self.assertRaises(ValueError):dns_configuration(status,'10.200.3.149')
