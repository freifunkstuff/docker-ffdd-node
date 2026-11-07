"""Read-only, scope-consistent Linux resource snapshots.

read_resources(scope='auto', root='/', reader=None, clock=time.monotonic,
               clock_ticks=None, environment=None) returns ResourceSnapshot;
as_dict() is JSON ready.
reader receives a rooted pathlib.Path and returns text (missing files may raise
OSError). Explicit host/container overrides are recommended for unusual runtimes
(e.g. unmarked container runtimes). Auto detects runtime markers
and container cgroup names, NOT merely finite systemd service limits.
Environment defaults to os.environ; standard KUBERNETES_SERVICE_HOST and systemd
container hints are recognized. Unmarked private cgroup namespaces (0::/) are
indistinguishable from hosts: applications should pass scope='container' when
they know they run in a container, especially with Kubernetes service links off.
No application-specific environment configuration is read here.

All memory values are bytes, CPU counters seconds, capacity CPU equivalents.
Container unlimited/hidden limits remain None, never host meminfo/cpu/loadavg.
Capacity is the minimum visible quota/cpuset bound; hidden ancestors cannot be
inferred. CPU count is the visible cpuset cardinality (not quota rounded up).
Memory remaining is limit-current, not reclaimable/available memory; ancestor
limits constrain the ceiling but ancestor sibling consumption is not inferred.
Container uptime is PID-namespace PID 1 age; host uptime is kernel uptime.
PID 1 uptime is reported only when /proc/1/cgroup matches /proc/self/cgroup;
missing or different memberships produce None with an explanatory source flag,
not host PID 1 age. This conservative guard can also hide valid uptime when
container init and this process occupy different cgroups. Matching cgroups do
not prove namespace isolation; true creation time is not generally discoverable.
Unavailable/malformed data is None. No container load average exists here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import os
from pathlib import Path, PurePosixPath
import re
import time
from typing import Callable, Mapping


@dataclass
class ResourceSnapshot:
    scope: str
    sampled_at: float
    cpu_count: int | None = None
    cpu_capacity: float | None = None
    cpu_user_seconds: float | None = None
    cpu_system_seconds: float | None = None
    cpu_total_seconds: float | None = None
    cpu_throttled_seconds: float | None = None
    cpu_nr_periods: int | None = None
    cpu_nr_throttled: int | None = None
    memory_limit_bytes: int | None = None
    memory_current_bytes: int | None = None
    memory_remaining_bytes: int | None = None
    memory_cache_bytes: int | None = None
    uptime_seconds: float | None = None
    load_average: tuple[float, float, float] | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def cpu_usage_delta(previous: ResourceSnapshot, current: ResourceSnapshot) -> float | None:
    """Return consumed CPU equivalents (1 = one busy CPU), NOT load average.

    Caller must sample the same workload; resets, scope changes and invalid
    intervals return None. Divide by current.cpu_capacity for capacity fraction.
    """
    elapsed = current.sampled_at - previous.sampled_at
    if previous.scope != current.scope or elapsed <= 0:
        return None
    if previous.cpu_total_seconds is None or current.cpu_total_seconds is None:
        return None
    used = current.cpu_total_seconds - previous.cpu_total_seconds
    return used / elapsed if used >= 0 else None


def _number(text, scale=1):
    try:
        value = float(text) / scale
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def _integer(text):
    try:
        value = int(text)
        return value if value >= 0 else None
    except (ValueError, TypeError):
        return None


def _pairs(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            value = _integer(fields[1])
            if value is not None:
                result[fields[0].rstrip(':')] = value
    return result


def _memberships(text):
    result = {}
    for line in text.splitlines():
        parts = line.split(':', 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[2].startswith('/'):
            return {}
        for controller in parts[1].split(','):
            result[controller] = parts[2]
    return result


def _cpuset(text):
    cpus = set()
    try:
        for item in text.strip().split(','):
            ends = [int(v) for v in item.split('-')]
            if len(ends) > 2 or min(ends) < 0 or ends[-1] < ends[0] or ends[-1] > 1048576:
                return None
            cpus.update(range(ends[0], ends[-1] + 1))
        return cpus or None
    except ValueError:
        return None


def read_resources(*, scope='auto', root='/', reader: Callable | None = None,
                   clock: Callable = time.monotonic, clock_ticks=None,
                   environment: Mapping[str, str] | None = None) -> ResourceSnapshot:
    """Collect one snapshot without subprocesses, writes or host fallback in containers."""
    if scope not in ('auto', 'host', 'container'):
        raise ValueError('scope must be auto, host or container')
    root = Path(root)
    def read(path):
        try:
            value = (reader or (lambda p: p.read_text()))(root / path.lstrip('/'))
            return value if isinstance(value, str) else ''
        except (OSError, UnicodeError, KeyError):
            return ''
    cgroups = read('/proc/self/cgroup')
    mounts = read('/proc/self/mountinfo')
    if scope == 'auto':
        env = os.environ if environment is None else environment
        markers = bool(env.get('KUBERNETES_SERVICE_HOST', '').strip()
                       or env.get('container', '').strip()
                       or read('/run/systemd/container').strip())
        # Empty marker files count as existing, including injected readers.
        for marker in ('/.dockerenv', '/run/.containerenv'):
            try:
                (reader or (lambda p: p.read_text()))(root / marker.lstrip('/'))
                markers = True
            except (OSError, UnicodeError, KeyError):
                pass
        scope = 'container' if markers or re.search(r'(?:^|[/:-])(docker|kubepods|libpod|lxc)(?:[/:-]|[.]|$)', cgroups) else 'host'
    s = ResourceSnapshot(scope=scope, sampled_at=clock())
    hz = clock_ticks if clock_ticks is not None else os.sysconf('SC_CLK_TCK')
    uptime = _number((read('/proc/uptime').split() or [''])[0])
    if scope == 'host':
        s.uptime_seconds = uptime
        s.sources.update(cpu='proc/stat', memory='proc/meminfo', uptime='proc/uptime')
        stat = read('/proc/stat')
        s.cpu_count = sum(bool(re.match(r'^cpu\d+\s', line)) for line in stat.splitlines()) or (os.cpu_count() if root == Path('/') and reader is None else None)
        s.cpu_capacity = float(s.cpu_count) if s.cpu_count else None
        for line in stat.splitlines():
            if line.startswith('cpu '):
                vals = [_number(v, hz) for v in line.split()[1:]]
                if len(vals) >= 4 and all(v is not None for v in vals):
                    s.cpu_user_seconds = sum(vals[:2])
                    s.cpu_system_seconds = vals[2] + sum(vals[5:7])
                    # Guest times are already included in user/nice; omit idle/iowait.
                    s.cpu_total_seconds = sum(vals[:8]) - vals[3] - (vals[4] if len(vals) > 4 else 0)
        mem = _pairs(read('/proc/meminfo'))
        s.memory_limit_bytes = mem.get('MemTotal', 0) * 1024 or None
        available = mem.get('MemAvailable', mem.get('MemFree'))
        s.memory_remaining_bytes = available * 1024 if available is not None else None
        if s.memory_limit_bytes is not None and s.memory_remaining_bytes is not None:
            s.memory_current_bytes = max(0, s.memory_limit_bytes - s.memory_remaining_bytes)
        s.memory_cache_bytes = mem['Cached'] * 1024 if 'Cached' in mem else None
        try:
            loads = tuple(float(v) for v in read('/proc/loadavg').split()[:3])
            if len(loads) == 3 and all(math.isfinite(v) and v >= 0 for v in loads):
                s.load_average = loads
        except ValueError:
            pass
        return s

    memberships = _memberships(cgroups)
    pid1_memberships = _memberships(read('/proc/1/cgroup'))
    if not memberships or not pid1_memberships:
        s.sources['uptime'] = 'unavailable:pid1-cgroup-unverified'
    elif memberships != pid1_memberships:
        s.sources['uptime'] = 'unavailable:pid1-cgroup-mismatch'
    else:
        s.sources['uptime'] = 'unavailable:pid1-stat-or-uptime'
        pidstat = read('/proc/1/stat')
        if ')' in pidstat:
            fields = pidstat[pidstat.rfind(')') + 1:].split()
            start = _number(fields[19], hz) if len(fields) > 19 else None
            if uptime is not None and start is not None and start <= uptime:
                s.uptime_seconds = uptime - start
                s.sources['uptime'] = 'proc/uptime-proc/1/stat'

    locations = {}
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    for line in mounts.splitlines():
        left, sep, right = line.partition(' - ')
        a, b = left.split(), right.split()
        if not sep or len(a) < 6 or len(b) < 3 or b[0] not in ('cgroup', 'cgroup2'):
            continue
        mountroot, mount = map(unescape, (a[3], a[4]))
        controllers = [''] if b[0] == 'cgroup2' else b[2].split(',')
        for controller in controllers:
            member = memberships.get(controller)
            if member is None:
                continue
            if member == '/':
                relative = ''  # cgroup namespace root
            elif mountroot == '/':
                relative = member.lstrip('/')
            elif member == mountroot or member.startswith(mountroot.rstrip('/') + '/'):
                relative = member[len(mountroot):].lstrip('/')
            else:
                continue
            if '..' in PurePosixPath(relative).parts:
                continue
            locations[controller] = (str(PurePosixPath(mount) / relative), mount, b[0] == 'cgroup2')
    def chain(controller):
        location = locations.get(controller)
        if not location:
            return []
        leaf, mount, _ = location
        paths = []
        p = PurePosixPath(leaf)
        while True:
            paths.append(str(p))
            if str(p) == mount or p == p.parent:
                break
            p = p.parent
        return paths
    v2 = '' in locations
    cpu_paths = chain('' if v2 else 'cpu')
    mem_paths = chain('' if v2 else 'memory')
    set_paths = chain('' if v2 else 'cpuset')
    capacities, sets = [], []
    for p in set_paths:
        cpus = _cpuset(read(p + ('/cpuset.cpus.effective' if v2 else '/cpuset.effective_cpus'))) or _cpuset(read(p + '/cpuset.cpus'))
        if cpus:
            sets.append(cpus)
    if sets:
        s.cpu_count = len(set.intersection(*sets))
        capacities.append(float(s.cpu_count))
    for p in cpu_paths:
        if v2:
            parts = read(p + '/cpu.max').split()
            quota = _number(parts[0]) if len(parts) == 2 else None
            period = _number(parts[1]) if len(parts) == 2 else None
        else:
            quota = _number(read(p + '/cpu.cfs_quota_us').strip())
            period = _number(read(p + '/cpu.cfs_period_us').strip())
        if quota is not None and period and quota > 0:
            capacities.append(quota / period)
    s.cpu_capacity = min(capacities) if capacities else None
    if cpu_paths:
        p = cpu_paths[0]
        stats = _pairs(read(p + '/cpu.stat'))
        scale = 1000000 if v2 else 1000000000
        s.cpu_throttled_seconds = stats.get('throttled_usec' if v2 else 'throttled_time')
        if s.cpu_throttled_seconds is not None:
            s.cpu_throttled_seconds /= scale
        s.cpu_nr_periods = stats.get('nr_periods')
        s.cpu_nr_throttled = stats.get('nr_throttled')
        if v2:
            for name, key in [('total', 'usage_usec'), ('user', 'user_usec'), ('system', 'system_usec')]:
                value = stats.get(key)
                setattr(s, 'cpu_' + name + '_seconds', value / 1000000 if value is not None else None)
        s.sources['cpu'] = ('cgroup2:' if v2 else 'cgroup1:') + p
    if not v2 and chain('cpuacct'):
        p = chain('cpuacct')[0]
        s.cpu_total_seconds = _number(read(p + '/cpuacct.usage').strip(), 1000000000)
        stats = _pairs(read(p + '/cpuacct.stat'))
        s.cpu_user_seconds = stats['user'] / hz if 'user' in stats else None
        s.cpu_system_seconds = stats['system'] / hz if 'system' in stats else None
        s.sources['cpu_usage'] = 'cgroup1:' + p
    limits = []
    for p in mem_paths:
        value = _integer(read(p + ('/memory.max' if v2 else '/memory.limit_in_bytes')).strip())
        if value is not None and (v2 or value < (1 << 60)):
            limits.append(int(value))
    s.memory_limit_bytes = min(limits) if limits else None
    if mem_paths:
        p = mem_paths[0]
        value = _integer(read(p + ('/memory.current' if v2 else '/memory.usage_in_bytes')).strip())
        s.memory_current_bytes = int(value) if value is not None else None
        stats = _pairs(read(p + '/memory.stat'))
        s.memory_cache_bytes = stats.get('file') if v2 else stats.get('total_cache', stats.get('cache'))
        s.sources['memory'] = ('cgroup2:' if v2 else 'cgroup1:') + p
    if s.memory_limit_bytes is not None and s.memory_current_bytes is not None:
        s.memory_remaining_bytes = max(0, s.memory_limit_bytes - s.memory_current_bytes)
    return s
