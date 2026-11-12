#!/usr/bin/env python3
"""Lightweight one-second counters; history publication lives on runtime tmpfs.

No bmxd/wg subprocesses, registration, key or identity changes. Optional daily
byte aggregates checkpoint separately on the data volume. The 30-second sysinfo producer stays independent. Full history publishes every15s;
a short live tail avoids transferring the full ring on every UI update.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import uuid
import signal

from statistics_history import StatsHistory
from system_resources import read_resources
from sysinfo import _read_interface_stats
from exit_observation import read_exit_counters, read_exit_generations
from exit_traffic import ExitTrafficLedger

HISTORY_DIRECTORY = Path('/dev/shm/ffdd-node-statistics')
WEBROOT = Path(os.environ.get('SYSINFO_WEBROOT', '/run/freifunk/www'))


def collect_snapshot() -> dict:
    snapshot = read_resources(scope=os.environ.get('SYSINFO_RESOURCE_SCOPE', 'container'))
    return {'timestamp': time.time(), 'data': {
        'resources': snapshot.as_dict(),
        'statistic': {'interfaces': _read_interface_stats()},
        'exit_interfaces': read_exit_counters(),
    }}


def write_public_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o755)
    content = json.dumps(payload, separators=(',', ':'), allow_nan=False) + '\n'
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.statistics-', delete=False) as f:
        f.write(content)
        temporary = Path(f.name)
    assert temporary.resolve().parent == path.parent.resolve()
    os.chmod(temporary, 0o644)
    # Configured target directory plus a fixed public filename; never /data.
    assert path.parent.resolve() == HISTORY_DIRECTORY.resolve()
    os.replace(temporary, path)


def publish_link(name: str, target: Path) -> None:
    assert name in ('statistics.json', 'statistics-live.json', 'exit-traffic.json')
    WEBROOT.mkdir(parents=True, exist_ok=True)
    path = WEBROOT / name
    if path.is_symlink() and path.resolve() == target.resolve():
        return
    if path.exists() or path.is_symlink():
        raise RuntimeError(f'Refusing to replace unrelated public endpoint {name}')
    path.symlink_to(target)


def main() -> None:
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    traffic = None
    if os.environ.get('EXIT_CONFIG_FILE', '').strip():
        try:
            traffic = ExitTrafficLedger(Path(os.environ.get('EXIT_TRAFFIC_FILE', '/data/statistic/exit-traffic.json')), timezone=os.environ.get('TZ', 'Europe/Berlin'))
        except (OSError, ValueError):
            print('statistics: traffic accounting state unavailable; existing file not replaced', flush=True)
            unavailable = HISTORY_DIRECTORY / 'exit-traffic.json'
            write_public_json(unavailable, {'version': 1, 'status': 'unavailable'})
            publish_link('exit-traffic.json', unavailable)
    history = StatsHistory()
    epoch = uuid.uuid4().hex
    full_path = HISTORY_DIRECTORY / 'statistics.json'
    live_path = HISTORY_DIRECTORY / 'statistics-live.json'
    last_full = float('-inf')
    deadline = time.monotonic()
    print('statistics: one-second sampling; detail5min, aggregation15s, history1h', flush=True)
    while running:
        try:
            frame = collect_snapshot()
            if traffic is not None:
                try:
                    traffic.sample(frame['timestamp'], frame['data']['exit_interfaces'], read_exit_generations())
                except (OSError, ValueError):
                    print('statistics: traffic accounting/checkpoint failed; history collection continues', flush=True)
            if history.append(frame):
                now = time.monotonic()
                if now - last_full >= 15:
                    full = history.export()
                    full['epoch'] = epoch
                    write_public_json(full_path, full)
                    publish_link('statistics.json', full_path)
                    if traffic is not None:
                        volume_path = HISTORY_DIRECTORY / 'exit-traffic.json'
                        write_public_json(volume_path, traffic.export())
                        publish_link('exit-traffic.json', volume_path)
                    last_full = now
                # Kept small; sampling remains in the in-memory ring.
                recent = history.tail(16)
                latest = float(frame['timestamp'])
                tail = {'version': 1, 'epoch': epoch, 'updated_at': latest,
                        'samples': [p for p in recent if float(p['timestamp']) >= latest - 16]}
                write_public_json(live_path, tail)
                publish_link('statistics-live.json', live_path)
        except Exception as exc:
            # Do not include source payloads, paths or credentials in diagnostics.
            print(f'statistics: collection failed ({type(exc).__name__})', flush=True)
        deadline += 1
        delay = deadline - time.monotonic()
        if delay <= 0:
            deadline = time.monotonic()
        else:
            time.sleep(delay)
    if traffic is not None:
        try:
            traffic.flush(force=True)
        except OSError:
            print('statistics: final traffic checkpoint failed', flush=True)


if __name__ == '__main__':
    main()
