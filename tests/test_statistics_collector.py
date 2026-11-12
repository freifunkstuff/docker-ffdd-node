import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import statistics_collector as collector
from statistics_history import StatsHistory
from system_resources import ResourceSnapshot


class StatisticsCollectorTests(unittest.TestCase):
    def test_light_sample_uses_only_resource_and_interface_readers(self):
        snapshot = ResourceSnapshot(scope='container', sampled_at=100, cpu_capacity=2,
                                    cpu_total_seconds=3, memory_current_bytes=1024,
                                    memory_limit_bytes=2048, uptime_seconds=10)
        with patch.object(collector, 'read_resources', return_value=snapshot) as resources, patch.object(collector, '_read_interface_snapshot', return_value=({'tbb_wg_rx':'123','tbb_wg_tx':'456'}, {})) as interfaces, patch.object(collector.time, 'time', return_value=100), patch.dict(os.environ, {'SYSINFO_RESOURCE_SCOPE':'container'}):
            sample = collector.collect_snapshot()
        resources.assert_called_once_with(scope='container'); interfaces.assert_called_once_with()
        ring=StatsHistory(); self.assertTrue(ring.append(sample))
        body=ring.export(); self.assertEqual(body['resolutions']['1'][0]['data']['resources']['cpu_total_seconds'],3)
        self.assertNotIn('"sampled_at":',json.dumps(body)); self.assertNotIn('"sources":',json.dumps(body))

    def test_runtime_publication_is_atomic_readable_and_idempotently_linked(self):
        with tempfile.TemporaryDirectory(prefix='ffdd-statistics-test-') as d:
            directory=Path(d)/'tmpfs';webroot=Path(d)/'www'
            with patch.object(collector,'HISTORY_DIRECTORY',directory),patch.object(collector,'WEBROOT',webroot):
                target=directory/'statistics.json'
                collector.write_public_json(target,{'version':1,'updated_at':10})
                collector.publish_link('statistics.json',target)
                collector.publish_link('statistics.json',target)
                collector.write_public_json(target,{'version':1,'updated_at':11})
                self.assertEqual(json.loads((webroot/'statistics.json').read_text())['updated_at'],11)
                self.assertEqual(target.stat().st_mode & 0o777,0o644)
                self.assertEqual(list(directory.glob('.statistics-*')),[])
                conflict=webroot/'statistics-live.json';conflict.write_text('unrelated')
                with self.assertRaises(RuntimeError):collector.publish_link('statistics-live.json',directory/'live.json')
                self.assertEqual(conflict.read_text(),'unrelated')
