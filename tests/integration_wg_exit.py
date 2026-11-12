#!/usr/bin/env python3
"""Offline, opt-in real-kernel WG/IPIP exit proof.

Run: python3 dockernode/tests/integration_wg_exit.py ffdd-node:exit-candidate
Creates three labeled, disposable containers on one INTERNAL Docker bridge. No
host mounts, real keys, host networking, privileged containers, or Internet.
Private keys are generated/read only inside disposable containers. Errors never
include container output (which could otherwise contain configuration secrets).
"""
from __future__ import annotations
import json
import subprocess
import sys
import time
import uuid

IMAGE = sys.argv[1] if len(sys.argv) > 1 else 'ffdd-node:exit-candidate'
TOKEN = 'ffdd-wg-proof-' + uuid.uuid4().hex[:12]
LABEL = 'ffdd.wg-exit-proof'
CAPS = ('NET_ADMIN', 'NET_RAW', 'NET_BIND_SERVICE', 'SETUID', 'SETGID', 'CHOWN', 'DAC_OVERRIDE', 'KILL')
GW, SERVER, CLIENT = (TOKEN + '-' + role for role in ('gateway', 'server', 'client'))
NETWORK = TOKEN + '-network'
SCRIPTS = '/usr/local/bin'


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and result.returncode:
        raise RuntimeError('isolated Docker operation failed (output suppressed)')
    return result


def execute(name, script, check=True):
    return docker('exec', name, 'sh', '-ec', script, check=check)


def address(name):
    info = json.loads(docker('inspect', name).stdout)[0]
    return info['NetworkSettings']['Networks'][NETWORK]['IPAddress']


def wait_for(predicate, description, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.5)
    raise AssertionError(description)


def status():
    result = execute(GW, 'test -f /tmp/exits-public.json && python3 -c "import json; print(json.dumps(json.load(open(\'/tmp/exits-public.json\'))))"', check=False)
    if result.returncode:
        return {}
    return json.loads(result.stdout)


def fetch(target='1.1.1.1'):
    code = "import urllib.request; print(urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://%s:8080/', timeout=3).read().decode())" % target
    return docker('exec', CLIENT, 'python3', '-c', code, check=False)


def assert_no_plain_leak():
    code = "import subprocess; s=subprocess.check_output(['iptables','-L','PROOF_NO_MAIN','-n','-v','-x'],text=True); assert all(int(l.split()[0]) == 0 for l in s.splitlines() if 'DROP' in l)"
    docker('exec', GW, 'python3', '-c', code)


def main():
    created = []
    network_created = False
    try:
        docker('network', 'create', '--internal', '--label', LABEL + '=' + TOKEN, NETWORK)
        network_created = True
        for name in (GW, SERVER, CLIENT):
            args = ['run', '-d', '--name', name, '--label', LABEL + '=' + TOKEN, '--network', NETWORK,
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
                    '--sysctl', 'net.ipv4.ip_forward=1', '--sysctl', 'net.ipv4.conf.all.rp_filter=0',
                    '--sysctl', 'net.ipv4.conf.default.rp_filter=0']
            for cap in CAPS:
                args += ['--cap-add', cap]
            docker(*args, '--entrypoint', 'sh', IMAGE, '-ec', 'exec sleep 3600')
            created.append(name)
        gw_ip, server_ip, client_ip = map(address, (GW, SERVER, CLIENT))
        for name in created:
            info = json.loads(docker('inspect', name).stdout)[0]
            assert not info['HostConfig']['Privileged'] and not info['HostConfig'].get('Binds')
            assert info['HostConfig']['NetworkMode'] == NETWORK
        execute(GW, 'ip address add 10.200.250.1/32 dev lo; ip route add 10.200.250.250/32 via ' + client_ip)
        execute(CLIENT, 'ip address add 10.200.250.250/32 dev lo; ip address add 10.99.0.1/32 dev lo; ip route add 10.200.250.1/32 via ' + gw_ip + '; ip link add clientipip type ipip local 10.200.250.250 remote 10.200.250.1; ip link set clientipip mtu 1280 up; ip route add 1.1.1.1/32 dev clientipip src 10.200.250.250; ip route add 192.168.9.1/32 dev clientipip src 10.200.250.250')
        for name in (GW, SERVER):
            execute(name, 'umask 077; wg genkey > /tmp/proof-private; wg pubkey < /tmp/proof-private > /tmp/proof-public')
        gw_public = execute(GW, 'cat /tmp/proof-public').stdout.strip()
        server_public = execute(SERVER, 'cat /tmp/proof-public').stdout.strip()
        # Only public keys cross the Docker API; no private-key argument/stdout.
        execute(SERVER, 'ip link add proofwg type wireguard; wg set proofwg private-key /tmp/proof-private listen-port 51820 peer ' + gw_public + ' allowed-ips 10.66.0.2/32; ip address add 10.66.0.1/24 dev proofwg; ip link set proofwg mtu 1280 up; ip address add 1.1.1.1/32 dev lo; ip address add 192.168.9.1/32 dev lo')
        http = "from http.server import BaseHTTPRequestHandler,HTTPServer\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  self.send_response(200); self.end_headers(); self.wfile.write(self.client_address[0].encode())\n def log_message(self,*a): pass\nHTTPServer(('0.0.0.0',8080),H).serve_forever()"
        docker('exec', '-d', SERVER, 'python3', '-c', http)
        execute(GW, 'for c in FFDD_EXIT_FORWARD FFDD_EXIT_INPUT FFDD_EXIT_OUTPUT; do iptables -N "$c"; done; iptables -t nat -N FFDD_EXIT_NAT; iptables -I FORWARD -j FFDD_EXIT_FORWARD; iptables -I INPUT -j FFDD_EXIT_INPUT; iptables -I OUTPUT -j FFDD_EXIT_OUTPUT; iptables -t nat -A POSTROUTING -j FFDD_EXIT_NAT; iptables -N PROOF_NO_MAIN; iptables -A PROOF_NO_MAIN -d 1.1.1.1 -o eth0 -j DROP; iptables -A FORWARD -j PROOF_NO_MAIN; iptables -A OUTPUT -j PROOF_NO_MAIN; ip route add 1.1.1.1/32 via ' + server_ip)
        # Locate installed modules without depending on image packaging layout.
        module = execute(GW, "python3 -c \"import pathlib; p=list(pathlib.Path('/usr/local').rglob('exit_service.py')); assert len(p)==1; print(p[0])\"").stdout.strip()
        assert module.startswith('/usr/local/') and module.endswith('/exit_service.py')
        config = "import json,pathlib; key=pathlib.Path('/tmp/proof-private').read_text().strip(); json.dump({'exits':[{'name':'offline','wireguard':{'privateKey':key,'addresses':['10.66.0.2/32'],'dns':['10.66.0.1'],'peer':{'publicKey':%r,'endpoint':%r}}}]},open('/tmp/proof-config.json','w'))" % (server_public, server_ip + ':51820')
        docker('exec', GW, 'python3', '-c', config)
        service_command = ('exec', '-d', '-e', 'EXIT_CONFIG_FILE=/tmp/proof-config.json', '-e', 'EXIT_STATUS_FILE=/tmp/exits-public.json', '-e', 'EXIT_NODE_IP=10.200.250.1', '-e', 'EXIT_MESH_SOURCE_NETS=10.200.0.0/16', '-e', 'MESH_INTERFACES=eth0', GW, 'python3', module)
        docker(*service_command)
        wait_for(lambda: status().get('reachable') is True, 'WG end-to-end health failed')
        response = fetch()
        assert response.returncode == 0 and response.stdout.strip() == '10.66.0.2', 'IPIP/WG/NAT roundtrip failed'
        print('PASS: FF client -> IPIP -> WG -> HTTP; return IPIP; observed source is WG NAT address')
        assert fetch('192.168.9.1').returncode != 0, 'private destination escaped policy'
        assert execute(CLIENT, 'ping -n -I 10.99.0.1 -c 1 -W 2 1.1.1.1', check=False).returncode != 0, 'untrusted source escaped policy'
        assert_no_plain_leak()
        print('PASS: private destination and invalid FF source denied')
        iface = status()['ifname']
        assert iface.startswith('wgx') and len(iface) == 13
        execute(GW, 'ip link set ' + iface + ' down')
        wait_for(lambda: status().get('phase') == 'unreachable', 'health failure did not close forwarding')
        assert fetch().returncode != 0, 'traffic survived WG-down'
        assert_no_plain_leak()
        execute(GW, 'ip link set ' + iface + ' up')
        wait_for(lambda: status().get('reachable') is True, 'same-profile health recovery failed')
        recovered = fetch()
        if recovered.returncode != 0 or recovered.stdout.strip() != '10.66.0.2':
            for command in ('ip -j route show table all', 'ip -j rule show', 'iptables -nvxL FFDD_EXIT_FORWARD', 'iptables -t nat -nvxL FFDD_EXIT_NAT'):
                print('SAFE DIAGNOSTIC:', command, execute(GW, command).stdout, flush=True)
            print('SAFE DIAGNOSTIC status:', json.dumps(status()), flush=True)
            raise AssertionError('HTTP/NAT did not recover after successful bound health probe')
        print('PASS: WG-down fails closed, no main-lane fallback; same-profile recovery')
        kill_controller = "import os,pathlib,signal; matches=[int(p.name) for p in pathlib.Path('/proc').iterdir() if p.name.isdigit() and (p/'cmdline').exists() and (p/'cmdline').read_bytes().split(bytes([0]))[:2] == [b'python3',%r]]; assert len(matches)==1; os.kill(matches[0],signal.SIGKILL); pathlib.Path('/tmp/exits-public.json').write_text('{}')" % module.encode()
        docker('exec', GW, 'python3', '-c', kill_controller)
        docker(*service_command)
        wait_for(lambda: status().get('reachable') is True, 'alias-owned SIGKILL crash recovery failed')
        assert fetch().stdout.strip() == '10.66.0.2', 'HTTP/NAT failed after controller crash recovery'
        assert_no_plain_leak()
        print('PASS: SIGKILL controller restart recovers alias-owned links, rules, routes and source guards')
        execute(GW, "printf '%s' '{invalid' > /tmp/proof-config.json")
        wait_for(lambda: status().get('phase') == 'error', 'invalid reload did not close exit')
        assert status().get('enabled') is True
        assert fetch().returncode != 0
        assert_no_plain_leak()
        print('PASS: invalid secret-config reload closes previous exit and reports sanitized error')
    finally:
        # Exact generated names plus ownership labels are checked before deletion.
        for name in reversed(created):
            info = docker('inspect', name, check=False)
            if info.returncode == 0:
                record = json.loads(info.stdout)[0]
                if record['Config']['Labels'].get(LABEL) != TOKEN or not name.startswith(TOKEN + '-'):
                    raise RuntimeError('container ownership mismatch; refusing cleanup')
                docker('rm', '-f', '-v', name)
        if network_created:
            info = json.loads(docker('network', 'inspect', NETWORK).stdout)[0]
            if info['Labels'].get(LABEL) != TOKEN or NETWORK != TOKEN + '-network':
                raise RuntimeError('network ownership mismatch; refusing cleanup')
            docker('network', 'rm', NETWORK)


if __name__ == '__main__':
    main()
