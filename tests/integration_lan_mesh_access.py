#!/usr/bin/env python3
"""Explicit isolated packet test; run with python3 tests/integration_lan_mesh_access.py [image].

Only synthetic internal Docker networks are used. No host networking, data
mounts, registry, bmxd, or real mesh. Requires Docker and NET_ADMIN.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "ffdd-node:local-mesh"
TOKEN = "ffdd-lan-access-" + uuid.uuid4().hex[:10]
LABEL = "ffdd.lan-access-test"
PRIMARY = "10.200.3.149"
containers: list[str] = []
networks: list[str] = []


def docker(*args: str, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"docker {args}: exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}")
    return result


def execute(name: str, *args: str):
    return docker("exec", name, *args)


def command(name: str, script: str):
    return execute(name, "sh", "-ec", script)


SERVERS = """
import http.server,json
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps({'sourceIP':self.client_address[0]}).encode()
  self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
 def log_message(self,*args): pass
http.server.ThreadingHTTPServer(('0.0.0.0',80),Handler).serve_forever()
"""


def request(name: str, host: str, *, expect=True, source_ip=None):
    # Only the explicit socket-failure sentinel counts as blocked. Docker exec,
    # Python errors, invalid JSON and non-200 HTTP responses must fail the test.
    code = f"""
import http.client,json
try:
 c=http.client.HTTPConnection({host!r},80,timeout=1.5)
 c.request('GET','/')
 r=c.getresponse()
 if r.status!=200: raise RuntimeError('UNEXPECTED_HTTP_STATUS '+str(r.status))
 body=json.loads(r.read()); c.close()
except OSError as exc:
 print('PACKET_BLOCKED:'+type(exc).__name__); raise SystemExit(23)
print('PACKET_OK:'+json.dumps(body))
"""
    result = docker("exec", name, "python3", "-c", code, check=False)
    if expect:
        assert result.returncode == 0 and result.stdout.startswith("PACKET_OK:"), (name, host, result)
        body = json.loads(result.stdout.removeprefix("PACKET_OK:"))
        if source_ip is not None:
            assert body["sourceIP"] == source_ip, (host, body, source_ip)
    else:
        assert result.returncode == 23 and result.stdout.startswith("PACKET_BLOCKED:"), (name, host, result)
    print(f"{'ALLOWED' if expect else 'BLOCKED'}: {name.removeprefix(TOKEN)} -> {host}" +
          (f" (server saw {body['sourceIP']})" if expect else ""), flush=True)


def policy(node: str, enabled=False):
    options = f", lan_sources=['198.18.110.3/32'], node_ip={PRIMARY!r}" if enabled else ""
    execute(node, "python3", "-c", "import sys; sys.path.insert(0,'/usr/local/bin'); "
            "from mesh_firewall import apply_mesh_forwarding_guard; "
            f"apply_mesh_forwarding_guard(['mesh0']{options})")


def rule_packets(node: str, table: str, chain: str, match: str):
    lines = execute(node, "iptables", "-t", table, "-nvxL", chain).stdout.splitlines()
    return sum(int(line.split()[0]) for line in lines if match in line)


def main():
    try:
        for suffix, subnet in (("lan", "198.18.110.0/24"), ("mesh", "198.19.110.0/24")):
            name = TOKEN + "-" + suffix
            docker("network", "create", "--internal", "--subnet", subnet,
                   "--label", f"{LABEL}={TOKEN}", name)
            networks.append(name)
        lan, mesh = networks
        node, allowed, untrusted, peer = [TOKEN + "-" + s for s in ("node", "allowed", "untrusted", "peer")]
        for name, network, ip in ((node, lan, "198.18.110.2"), (allowed, lan, "198.18.110.3"),
                                  (untrusted, lan, "198.18.110.4"), (peer, mesh, "198.19.110.3")):
            docker("create", "--name", name, "--label", f"{LABEL}={TOKEN}", "--network", f"name={network},driver-opt=com.docker.network.endpoint.sysctls=net.ipv4.conf.IFNAME.rp_filter=0",
                   "--ip", ip, "--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.ip_forward=" + ("1" if name == node else "0"),
                   "--sysctl", "net.ipv4.conf.all.rp_filter=0", "--sysctl", "net.ipv4.conf.default.rp_filter=0",
                   "--sysctl", "net.ipv4.conf.lo.rp_filter=0", "--entrypoint", "sleep", IMAGE, "600")
            containers.append(name)
        docker("network", "connect", "--driver-opt", "com.docker.network.endpoint.sysctls=net.ipv4.conf.IFNAME.rp_filter=0", "--ip", "198.19.110.2", mesh, node)
        for name in containers:
            docker("start", name)
            execute(name, "python3", "-c", "from pathlib import Path; values={str(p):p.read_text().strip() for p in Path('/proc/sys/net/ipv4/conf').glob('*/rp_filter')}; assert all(v=='0' for v in values.values()), values")
            docker("exec", "-d", name, "python3", "-c", SERVERS)
        links = json.loads(execute(node, "ip", "-j", "addr").stdout)
        by_ip = {a['local']: link['ifname'].split('@')[0] for link in links for a in link.get('addr_info', [])}
        original = by_ip['198.19.110.2']
        command(node, f"ip link set {original} down; ip link set {original} name mesh0; ip link set mesh0 up; "
                "ip addr add 10.201.3.149/16 dev mesh0; ip link add bmx_prime type dummy; ip link set bmx_prime up; "
                f"ip addr add {PRIMARY}/32 dev bmx_prime; ip route replace default via 198.18.110.1; "
                f"ip route add 10.200.0.0/16 via 198.19.110.3 src {PRIMARY}; "
                "ip route add 203.0.113.10/32 via 198.18.110.4")
        command(peer, "ip addr add 10.201.5.194/16 dev eth0; ip addr add 10.200.5.194/32 dev eth0; "
                "ip route add 10.200.0.0/16 via 198.19.110.2; ip route add 198.18.110.0/24 via 198.19.110.2")
        for name in (allowed, untrusted):
            command(name, "ip route add 10.200.0.0/16 via 198.18.110.2; ip route add 10.201.0.0/16 via 198.18.110.2; ip route add 198.19.110.0/24 via 198.18.110.2")
        command(allowed, "ip route add 203.0.113.10/32 via 198.18.110.2")
        command(untrusted, "ip addr add 203.0.113.10/32 dev eth0")
        # Startup readiness is explicit and does not mistake exec errors for packet blocking.
        for name in containers:
            execute(name, "python3", "-c", "import socket,time\nfor i in range(50):\n try:\n  s=socket.create_connection(('127.0.0.1',80),.1); s.close(); break\n except OSError: time.sleep(.1)\nelse: raise RuntimeError('HTTP server not ready')")
        print("Positive controls without policy", flush=True)
        request(allowed, "10.200.5.194", source_ip="198.18.110.3")
        request(untrusted, "10.200.5.194", source_ip="198.18.110.4")
        request(allowed, "10.201.5.194", source_ip="198.18.110.3")
        request(peer, "198.18.110.3", source_ip="198.19.110.3")
        request(allowed, "203.0.113.10", source_ip="198.18.110.3")
        policy(node)
        request(allowed, "10.200.5.194", expect=False)
        request(peer, "198.18.110.3", expect=False)
        command(peer, "ip route del 198.18.110.0/24")
        policy(node, True)
        request(allowed, "10.200.5.194", source_ip=PRIMARY)
        request(allowed, PRIMARY, source_ip="198.18.110.3")
        request(untrusted, "10.200.5.194", expect=False)
        request(allowed, "10.201.5.194", expect=False)
        request(allowed, "203.0.113.10", expect=False)
        command(peer, "ip route add 198.18.110.0/24 via 198.19.110.2")
        request(peer, "198.18.110.3", expect=False)
        # Spoof a trusted LAN source from mesh ingress. Mesh-to-mesh forwarding
        # is intentionally possible, but neither authorization nor SNAT is.
        command(peer, "ip addr add 198.18.110.3/32 dev eth0; ip route add 10.200.6.200/32 via 198.19.110.2")
        command(node, "ip route add 10.200.6.200/32 via 198.19.110.3")
        nat_before = rule_packets(node, "nat", "FFDD_NODE_LAN_NAT", "SNAT")
        auth_before = rule_packets(node, "mangle", "FFDD_NODE_LAN_AUTH", "CONNMARK")
        forward_before = rule_packets(node, "filter", "FFDD_NODE_FORWARD", "ACCEPT")
        execute(peer, "python3", "-c", "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('198.18.110.3',0)); s.sendto(b'spoof-test',('10.200.6.200',45999))")
        assert rule_packets(node, "filter", "FFDD_NODE_FORWARD", "ACCEPT") > forward_before, 'spoof packet did not traverse Node'
        assert rule_packets(node, "nat", "FFDD_NODE_LAN_NAT", "SNAT") == nat_before, 'mesh spoof was SNATed'
        assert rule_packets(node, "mangle", "FFDD_NODE_LAN_AUTH", "CONNMARK") == auth_before, 'mesh spoof was authorized'
        print("PASS: mesh-ingress source spoof traversed Node but gained no authorization/SNAT", flush=True)
        command(peer, "ip addr del 198.18.110.3/32 dev eth0")
        policy(node)
        request(allowed, "10.200.5.194", expect=False)
        for table, chain in (("filter", "FFDD_NODE_FORWARD"), ("nat", "FFDD_NODE_LAN_NAT"), ("mangle", "FFDD_NODE_LAN_AUTH")):
            print(execute(node, "iptables", "-t", table, "-nvxL", chain).stdout, flush=True)
        print("PASS: isolated real-packet LAN mesh access, primary-IP SNAT, scope/ingress restrictions and disable", flush=True)
    finally:
        for name in reversed(containers):
            result = docker("inspect", name, check=False)
            if result.returncode == 0 and json.loads(result.stdout)[0]['Config']['Labels'].get(LABEL) == TOKEN:
                docker("rm", "-f", "-v", name)
        for name in reversed(networks):
            result = docker("network", "inspect", name, check=False)
            if result.returncode == 0 and json.loads(result.stdout)[0].get('Labels', {}).get(LABEL) == TOKEN:
                docker("network", "rm", name)


if __name__ == '__main__':
    main()
