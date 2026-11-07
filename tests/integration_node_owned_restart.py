#!/usr/bin/env python3
"""Real Docker app recreation in a live, isolated pod-like network namespace.

Only UUID-labelled containers and a named data volume are created/deleted. No
host mounts, host networking, registry, privileged containers or SYS_ADMIN.
Run directly, optionally passing an already-built image name. Prints no secrets.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

IMAGE = sys.argv[1] if len(sys.argv) > 1 else 'ffdd-node:resource-network-candidate'
TOKEN = uuid.uuid4().hex
LABEL = 'ffdd.node-owned-restart'
KEEPER = 'ffdd-restart-keeper-' + TOKEN
VOLUME = 'ffdd-restart-data-' + TOKEN
APPS = ['ffdd-restart-app-' + TOKEN + '-' + str(i) for i in (1, 2)]
CAPS = ('NET_ADMIN', 'NET_RAW', 'NET_BIND_SERVICE', 'SETUID', 'SETGID',
        'CHOWN', 'DAC_OVERRIDE', 'KILL')
ENV = {'NODE_REGISTRATION_MODE': 'temporary', 'BACKBONE_ENABLED': '0',
       'NODE_TYPE': 'node', 'NODE_NAME': 'isolated-restart-proof',
       'NODE_CONTACT_EMAIL': 'test@example.org', 'MESH_INTERFACES': 'mesh0',
       'NODE_UPLINK_INTERFACE': 'lan0', 'NODE_UPLINK_GATEWAY': '192.168.61.1',
       'NODE_MANAGEMENT_INTERFACE': 'eth0', 'NODE_MANAGEMENT_GATEWAY': '',
       'NODE_MANAGEMENT_ROUTES': '10.42.0.0/16 10.43.0.0/16 192.168.1.9/32',
       'LAN_MESH_ACCESS_ENABLED': 'true', 'LAN_MESH_ACCESS_SOURCES': '192.168.0.0/16'}
NETWORK_PROOF = r'''
import json, subprocess
routes=json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','main']))
defaults=[r for r in routes if r.get('dst')=='default']
assert len(defaults)==1 and defaults[0]['dev']=='lan0' and defaults[0]['gateway']=='192.168.61.1'
for prefix in ['10.42.0.0/16','10.43.0.0/16','192.168.1.9']:
 assert any(r.get('dst')==prefix and r.get('dev')=='eth0' and r.get('gateway')=='10.42.0.1' for r in routes)
mesh=json.loads(subprocess.check_output(['ip','-j','link','show','dev','mesh0']))[0]
assert 'UP' in mesh['flags']
rules=subprocess.check_output(['iptables','-S'],text=True).splitlines()
forward=[r for r in rules if r.startswith('-A FORWARD ')]
assert forward[0]=='-A FORWARD -j FFDD_NODE_FORWARD'
assert '-A FFDD_NODE_FORWARD -j DROP' in rules
nat=subprocess.check_output(['iptables','-t','nat','-S'],text=True)
assert '-o mesh0' in nat and '-j SNAT --to-source ' in nat
marks=subprocess.check_output(['iptables','-t','mangle','-S'],text=True)
assert '-i lan0' in marks and '-d 10.200.0.0/16' in marks
assert '-i eth0' not in marks
'''
APP_PROOF = NETWORK_PROOF + r'''
import hashlib, pathlib, urllib.request
with urllib.request.urlopen('http://127.0.0.1/sysinfo-json.cgi',timeout=2) as response:
 d=json.load(response)['data']
r=d['resources']
assert r['scope']=='container' and r['cpu_capacity']==2
assert r['memory_limit_bytes']==256*1024*1024
assert 0<=r['uptime_seconds']<60
import os
pid1_ticks=int(pathlib.Path('/proc/1/stat').read_text().split()[21])
pid1_age=float(pathlib.Path('/proc/uptime').read_text().split()[0])-pid1_ticks/os.sysconf('SC_CLK_TCK')
# Sysinfo is cached on a 30-second service loop.
assert -1 <= pid1_age-r['uptime_seconds'] <= 31
assert d['system']['cpucount']=='2'
node=int(d['common']['node'])
assert 900<=node<=999
# Store only the digest, never the YAML (which contains private keys).
print(json.dumps({'id':node,'node_yaml_sha256':hashlib.sha256(pathlib.Path('/data/node.yaml').read_bytes()).hexdigest(),
 'uptime_seconds':r['uptime_seconds'],'scope':r['scope'],'cpu_capacity':r['cpu_capacity'],
 'memory_limit_bytes':r['memory_limit_bytes'],
 'pid1_start_ticks':int(pathlib.Path('/proc/1/stat').read_text().split()[21])}))
'''


def main():
    deadline = time.monotonic() + 90
    created = set()
    volume_created = False
    keeper_id = None

    def docker(*args, check=True, cleanup=False):
        timeout = 12 if cleanup else min(10, max(.1, deadline-time.monotonic()))
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            # Do not echo logs, config or exec output: these can contain keys.
            raise RuntimeError('docker %s failed (exit %s)' % (args[0], result.returncode))
        return result

    def inspect(name, kind='container', cleanup=False):
        return json.loads(docker(kind, 'inspect', name, cleanup=cleanup).stdout)[0]

    def checked_remove(name):
        info = inspect(name, cleanup=True)
        assert info['Config']['Labels'].get(LABEL) == TOKEN
        mode = info['HostConfig']['NetworkMode']
        if name in APPS:
            assert mode == 'container:' + keeper_id
        else:
            assert name == KEEPER and mode == 'none'
        docker('rm', '-f', '-v', name, cleanup=True)
        created.remove(name)

    def start_app(name):
        args = ['run', '-d', '--name', name, '--label', LABEL+'='+TOKEN,
                '--network', 'container:'+keeper_id, '--cpus', '2', '--memory', '256m',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
                '--mount', 'type=volume,src='+VOLUME+',dst=/data']
        for cap in CAPS:
            args += ['--cap-add', cap]
        for key, value in ENV.items():
            args += ['-e', key+'='+value]
        # No entrypoint override: fresh app root and normal image startup.
        docker(*args, IMAGE)
        created.add(name)
        info = inspect(name)
        assert not info['HostConfig']['Privileged'] and not info['HostConfig'].get('Binds')
        assert info['HostConfig']['CapDrop'] == ['ALL']
        assert {cap.removeprefix('CAP_') for cap in info['HostConfig']['CapAdd']} == set(CAPS)
        startup_deadline = min(deadline, time.monotonic()+30)
        last_error = 'BMXD not ready'
        while time.monotonic() < startup_deadline:
            status = docker('exec', name, 'bmxd', '-c', 'status', check=False)
            if status.returncode == 0 and 'BMX' in status.stdout:
                # Refresh the normal producer: its 30s cache may predate temporary
                # identity assignment, while this proof has a strict 30s bound.
                docker('exec', name, 'python3', '/usr/local/bin/sysinfo.py')
                proof = docker('exec', name, 'python3', '-c', APP_PROOF, check=False)
                if proof.returncode == 0:
                    return json.loads(proof.stdout)
                # Capture only traceback location/type, not arbitrary runtime output.
                lines = proof.stderr.splitlines()
                locations = [line.strip() for line in lines if line.startswith('  File "<string>", line ')]
                last_error = 'proof exit %s at %s' % (proof.returncode, locations[-1:] or ['unknown location'])
            time.sleep(.25)
        raise RuntimeError('startup verification failed within 30s: '+last_error)

    try:
        docker('volume', 'create', '--label', LABEL+'='+TOKEN, VOLUME)
        volume_created = True
        setup = ('ip link add eth0 type dummy; ip addr add 10.42.0.2/24 dev eth0; ip link set eth0 up; '
                 'ip route add default via 10.42.0.1 dev eth0; '
                 'ip link add lan0 type dummy; ip addr add 192.168.61.10/24 dev lan0; ip link set lan0 up; '
                 'ip link add mesh0 type dummy; exec python3 -c "import time; time.sleep(300)"')
        docker('run', '-d', '--name', KEEPER, '--label', LABEL+'='+TOKEN,
               '--network', 'none', '--sysctl', 'net.ipv4.ip_forward=1',
               '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN',
               '--security-opt', 'no-new-privileges=true', '--entrypoint', 'sh', IMAGE, '-ec', setup)
        created.add(KEEPER)
        keeper_id = inspect(KEEPER)['Id']
        docker('exec', KEEPER, 'ip', 'link', 'show', 'mesh0')
        first = start_app(APPS[0])
        # A rootfs marker must not survive recreation; /data alone persists.
        docker('exec', APPS[0], 'touch', '/run/restart-proof-root-marker')
        docker('stop', '-t', '2', APPS[0])
        checked_remove(APPS[0])
        docker('exec', KEEPER, 'python3', '-c', NETWORK_PROOF)
        second = start_app(APPS[1])
        docker('exec', APPS[1], 'test', '!', '-e', '/run/restart-proof-root-marker')
        assert first['id'] == second['id']
        assert first['node_yaml_sha256'] == second['node_yaml_sha256']
        assert second['pid1_start_ticks'] > first['pid1_start_ticks']
        keeper_ticks = int(docker('exec', KEEPER, 'python3', '-c',
            "from pathlib import Path; print(Path('/proc/1/stat').read_text().split()[21])").stdout)
        assert second['pid1_start_ticks'] > keeper_ticks
        print(json.dumps({'first': first, 'recreated': second,
                          'live_keeper_network_reused': True, 'fresh_app_root': True,
                          'management_gateway_recovered': True, 'no_secret_data_printed': True}))
        print('PASS: isolated app recreation, persistent identity, owned network and own-PID1 resources')
    finally:
        errors = []
        for name in reversed(APPS):
            if name in created:
                try:
                    checked_remove(name)
                except Exception as exc:
                    errors.append(type(exc).__name__ + ': checked app cleanup failed')
        if KEEPER in created:
            try:
                checked_remove(KEEPER)
            except Exception as exc:
                errors.append(type(exc).__name__ + ': checked keeper cleanup failed')
        if volume_created:
            try:
                assert inspect(VOLUME, 'volume', cleanup=True)['Labels'].get(LABEL) == TOKEN
                docker('volume', 'rm', VOLUME, cleanup=True)
            except Exception as exc:
                errors.append(type(exc).__name__ + ': checked volume cleanup failed')
        if errors:
            raise RuntimeError('; '.join(errors))


if __name__ == '__main__':
    main()
