"""Bounded, process-local sysinfo history; no rates, identity data or persistence."""
from collections import deque
from copy import deepcopy
import math
import re


HISTORY_SECONDS = 3600
MAX_SAMPLES = 315
DETAIL_SECONDS = 300
RESOLUTION_SECONDS = 15
_MAX_NUMBER = 1.7976931348623157e308
# The frontend uses JavaScript Number; timestamps beyond its exact integer range
# are not meaningful Unix times and can poison ordering indefinitely.
_MAX_TIMESTAMP = 9007199254740991
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:kB|KiB)?$", re.ASCII)
_NUMBER = re.compile(r"^[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?$", re.ASCII)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str):
        if len(value) > 64 or not _NUMBER.fullmatch(value.strip()):
            return None
        value = value.strip()
    try:
        numeric = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0 or numeric > _MAX_NUMBER:
        return None
    # Normalize strings to Number-compatible numeric values; retain raw counters,
    # not derived rates. Huge Python ints must not escape the bounded wire schema.
    return numeric


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _copy_numbers(source, target, keys):
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            target[key] = value


def _memory(value):
    if isinstance(value, str):
        if len(value) > 64:
            return None
        value = value.strip()
        match = _MEMORY.fullmatch(value)
        if not match or _number(match[1]) is None:
            return None
        return value
    return _number(value)


def _compact(payload):
    if not isinstance(payload, dict):
        return None
    timestamp = _number(payload.get('timestamp'))
    if timestamp is None or not 0 < timestamp <= _MAX_TIMESTAMP:
        return None
    source = _mapping(payload.get('data'))
    data = {}
    # Even malformed/empty resource frames must retain presence, otherwise the
    # frontend might silently substitute legacy host uptime or memory.
    if 'resources' in source:
        resources = _mapping(source['resources'])
        compact = {}
        scope = resources.get('scope')
        if scope in ('host', 'container'):
            compact['scope'] = scope
            _copy_numbers(resources, compact, ('cpu_capacity', 'cpu_total_seconds'))
        _copy_numbers(resources, compact, ('memory_current_bytes', 'memory_limit_bytes',
                                          'memory_remaining_bytes', 'uptime_seconds'))
        data['resources'] = compact
    else:
        uptime = _mapping(source.get('system')).get('uptime')
        if isinstance(uptime, str):
            uptime = uptime.strip().split()[0] if len(uptime) <= 64 and uptime.strip() else None
        uptime = _number(uptime)
        if uptime is not None:
            data['system'] = {'uptime': uptime}
    statistic = _mapping(source.get('statistic'))
    compact = {}
    interfaces = {}
    _copy_numbers(_mapping(statistic.get('interfaces')), interfaces,
                  ('tbb_wg_rx', 'tbb_wg_tx', 'tbb_fastd_rx', 'tbb_fastd_tx'))
    if interfaces:
        compact['interfaces'] = interfaces
    for key in ('meminfo_MemTotal', 'meminfo_MemFree'):
        value = _memory(statistic.get(key))
        if value is not None:
            compact[key] = value
    if compact:
        data['statistic'] = compact
    return {'timestamp': timestamp, 'data': data}


def _discontinuous(previous, current):
    flags = dict.fromkeys(('discontinuous', 'cpu_discontinuous',
                           'rx_discontinuous', 'tx_discontinuous'), False)
    b = current['data'].get('resources', {})
    a = previous['data'].get('resources', {}) if previous else b
    if previous:
        flags['discontinuous'] = (current['timestamp'] - previous['timestamp'] > 2
                                  or a.get('scope') != b.get('scope'))
        old_uptime = a.get('uptime_seconds', previous['data'].get('system', {}).get('uptime'))
        new_uptime = b.get('uptime_seconds', current['data'].get('system', {}).get('uptime'))
        if ((old_uptime is None) != (new_uptime is None)
                or (old_uptime is not None and new_uptime is not None and new_uptime < old_uptime)):
            flags['discontinuous'] = True
    flags['cpu_discontinuous'] = (
        b.get('scope') not in ('host', 'container')
        or 'cpu_total_seconds' not in a or 'cpu_total_seconds' not in b
        or not a.get('cpu_capacity') or not b.get('cpu_capacity')
        or a.get('cpu_capacity') != b.get('cpu_capacity')
        or b.get('cpu_total_seconds', 0) < a.get('cpu_total_seconds', 0))
    after = current['data'].get('statistic', {}).get('interfaces', {})
    before = previous['data'].get('statistic', {}).get('interfaces', {}) if previous else after
    for direction in ('rx', 'tx'):
        keys = {key for key in before.keys() | after.keys() if key.endswith('_' + direction)}
        flags[direction + '_discontinuous'] = not keys or any(
            key not in before or key not in after or after[key] < before[key] for key in keys)
    return flags


class StatsHistory:
    """Process-local 1s detail and incremental 15s aggregates, without raw archive.

    Backward time is rejected until the source clock catches up. Coarse CPU and
    network counters are the last actual raw values, never averages or rates.
    RAM uses a running mean, extrema and observation count. Global discontinuity
    covers source gaps, scope changes and uptime resets; metric-specific sticky
    flags isolate CPU, RX and TX resets or unavailable counters.
    """

    def __init__(self, max_samples=MAX_SAMPLES):
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 1 <= max_samples <= MAX_SAMPLES:
            max_samples = MAX_SAMPLES
        self._samples = deque(maxlen=max_samples)
        self._coarse = deque(maxlen=241)
        self._memory_count = 0
        self._memory_mean = 0
        self._memory_min = 0
        self._memory_max = 0

    def append(self, payload: dict):
        sample = _compact(payload)
        previous = self._samples[-1] if self._samples else None
        if sample is None or (previous and sample['timestamp'] <= previous['timestamp']):
            return False
        timestamp = sample['timestamp']
        bucket = int(timestamp // RESOLUTION_SECONDS)
        discontinuous = _discontinuous(previous, sample)
        if self._coarse and int(self._coarse[-1]['timestamp'] // RESOLUTION_SECONDS) == bucket:
            aggregation = dict(self._coarse.pop()['aggregation'])
            aggregation['count'] += 1
            aggregation['end'] = timestamp
            for key, value in discontinuous.items():
                aggregation[key] |= value
        else:
            aggregation = {'count': 1, 'start': timestamp, 'end': timestamp,
                           **discontinuous}
            self._memory_count = 0
        coarse = deepcopy(sample)
        memory = sample['data'].get('resources', {}).get('memory_current_bytes')
        if memory is not None:
            self._memory_count += 1
            if self._memory_count == 1:
                self._memory_mean = self._memory_min = self._memory_max = memory
            else:
                self._memory_mean += (memory - self._memory_mean) / self._memory_count
                self._memory_min = min(self._memory_min, memory)
                self._memory_max = max(self._memory_max, memory)
        if self._memory_count:
            resources = coarse['data'].setdefault('resources', {})
            resources.update(memory_current_bytes=self._memory_mean,
                             memory_current_min_bytes=self._memory_min,
                             memory_current_max_bytes=self._memory_max)
        aggregation['memory_count'] = self._memory_count
        coarse['aggregation'] = aggregation
        self._coarse.append(coarse)
        # Fine detail includes exactly the trailing five minutes. Keeping the
        # aggregates from the start avoids any overlap/weighted-boundary merge.
        while self._samples and self._samples[0]['timestamp'] < timestamp - DETAIL_SECONDS:
            self._samples.popleft()
        self._samples.append(sample)
        while self._coarse and self._coarse[0]['timestamp'] < timestamp - HISTORY_SECONDS:
            self._coarse.popleft()
        return True

    def tail(self, seconds=16):
        """Detached recent raw frames, without constructing the coarse export.

        Duration is clamped to the retained five minutes; invalid values use
        the default. The interval includes both endpoints, relative to source
        time. This method does not read a wall clock or touch persistent state.
        """
        duration = _number(seconds)
        if duration is None:
            duration = 16
        duration = min(duration, DETAIL_SECONDS)
        if not self._samples:
            return []
        cutoff = self._samples[-1]['timestamp'] - duration
        recent = []
        for sample in reversed(self._samples):
            if sample['timestamp'] < cutoff:
                break
            recent.append(sample)
        return deepcopy(list(reversed(recent)))

    def snapshot(self):
        """Return detached, JSON-ready resolution arrays (no root samples)."""
        return {'version': 1, 'history_seconds': HISTORY_SECONDS,
                'detail_seconds': DETAIL_SECONDS,
                'updated_at': self._samples[-1]['timestamp'] if self._samples else None,
                'resolutions': {'1': deepcopy(list(self._samples)),
                                '15': deepcopy(list(self._coarse))}}

    def export(self):
        """Alias for snapshot, suitable for the sysinfo history HTTP response."""
        return self.snapshot()
