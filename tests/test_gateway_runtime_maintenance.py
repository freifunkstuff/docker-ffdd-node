"""Periodic repair preserves exact prior confirmations, never new enrollment."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gateway_runtime as runtime
from gateway_health import runtime_ready, peer_confirmed
try:
    from .test_gateway_runtime import FakeRunner, key
except ImportError:
    from test_gateway_runtime import FakeRunner, key


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = self.root/'bmxd.env'
        self.env.write_text('BMXD_PRIMARY_IP="10.200.3.149"\nBMXD_BACKBONE_INTERFACES=""\n')
        self.runner = FakeRunner()
        self.owner = runtime.RuntimeOwner(self.runner, self.root/'runtime', self.env, self.root/'public.json')
        self.outgoing = [dict(node=51020, key=key(2), endpoint='example.invalid:5003')]
        self.incoming = [dict(node=15, publickey=key(3))]
        self.records = []
        original = self.runner.run
        def observe(*args, **kwargs):
            if 'private-key' in args:
                self.records.append(json.loads((self.root/'runtime/runtime.json').read_text()))
            return original(*args, **kwargs)
        self.runner.run = observe

    def apply(self):
        return self.owner.reconcile(913, key(1), self.outgoing, self.incoming)

    def seed_inventory(self):
        self.runner.current = key(2)+'\n'+key(3)
        self.runner.links = [{'ifname':'tbbwg','flags':['UP'],'linkinfo':{'info_kind':'wireguard'}}]
        for node in [51020,15]:
            self.runner.links.append({'ifname':'tbb_wg'+str(node),'flags':['UP'],
                'ifalias':runtime.ALIAS+str(node),'linkinfo':{'info_kind':'ipip',
                'info_data':{'local':'10.203.3.149','remote':runtime.node_addresses(node)['wireguard_ip']}}})

    def repair(self):
        self.owner._last_apply -= 31
        self.apply()
        return self.records[-1]

    def test_unchanged_repairs_confirm_only_prior_applied_peers(self):
        self.apply()
        self.assertEqual(self.records[-1]['state'],'applying')
        self.assertFalse(runtime_ready(self.records[-1]))
        self.seed_inventory()
        for _ in range(3):
            self.apply()  # Successful live observation, not last full-apply time.
            record = self.repair()
            self.assertEqual(record['state'],'repairing')
            self.assertEqual(record['peers'],{})
            self.assertTrue(peer_confirmed(record,15,key(3),now=record['timestamp']+3))
            self.assertTrue(peer_confirmed(record,51020,key(2),now=record['timestamp']+3))
            self.assertFalse(peer_confirmed(record,16,key(4),now=record['timestamp']+3))
            self.assertFalse(peer_confirmed(record,15,key(3),now=record['timestamp']+10))
            self.assertTrue(runtime_ready(record,now=record['timestamp']+3))
            self.assertFalse(runtime_ready(record,now=record['timestamp']+10))

    def test_missing_or_conflicting_inventory_never_grants_lease(self):
        for defect in ['base','down','remote','key','stale_owned']:
            with self.subTest(defect=defect):
                self.owner._fingerprint=None;self.apply();self.seed_inventory()
                if defect=='base':self.runner.links=self.runner.links[1:]
                elif defect=='down':self.runner.links[1]['flags']=[]
                elif defect=='remote':self.runner.links[1]['linkinfo']['info_data']['remote']='10.203.1.1'
                elif defect=='key':self.runner.current=key(2)
                else:self.runner.links.append({'ifname':'tbb_wg7','ifalias':runtime.ALIAS+'7'})
                try:self.repair()
                except runtime.RuntimeFailure:pass
                record=self.records[-1]
                self.assertEqual(record['state'],'applying')
                self.assertFalse(runtime_ready(record))
                self.runner.links=[];self.runner.current=key(2)

    def test_changed_intent_never_gets_maintenance_lease(self):
        self.apply();self.seed_inventory()
        self.outgoing[0]['endpoint']='changed.invalid:5003'
        self.apply()
        self.assertEqual(self.records[-1]['state'],'applying')

    def test_failed_repair_immediately_invalidates_lease_and_retry(self):
        self.apply();self.seed_inventory();self.runner.fail='bmxd'
        with self.assertRaises(runtime.RuntimeFailure):self.repair()
        self.assertEqual(self.records[-1]['state'],'repairing')
        result=json.loads((self.root/'runtime/runtime.json').read_text())
        self.assertEqual(result['state'],'retrying');self.assertFalse(runtime_ready(result))
        self.assertIsNone(self.owner._fingerprint);self.assertIsNone(self.owner._last_success)
        self.assertEqual(self.owner._confirmed_peers,{})
        self.assertNotIn('confirmed_peers',result)
        self.runner.fail=None;self.apply()
        self.assertEqual(self.records[-1]['state'],'applying')

    def test_fresh_owner_never_reuses_persisted_positive_lease(self):
        self.apply();self.seed_inventory();self.repair()
        fresh=runtime.RuntimeOwner(self.runner,self.root/'runtime',self.env,self.root/'public.json')
        self.assertEqual(fresh._confirmed_peers,{})
        fresh.reconcile(913,key(1),self.outgoing,self.incoming)
        self.assertEqual(self.records[-1]['state'],'applying')
        self.assertNotIn('confirmed_peers',self.records[-1])

    def test_successful_snapshot_replacement_drops_removed_peer(self):
        self.apply();self.seed_inventory()
        self.assertIn('15',self.owner._confirmed_peers)
        self.incoming=[]
        self.apply()
        self.assertEqual(set(self.owner._confirmed_peers),{'51020'})
        self.assertNotIn('confirmed_peers',self.records[-1])
