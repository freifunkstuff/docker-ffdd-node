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

    def test_large_limits_are_preserved_for_custom_commands(self):
        available = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        limit = 131072 if available == resource.RLIM_INFINITY else min(131072, available)
        if limit <= 65536:
            self.skipTest('host hard limit too low for large-limit regression')
        self.assertEqual(self.run_with_limits(limit, limit), [limit, limit])

    def test_stricter_limits_are_preserved(self):
        self.assertEqual(self.run_with_limits(1024, 4096), [1024, 4096])

    def test_mixed_soft_and_hard_limits_are_preserved(self):
        available = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        limit = 131072 if available == resource.RLIM_INFINITY else min(131072, available)
        if limit <= 65536:
            self.skipTest('host hard limit too low for large-limit regression')
        self.assertEqual(self.run_with_limits(1024, limit), [1024, limit])


if __name__ == '__main__':
    unittest.main()
