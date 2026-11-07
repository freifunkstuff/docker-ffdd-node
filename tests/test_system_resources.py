from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from system_resources import read_resources, cpu_usage_delta


def pidstat(start):
    return '1 (init (with) spaces)) S ' + ' '.join(['0'] * 18 + [str(start)])


class ResourceTests(unittest.TestCase):
    def snapshot(self, files, scope='container', now=10, root='/fixture', environment=None):
        def reader(path):
            key = '/' + str(path.relative_to(root))
            if key not in files:
                raise FileNotFoundError(key)
            return files[key]
        return read_resources(scope=scope, root=root, reader=reader,
                              clock=lambda: now, clock_ticks=100,
                              environment={} if environment is None else environment)

    def v2(self):
        return {
            '/proc/self/cgroup': '0::/work/leaf\n',
            '/proc/1/cgroup': '0::/work/leaf\n',
            '/proc/self/mountinfo': '29 22 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n',
            '/sys/fs/cgroup/work/leaf/cpu.max': '150000 100000',
            '/sys/fs/cgroup/work/leaf/cpuset.cpus.effective': '0-3,6',
            '/sys/fs/cgroup/work/leaf/cpu.stat': 'usage_usec 3000000\nuser_usec 2000000\nsystem_usec 1000000\nnr_periods 9\nnr_throttled 2\nthrottled_usec 250000',
            '/sys/fs/cgroup/work/leaf/memory.max': '8192',
            '/sys/fs/cgroup/work/leaf/memory.current': '3000',
            '/sys/fs/cgroup/work/leaf/memory.stat': 'file 1000\nanon 2000',
            '/proc/uptime': '1000.50 900',
            '/proc/1/stat': pidstat(10000),
        }

    def test_v2_fraction_counters_cache_bytes_uptime(self):
        s = self.snapshot(self.v2())
        self.assertEqual(s.cpu_count, 5)
        self.assertEqual(s.cpu_capacity, 1.5)
        self.assertEqual((s.cpu_user_seconds, s.cpu_system_seconds, s.cpu_total_seconds), (2, 1, 3))
        self.assertEqual((s.cpu_nr_periods, s.cpu_nr_throttled, s.cpu_throttled_seconds), (9, 2, .25))
        self.assertEqual((s.memory_limit_bytes, s.memory_current_bytes, s.memory_remaining_bytes, s.memory_cache_bytes), (8192, 3000, 5192, 1000))
        self.assertEqual(s.uptime_seconds, 900.5)
        self.assertIsNone(s.load_average)
        self.assertEqual(s.as_dict()['scope'], 'container')

    def test_visible_ancestor_limits_cpuset_intersection(self):
        files = self.v2()
        files.update({
            '/sys/fs/cgroup/work/cpu.max': '50000 100000',
            '/sys/fs/cgroup/work/memory.max': '4096',
            '/sys/fs/cgroup/cpuset.cpus.effective': '2-6',
        })
        s = self.snapshot(files)
        self.assertEqual(s.cpu_capacity, .5)
        self.assertEqual(s.cpu_count, 3)
        self.assertEqual(s.memory_limit_bytes, 4096)
        self.assertEqual(s.memory_remaining_bytes, 1096)

    def test_unlimited_container_never_leaks_host(self):
        files = self.v2()
        files['/sys/fs/cgroup/work/leaf/cpu.max'] = 'max 100000'
        del files['/sys/fs/cgroup/work/leaf/cpuset.cpus.effective']
        files['/sys/fs/cgroup/work/leaf/memory.max'] = 'max'
        files.update({'/proc/meminfo': 'MemTotal: 999999 kB\nCached: 888 kB', '/proc/stat': 'cpu 1 2 3 4\ncpu0 1 2 3 4', '/proc/loadavg': '9 9 9'})
        s = self.snapshot(files)
        self.assertIsNone(s.cpu_count)
        self.assertIsNone(s.cpu_capacity)
        self.assertIsNone(s.memory_limit_bytes)
        self.assertIsNone(s.memory_remaining_bytes)
        self.assertEqual(s.memory_current_bytes, 3000)
        self.assertEqual(s.memory_cache_bytes, 1000)
        self.assertIsNone(s.load_average)

    def test_v1_separate_mounts_and_units(self):
        files = {'/proc/self/cgroup': '2:cpu,cpuacct:/docker/xyz\n3:memory:/docker/xyz\n4:cpuset:/docker/xyz', '/proc/self/mountinfo': '\n'.join(
            f'{i} 22 0:{i} /docker/xyz /cg/{name} rw - cgroup cgroup rw,{controllers}'
            for i, name, controllers in [(30, 'cpu', 'cpu,cpuacct'), (31, 'memory', 'memory'), (32, 'cpuset', 'cpuset')])}
        files.update({
            '/cg/cpu/cpu.cfs_quota_us': '25000', '/cg/cpu/cpu.cfs_period_us': '100000',
            '/cg/cpu/cpuacct.usage': '4000000000', '/cg/cpu/cpuacct.stat': 'user 300\nsystem 100',
            '/cg/cpu/cpu.stat': 'nr_periods 10\nnr_throttled 3\nthrottled_time 500000000',
            '/cg/cpuset/cpuset.cpus': '1-2,4', '/cg/memory/memory.limit_in_bytes': '10000',
            '/cg/memory/memory.usage_in_bytes': '11000', '/cg/memory/memory.stat': 'cache 10\ntotal_cache 100',
        })
        s = self.snapshot(files)
        self.assertEqual((s.cpu_count, s.cpu_capacity), (3, .25))
        self.assertEqual((s.cpu_user_seconds, s.cpu_system_seconds, s.cpu_total_seconds), (3, 1, 4))
        self.assertEqual(s.cpu_throttled_seconds, .5)
        self.assertEqual(s.memory_remaining_bytes, 0)
        self.assertEqual(s.memory_cache_bytes, 100)
        files['/cg/memory/memory.limit_in_bytes'] = str((1 << 63) - 4096)
        self.assertIsNone(self.snapshot(files).memory_limit_bytes)
        files['/cg/memory/memory.stat'] = 'cache 20'
        self.assertEqual(self.snapshot(files).memory_cache_bytes, 20)

    def test_host_uses_kernel_not_pid1_uptime_and_ignores_service_limits(self):
        files = self.v2()
        files['/proc/self/cgroup'] = '0::/system.slice/example.service'
        files.update({'/proc/stat': 'cpu 100 20 30 400 50 6 4 0 10 2\ncpu0 0\ncpu1 0',
                      '/proc/meminfo': 'MemTotal: 100 kB\nMemAvailable: 30 kB\nMemFree: 10 kB\nCached: 20 kB',
                      '/proc/loadavg': '.1 .2 .3 1/2 3'})
        s = self.snapshot(files, scope='auto')
        self.assertEqual(s.scope, 'host')
        self.assertEqual(s.uptime_seconds, 1000.5)
        self.assertEqual((s.cpu_count, s.cpu_capacity), (2, 2))
        self.assertAlmostEqual(s.cpu_total_seconds, 1.6)
        self.assertAlmostEqual(s.cpu_user_seconds, 1.2)
        self.assertAlmostEqual(s.cpu_system_seconds, .4)
        self.assertEqual((s.memory_limit_bytes, s.memory_current_bytes, s.memory_remaining_bytes, s.memory_cache_bytes), (102400, 71680, 30720, 20480))
        self.assertEqual(s.load_average, (.1, .2, .3))
        files['/proc/meminfo'] = 'MemTotal: 100 kB\nMemFree: 5 kB'
        self.assertEqual(self.snapshot(files, scope='host').memory_remaining_bytes, 5120)

    def test_auto_markers_and_container_paths_but_override_wins(self):
        for marker in ('/.dockerenv', '/run/.containerenv', '/run/systemd/container'):
            files = {marker: '' if marker != '/run/systemd/container' else 'podman'}
            self.assertEqual(self.snapshot(files, scope='auto').scope, 'container')
            self.assertEqual(self.snapshot(files, scope='host').scope, 'host')
        for path in ('/docker/123', '/system.slice/docker-123.scope', '/kubepods/burstable/123', '/lxc/test'):
            self.assertEqual(self.snapshot({'/proc/self/cgroup': '0::' + path}, scope='auto').scope, 'container')

    def test_namespaced_root_mount_and_escaped_mountpoint(self):
        files = {'/proc/self/cgroup': '0::/', '/proc/self/mountinfo': '1 2 0:1 /docker/id /cg\\040space rw - cgroup2 cgroup rw',
                 '/cg space/cpu.max': '10000 100000', '/cg space/memory.max': '0', '/cg space/memory.current': '0'}
        s = self.snapshot(files)
        self.assertEqual(s.cpu_capacity, .1)
        self.assertEqual(s.memory_limit_bytes, 0)
        self.assertEqual(s.memory_remaining_bytes, 0)

    def test_kubernetes_private_cgroup_namespace_uses_environment_hint(self):
        files = {
            '/proc/self/cgroup': '0::/',
            '/proc/1/cgroup': '0::/',
            '/proc/self/mountinfo': '1 2 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw',
            '/sys/fs/cgroup/cpu.max': '200000 100000',
            '/sys/fs/cgroup/cpuset.cpus.effective': '0-7',
            '/sys/fs/cgroup/cpu.stat': 'usage_usec 5000000',
            '/sys/fs/cgroup/memory.max': '536870912',
            '/sys/fs/cgroup/memory.current': '104857600',
            '/sys/fs/cgroup/memory.stat': 'file 1024',
            '/proc/meminfo': 'MemTotal: 14680064 kB\nCached: 99999 kB',
            '/proc/stat': 'cpu 100000 0 0 0\ncpu0 0',
            '/proc/loadavg': '100 100 100',
            '/proc/uptime': '1000 0',
            '/proc/1/stat': pidstat(10000),
        }
        for environment in ({'KUBERNETES_SERVICE_HOST': '10.43.0.1'}, {'container': 'podman'}):
            s = self.snapshot(files, scope='auto', environment=environment)
            self.assertEqual(s.scope, 'container')
            self.assertEqual((s.cpu_count, s.cpu_capacity, s.cpu_total_seconds), (8, 2, 5))
            self.assertEqual(s.memory_limit_bytes, 536870912)
            self.assertEqual(s.memory_cache_bytes, 1024)
            self.assertEqual(s.uptime_seconds, 900)
            self.assertIsNone(s.load_average)
            self.assertEqual(self.snapshot(files, scope='host', environment=environment).scope, 'host')
        # Private cgroup roots alone do not identify containers: native hosts
        # and systemd services can expose the same cgroup layout.
        self.assertEqual(self.snapshot(files, scope='auto', environment={}).scope, 'host')
        self.assertEqual(self.snapshot(files, scope='auto', environment={'KUBERNETES_SERVICE_HOST': '', 'container': ''}).scope, 'host')
        self.assertEqual(self.snapshot(files, scope='container', environment={}).scope, 'container')

    def test_container_uptime_requires_verified_matching_pid1_cgroup(self):
        files = self.v2()
        self.assertEqual(self.snapshot(files).uptime_seconds, 900.5)
        files['/proc/1/cgroup'] = '0::/init.scope'
        s = self.snapshot(files)
        self.assertIsNone(s.uptime_seconds)
        self.assertEqual(s.sources['uptime'], 'unavailable:pid1-cgroup-mismatch')
        self.assertEqual(s.cpu_capacity, 1.5)
        for value in ('', 'invalid', '0::relative'):
            files['/proc/1/cgroup'] = value
            s = self.snapshot(files)
            self.assertIsNone(s.uptime_seconds)
            self.assertEqual(s.sources['uptime'], 'unavailable:pid1-cgroup-unverified')
        del files['/proc/1/cgroup']
        self.assertIsNone(self.snapshot(files).uptime_seconds)
        files['/proc/1/cgroup'] = files.pop('/proc/self/cgroup')
        self.assertIsNone(self.snapshot(files).uptime_seconds)
        # Native host kernel uptime is independent of PID 1 membership.
        self.assertEqual(self.snapshot(files, scope='host').uptime_seconds, 1000.5)

    def test_missing_bad_data_stays_unknown(self):
        for files in ({}, {'/proc/uptime': 'nan', '/proc/1/stat': pidstat(1)}, {'/proc/uptime': '1', '/proc/1/stat': pidstat(1000)}):
            s = self.snapshot(files)
            self.assertIsNone(s.uptime_seconds)
            self.assertIsNone(s.cpu_capacity)
            self.assertIsNone(s.memory_cache_bytes)
        files = self.v2()
        for key in list(files):
            if key.startswith('/sys/'):
                files[key] = 'bad data'
        s = self.snapshot(files)
        self.assertIsNone(s.cpu_total_seconds)
        self.assertIsNone(s.cpu_count)
        self.assertIsNone(s.memory_limit_bytes)
        with self.assertRaises(ValueError):
            self.snapshot({}, scope='invalid')

    def test_delta_separate_from_load_average(self):
        files = self.v2()
        a = self.snapshot(files, now=1)
        files['/sys/fs/cgroup/work/leaf/cpu.stat'] = 'usage_usec 5000000'
        b = self.snapshot(files, now=3)
        self.assertEqual(cpu_usage_delta(a, b), 1)
        self.assertIsNone(cpu_usage_delta(b, a))
        self.assertIsNone(cpu_usage_delta(a, a))
        b.scope = 'host'
        self.assertIsNone(cpu_usage_delta(a, b))


if __name__ == '__main__':
    unittest.main()
