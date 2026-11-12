import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gateway_control as control


class ControlHealthTests(unittest.TestCase):
    def test_bad_runtime_does_not_hide_other_checks(self):
        def read(path, maximum):
            if 'runtime' in str(path):
                raise ValueError('invalid runtime')
            return {'timestamp': 100, 'exits': [{'active': True, 'state': 'ready', 'reachable': True}]}
        with patch.object(control, 'fresh_json', side_effect=read), \
             patch.object(control.Path, 'read_text', side_effect=FileNotFoundError), \
             patch.object(control.time, 'time', return_value=100), \
             patch.object(control.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps({'status': 'NotRestricted', 'server': {'node': 1}}))), \
             patch.object(control, 'dns_probe', return_value=True):
            self.assertEqual(control.readiness(), {'exit': True, 'peers': False, 'admission': True, 'dns': True})

    def test_lease_expires_during_probe_no_announcement(self):
        record = dict(state='repairing', timestamp=97, last_success_at=96, repair_started_at=97)
        checks = {'exit': True, 'peers': True, 'admission': True, 'dns': True}
        with patch.object(control, 'exit_readiness', return_value=True), \
             patch.object(control, 'fresh_json', return_value=record), \
             patch('gateway_health.time.time', return_value=107):
            healthy = control.recheck_before_announce(checks)
        runner = unittest.mock.Mock(return_value=unittest.mock.Mock(returncode=0))
        controller = control.AnnouncementController(runner)
        controller.apply(healthy, True)
        self.assertFalse(controller.advertised)
        self.assertNotIn('-g', runner.call_args.args[0])

    def test_repair_retains_public_counts_only_during_lease(self):
        record = dict(state='repairing', timestamp=97, last_success_at=96, repair_started_at=97, peers={})
        summary = dict(timestamp=96, peers=141, inbound=141, incoming_connected=97)
        for now, expected in ((100, (141, 141, 97)), (107, (None, None, None))):
            with patch.object(control, 'fresh_json', side_effect=lambda path, *args: record if 'runtime' in path else summary), \
                 patch('gateway_health.time.time', return_value=now), \
                 patch('gateway_fastd.GatewayFastd', side_effect=OSError):
                wg, _ = control.peer_summary()
            self.assertEqual((wg['peers'], wg['inbound'], wg['active']), expected)
        with patch.object(control, 'fresh_json', side_effect=[record, dict(summary, peers=True)]), \
             patch('gateway_health.time.time', return_value=100), \
             patch('gateway_fastd.GatewayFastd', side_effect=OSError):
            self.assertIsNone(control.peer_summary()[0]['peers'])

    def test_fresh_json_rejects_invalid_timestamps(self):
        for stamp in (True, float('nan'), float('inf'), 101, 84):
            with patch.object(control.Path, 'stat') as stat, \
                 patch.object(control.Path, 'read_text', return_value=json.dumps({'timestamp': stamp})), \
                 patch.object(control.time, 'time', return_value=100):
                stat.return_value.st_size = 20
                with self.assertRaises(ValueError):
                    control.fresh_json('/unused')
