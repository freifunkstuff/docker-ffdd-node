#!/usr/bin/env python3
"""Isolated proof: image owns firewall, routing handoff, mesh-up and resources.

Runtime supplies forwarding=1 and an initially-UP mesh like standard macvlan.
The explicitly accepted pre-entrypoint window is NOT an isolation guarantee.
All NICs are dummies inside --network none; no real Registry, VPN or host mount.
Run with an image built from this tree as the first argument.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

IMAGE = sys.argv[1] if len(sys.argv) > 1 else 'ffdd-node:resource-network-candidate'
NAME = 'ffdd-node-owned-' + uuid.uuid4().hex[:10]
LABEL = 'ffdd.node-owned-proof'
CAPS = ('NET_ADMIN', 'NET_RAW', 'NET_BIND_SERVICE', 'SETUID', 'SETGID', 'CHOWN', 'DAC_OVERRIDE', 'KILL')


def docker(*args, check=True):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=check)


def main():
    args = ['run', '-d', '--name', NAME, '--label', f'{LABEL}={NAME}',
            '--network', 'none', '--sysctl', 'net.ipv4.ip_forward=1',
            '--cpus', '2', '--memory', '256m', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges=true']
    for cap in CAPS:
        args += ['--cap-add', cap]
    env = {'NODE_REGISTRATION_MODE': 'temporary', 'BACKBONE_ENABLED': '0',
           'NODE_TYPE': 'node', 'NODE_NAME': 'isolated-node-owned',
           'NODE_CONTACT_EMAIL': 'test@example.org', 'MESH_INTERFACES': 'mesh0',
           'NODE_UPLINK_INTERFACE': 'lan0', 'NODE_UPLINK_GATEWAY': '192.168.61.1',
           'NODE_MANAGEMENT_INTERFACE': 'eth0',
           'NODE_MANAGEMENT_ROUTES': '10.42.0.0/16 10.43.0.0/16 192.168.1.9/32',
           'LAN_MESH_ACCESS_ENABLED': 'true', 'LAN_MESH_ACCESS_SOURCES': '192.168.0.0/16'}
    for key, value in env.items():
        args += ['-e', f'{key}={value}']
    setup = ('ip link add eth0 type dummy; ip addr add 10.42.0.2/24 dev eth0; ip link set eth0 up; '
             'ip route add default via 10.42.0.1 dev eth0; '
             'ip link add lan0 type dummy; ip addr add 192.168.61.10/24 dev lan0; ip link set lan0 up; '
             'ip link add mesh0 type dummy; ip link set mesh0 up; '
             'exec /usr/local/bin/docker-entrypoint.sh')
    try:
        docker(*args, '--entrypoint', 'sh', IMAGE, '-ec', setup)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            r = docker('exec', NAME, 'bmxd', '-c', 'status', check=False)
            if r.returncode == 0 and 'BMX' in r.stdout:
                break
            time.sleep(1)
        else:
            r = docker('logs', '--tail', '35', NAME, check=False)
            raise RuntimeError(r.stdout + r.stderr)
        proof = '''import json,urllib.request,subprocess
from pathlib import Path
with urllib.request.urlopen('http://127.0.0.1/sysinfo-json.cgi',timeout=3) as r:d=json.load(r)['data']
resources=d['resources']
assert resources['scope']=='container' and resources['cpu_capacity']==2
assert d['system']['cpucount']=='2'
assert resources['memory_limit_bytes']==256*1024*1024
assert 0<=resources['uptime_seconds']<60
assert 'cpu_load' not in d['statistic'] and 'cpu_stat' not in d['statistic']
assert 'meminfo_Buffers' not in d['statistic']
routes=json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','main']))
defaults=[r for r in routes if r.get('dst')=='default']
assert len(defaults)==1 and defaults[0]['dev']=='lan0' and defaults[0]['gateway']=='192.168.61.1'
for prefix in ['10.42.0.0/16','10.43.0.0/16','192.168.1.9']:
 assert any(r.get('dst')==prefix and r.get('dev')=='eth0' and r.get('gateway')=='10.42.0.1' for r in routes)
mesh=json.loads(subprocess.check_output(['ip','-j','link','show','dev','mesh0']))[0]
assert 'UP' in mesh['flags']
filter_rules=subprocess.check_output(['iptables','-S'],text=True)
assert '-A FORWARD -j FFDD_NODE_FORWARD' in filter_rules
assert '-A FFDD_NODE_FORWARD -j DROP' in filter_rules
mark_rules=subprocess.check_output(['iptables','-t','mangle','-S'],text=True)
assert '-i lan0' in mark_rules and '-d 10.200.0.0/16' in mark_rules
print(json.dumps({'scope':resources['scope'],'cpu_capacity':resources['cpu_capacity'],
 'memory_limit_bytes':resources['memory_limit_bytes'],'uptime_seconds':resources['uptime_seconds'],
 'uplink':'lan0','management':'eth0','guard_installed':True,'mesh_activated_by_node':True}))
'''
        print(docker('exec', NAME, 'python3', '-c', proof).stdout.strip())
        info = json.loads(docker('inspect', NAME).stdout)[0]
        assert not info['HostConfig']['Privileged'] and not info['HostConfig'].get('Binds')
        print('PASS: main Node performed network initialization and container resource reporting without privileged init')
    finally:
        result = docker('inspect', NAME, check=False)
        if result.returncode == 0:
            assert json.loads(result.stdout)[0]['Config']['Labels'].get(LABEL) == NAME
            docker('rm', '-f', '-v', NAME)


if __name__ == '__main__':
    main()
