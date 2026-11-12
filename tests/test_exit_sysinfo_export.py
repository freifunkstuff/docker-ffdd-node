from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import sysinfo
from system_resources import ResourceSnapshot


class ExitSysinfoExportTests(unittest.TestCase):
    def test_legacy_vpn_counters_aggregate_exit_interfaces_not_backbones(self):
        observed={'a':{'rx_bytes':100,'tx_bytes':50},'b':{'rx_bytes':20,'tx_bytes':0}}
        with patch.object(sysinfo,'read_exit_counters',return_value=observed),patch.object(Path,'exists',return_value=False):
            self.assertEqual(sysinfo._read_exit_interface_stats(),{'vpn_rx':'120','vpn_tx':'50'})
        with patch.object(sysinfo,'read_exit_counters',return_value={'a':{}}),patch.object(Path,'exists',return_value=False):
            self.assertEqual(sysinfo._read_exit_interface_stats(),{})

    def test_public_monotonic_summary_only_not_calendar_or_private_baselines(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'volume.json';p.write_text(json.dumps({'version':1,'totals':{'rx_bytes':123,'tx_bytes':456},'started_at':10,'updated_at':20,'days':[{'date':'2026-11-08'}],'baselines':{'private':'not-exported'}}))
            with patch.dict(sysinfo.os.environ,{'EXIT_TRAFFIC_PUBLIC_FILE':str(p)}),patch.object(sysinfo,'_read_gateway_usage',return_value=[]),patch.object(sysinfo,'_read_interface_stats',return_value={}),patch.object(sysinfo,'_read_exit_interface_stats',return_value={}):
                block=sysinfo._build_statistic_block(ResourceSnapshot(scope='container',sampled_at=20))
            self.assertEqual(block['exit_traffic'],{'rx_bytes_total':123,'tx_bytes_total':456,'started_at':10,'updated_at':20})
            self.assertNotIn('baselines',json.dumps(block));self.assertNotIn('days',block['exit_traffic'])
