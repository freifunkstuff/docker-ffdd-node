from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from statistics_history import StatsHistory


def frame(timestamp=100, **resources):
    return {'timestamp': timestamp, 'data': {'resources': resources}}


class StatsHistoryTests(unittest.TestCase):
    def test_envelope_empty_and_detached(self):
        history = StatsHistory()
        self.assertEqual(history.export(), {'version': 1, 'history_seconds': 3600,
                                           'updated_at': None, 'detail_seconds': 300,
                                           'resolutions': {'1': [], '15': []}})
        payload = frame(cpu_total_seconds=5, scope='container')
        history.append(payload)
        payload['data']['resources']['cpu_total_seconds'] = 999
        snapshot = history.snapshot()
        snapshot['resolutions']['1'][0]['data']['resources']['cpu_total_seconds'] = 999
        self.assertEqual(history.export()['resolutions']['1'][0]['data']['resources']['cpu_total_seconds'], 5)

    def test_time_pruning_inclusive_and_cap(self):
        history = StatsHistory()
        for timestamp in range(1, 7202):
            history.append(frame(timestamp))
        output = history.export()
        self.assertEqual(len(output['resolutions']['1']), 301)
        self.assertEqual(output['resolutions']['1'][0]['timestamp'], 6901)
        coarse = output['resolutions']['15']
        self.assertLessEqual(len(coarse), 241)
        self.assertTrue(all(point['timestamp'] >= 3601 for point in coarse))
        self.assertLessEqual(len(history._samples) + len(history._coarse), 556)
        self.assertLess(len(json.dumps(output)), 500_000)

    def test_constructor_limit_and_bounded_fallback(self):
        for cap, expected in [(3, 3), (256, 256), (0, 299), (-1, 299),
                              (999999, 299), (None, 299), (True, 299), ('3', 299)]:
            history = StatsHistory(cap)
            for timestamp in range(1, 300):
                history.append(frame(timestamp))
            self.assertEqual(len(history.export()['resolutions']['1']), expected)

    def test_duplicate_backward_and_counter_reset(self):
        history = StatsHistory()
        self.assertTrue(history.append(frame('100', scope='host', cpu_total_seconds='300')))
        self.assertFalse(history.append(frame(100)))
        self.assertFalse(history.append(frame(99)))
        self.assertTrue(history.append(frame(101, scope='host', cpu_total_seconds=0)))
        samples = history.export()['resolutions']['1']
        self.assertEqual([x['data']['resources']['cpu_total_seconds'] for x in samples], [300, 0])
        self.assertNotIn('cpu', samples[-1])

    def test_timestamps_and_malformed_fields(self):
        history = StatsHistory()
        for value in [None, '', ' ', True, False, {}, [], -1, 0, 'nan', float('inf'),
                      '2026-01-01', 10**500, '1e999', 9007199254740992]:
            self.assertFalse(history.append(frame(value)), repr(value))
        self.assertTrue(history.append(frame('1.5e3', scope='container', cpu_capacity='0',
                                            cpu_total_seconds='bad', memory_current_bytes=None,
                                            memory_remaining_bytes=False, memory_limit_bytes='nan')))
        self.assertEqual(history.export()['resolutions']['1'][0], {
            'timestamp': 1500, 'data': {'resources': {'scope': 'container', 'cpu_capacity': 0}}})

    def test_privacy_allowlist_and_raw_values(self):
        payload = frame(scope='container', cpu_capacity='1.5', cpu_total_seconds='12.75',
                        memory_current_bytes=0, memory_limit_bytes='8192', memory_remaining_bytes=8192,
                        uptime_seconds='0', sources='/secret', error='secret')
        payload.update({'name': 'SECRET', 'contact': 'SECRET', 'key': 'SECRET'})
        payload['data'].update({'gps': 'SECRET', 'bmxd': {'routes': 'SECRET'},
                                'system': {'uptime': 'SECRET', 'hostname': 'SECRET'},
                                'statistic': {'meminfo_MemTotal': '8 kB', 'meminfo_MemFree': '0 KiB',
                                              'interfaces': {'tbb_wg_rx': '0', 'tbb_wg_tx': '120',
                                                             'SIOCGIF': 'SECRET', 'eth0': 'SECRET'}}})
        history = StatsHistory()
        history.append(payload)
        output = history.export()['resolutions']['1'][0]['data']
        self.assertEqual(output['resources']['memory_current_bytes'], 0)
        self.assertEqual(output['resources']['uptime_seconds'], 0)
        self.assertEqual(output['statistic']['interfaces'], {'tbb_wg_rx': 0, 'tbb_wg_tx': 120})
        self.assertEqual(output['statistic']['meminfo_MemFree'], '0 KiB')
        self.assertNotIn('SECRET', json.dumps(history.export()))
        self.assertNotIn('"sources"', json.dumps(history.export()))
        self.assertNotIn('system', output)

    def test_unknown_scope_and_resource_presence_no_host_fallback(self):
        for resource in [{}, None, 'bad', {'scope': 'unknown', 'cpu_capacity': 2,
                                         'cpu_total_seconds': 100, 'memory_current_bytes': 42}]:
            history = StatsHistory()
            history.append({'timestamp': 1, 'data': {'resources': resource,
                                                    'system': {'uptime': '999 888'}}})
            data = history.export()['resolutions']['1'][0]['data']
            self.assertIn('resources', data)
            self.assertNotIn('system', data)
            self.assertNotIn('cpu_total_seconds', data['resources'])
            self.assertNotIn('cpu_capacity', data['resources'])
        self.assertEqual(data['resources']['memory_current_bytes'], 42)

    def test_legacy_uptime_memory_units_and_gaps(self):
        history = StatsHistory()
        history.append({'timestamp': 1, 'data': {'system': {'uptime': '123.5 456'},
                                                'statistic': {'meminfo_MemTotal': '64 kB',
                                                              'meminfo_MemFree': '10 MB'}}})
        data = history.export()['resolutions']['1'][0]['data']
        self.assertEqual(data['system']['uptime'], 123.5)
        self.assertEqual(data['statistic'], {'meminfo_MemTotal': '64 kB'})
        for timestamp, value in enumerate(['x' * 65, 'nan', '-1 kB', '1e3 kB', None, True], 2):
            history.append({'timestamp': timestamp, 'data': {'statistic': {'meminfo_MemTotal': value}}})
            self.assertEqual(history.export()['resolutions']['1'][-1]['data'], {})
        self.assertTrue(history.append({'timestamp': 100}))
        self.assertEqual(history.export()['resolutions']['1'][-1]['data'], {})

    def test_full_ring_wire_size_bound(self):
        history = StatsHistory()
        for timestamp in range(1, 7202):
            payload = frame(timestamp, scope='container', cpu_capacity=1e308,
                            cpu_total_seconds=1e308, memory_current_bytes=1e308,
                            memory_limit_bytes=1e308, memory_remaining_bytes=1e308,
                            uptime_seconds=1e308)
            payload['data']['statistic'] = {
                'interfaces': {key: 1e308 for key in ('tbb_wg_rx', 'tbb_wg_tx', 'tbb_fastd_rx', 'tbb_fastd_tx')},
                'meminfo_MemTotal': '0' * 60 + ' kB', 'meminfo_MemFree': '0' * 60 + ' kB'}
            history.append(payload)
        self.assertLess(len(json.dumps(history.export(), allow_nan=False)), 500_000)

    def measured(self, timestamp, memory=10, counter=None, scope='container', capacity=1, uptime=None):
        counter = timestamp if counter is None else counter
        payload = frame(timestamp, scope=scope, cpu_capacity=capacity, cpu_total_seconds=counter,
                        uptime_seconds=timestamp if uptime is None else uptime,
                        memory_current_bytes=memory)
        payload['data']['statistic'] = {'interfaces': {'tbb_wg_rx': counter, 'tbb_wg_tx': counter * 2}}
        return payload

    def test_ram_mean_extrema_count_and_end_counters(self):
        history = StatsHistory()
        for timestamp, memory in [(30, 0), (31, 30), (32, 60)]:
            history.append(self.measured(timestamp, memory))
        output = history.export()
        coarse = output['resolutions']['15'][0]
        self.assertEqual(coarse['timestamp'], 32)
        self.assertEqual(coarse['aggregation'], {'count': 3, 'start': 30, 'end': 32,
                                               'discontinuous': False, 'cpu_discontinuous': False,
                                               'rx_discontinuous': False, 'tx_discontinuous': False,
                                               'memory_count': 3})
        resources = coarse['data']['resources']
        self.assertEqual(resources['memory_current_bytes'], 30)
        self.assertEqual(resources['memory_current_min_bytes'], 0)
        self.assertEqual(resources['memory_current_max_bytes'], 60)
        self.assertEqual(resources['cpu_total_seconds'], 32)
        self.assertEqual(coarse['data']['statistic']['interfaces']['tbb_wg_rx'], 32)
        self.assertEqual([p['data']['resources']['memory_current_bytes'] for p in output['resolutions']['1']], [0, 30, 60])

    def test_bucket_boundary_and_missing_ram(self):
        history = StatsHistory()
        history.append(self.measured(14, 10))
        history.append(self.measured(15, 100))
        history.append(frame(16))
        history.append(self.measured(17, 300))
        coarse = history.export()['resolutions']['15']
        self.assertEqual([p['aggregation']['count'] for p in coarse], [1, 3])
        self.assertEqual(coarse[1]['aggregation']['memory_count'], 2)
        self.assertEqual(coarse[1]['data']['resources']['memory_current_bytes'], 200)
        self.assertTrue(coarse[1]['aggregation']['discontinuous'])

    def test_hidden_resets_scope_capacity_gap_and_missing_counters(self):
        missing = self.measured(31)
        missing['data'].pop('statistic')
        for middle in [self.measured(31, counter=0), self.measured(31, scope='host'),
                       self.measured(31, capacity=2), self.measured(31, uptime=1),
                       self.measured(34), frame(31), missing]:
            history = StatsHistory()
            history.append(self.measured(30))
            history.append(middle)
            history.append(self.measured(35))
            coarse = history.export()['resolutions']['15'][0]
            self.assertTrue(any(coarse['aggregation'][key] for key in
                                ('discontinuous', 'cpu_discontinuous', 'rx_discontinuous', 'tx_discontinuous')), middle)
            self.assertEqual(coarse['data']['resources']['cpu_total_seconds'], 35)
        history = StatsHistory()
        history.append(self.measured(29))
        history.append(self.measured(30, counter=0))
        self.assertTrue(history.export()['resolutions']['15'][-1]['aggregation']['cpu_discontinuous'])

    def test_metric_flags_isolate_network_and_cpu(self):
        history = StatsHistory()
        for timestamp in (30, 31):
            payload = self.measured(timestamp)
            payload['data'].pop('statistic')
            payload['data']['resources'].pop('uptime_seconds')
            history.append(payload)
        aggregation = history.export()['resolutions']['15'][0]['aggregation']
        self.assertFalse(aggregation['discontinuous'])
        self.assertFalse(aggregation['cpu_discontinuous'])
        self.assertTrue(aggregation['rx_discontinuous'])
        self.assertTrue(aggregation['tx_discontinuous'])
        history = StatsHistory()
        history.append(self.measured(30))
        payload = self.measured(31)
        payload['data']['statistic']['interfaces']['tbb_wg_rx'] = 0
        history.append(payload)
        aggregation = history.export()['resolutions']['15'][0]['aggregation']
        self.assertFalse(aggregation['discontinuous'])
        self.assertFalse(aggregation['cpu_discontinuous'])
        self.assertTrue(aggregation['rx_discontinuous'])
        self.assertFalse(aggregation['tx_discontinuous'])

    def test_tail_bounds_privacy_and_copy(self):
        history = StatsHistory()
        self.assertEqual(history.tail(), [])
        for timestamp in range(1, 401):
            payload = self.measured(timestamp)
            payload['secret'] = 'SECRET'
            history.append(payload)
        tail = history.tail()
        self.assertEqual([sample['timestamp'] for sample in tail], list(range(384, 401)))
        self.assertEqual(len(history.tail(0)), 1)
        self.assertEqual(len(history.tail(999999)), 301)
        self.assertEqual(len(history.tail(None)), 17)
        self.assertNotIn('SECRET', json.dumps(tail))
        tail[-1]['data']['resources']['cpu_total_seconds'] = 999
        self.assertEqual(history.tail(0)[0]['data']['resources']['cpu_total_seconds'], 400)

    def exit_frame(self, timestamp, exits):
        payload = self.measured(timestamp)
        payload['data']['exit_interfaces'] = exits
        return payload

    def test_exit_end_counters_and_isolated_reset(self):
        history = StatsHistory()
        history.append(self.exit_frame(30, {'public-exit': {'rx_bytes': '100', 'tx_bytes': 200}}))
        history.append(self.exit_frame(31, {'public-exit': {'rx_bytes': 150, 'tx_bytes': 220}}))
        coarse = history.export()['resolutions']['15'][0]
        self.assertEqual(coarse['data']['exit_interfaces'], {'public-exit': {'rx_bytes': 150, 'tx_bytes': 220}})
        self.assertEqual(coarse['aggregation']['exit_discontinuous'], {'public-exit': {'rx': False, 'tx': False}})
        history.append(self.exit_frame(32, {'public-exit': {'rx_bytes': 0, 'tx_bytes': 230}}))
        history.append(self.exit_frame(33, {'public-exit': {'rx_bytes': 170, 'tx_bytes': 240}}))
        coarse = history.export()['resolutions']['15'][0]
        aggregation = coarse['aggregation']
        self.assertEqual(aggregation['exit_discontinuous']['public-exit'], {'rx': True, 'tx': False})
        for key in ('discontinuous', 'cpu_discontinuous', 'rx_discontinuous', 'tx_discontinuous'):
            self.assertFalse(aggregation[key])
        self.assertEqual(coarse['data']['exit_interfaces']['public-exit']['rx_bytes'], 170)

    def test_exit_appearance_disappearance_missing_direction_and_gap(self):
        history = StatsHistory()
        history.append(self.exit_frame(30, {}))
        history.append(self.exit_frame(31, {'exit': {'rx_bytes': 0, 'tx_bytes': 1}}))
        history.append(self.exit_frame(32, {}))
        coarse = history.export()['resolutions']['15'][0]
        self.assertNotIn('exit_interfaces', coarse['data'])
        self.assertEqual(coarse['aggregation']['exit_discontinuous']['exit'], {'rx': True, 'tx': True})
        history = StatsHistory()
        history.append(self.exit_frame(30, {'exit': {'rx_bytes': 10, 'tx_bytes': 10}}))
        history.append(self.exit_frame(31, {'exit': {'rx_bytes': 20}}))
        flags = history.export()['resolutions']['15'][0]['aggregation']['exit_discontinuous']['exit']
        self.assertEqual(flags, {'rx': False, 'tx': True})
        history.append(self.exit_frame(34, {'exit': {'rx_bytes': 30, 'tx_bytes': 30}}))
        flags = history.export()['resolutions']['15'][0]['aggregation']['exit_discontinuous']['exit']
        self.assertEqual(flags, {'rx': True, 'tx': True})

    def test_exit_allowlist_privacy_and_copy(self):
        exits = {name: {'rx_bytes': 1, 'tx_bytes': 1} for name in
                 ('constructor', 'prototype', '__proto__', '../bad', 'ä', 'a' * 65, '', '_bad')}
        exits.update({'zero': {'rx_bytes': '0', 'tx_bytes': False, 'PrivateKey': 'SECRET',
                               'dns': 'SECRET', 'interface': 'SECRET', 'endpoint': 'SECRET'},
                      'bad-values': {'rx_bytes': 'nan', 'tx_bytes': -1},
                      'nested': {'rx_bytes': {'PrivateKey': 'SECRET'}, 'tx_bytes': float('inf')}})
        history = StatsHistory()
        history.append(self.exit_frame(30, exits))
        tail = history.tail()
        self.assertEqual(tail[0]['data']['exit_interfaces'], {'zero': {'rx_bytes': 0}, 'bad-values': {}, 'nested': {}})
        self.assertNotIn('SECRET', json.dumps(history.export(), allow_nan=False))
        tail[0]['data']['exit_interfaces']['zero']['rx_bytes'] = 999
        self.assertEqual(history.tail()[0]['data']['exit_interfaces']['zero']['rx_bytes'], 0)
        coarse = history.export()
        coarse['resolutions']['15'][0]['aggregation']['exit_discontinuous']['zero']['rx'] = True
        self.assertFalse(history.export()['resolutions']['15'][0]['aggregation']['exit_discontinuous']['zero']['rx'])

    def test_exit_profile_and_churn_bounds(self):
        history = StatsHistory()
        for index in range(100):
            exits = {f'profile-{index}-{number}': {'rx_bytes': 0, 'tx_bytes': 0} for number in range(20)}
            history.append(self.exit_frame(30 + index / 100, exits))
        coarse = history.export()['resolutions']['15'][0]
        self.assertEqual(len(coarse['data']['exit_interfaces']), 8)
        self.assertLessEqual(len(coarse['aggregation']['exit_discontinuous']), 16)
        self.assertTrue(all(flag['rx'] and flag['tx'] for flag in coarse['aggregation']['exit_discontinuous'].values()))
        self.assertLessEqual(len(history.tail()), 100)


if __name__ == '__main__':
    unittest.main()
