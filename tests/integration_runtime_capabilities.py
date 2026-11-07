#!/usr/bin/env python3
"""Explicit Docker proof of the offloader capability set, with no real network.

Run: python3 tests/integration_runtime_capabilities.py IMAGE
No host mounts, real identity, Registry, VPN endpoint, or Internet are used.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

IMAGE = sys.argv[1] if len(sys.argv) > 1 else 'ffdd-node:latest'
NAME = 'ffdd-capability-proof-' + uuid.uuid4().hex[:10]
LABEL = 'ffdd.capability-proof'
CAPS = ('NET_ADMIN', 'NET_RAW', 'NET_BIND_SERVICE', 'SETUID', 'SETGID',
        'CHOWN', 'DAC_OVERRIDE', 'KILL')


def docker(*args, check=True):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=check)


def main():
    args = ['run', '-d', '--name', NAME, '--label', f'{LABEL}={NAME}',
            '--network', 'none', '--sysctl', 'net.ipv4.ip_forward=1',
            '--security-opt', 'no-new-privileges=true', '--cap-drop', 'ALL']
    for cap in CAPS:
        args += ['--cap-add', cap]
    for env in ('NODE_REGISTRATION_MODE=temporary', 'BACKBONE_ENABLED=0',
                'MESH_INTERFACES=meshproof0', 'NODE_TYPE=node',
                'NODE_NAME=isolated-capability-proof', 'NODE_CONTACT_EMAIL=test@example.org'):
        args += ['-e', env]
    # The dummy link substitutes the runtime-provided mesh attachment. Never use
    # a host network or macvlan here: the synthetic 9xx identity stays isolated.
    args += ['--entrypoint', 'sh', IMAGE, '-ec',
             'ip link add meshproof0 type dummy; exec /usr/local/bin/docker-entrypoint.sh']
    try:
        docker(*args)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = docker('exec', NAME, 'bmxd', '-c', 'status', check=False)
            if status.returncode == 0 and 'BMX' in status.stdout:
                break
            time.sleep(1)
        else:
            logs = docker('logs', '--tail', '30', NAME, check=False)
            raise RuntimeError('BMXD startup failed:\n' + logs.stdout + logs.stderr)
        proof = '''import json,urllib.request,yaml
from pathlib import Path
state=yaml.safe_load(Path('/data/node.yaml').read_text())
assert 900<=state['temporary']['node_id']<1000
assert 'registration' not in state
with urllib.request.urlopen('http://127.0.0.1/sysinfo-json.cgi',timeout=3) as r:
 data=json.load(r)
assert data['data']['system']['node_type']=='node'
print('PASS: actual registrar, bmxd, nginx and sysinfo started')
'''
        print(docker('exec', NAME, 'python3', '-c', proof).stdout.strip())
        docker('exec', NAME, 'sh', '-ec',
               'ip link add wg-capproof type wireguard; '
               'ip link add ipip-capproof type ipip local 127.0.0.1 remote 127.0.0.2')
        info = json.loads(docker('inspect', NAME).stdout)[0]
        assert not info['HostConfig']['Privileged'] and not info['HostConfig'].get('Devices')
        assert not info['HostConfig'].get('Binds')
        print('PASS: nonprivileged capability set supports WG/IPIP, without TUN or host mounts')
    finally:
        result = docker('inspect', NAME, check=False)
        if result.returncode == 0:
            assert json.loads(result.stdout)[0]['Config']['Labels'].get(LABEL) == NAME
            docker('rm', '-f', '-v', NAME)


if __name__ == '__main__':
    main()
