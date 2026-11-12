from pathlib import Path
import json
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import sysinfo
import statistics_collector as collector
from statistics_history import StatsHistory
from system_resources import ResourceSnapshot


class BackboneCounterMembershipTests(unittest.TestCase):
    def test_kernel_roster_survives_all_control_plane_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            net = root / 'net'; net.mkdir()
            def device(name, alias, index, rx=1000, tx=2000):
                base = net / name; base.mkdir()
                (base / 'statistics').mkdir()
                (base / 'ifalias').write_text(alias + '\n')
                (base / 'ifindex').write_text(str(index))
                (base / 'statistics/rx_bytes').write_text(str(rx))
                (base / 'statistics/tx_bytes').write_text(str(tx))
            for node in range(1, 144):
                device('tbb_wg' + str(node), 'ffdd-gateway:' + str(node), node)
            device('tbb_wg999', '', 999, 7, 9)  # configured legacy outgoing
            for name, alias in [('tbbwg', 'ffdd-gateway:1'), ('tbb_wg444', 'foreign:444'),
                                ('tbb_wg445', 'ffdd-gateway:446'), ('tbb_wg123456', 'ffdd-gateway:123456'),
                                ('tbb_wg١', 'ffdd-gateway:١')]:
                device(name, alias, 1000, 999999, 999999)
            status = root / 'runtime.json'
            original_path = Path
            def mapped(value):
                if value == '/sys/class/net':
                    return net
                if value == '/sys/class/net/tbb_fastd':
                    return net / 'tbb_fastd'
                return original_path(value)
            with patch.dict(os.environ, {'NODE_ROLE': 'gateway', 'GATEWAY_RUNTIME_STATUS_FILE': str(status)}), \
                    patch.object(sysinfo, 'Path', side_effect=mapped), \
                    patch.object(sysinfo, 'load_runtime_env', return_value={}), \
                    patch.object(sysinfo, 'parse_configured_peers', return_value=('', {'legacy': SimpleNamespace(ifname='tbb_wg999'), 'bad': SimpleNamespace(ifname='../../secret')})):
                first = None
                for state in ('ready', 'applying', 'repairing', 'retrying', 'failed'):
                    status.write_text(json.dumps({'status': state, 'peers': {}}))
                    snapshot = sysinfo._read_interface_snapshot()
                    self.assertEqual(snapshot[0]['tbb_wg_rx'], str(143000 + 7))
                    self.assertEqual(snapshot[0]['tbb_wg_tx'], str(286000 + 9))
                    self.assertEqual(len(snapshot[1]['tbb_wg_rx']), 144)
                    if first is not None:
                        self.assertEqual(snapshot, first)
                    first = snapshot
                (net / 'tbb_wg1/ifindex').write_text('9000')
                self.assertNotEqual(first[1], sysinfo._read_interface_snapshot()[1])
                with patch.dict(os.environ, {'NODE_ROLE': 'node'}):
                    self.assertEqual(sysinfo._read_interface_stats()['tbb_wg_rx'], '7')

    def test_membership_changes_gap_only_affected_direction_once(self):
        resource = ResourceSnapshot(scope='container', sampled_at=100, cpu_total_seconds=3)
        previous = {}
        ring = StatsHistory()
        a = frozenset({('tbb_wg1', '1')})
        b = frozenset({('tbb_wg1', '1'), ('tbb_wg2', '2')})
        recreated = frozenset({('tbb_wg1', '3'), ('tbb_wg2', '2')})
        # Addition, recreation and removal, plus an RX-only membership change.
        generations = [(a,a), (b,b), (b,b), (recreated,recreated),
                       (recreated,recreated), (a,recreated), (a,recreated), (a,a), (a,a)]
        for index, (rx, tx) in enumerate(generations):
            counters = {'tbb_wg_rx': '0', 'tbb_wg_tx': '0', 'tbb_fastd_rx': '10', 'tbb_fastd_tx': '20'}
            inventory = {'tbb_wg_rx': rx, 'tbb_wg_tx': tx}
            with patch.object(collector, 'read_resources', return_value=resource), \
                    patch.object(collector, '_read_interface_snapshot', return_value=(counters, inventory)), \
                    patch.object(collector, 'read_exit_counters', return_value={}), \
                    patch.object(collector.time, 'time', return_value=100 + index):
                sample = collector.collect_snapshot(previous)
            interfaces = sample['data']['statistic']['interfaces']
            for direction, members, position in [('rx',rx,0), ('tx',tx,1)]:
                changed = index > 0 and members != generations[index-1][position]
                self.assertEqual('tbb_wg_' + direction in interfaces, not changed)
            self.assertEqual(interfaces['tbb_fastd_rx'], '10')
            self.assertEqual(sample['data']['resources']['cpu_total_seconds'], 3)
            self.assertTrue(ring.append(sample))
        public = json.dumps(ring.export())
        self.assertNotIn('tbb_wg1', public)
        self.assertNotIn('inventory', public)
