from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import registrar
from node_config import build_base_config


class GatewayRegistrarTests(unittest.TestCase):
    def test_gateway_client_reconciler_only_publishes_intent_never_mutates_kernel(self):
        config=build_base_config();config['system']['node_role']='gateway';config['wireguard']={'port':5003}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch.dict(os.environ,{'GATEWAY_RUNTIME_DIR':str(root/'gateway')}),patch.object(registrar,'WIREGUARD_RUNTIME_DIR',root/'wg'),patch.object(registrar,'WIREGUARD_ENV_FILE',root/'wg/wireguard.env'),patch.object(registrar,'_list_wireguard_tunnels',return_value=['tbb_wg900']),patch.object(registrar,'_ensure_wireguard_interface') as base,patch.object(registrar,'_sync_wireguard_peers') as peers,patch.object(registrar,'_run_ip') as ip:
                changed,interfaces=registrar.reconcile_wireguard_runtime(config,'unused-secret',[],913,'A'*43+'=')
            self.assertTrue(changed);self.assertEqual(interfaces,['tbb_wg900'])
            base.assert_not_called();peers.assert_not_called();ip.assert_not_called()
            self.assertEqual(json.loads((root/'gateway/outgoing.json').read_text()),{'peers':[]})

    def test_gateway_fastd_listens_without_outgoing_and_does_not_touch_accepted(self):
        config=build_base_config();config['system']['node_role']='gateway';config['wireguard']={'port':5003}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);accepted=root/'incoming/accepted';accepted.mkdir(parents=True);record=accepted/'keep.conf';record.write_text('key "'+'a'*64+'";')
            with patch.dict(os.environ,{'GATEWAY_FASTD_ROOT':str(root/'incoming')}),patch.object(registrar,'FASTD_RUNTIME_DIR',root),patch.object(registrar,'FASTD_CONFIG',root/'fastd.conf'),patch.object(registrar,'FASTD_ENV_FILE',root/'fastd.env'),patch.object(registrar,'PEER_DIR',root/'outgoing'):
                registrar.render_fastd_runtime(config,'0'*64,[],913)
            text=(root/'fastd.conf').read_text()
            self.assertIn('gateway_fastd.py verify',text);self.assertIn(str(accepted),text)
            self.assertIn('method "null+salsa2012+umac"',text)
            self.assertTrue(record.exists())
