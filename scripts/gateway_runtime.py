#!/usr/bin/env python3
"""Opt-in gateway WireGuard owner. No imports have kernel side effects.

The registrar publishes outgoing intent; this owner alone reconciles its union
with inbound enrollment. Failed applications are never advertised as applied.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from backbone_runtime import load_runtime_env, parse_configured_peers
from node_config import node_addresses
from gateway_health import MAX_STATUS_AGE

RUNTIME_DIR = Path('/run/freifunk/gateway')
BMXD_ENV = Path('/run/freifunk/bmxd/bmxd.env')
WG_ENV = Path('/run/freifunk/wireguard/wireguard.env')
ALIAS = 'ffdd-gateway:'


class RuntimeFailure(RuntimeError):
    """Intentionally contains no command output or private material."""


class CommandRunner:
    def run(self, *args, check=True, input=None):
        try:
            result = subprocess.run(list(args), input=input, text=True,
                                    capture_output=True, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeFailure('runtime command unavailable or timed out') from None
        if check and result.returncode:
            raise RuntimeFailure('runtime command failed: ' + args[0])
        return result


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def valid_key(key):
    try:
        return isinstance(key, str) and len(base64.b64decode(key, validate=True)) == 32
    except (ValueError, TypeError):
        return False


def effective_node(env):
    """The applied primary address, not stale node.yaml registration metadata."""
    if env.get('BMXD_NODE_ID'):
        node = int(env['BMXD_NODE_ID'])
        if not 0 <= node <= 65279:
            raise ValueError('invalid effective gateway node')
        return node
    ip = ipaddress.IPv4Address(env['BMXD_PRIMARY_IP'])
    octets = str(ip).split('.')
    if octets[:2] != ['10', '200'] or int(octets[3]) == 0:
        raise ValueError('invalid effective gateway address')
    node = int(octets[2]) * 255 + int(octets[3]) - 1
    if not 0 <= node <= 65279:
        raise ValueError('invalid effective gateway node')
    return node


def union_peers(node, outgoing, incoming):
    outgoing, incoming = list(outgoing), list(incoming)
    for tombstone in (p for p in incoming if p.get('revoked')):
        number, key = int(tombstone['node']), tombstone.get('publickey', tombstone.get('key'))
        for peer in outgoing:
            other_node, other_key = int(peer['node']), peer.get('publickey', peer.get('key'))
            if (number == other_node or key == other_key) and (number != other_node or key != other_key):
                raise ValueError('revocation conflicts with outgoing identity')
        # Operator revocation overrides matching outgoing intent; TTL expiry
        # does not create a tombstone and therefore cannot remove static peers.
        outgoing = [p for p in outgoing if not (int(p['node']) == number
                    and p.get('publickey', p.get('key')) == key)]
    peers, keys, nodes, names = [], set(), set(), set()
    for inbound, records in ((False, outgoing), (True, incoming)):
        for record in records:
            if inbound and record.get('revoked'):
                continue
            number = int(record['node'])
            key = record.get('publickey', record.get('key'))
            name = record.get('ifname', 'tbb_wg' + str(number))
            if (number == node or not 0 <= number <= 65279 or not valid_key(key)
                    or not re.fullmatch(r'tbb_wg[0-9]+', name) or len(name) > 15):
                raise ValueError('invalid gateway peer identity')
            if key in keys or number in nodes or name in names:
                exact = next((p for p in peers if p['node'] == number and p['key'] == key
                              and p['ifname'] == name), None)
                if inbound and exact is not None and exact['outgoing'] and not exact['inbound']:
                    # Reciprocal enrollment shares one physical peer; retain the
                    # configured endpoint and both ownership origins.
                    exact['inbound'] = True
                    continue
                raise ValueError('conflicting inbound/outgoing gateway identities')
            endpoint = record.get('endpoint')
            if not inbound and endpoint is None and record.get('host'):
                endpoint = f"{record['host']}:{record['endpoint_port']}"
            peers.append(dict(node=number, key=key, ifname=name,
                              endpoint=endpoint, inbound=inbound, outgoing=not inbound))
            keys.add(key); nodes.add(number); names.add(name)
    return peers


class RuntimeOwner:
    def __init__(self, runner=None, runtime_dir=RUNTIME_DIR, bmxd_env=BMXD_ENV,
                 public_path='/run/freifunk/www/gateway.json'):
        self.runner = runner or CommandRunner()
        self.runtime_dir = Path(runtime_dir)
        self.bmxd_env = Path(bmxd_env)
        self.public_path = Path(public_path)
        self._fingerprint = None
        self._last_apply = 0.0
        self._last_success = None

    def cmd(self, *args, **kwargs):
        return self.runner.run(*args, **kwargs)

    def links(self):
        result = self.cmd('ip', '-d', '-j', 'link', 'show')
        return {link['ifname']: link for link in json.loads(result.stdout)}

    def attach(self, name, remove=False):
        self.cmd('bmxd', '-c', 'dev=' + ('-' if remove else '') + name,
                 *([] if remove else ['/linklayer', '1']))

    def ensure_base(self, node, secret, port, links):
        if not valid_key(secret):
            raise ValueError('gateway requires existing WireGuard identity')
        if 'tbbwg' not in links:
            self.cmd('ip', 'link', 'add', 'tbbwg', 'type', 'wireguard')
        elif links['tbbwg'].get('linkinfo', {}).get('info_kind') != 'wireguard':
            raise RuntimeFailure('refusing non-WireGuard tbbwg')
        addresses = node_addresses(node)
        self.cmd('ip', 'link', 'set', 'tbbwg', 'mtu', '1320')
        self.cmd('ip', 'addr', 'replace', addresses['wireguard_ip'] + '/32', 'dev', 'tbbwg')
        fd, filename = tempfile.mkstemp(prefix='wg-key-', dir=self.runtime_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(secret + '\n')
            self.cmd('wg', 'set', 'tbbwg', 'private-key', filename, 'listen-port', str(port))
        finally:
            os.unlink(filename)
        self.cmd('ip', 'link', 'set', 'tbbwg', 'up')
        self.cmd('ip', 'route', 'replace', '10.203.0.0/16', 'dev', 'tbbwg',
                 'src', addresses['wireguard_ip'])

    def ensure_tunnel(self, node, peer, links):
        name = peer['ifname']
        local = node_addresses(node)
        remote = node_addresses(peer['node'])['wireguard_ip']
        link = links.get(name)
        if link:
            info = link.get('linkinfo', {})
            data = info.get('info_data', {})
            # Adopt legacy outgoing links only if topology is exactly ours.
            if (info.get('info_kind') != 'ipip' or data.get('local') != local['wireguard_ip']
                    or data.get('remote') != remote
                    or link.get('ifalias', '') not in ('', ALIAS + str(peer['node']))):
                raise RuntimeFailure('refusing conflicting IPIP interface')
        else:
            self.cmd('ip', 'link', 'add', name, 'type', 'ipip', 'remote', remote,
                     'local', local['wireguard_ip'])
        self.cmd('ip', 'link', 'set', 'dev', name, 'alias', ALIAS + str(peer['node']))
        self.cmd('ip', 'addr', 'replace', local['nonprimary_ip'] + '/16',
                 'broadcast', local['mesh_broadcast'], 'dev', name)
        self.cmd('ip', 'link', 'set', name, 'up')

    def update_bmxd_env(self, names):
        # Preserve registrar-owned settings and non-WG interfaces. A launcher
        # restart then sees the same incoming links that were dynamically added.
        text = self.bmxd_env.read_text()
        env = load_runtime_env(self.bmxd_env)
        other = [n for n in env.get('BMXD_BACKBONE_INTERFACES', '').split()
                 if not n.startswith('tbb_wg')]
        line = 'BMXD_BACKBONE_INTERFACES="' + ' '.join(other + sorted(names)) + '"'
        text, count = re.subn(r'^BMXD_BACKBONE_INTERFACES=.*$', line, text, flags=re.M)
        if not count:
            text += '\n' + line + '\n'
        if text != self.bmxd_env.read_text():
            fd, temp = tempfile.mkstemp(dir=self.bmxd_env.parent)
            try:
                with os.fdopen(fd, 'w') as handle:
                    handle.write(text)
                os.replace(temp, self.bmxd_env)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)

    def inventory_usable(self, node, peers, links, current):
        """Bounded maintenance eligibility, not an enrollment acknowledgment."""
        base = links.get('tbbwg', {})
        if base.get('linkinfo', {}).get('info_kind') != 'wireguard' or 'UP' not in base.get('flags', []):
            return False
        if current != {peer['key'] for peer in peers}:
            return False
        desired_names = {peer['ifname'] for peer in peers}
        if any(link.get('ifalias', '').startswith(ALIAS) and name not in desired_names for name, link in links.items()):
            return False
        local = node_addresses(node)['wireguard_ip']
        for peer in peers:
            link = links.get(peer['ifname'], {})
            info = link.get('linkinfo', {})
            data = info.get('info_data', {})
            if ('UP' not in link.get('flags', []) or info.get('info_kind') != 'ipip'
                    or data.get('local') != local or data.get('remote') != node_addresses(peer['node'])['wireguard_ip']
                    or link.get('ifalias', '') not in ('', ALIAS + str(peer['node']))):
                return False
        return True

    def invalidate(self):
        self._fingerprint = None
        self._last_success = None
        atomic_json(self.runtime_dir / 'runtime.json', {'peers': {}, 'state': 'retrying', 'timestamp': time.time()})

    def reconcile(self, node, secret, outgoing, incoming, port=5003):
        try:
            return self._reconcile(node, secret, outgoing, incoming, port)
        except Exception:
            self.invalidate()
            raise

    def _reconcile(self, node, secret, outgoing, incoming, port=5003):
        peers = union_peers(node, outgoing, incoming)  # validate before any mutation
        if not 1 <= int(port) <= 65535:
            raise ValueError('invalid gateway listen port')
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with (self.runtime_dir / 'owner.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fingerprint = hashlib.sha256(json.dumps([node, secret, port, peers],
                                                     sort_keys=True).encode()).digest()
            unchanged = fingerprint == self._fingerprint
            if unchanged and time.monotonic() - self._last_apply < 30:
                return self.refresh_status(peers)
            # A periodic repair must not withdraw an otherwise healthy service.
            # Only the same live owner with unchanged intent and intact inventory
            # can grant a short, non-renewable maintenance lease. API peers stay
            # empty until the entire apply succeeds, exactly as on initial setup.
            self._fingerprint = None
            links = self.links()
            now = time.time()
            recent = self._last_success is not None and 0 <= now - self._last_success <= MAX_STATUS_AGE
            maintenance = False
            if unchanged and recent:
                observed = set(self.cmd('wg', 'show', 'tbbwg', 'peers').stdout.split())
                maintenance = self.inventory_usable(node, peers, links, observed)
            pending = {'peers': {}, 'state': 'repairing' if maintenance else 'applying', 'timestamp': now}
            if maintenance:
                pending.update(last_success_at=self._last_success, repair_started_at=now)
            atomic_json(self.runtime_dir / 'runtime.json', pending)
            self.ensure_base(node, secret, port, links)
            current = set(self.cmd('wg', 'show', 'tbbwg', 'peers').stdout.split())
            for peer in peers:
                args = ['wg', 'set', 'tbbwg', 'peer', peer['key'], 'allowed-ips',
                        node_addresses(peer['node'])['wireguard_ip'] + '/32',
                        'persistent-keepalive', '25']
                if peer['endpoint']:
                    args.extend(['endpoint', peer['endpoint']])
                self.cmd(*args)
                self.ensure_tunnel(node, peer, links)
            # Stage every kernel link before contacting BMXD. Its launcher may
            # still be waiting for the full interface topology during startup.
            for peer in peers:
                self.attach(peer['ifname'])
            desired_names = {p['ifname'] for p in peers}
            # Never delete arbitrary prefix-matching or unaliased legacy links.
            for name, link in links.items():
                if link.get('ifalias', '').startswith(ALIAS) and name not in desired_names:
                    self.attach(name, remove=True)
                    self.cmd('ip', 'link', 'del', name)
            for key in current - {p['key'] for p in peers}:
                self.cmd('wg', 'set', 'tbbwg', 'peer', key, 'remove')
            self.update_bmxd_env(desired_names)
            result = self.refresh_status(peers)
            self._fingerprint = fingerprint
            self._last_apply = time.monotonic()
            return result

    def refresh_status(self, peers):
        handshakes = {}
        for line in self.cmd('wg', 'show', 'tbbwg', 'latest-handshakes').stdout.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].isdigit() and int(fields[1]) > 0:
                handshakes[fields[0]] = int(fields[1])
        status = {str(p['node']): dict(key=p['key'], applied=True,
                  state='connected' if 0 < time.time() - handshakes.get(p['key'], 0) < 180 else 'applied',
                  ifname=p['ifname'], inbound=p['inbound'], outgoing=p['outgoing'],
                  origin='both' if p['inbound'] and p['outgoing'] else ('incoming' if p['inbound'] else 'outgoing'),
                  handshake=handshakes.get(p['key'], 0)) for p in peers}
        self._last_success = time.time()
        atomic_json(self.runtime_dir / 'runtime.json',
                    {'peers': status, 'state': 'ready', 'timestamp': self._last_success})
        atomic_json(self.public_path, {'timestamp': self._last_success, 'peers': len(peers), 'inbound': sum(p['inbound'] for p in peers),
                                      'incoming_connected': sum(p['inbound'] and p['state'] == 'connected' for p in status.values()),
                                     'connected': sum(p['state'] == 'connected' for p in status.values())}, 0o644)
        return {p['key']: handshakes[p['key']] for p in peers
                if p['inbound'] and p['key'] in handshakes}


def outgoing_peers(path=WG_ENV):
    _, peers = parse_configured_peers(load_runtime_env(path))
    return [dict(node=p.node, key=p.public_key, ifname=p.ifname,
                 endpoint=f'{p.host}:{p.port}') for p in peers.values()]


def main():
    if os.environ.get('NODE_ROLE', 'node') != 'gateway':
        return 0
    import yaml
    from gateway_store import GatewayStore
    owner = RuntimeOwner(runtime_dir=Path(os.environ.get('GATEWAY_RUNTIME_DIR', str(RUNTIME_DIR))),
                         bmxd_env=Path(os.environ.get('BMXD_ENV_FILE', str(BMXD_ENV))))
    store = GatewayStore(state_dir=os.environ.get('GATEWAY_STATE_DIR', '/data/gateway'),
                         max_peers=int(os.environ.get('GATEWAY_MAX_PEERS', '512')))
    identity_applied = None
    while True:
        try:
            env = load_runtime_env(owner.bmxd_env)
            node = effective_node(env)
            with Path(os.environ.get('NODE_YAML', '/data/node.yaml')).open() as handle:
                state = yaml.safe_load(handle) or {}
            secret = state.get('wireguard', {}).get('secret')
            if not valid_key(secret):
                raise RuntimeFailure('waiting for existing gateway identity')
            port = int(os.environ.get('WIREGUARD_PORT', '5003'))
            # Refresh known handshakes before testing retention. Never use dump,
            # which includes the interface private key in its first line.
            live = owner.cmd('wg', 'show', 'tbbwg', 'latest-handshakes', check=False)
            observed = {}
            for line in live.stdout.splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1].isdigit():
                    observed[fields[0]] = int(fields[1])
            store.update_handshakes(observed)
            now = time.time()
            days = float(os.environ.get('GATEWAY_RETENTION_DAYS', '30'))
            candidates = store.expired(now=now, days=days)
            dry_run = os.environ.get('GATEWAY_GC_DRY_RUN', 'true').lower() != 'false'
            incoming = store.snapshot()
            if not dry_run:
                expired_nodes = {p['node'] for p in candidates}
                incoming = [p for p in incoming if p['node'] not in expired_nodes]
            handshakes = owner.reconcile(node, secret, outgoing_peers(), incoming, port)
            store.update_handshakes(handshakes)
            if not dry_run:
                # Store deletion is the final commit, only after WG/IPIP/BMXD
                # detach succeeded. On failure retained intent is retried.
                store.expire(now=now, days=days, dry_run=False)
            if identity_applied != owner._last_apply:
                public_key = owner.cmd('wg', 'show', 'tbbwg', 'public-key').stdout.strip()
                if not valid_key(public_key):
                    raise RuntimeFailure('gateway public identity unavailable')
                atomic_json(owner.runtime_dir / 'identity.json',
                            {'node': node, 'key': public_key, 'port': port}, 0o644)
                identity_applied = owner._last_apply
            summary = json.loads(owner.public_path.read_text())
            summary.update(active=summary['connected'], conflicts=0,
                           expiryCandidates=len(candidates), gcDryRun=dry_run)
            atomic_json(owner.public_path, summary, 0o644)
        except Exception:
            # Never print subprocess stderr, YAML, or exception reprs: they may
            # contain key material. Pending peers remain unacknowledged.
            owner.invalidate()
        time.sleep(1)


if __name__ == '__main__':
    raise SystemExit(main())
