from pathlib import Path
import json
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import gateway_init
import sysinfo


class GatewayObservationTests(unittest.TestCase):
    def test_shutdown_withdraws_before_exit_and_backbone_teardown(self):
        calls=[]
        gateway_init.shutdown_services(lambda args,**kwargs:calls.append(args[-1]))
        self.assertLess(calls.index('/etc/service/gateway-control'),calls.index('/etc/service/exits'))
        self.assertLess(calls.index('/etc/service/exits'),calls.index('/etc/service/bmxd'))
        self.assertIn('/etc/service/statistics',calls)

    def test_incoming_backbone_counters_are_counted_once_without_underlay_or_paths(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'runtime.json';path.write_text(json.dumps({'peers':{'111':{'ifname':'tbb_wg111','applied':True},'999':{'ifname':'tbb_wg999','applied':True},'bad':{'ifname':'../../secret','applied':True}}}))
            observed=[]
            def read(p):
                observed.append(str(p))
                values={'tbb_wg111':{'rx_bytes':'10','tx_bytes':'20'},'tbb_wg999':{'rx_bytes':'30','tx_bytes':'40'}}
                return values.get(p.parent.parent.name,{}).get(p.name,'')
            kernel = Path(d) / 'net'; kernel.mkdir()
            for node in ('111', '999'):
                device = kernel / ('tbb_wg' + node); device.mkdir()
                (device / 'ifalias').write_text('ffdd-gateway:' + node + '\n')
            original_path = Path
            def mapped_path(value):
                return kernel if value == '/sys/class/net' else original_path(value)
            with patch.dict(os.environ,{'NODE_ROLE':'gateway','GATEWAY_RUNTIME_STATUS_FILE':str(path)}),patch.object(sysinfo,'Path',side_effect=mapped_path),patch.object(sysinfo,'load_runtime_env',return_value={}),patch.object(sysinfo,'parse_configured_peers',return_value=('',{'a':SimpleNamespace(ifname='tbb_wg111')})),patch.object(sysinfo,'_read_text',side_effect=read):
                data=sysinfo._read_interface_stats()
            self.assertEqual(data['tbb_wg_rx'],'40');self.assertEqual(data['tbb_wg_tx'],'60')
            self.assertFalse(any('secret'in p or '/tbbwg/'in p for p in observed))
