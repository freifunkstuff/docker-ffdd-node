#!/usr/bin/env python3
"""Opt-in full-image gateway role proof using disposable isolated Docker resources.

Run: python3 tests/integration_gateway_wg.py --image ffdd-node:gateway-candidate
Requires Docker, WireGuard kernel support and /dev/net/tun. No real identities,
production network access, host networking, published ports or host bind mounts.
Keys are generated inside disposable containers and never printed. This runs the
actual image entrypoint, enrollment HTTP route and supervised gateway runtime.
"""
import argparse
import base64
import io
import json
import subprocess
import tarfile
import time
import uuid

CAPS = ('NET_ADMIN', 'NET_RAW', 'NET_BIND_SERVICE', 'SETUID', 'SETGID', 'CHOWN', 'DAC_OVERRIDE', 'KILL')
LABEL = 'ffdd.gateway-wg-proof'


def docker(*args, timeout=60, check=True, **kwargs):
    return subprocess.run(['docker', *args], timeout=timeout, check=check,
                          capture_output=True, **kwargs)


def python(container, script, *args):
    # Errors do not echo command bodies or captured key-related subprocess data.
    result = docker('exec', container, 'python3', '-c', script, *args, check=False)
    if result.returncode:
        raise AssertionError('isolated container assertion failed')
    return result.stdout.decode().strip()


def wait(predicate, label, seconds=75):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (AssertionError, ValueError, KeyError, subprocess.CalledProcessError):
            pass
        time.sleep(.5)
    raise AssertionError('timeout: ' + label)


def seed(container):
    data = b'temporary:\n  node_id: 901\n'
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        info = tarfile.TarInfo('node.yaml')
        info.size, info.mode = len(data), 0o600
        tar.addfile(info, io.BytesIO(data))
    docker('cp', '-', container + ':/data', input=archive.getvalue())


HTTP = r'''
import json,sys,urllib.request,urllib.error
try:
    with urllib.request.urlopen('http://'+sys.argv[1]+'/wg.cgi'+sys.argv[2],timeout=6) as response:
        print(json.dumps({'code':response.status,'body':json.load(response)}))
except urllib.error.HTTPError as error:
    print(json.dumps({'code':error.code,'body':{}}))
'''
CLIENT_INIT = r'''
import pathlib,subprocess
p=pathlib.Path('/tmp/proof-key');p.touch(mode=0o600)
s=subprocess.check_output(['wg','genkey'])
p.write_bytes(s);p.chmod(0o600)
k=subprocess.check_output(['wg','pubkey'],input=s).decode().strip()
pathlib.Path('/tmp/proof-public').write_text(k)
'''
CLIENT_QUERY = r'''
import json,pathlib,sys,urllib.request,urllib.parse
k=pathlib.Path('/tmp/proof-public').read_text()
u='http://'+sys.argv[1]+'/wg.cgi?'+urllib.parse.urlencode({'node':902,'key':k})
with urllib.request.urlopen(u,timeout=7) as response: print(json.dumps(json.load(response)))
'''
CLIENT_SETUP = r'''
import json,pathlib,subprocess,sys
server=json.loads(sys.argv[1]);host=sys.argv[2]
def run(*args): subprocess.run(args,check=True,capture_output=True)
run('ip','link','add','proofwg','type','wireguard')
run('ip','addr','add','10.203.3.138/32','dev','proofwg')
run('wg','set','proofwg','private-key','/tmp/proof-key','peer',server['key'],
    'allowed-ips','10.203.3.137/32','persistent-keepalive','1','endpoint',host+':'+str(server['port']))
run('ip','link','set','proofwg','up')
run('ip','route','add','10.203.3.137/32','dev','proofwg','src','10.203.3.138')
run('ip','link','add','proofipip','type','ipip','local','10.203.3.138','remote','10.203.3.137')
run('ip','addr','add','10.201.3.138/16','broadcast','10.255.255.255','dev','proofipip')
run('ip','link','set','proofipip','up')
run('ip','link','add','bmx_prime','type','bridge')
run('ip','addr','add','10.200.3.138/32','dev','bmx_prime')
run('ip','link','set','bmx_prime','up')
run('ip','rule','add','pref','500','to','10.200.0.0/15','lookup','64')
pathlib.Path('/tmp/proof-bmxd').mkdir()
log=open('/tmp/proof-bmxd.log','wb')
subprocess.Popen(['bmxd','--runtime_dir','/tmp/proof-bmxd','--no_fork','0',
    '--network','10.200.0.0/16','--netid','0','--ogm_interval','1000',
    '--dev=bmx_prime','/linklayer','0','--dev=proofipip','/linklayer','1'],stdout=log,stderr=log)
'''
DIAGNOSTIC = r'''
import json,subprocess
result={}
for label,cmd in [('rules',['ip','rule','show']),('routes',['ip','route','show','table','all']),
                  ('returnNP',['ip','route','get','10.201.3.138','from','10.201.3.137']),
                  ('returnPrimary',['ip','route','get','10.200.3.138','from','10.200.3.137'])]:
    p=subprocess.run(cmd,capture_output=True,text=True);result[label]=p.stdout.strip()
for field in ('latest-handshakes','transfer'):
    p=subprocess.run(['wg','show','all',field],capture_output=True,text=True)
    result[field]=[line.split()[2:] for line in p.stdout.splitlines()]
p=subprocess.run(['bmxd','-c','show=links'],capture_output=True,text=True)
result['bmxdLinks']=p.stdout[-3000:]
print(json.dumps(result))
'''


FASTD_CLIENT = r'''
import os,pathlib,subprocess,sys
root=pathlib.Path('/tmp/proof-fastd');root.mkdir(mode=0o700)
secret=subprocess.check_output(['fastd','--generate-key','--machine-readable'],stderr=subprocess.DEVNULL).decode().strip()
conf=root/'client.conf'
conf.write_text('secret "'+secret+'";\nmode tap;\nmethod "null";\nmtu 1200;\ndrop capabilities no;\ninterface "prooffastd";\npeer "gateway" { key "'+sys.argv[1]+'"; remote ipv4 "'+sys.argv[2]+'":5002; }\n')
conf.chmod(0o600)
log=(root/'log').open('wb')
p=subprocess.Popen(['fastd','--config',str(conf)],stdout=log,stderr=log)
(root/'pid').write_text(str(p.pid))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='ffdd-node:gateway-candidate')
    args = parser.parse_args()
    token = 'ffdd-gwwg-' + uuid.uuid4().hex[:16]
    containers, network, volume = [], False, False
    caps = [item for cap in CAPS for item in ('--cap-add', cap)]

    def create_gateway(restricted=False, role='gateway', use_volume=True):
        env = dict(NODE_ROLE=role, GATEWAY_ALLOW_IDENTITY_GENERATION='true',
                   NODE_REGISTRATION_MODE='temporary', BACKBONE_ENABLED='0', BACKBONE_PEERS='',
                   MESH_INTERFACES='', NODE_CONTACT_EMAIL='isolated-proof@example.invalid',
                   NODE_CONTACT_NAME='Isolated proof', NODE_CONTACT_LOCATION='test namespace',
                   GATEWAY_WG_RESTRICTED=str(restricted).lower(), GATEWAY_GC_DRY_RUN='true')
        envargs = [item for k, v in env.items() for item in ('-e', k + '=' + v)]
        mount = ['--mount', 'type=volume,src=' + token + ',dst=/data'] if use_volume else []
        container = docker('create', '--network', token, '--label', LABEL + '=' + token,
                           '--cap-drop', 'ALL', *caps, '--device', '/dev/net/tun:/dev/net/tun',
                           *mount, *envargs, args.image).stdout.decode().strip()
        containers.append(container)
        if not restricted:
            seed(container)
        docker('start', container)
        return container

    def address(container):
        value = json.loads(docker('inspect', container).stdout)[0]
        return value['NetworkSettings']['Networks'][token]['IPAddress']

    def owned_remove(container):
        value = json.loads(docker('inspect', container).stdout)[0]
        if value['Config']['Labels'].get(LABEL) != token:
            raise AssertionError('refusing cleanup of unowned container')
        docker('rm', '-f', '-v', container)
        containers.remove(container)

    try:
        docker('image', 'inspect', args.image)
        docker('network', 'create', '--internal', '--label', LABEL + '=' + token, token)
        network = True
        docker('volume', 'create', '--label', LABEL + '=' + token, token)
        volume = True
        gateway = create_gateway()
        client = docker('create', '--network', token, '--label', LABEL + '=' + token,
                        '--cap-drop', 'ALL', *caps, '--device', '/dev/net/tun:/dev/net/tun', '--entrypoint', 'sh', args.image,
                        '-c', 'while :; do sleep 3600; done').stdout.decode().strip()
        containers.append(client)
        docker('start', client)
        python(client, CLIENT_INIT)
        host = address(gateway)
        wait(lambda: json.loads(python(client, HTTP, host, ''))['body'].get('status') == 'NotRestricted',
             'full image discovery')
        accepted = wait(lambda: (lambda r: r if r.get('status') in
                        ('RequestAccepted', 'RequestAlreadyRegistered') else None)
                        (json.loads(python(client, CLIENT_QUERY, host))), 'kernel-confirmed enrollment')
        assert accepted['server']['node'] == 901
        python(client, CLIENT_SETUP, json.dumps(accepted['server']), host)
        try:
            wait(lambda: python(client, "import subprocess; r=subprocess.run(['ping','-c','1','-W','1','-I','10.200.3.138','10.200.3.137'],capture_output=True); print(r.returncode==0)") == 'True',
                 'real WireGuard IPIP BMXD mesh-source ping')
        except AssertionError:
            print('SANITIZED gateway diagnostics ' + python(gateway, DIAGNOSTIC), flush=True)
            print('SANITIZED client diagnostics ' + python(client, DIAGNOSTIC), flush=True)
            raise
        wait(lambda: python(gateway, "import subprocess; rows=subprocess.check_output(['wg','show','tbbwg','latest-handshakes'],text=True).splitlines(); print(any(int(r.split()[1])>0 for r in rows))") == 'True',
             'server kernel nonzero handshake')
        wait(lambda: python(gateway, "import subprocess; r=subprocess.run(['ping','-c','1','-W','1','-I','10.200.3.137','10.200.3.138'],capture_output=True); print(r.returncode==0)") == 'True',
             'reverse BMXD learned mesh route')
        print('PASS full role HTTP enrollment, real WG handshake and bidirectional BMXD mesh-source ping')
        server_fastd_key = python(gateway, "import subprocess;print(subprocess.check_output(['fastd','--config','/run/freifunk/fastd/fastd.conf','--show-key','--machine-readable'],stderr=subprocess.DEVNULL,text=True).strip())")
        python(client, FASTD_CLIENT, server_fastd_key, host)
        try:
            wait(lambda: python(gateway, "import sys;sys.path.insert(0,'/usr/local/bin');from gateway_fastd import GatewayFastd;print(GatewayFastd().counts()['connected']>0)") == 'True',
                 'full-role TAP fastd unknown admission and establishment', seconds=40)
            print('PASS full-role legacy TAP fastd listener, unknown admission and real establish hook')
        except AssertionError:
            print('SANITIZED fastd client diagnostics ' + python(client,
                  "import pathlib,re;print(re.sub(r'[0-9a-fA-F]{64}','[redacted]',pathlib.Path('/tmp/proof-fastd/log').read_text()[-5000:]))"), flush=True)
            print('SANITIZED fastd server diagnostics ' + python(gateway,
                  "import sys;sys.path.insert(0,'/usr/local/bin');from gateway_fastd import GatewayFastd;import json;print(json.dumps(GatewayFastd().counts()))"), flush=True)
            raise
        finally:
            python(client, "import contextlib,os,pathlib,signal\nwith contextlib.suppress(ProcessLookupError): os.kill(int(pathlib.Path('/tmp/proof-fastd/pid').read_text()),signal.SIGTERM)")
        assert json.loads(python(client, CLIENT_QUERY, host))['status'] == 'RequestAlreadyRegistered'
        conflict = python(client, HTTP, host, '?node=902&key=' + base64.b64encode(bytes([7]) * 32).decode())
        assert json.loads(conflict)['body']['status'] == 'RequestConflict'
        python(gateway, "import subprocess;subprocess.run(['sv','kill','/etc/service/gateway-runtime'],check=True,capture_output=True)")
        wait(lambda: json.loads(python(client, CLIENT_QUERY, host)).get('status') == 'RequestAlreadyRegistered',
             'supervised runtime restart')
        print('PASS idempotent enrollment, conflicting claim rejection, supervised restart')
        owned_remove(gateway)
        gateway = create_gateway(restricted=True)
        host = address(gateway)
        wait(lambda: json.loads(python(client, HTTP, host, ''))['body'].get('status') == 'Restricted',
             'restricted persistent restart')
        wait(lambda: json.loads(python(client, CLIENT_QUERY, host)).get('status') == 'RequestAlreadyRegistered',
             'known persisted enrollment in restricted mode')
        unknown = json.loads(python(client, HTTP, host, '?node=903&key=' + base64.b64encode(bytes([8]) * 32).decode()))
        assert unknown['body']['status'] == 'Restricted'
        print('PASS persistent known admission and restricted unknown denial')
        node = create_gateway(role='node', use_volume=False)
        nodehost = address(node)
        wait(lambda: json.loads(python(client, HTTP, nodehost, ''))['code'] == 404, 'node-role HTTP 404')
        print('PASS node role gateway HTTP route disabled')
    finally:
        for container in list(reversed(containers)):
            owned_remove(container)
        for kind, enabled in [('volume', volume), ('network', network)]:
            if enabled:
                metadata = json.loads(docker(kind, 'inspect', token).stdout)[0]
                if metadata.get('Labels', {}).get(LABEL) != token:
                    raise AssertionError('refusing cleanup of unowned ' + kind)
                docker(kind, 'rm', token)


if __name__ == '__main__':
    main()
