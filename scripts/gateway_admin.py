#!/usr/bin/env python3
"""Local gateway operator commands. Never owns or mutates the kernel dataplane.

wg-gc is deliberately preview-only: configure the runtime daemon's retention and
GATEWAY_GC_DRY_RUN setting to apply GC with kernel-before-store ordering.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from gateway_fastd import GatewayFastd, public_key
from gateway_store import GatewayStore, validate_node


class AdminError(Exception):
    pass


class Runner:
    def run(self, args, timeout):
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False, shell=False)


def removed(runtime_path, key, ifname, runner, deadline, clock):
    """Require completed owner reconciliation AND independent kernel evidence."""
    state = json.loads(runtime_path.read_text())
    if state.get('state') != 'ready' or not isinstance(state.get('peers'), dict):
        return False
    if any(p.get('key') == key and p.get('applied') for p in state['peers'].values()):
        return False
    def query(args):
        remaining = deadline - clock()
        if remaining <= 0:
            raise AdminError()
        result = runner.run(args, timeout=remaining)
        if result.returncode != 0:
            raise AdminError()
        return result.stdout
    if key in query(['wg', 'show', 'tbbwg', 'peers']).split():
        return False
    links = json.loads(query(['ip', '-j', 'link', 'show']))
    return isinstance(links, list) and all(link['ifname'] != ifname for link in links)


def main(argv=None, env=None, *, store_factory=GatewayStore,
         fastd_factory=GatewayFastd, runner=None, clock=time.monotonic,
         sleep=time.sleep, now=time.time, getuid=os.geteuid):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    pending = False
    try:
        # Role check precedes constructors, reads, locks, and directory creation.
        if env.get('NODE_ROLE', 'node') != 'gateway' or getuid() != 0:
            raise AdminError()
        if not argv:
            raise AdminError()
        command = argv[0]
        valid = ((command == 'list' and argv[1:] in ([], ['--fingerprints'])) or
                 (command == 'wg-gc' and argv[1:] in ([], ['--dry-run'])) or
                 (command in ('wg-revoke', 'fastd-revoke', 'fastd-unrevoke') and len(argv) == 2))
        if not valid:
            raise AdminError()
        runner = runner or Runner()
        if command.startswith('fastd-'):
            key = public_key(argv[1])
            gateway = fastd_factory(env)
            if command == 'fastd-unrevoke':
                if not gateway.unrevoke(key):
                    raise AdminError()
                print('fastd revocation cleared; fresh admission uses current policy')
                return 0
            if not gateway.revoke(key):
                raise AdminError()
            pending = True
            sys.stderr.write('Warning: restarting fastd reconnects all fastd peers.\n')
            result = runner.run(['sv', 'restart', '/etc/service/fastd'], timeout=5)
            if result.returncode != 0:
                raise AdminError()
            print('fastd revoked; supervised service restarted')
            return 0
        node = validate_node(argv[1]) if command == 'wg-revoke' else None
        store = store_factory(state_dir=env.get('GATEWAY_STATE_DIR', '/data/gateway'),
                              max_peers=int(env.get('GATEWAY_MAX_PEERS', '512')))
        if command == 'wg-gc':
            timestamp = now()
            candidates = store.expired(now=timestamp, days=float(env.get('GATEWAY_RETENTION_DAYS', '30')))
            print(json.dumps({'dryRun': True, 'candidates': [
                {'node': p['node'], 'ageDays': int((timestamp - max(p['created_at'], p['last_handshake_at'] or 0)) // 86400)}
                for p in candidates]}, sort_keys=True))
            return 0
        peers = store.snapshot()
        if command == 'list':
            records = []
            for p in peers:
                record = {'node': p['node'], 'state': 'revoked' if p.get('revoked') else 'desired',
                          'pinned': bool(p.get('pinned'))}
                if '--fingerprints' in argv:
                    record['publicKeyFingerprint'] = hashlib.sha256(p['publickey'].encode('ascii')).hexdigest()[:16]
                records.append(record)
            print(json.dumps({'counts': {'total': len(peers), 'revoked': sum(bool(p.get('revoked')) for p in peers)},
                              'nodes': records, 'fastd': fastd_factory(env).counts()}, sort_keys=True))
            return 0
        peer = next((p for p in peers if p['node'] == node), None)
        if peer is None or not store.revoke(node):
            raise AdminError()
        pending = True
        deadline = clock() + 5
        path = Path(env.get('GATEWAY_RUNTIME_DIR', '/run/freifunk/gateway')) / 'runtime.json'
        while clock() < deadline:
            try:
                if removed(path, peer['publickey'], 'tbb_wg' + str(node), runner, deadline, clock):
                    print('WireGuard revoked; runtime peer and tunnel removed')
                    return 0
            except Exception:
                pass
            remaining = deadline - clock()
            if remaining > 0:
                sleep(min(0.1, remaining))
        raise AdminError()
    except Exception:
        # No exception text, subprocess output, arbitrary state, or input echoes.
        sys.stderr.write('gateway admin: revocation saved; runtime removal pending\n' if pending
                         else 'gateway admin: command failed\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
