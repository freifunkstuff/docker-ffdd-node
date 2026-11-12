#!/usr/bin/env python3
"""Gateway readiness/announcement controller, disabled unless explicitly requested."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import time
import urllib.request
from gateway_runtime import atomic_json
from gateway_health import MAX_STATUS_AGE, runtime_ready, valid_timestamp

PUBLIC = Path('/run/freifunk/www/gateway-control.json')


def enabled(value):
    return str(value).lower() in ('1', 'true', 'yes')


def withdraw_gateway(force=False):
    if os.environ.get('NODE_ROLE', 'node') != 'gateway':
        return True
    if not force and not enabled(os.environ.get('GATEWAY_ANNOUNCE', 'false')):
        return True
    try:
        result = subprocess.run(['bmxd', '-c', '-r', os.environ.get('BMXD_ROUTING_CLASS', '3')], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            atomic_json(Path('/run/freifunk/gateway/withdrawn.json'), {'timestamp': time.time()})
            return True
        return False
    except (OSError, subprocess.TimeoutExpired):
        return False


def dns_probe():
    # Unique public lookup prevents a cached positive record hiding upstream loss.
    name = f'g{int(time.time()) // 5}.example.com'
    payload = struct.pack('!HHHHHH', 0xF7DD, 0x0100, 1, 0, 0, 0)
    payload += b''.join(bytes([len(part)]) + part.encode('ascii') for part in name.split('.')) + b'\0' + struct.pack('!HH', 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.5)
            sock.sendto(payload, ('127.0.0.1', 53))
            data, _ = sock.recvfrom(4096)
        identifier, flags = struct.unpack('!HH', data[:4])
        return identifier == 0xF7DD and bool(flags & 0x8000) and flags & 15 in (0, 3)
    except (OSError, ValueError, struct.error):
        return False


def fresh_json(path, maximum=65536):
    path = Path(path)
    if path.stat().st_size > maximum:
        raise ValueError('invalid health record')
    data = json.loads(path.read_text())
    stamp = data.get('timestamp') if isinstance(data, dict) else None
    now = time.time()
    if not valid_timestamp(stamp, now) or now - stamp > MAX_STATUS_AGE:
        raise ValueError('stale health record')
    return data


class AnnouncementController:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run
        self.advertised = False
        self.initialized = False

    def apply(self, healthy, requested):
        desired = bool(healthy and requested)
        if self.initialized and desired == self.advertised:
            return True
        args = ['bmxd', '-c']
        if desired:
            bandwidth = os.environ.get('GATEWAY_BANDWIDTH', '8mbit/8mbit')
            if not re.fullmatch(r'[1-9][0-9]*(?:kbit|mbit|gbit)/[1-9][0-9]*(?:kbit|mbit|gbit)', bandwidth, re.I):
                raise ValueError('invalid gateway bandwidth')
            community = '1' if enabled(os.environ.get('GATEWAY_COMMUNITY_SERVER', 'false')) else '0'
            args += ['--community_gateway', community, '-g', bandwidth]
        else:
            args += ['-r', os.environ.get('BMXD_ROUTING_CLASS', '3')]
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=2)
            if result.returncode:
                self.initialized = False
                return False
        except (OSError, subprocess.TimeoutExpired):
            self.initialized = False
            return False
        self.advertised = desired
        self.initialized = True
        return True


def exit_readiness():
    try:
        status = fresh_json(os.environ.get('EXIT_STATUS_FILE', '/run/freifunk/www/exits.json'), 32768)
        active = next((item for item in status.get('exits', []) if item.get('active')), {})
        withdrawn_at = 0
        try:
            withdrawn_at = json.loads(Path('/run/freifunk/gateway/withdrawn.json').read_text()).get('timestamp', 0)
        except FileNotFoundError:
            pass
        return (active.get('state') == 'ready' and active.get('reachable') is True
                and valid_timestamp(withdrawn_at) and status['timestamp'] > withdrawn_at)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def runtime_readiness():
    try:
        return runtime_ready(fresh_json(os.environ.get('GATEWAY_RUNTIME_STATUS_FILE', '/run/freifunk/gateway/runtime.json'), 1024 * 1024))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def readiness():
    exit_ready = exit_readiness()
    peers_ready = runtime_readiness()
    api_ready = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:9081/wg.cgi', timeout=1) as response:
            data = json.load(response)
        api_ready = data.get('status') in ('NotRestricted', 'Restricted') and bool(data.get('server'))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    dns_ready = dns_probe() if exit_ready else False
    return {'exit': exit_ready, 'peers': peers_ready, 'admission': api_ready, 'dns': dns_ready}


def recheck_before_announce(checks):
    # Blocking probes must not extend either the runtime lease or exit snapshot.
    checks['exit'] = exit_readiness()
    checks['peers'] = runtime_readiness()
    return all(checks.values())


def daemon_generation():
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            if (path / 'comm').read_text().strip() == 'bmxd' and b'-c' not in (path / 'cmdline').read_bytes().split(b'\0'):
                return (path.name, (path / 'stat').read_text().split()[21])
        except (OSError, IndexError):
            pass
    return None


def peer_summary():
    wg = {'peers': 0, 'inbound': 0, 'active': 0, 'restricted': enabled(os.environ.get('GATEWAY_WG_RESTRICTED', 'false')), 'retentionDays': int(os.environ.get('GATEWAY_RETENTION_DAYS', '30')), 'expiryCandidates': 0}
    fastd = {'accepted': None, 'connected': None, 'restricted': enabled(os.environ.get('GATEWAY_FASTD_RESTRICT', 'false'))}
    try:
        state = fresh_json('/run/freifunk/gateway/runtime.json', 1024 * 1024)
        if not runtime_ready(state):
            raise ValueError('runtime unavailable')
        if state.get('state') == 'repairing':
            # Public metrics are display-only; never use these for admission.
            summary = fresh_json('/run/freifunk/www/gateway.json')
            for target, source in (('peers', 'peers'), ('inbound', 'inbound'), ('active', 'incoming_connected')):
                count = summary.get(source)
                if type(count) is not int or count < 0:
                    raise ValueError('invalid public count')
                wg[target] = count
        else:
            peers = state.get('peers', {})
            wg['peers'] = len(peers)
            wg['inbound'] = sum(bool(peer.get('inbound')) for peer in peers.values())
            wg['active'] = sum(bool(peer.get('inbound')) and peer.get('state') == 'connected' for peer in peers.values())
            summary = json.loads(Path('/run/freifunk/www/gateway.json').read_text())
        wg['expiryCandidates'] = summary.get('expiryCandidates', 0)
    except (OSError, ValueError, AttributeError, TypeError):
        wg.update(peers=None, inbound=None, active=None, expiryCandidates=None)
    try:
        from gateway_fastd import GatewayFastd
        counts = GatewayFastd().counts()
        fastd.update(accepted=counts.get('accepted'), connected=counts.get('connected'))
    except Exception:
        pass
    return wg, fastd


def main():
    if os.environ.get('NODE_ROLE', 'node') != 'gateway':
        return
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    control = AnnouncementController()
    previous_generation = None
    try:
        while running:
            withdrawal = Path('/run/freifunk/gateway/withdrawn.json')
            generation = (daemon_generation(), withdrawal.stat().st_mtime_ns if withdrawal.exists() else 0)
            if generation != previous_generation:
                control.initialized = False
                previous_generation = generation
            checks = {'exit': False, 'peers': False, 'admission': False, 'dns': False}
            try:
                checks = readiness()
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            healthy = all(checks.values())
            if healthy:
                healthy = recheck_before_announce(checks)
            requested = enabled(os.environ.get('GATEWAY_ANNOUNCE', 'false'))
            applied = control.apply(healthy, requested)
            wg, fastd = peer_summary()
            atomic_json(PUBLIC, {'timestamp': time.time(), 'role': 'gateway', 'healthy': healthy, 'wg': wg, 'fastd': fastd,
                                'announcementEnabled': requested, 'advertised': control.advertised if applied else None,
                                'checks': checks, 'gcDryRun': os.environ.get('GATEWAY_GC_DRY_RUN', 'true').lower() != 'false'}, 0o644)
            time.sleep(2)
    finally:
        withdraw_gateway(force=True)


if __name__ == '__main__':
    main()
