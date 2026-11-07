from __future__ import annotations

import json
from pathlib import Path
import resource
import subprocess
import sys
import unittest

ENTRYPOINT = Path(__file__).resolve().parents[1] / 'scripts/docker-entrypoint.sh'


class EntrypointLimitsTests(unittest.TestCase):
    def run_with_limits(self, soft: int, hard: int):
        def configure():
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        result = subprocess.run(
            ['sh', str(ENTRYPOINT), sys.executable, '-c',
             'import json,resource; print(json.dumps(resource.getrlimit(resource.RLIMIT_NOFILE)))'],
            preexec_fn=configure, capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def test_large_limits_are_bounded_even_for_custom_commands(self):
        available = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        limit = 131072 if available == resource.RLIM_INFINITY else min(131072, available)
        if limit <= 65536:
            self.skipTest('host hard limit already below runtime cap')
        self.assertEqual(self.run_with_limits(limit, limit), [65536, 65536])

    def test_stricter_limits_are_preserved(self):
        self.assertEqual(self.run_with_limits(1024, 4096), [1024, 4096])

    def test_large_hard_limit_is_capped_without_raising_small_soft_limit(self):
        available = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        limit = 131072 if available == resource.RLIM_INFINITY else min(131072, available)
        if limit <= 65536:
            self.skipTest('host hard limit already below runtime cap')
        self.assertEqual(self.run_with_limits(1024, limit), [1024, 65536])


if __name__ == '__main__':
    unittest.main()
