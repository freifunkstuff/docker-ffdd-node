"""Exit-only DNS forwarding, without using the ordinary node/WAN resolver."""
from __future__ import annotations
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from exit_observation import read_node_primary

RUNTIME = Path('/run/freifunk/exit-dns')
INTERFACE = re.compile(r'^wgx[0-9a-f]{10}$')


def dns_configuration(status: dict, primary: str) -> str | None:
    gateway = os.environ.get('NODE_ROLE', 'node') == 'gateway'
    if not isinstance(status, dict):
        status = {}
    if not status.get('enabled') and not gateway:
        return None
    node_ip = str(ipaddress.IPv4Address(primary))
    if ipaddress.IPv4Address(node_ip) not in ipaddress.IPv4Network('10.200.0.0/16'):
        raise ValueError('invalid DNS listener')
    profiles = status.get('exits', [])
    if not isinstance(profiles, list):
        return None
    active = next((p for p in profiles[:8] if isinstance(p, dict) and p.get('active')), None)
    upstreams = []
    if active and active.get('state') == 'ready' and active.get('reachable') is True:
        interface = active.get('interface', '')
        if not isinstance(interface, str) or not INTERFACE.fullmatch(interface):
            raise ValueError('invalid DNS egress')
        addresses = active.get('dns', [])
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 4:
            raise ValueError('invalid DNS servers')
        upstreams.extend(f'server={ipaddress.IPv4Address(ip)}@{interface}' for ip in addresses)
    if gateway:
        zones = os.environ.get('GATEWAY_DNS_ZONES', 'ffdd,ffl,200.10.in-addr.arpa,201.10.in-addr.arpa,203.10.in-addr.arpa').split(',')
        mesh_servers = os.environ.get('GATEWAY_DNS_MESH_FORWARDERS', '10.200.0.4,10.200.0.16').split(',')
        if not 1 <= len(zones) <= 12 or not 1 <= len(mesh_servers) <= 4:
            raise ValueError('invalid community DNS configuration')
        for zone in zones:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,127}', zone):
                raise ValueError('invalid community DNS zone')
            for address in mesh_servers:
                server = ipaddress.IPv4Address(address)
                if server not in ipaddress.IPv4Network('10.200.0.0/15') or str(server) == node_ip:
                    raise ValueError('invalid community DNS forwarder')
                upstreams.append(f'server=/{zone}/{server}@{node_ip}')
    if not upstreams:
        return None
    return '\n'.join([
        'port=53', 'no-resolv', 'no-poll', 'no-hosts', 'strict-order',
        'bind-interfaces', f'listen-address=127.0.0.1,{node_ip}',
        'cache-size=150', 'pid-file=', 'log-facility=-',
        *upstreams, '',
    ])


def main() -> None:
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = None
    previous = None
    path = Path(os.environ.get('EXIT_STATUS_FILE', '/run/freifunk/www/exits.json'))
    try:
        while running:
            try:
                primary = read_node_primary()
                status = {}
                if path.exists():
                    if path.stat().st_size > 32768:
                        raise ValueError('invalid status')
                    status = json.loads(path.read_text())
                config = dns_configuration(status, primary) if primary else None
            except (OSError, ValueError, TypeError):
                config = None
            if child is not None and child.poll() is not None:
                child = None
                previous = None
            if config != previous:
                if child is not None:
                    child.terminate()
                    try: child.wait(timeout=3)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
                    child = None
                previous = config
                if config:
                    RUNTIME.mkdir(parents=True, exist_ok=True)
                    file = RUNTIME / 'dnsmasq.conf'
                    file.write_text(config)
                    os.chmod(file, 0o600)
                    child = subprocess.Popen(['dnsmasq', '--keep-in-foreground', '--conf-file=' + str(file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: child.kill(); child.wait()


if __name__ == '__main__':
    main()
