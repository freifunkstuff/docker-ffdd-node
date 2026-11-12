"""Small persistent aggregate exit-traffic ledger (no per-client information).

Counters are sampled, not packet timestamps. Intervals crossing midnight in at
most one second are proportionally apportioned using elapsed UTC time; longer
cross-day intervals are credited to the endpoint day and both endpoint days are
marked partial. This deliberately does not claim exact per-day attribution.
"""
import copy
from datetime import datetime, timedelta, timezone as dt_timezone
import json
from fractions import Fraction
import math
import os
from pathlib import Path
import re
import tempfile
import time
from zoneinfo import ZoneInfo

_MAX_FILE = 256 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_DIRECTIONS = ("rx_bytes", "tx_bytes")


def _counter(value):
    return type(value) is int and 0 <= value <= (1 << 64) - 1


def _number(value):
    return type(value) in (int, float) and 0 <= value <= 253402214400 and math.isfinite(value)


def _name(value):
    return isinstance(value, str) and bool(_NAME.fullmatch(value)) and value not in ("__proto__", "prototype", "constructor")


class ExitTrafficLedger:
    def __init__(self, path, timezone="Europe/Berlin", retention_days=30, checkpoint_seconds=30):
        if retention_days != 30 or isinstance(retention_days, bool):
            raise ValueError("invalid traffic retention")
        if not _number(checkpoint_seconds):
            raise ValueError("invalid traffic checkpoint")
        try:
            self._zone = ZoneInfo(timezone)
        except (ValueError, KeyError, TypeError):
            raise ValueError("invalid traffic timezone") from None
        self.path = Path(path)
        self.timezone = timezone
        self.retention_days = 30
        self.checkpoint_seconds = checkpoint_seconds
        self.error = None
        self._dirty = False
        self._last_attempt = None
        self._state = dict(version=1, timezone=timezone, retention_days=30,
                           started_at=None, updated_at=None,
                           totals=dict(rx_bytes=0, tx_bytes=0), days=[], baselines={})
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(_MAX_FILE + 1)
            if len(raw) > _MAX_FILE:
                raise ValueError()
            state = json.loads(raw)
            self._validate(state)
            self._state = state
            self._prune(time.time())
        except FileNotFoundError:
            pass
        except Exception:
            self.error = "invalid traffic state"
            raise ValueError(self.error) from None

    def _validate(self, state):
        if not isinstance(state, dict) or set(state) != set(self._state):
            raise ValueError()
        if type(state['version']) is not int or state['version'] != 1 or state['timezone'] != self.timezone or type(state['retention_days']) is not int or state['retention_days'] != 30:
            raise ValueError()
        for key in ('started_at', 'updated_at'):
            if state[key] is not None and not _number(state[key]):
                raise ValueError()
        if (state['started_at'] is None) != (state['updated_at'] is None):
            raise ValueError()
        if state['started_at'] is not None and state['started_at'] > state['updated_at']:
            raise ValueError()
        totals = state['totals']
        if not isinstance(totals, dict) or set(totals) != set(_DIRECTIONS) or any(type(v) is not int or v < 0 for v in totals.values()):
            raise ValueError()
        days = state['days']
        if not isinstance(days, list) or len(days) > 30:
            raise ValueError()
        dates = []
        for day in days:
            if not isinstance(day, dict) or set(day) != {'date', 'rx_bytes', 'tx_bytes', 'partial'}:
                raise ValueError()
            date = datetime.strptime(day['date'], '%Y-%m-%d').date()
            if date.isoformat() != day['date'] or type(day['partial']) is not bool:
                raise ValueError()
            if any(type(day[k]) is not int or day[k] < 0 or day[k] > totals[k] for k in _DIRECTIONS):
                raise ValueError()
            dates.append(day['date'])
        if dates != sorted(set(dates)):
            raise ValueError()
        baselines = state['baselines']
        if not isinstance(baselines, dict) or len(baselines) > 8:
            raise ValueError()
        for name, base in baselines.items():
            if not _name(name) or not isinstance(base, dict) or set(base) != {'generation', 'rx_bytes', 'tx_bytes'}:
                raise ValueError()
            if not isinstance(base['generation'], str) or not 1 <= len(base['generation']) <= 256:
                raise ValueError()
            for key in _DIRECTIONS:
                item = base[key]
                if item is not None and (not isinstance(item, dict) or set(item) != {'value', 'time'} or not _counter(item['value']) or not _number(item['time']) or state['updated_at'] is None or item['time'] > state['updated_at']):
                    raise ValueError()

    def _date(self, timestamp):
        return datetime.fromtimestamp(timestamp, self._zone).date()

    def _prune(self, timestamp):
        today = self._date(timestamp)
        earliest = (today - timedelta(days=29)).isoformat()
        latest = today.isoformat()
        before = self._state['days']
        self._state['days'] = [d for d in before if earliest <= d['date'] <= latest]
        if before != self._state['days']:
            self._dirty = True

    def _day(self, date, partial=False):
        key = date.isoformat()
        for day in self._state['days']:
            if day['date'] == key:
                day['partial'] |= partial
                return day
        day = dict(date=key, rx_bytes=0, tx_bytes=0, partial=bool(partial))
        self._state['days'].append(day)
        self._state['days'].sort(key=lambda d: d['date'])
        return day

    def _delta(self, start, end, key, delta):
        first, last = self._date(start), self._date(end)
        self._state['totals'][key] += delta
        if first == last:
            self._day(last)[key] += delta
        elif end - start <= 1:
            midnight = datetime.combine(last, datetime.min.time(), self._zone).timestamp()
            earlier = int(delta * (Fraction(midnight) - Fraction(start)) / (Fraction(end) - Fraction(start)))
            self._day(first)[key] += earlier
            self._day(last)[key] += delta - earlier
        else:
            self._day(first, True)
            self._day(last, True)[key] += delta

    def _gap(self, base):
        if base:
            for key in _DIRECTIONS:
                if base[key] is not None:
                    self._day(self._date(base[key]['time']), True)

    def sample(self, timestamp, counters, generations):
        if not _number(timestamp) or not isinstance(counters, dict) or not isinstance(generations, dict):
            raise ValueError("invalid traffic sample")
        state = self._state
        if state['updated_at'] is not None and timestamp < state['updated_at']:
            raise ValueError("out of order traffic sample")
        fresh = state['started_at'] is None
        if fresh:
            state['started_at'] = timestamp
        state['updated_at'] = timestamp
        today = self._date(timestamp)
        self._day(today, fresh)
        names = sorted(n for n in counters if _name(n))[:8]
        # Missing sources lose their baseline: disappearance is an observation gap.
        for name in list(state['baselines']):
            if name not in names:
                self._gap(state['baselines'][name])
                del state['baselines'][name]
                self._day(today, True)
        for name in names:
            generation = generations.get(name)
            values = counters[name]
            if not isinstance(generation, str) or not 1 <= len(generation) <= 256 or not isinstance(values, dict):
                self._gap(state['baselines'].pop(name, None))
                self._day(today, True)
                continue
            base = state['baselines'].get(name)
            if base is None or base['generation'] != generation:
                self._gap(base)
                base = dict(generation=generation, rx_bytes=None, tx_bytes=None)
                state['baselines'][name] = base
                self._day(today, True)
            for key in _DIRECTIONS:
                value = values.get(key)
                previous = base[key]
                if not _counter(value):
                    if previous is not None:
                        self._day(self._date(previous['time']), True)
                    base[key] = None
                    self._day(today, True)
                    continue
                if previous is not None:
                    if value >= previous['value']:
                        self._delta(previous['time'], timestamp, key, value - previous['value'])
                    else:
                        self._day(self._date(previous['time']), True)
                        self._day(today, True)
                base[key] = dict(value=value, time=timestamp)
        self._prune(timestamp)
        self._dirty = True
        self.flush()

    def export(self):
        return copy.deepcopy({k: v for k, v in self._state.items() if k != 'baselines'})

    def flush(self, force=False):
        now = time.monotonic()
        if not self._dirty or (not force and self._last_attempt is not None and now - self._last_attempt < self.checkpoint_seconds):
            return
        self._last_attempt = now
        temporary = None
        try:
            payload = json.dumps(self._state, separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(payload) > _MAX_FILE:
                raise ValueError()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.exit-traffic-', dir=self.path.parent)
            with os.fdopen(fd, 'wb') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self._dirty = False
            self.error = None
        except Exception:
            self.error = 'traffic checkpoint failed'
            raise OSError(self.error) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
