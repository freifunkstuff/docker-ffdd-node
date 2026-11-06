#!/usr/bin/env python3
"""Explicit Docker integration test: real packets, two mesh LANs and one uplink.

Usage: python3 tests/integration_mesh_isolation.py [ffdd-node:local-mesh]
Never attaches the real mesh, mounts user data, or starts the registrar/bmxd.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "ffdd-node:local-mesh"
TOKEN = "ffdd-isolation-" + uuid.uuid4().hex[:10]
LABEL = "ffdd.isolation-test"
containers: list[str] = []
networks: list[str] = []


def docker(*args: str, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def execute(container: str, *args: str):
    return docker("exec", container, *args)


def command(container: str, script: str):
    return execute(container, "sh", "-ec", script)


def request(container: str, host: str, *, port=80, source=None, expect=True):
    code = f"""
import http.client,sys
try:
 c=http.client.HTTPConnection({host!r},{port},timeout=1.0,source_address={source!r})
 c.request('GET','/')
 r=c.getresponse()
 ok=r.status==200
 c.close()
 if not ok:
  print('UNEXPECTED_HTTP_STATUS',r.status); sys.exit(2)
 print('REACHABLE'); sys.exit(0)
except OSError as exc:
 print('CONNECTION_BLOCKED',type(exc).__name__); sys.exit(1)
"""
    result = docker("exec", container, "python3", "-c", code, check=False)
    correct = (result.returncode == 0) if expect else (result.returncode == 1 and result.stdout.startswith("CONNECTION_BLOCKED"))
    if not correct:
        raise AssertionError(f"{container}: HTTP {host}:{port}, source={source}, expected reachable={expect}: {result.stderr}")
    print(("ALLOWED" if expect else "BLOCKED") + f": {container.removeprefix(TOKEN)} -> {host}:{port}", flush=True)


SERVERS = """
import http.server,socketserver,socket,threading
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(b'isolation-test')
 def log_message(self,*args): pass
class Echo(socketserver.BaseRequestHandler):
 def handle(self): self.request[1].sendto(b'mesh-echo',self.client_address)
for port in (80,8081):
 s=http.server.ThreadingHTTPServer(('0.0.0.0',port),Handler)
 threading.Thread(target=s.serve_forever,daemon=True).start()
u=socketserver.ThreadingUDPServer(('0.0.0.0',4305),Echo)
u.serve_forever()
"""

try:
    # Private, distinct benchmark subnets; --internal prevents external egress.
    for suffix, subnet in (("mesh", "198.18.106.0/24"), ("uplink", "198.19.106.0/24"), ("mesh2", "198.18.107.0/24")):
        name = TOKEN + "-" + suffix
        subnet6 = {"mesh": "fd42:18:106::/64", "uplink": "fd42:19:106::/64", "mesh2": "fd42:18:107::/64"}[suffix]
        docker("network", "create", "--internal", "--ipv6", "--subnet", subnet, "--subnet", subnet6, "--label", f"{LABEL}={TOKEN}", name)
        networks.append(name)
    mesh_net, uplink_net, mesh2_net = networks
    node, client, uplink, peer = [TOKEN + "-" + suffix for suffix in ("node", "client", "uplink", "peer")]
    for name, network, address in ((node,uplink_net,"198.19.106.2"),(client,mesh_net,"198.18.106.3"),(uplink,uplink_net,"198.19.106.3"),(peer,mesh2_net,"198.18.107.3")):
        docker("create", "--name", name, "--label", f"{LABEL}={TOKEN}", "--network", network, "--ip", address,
               "--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.ip_forward=" + ("1" if name == node else "0"),
               "--sysctl", "net.ipv4.conf.all.rp_filter=1", "--sysctl", "net.ipv4.conf.default.rp_filter=1",
               "--sysctl", "net.ipv6.conf.all.forwarding=0", "--entrypoint", "sleep", IMAGE, "600")
        containers.append(name)
    docker("network", "connect", "--ip", "198.18.106.2", mesh_net, node)
    docker("network", "connect", "--ip", "198.18.107.2", mesh2_net, node)
    for name in containers:
        docker("start", name)
        docker("exec", "-d", name, "python3", "-c", SERVERS)
    links = json.loads(execute(node, "ip", "-j", "addr").stdout)
    by_address = {a["local"]: link["ifname"].split("@")[0] for link in links for a in link.get("addr_info", [])}
    for address, name in (("198.18.106.2", "mesh0"), ("198.18.107.2", "mesh1")):
        original = by_address[address]
        command(node, f"ip link set {original} down; ip link set {original} name {name}; ip link set {name} up")
    command(node, f"ip route add default via 198.19.106.3 dev {by_address['198.19.106.2']}")
    command(peer, "ip route add 198.18.106.0/24 via 198.18.107.2")
    command(client, "ip addr add 10.201.1.10/16 broadcast 10.255.255.255 dev eth0; "
                    "ip addr add 192.0.2.10/32 dev eth0; "
                    "ip route add 198.19.106.0/24 via 198.18.106.2; "
                    "ip route add 203.0.113.10/32 via 198.18.106.2; "
                    "ip route add 198.18.107.0/24 via 198.18.106.2")
    command(uplink, "ip addr add 203.0.113.10/32 dev eth0; "
                    "ip route add 198.18.106.0/24 via 198.19.106.2; "
                    "ip route add 10.201.0.0/16 via 198.19.106.2")

    # Before policy/L3 initialization, reproduce the real no-IPAM mesh link.
    # A peer knows the MAC and sends unicast, so an ARP failure cannot fake safety.
    link = json.loads(execute(node, "ip", "-j", "link", "show", "dev", "mesh0").stdout)[0]
    command(client, f"ip neigh replace 198.18.106.2 lladdr {link['address']} dev eth0 nud permanent")
    command(node, "ip addr del 198.18.106.2/24 dev mesh0")
    counter_code = "from pathlib import Path; lines=Path('/proc/net/netstat').read_text().splitlines(); d={}; [d.update(dict(zip(lines[i].split()[1:], map(int,lines[i+1].split()[1:])))) for i in range(0,len(lines)-1,2)]; print(d.get('IPReversePathFilter',0))"
    before = int(execute(node, "python3", "-c", counter_code).stdout)
    execute(client, "python3", "-c", "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('10.201.1.10',0)); s.sendto(b'startup-isolation',('203.0.113.10',4305))")
    after = int(execute(node, "python3", "-c", counter_code).stdout)
    assert after > before, 'unaddressed mesh startup packet was not rejected by runtime rp_filter'
    print("BLOCKED before policy: unaddressed mesh ingress (strict runtime reverse-path filter counter increased)", flush=True)
    command(node, "ip addr add 198.18.106.2/24 dev mesh0; ip addr add 10.201.3.233/16 broadcast 10.255.255.255 dev mesh0")

    node_info = json.loads(docker("inspect", node).stdout)[0]
    node_mesh6 = node_info["NetworkSettings"]["Networks"][mesh_net]["GlobalIPv6Address"]
    assert node_mesh6
    # Linux may drop global IPv6 addresses when a link is taken down to rename.
    command(node, f"ip -6 addr replace {node_mesh6}/64 dev mesh0 nodad")
    execute(client, "ping", "-6", "-c", "1", "-W", "2", node_mesh6)
    print("ALLOWED before policy: IPv6 mesh echo", flush=True)

    # Positive controls: routing really works without the firewall. These are
    # synthetic isolated networks, not the host's LAN or the real FFDD mesh.
    request(client, "203.0.113.10")
    request(client, "198.19.106.3")
    request(uplink, "198.18.106.3")
    request(client, "198.19.106.2")
    request(client, "10.201.3.233", port=8081)
    request(client, "198.18.107.3")
    request(node, "203.0.113.10", source=("10.201.3.233",0))
    command(node, "ip route add 192.0.2.10/32 dev mesh0")
    request(node, "192.0.2.10")
    execute(node, "python3", "-c", "import sys; sys.path.insert(0,'/usr/local/bin'); from mesh_firewall import apply_mesh_forwarding_guard; apply_mesh_forwarding_guard(['mesh0','mesh1']); apply_mesh_forwarding_guard(['mesh0','mesh1'])")

    request(client, "10.201.3.233")
    request(uplink, "198.19.106.2")
    request(node, "203.0.113.10")
    request(client, "198.18.107.3")
    request(client, "203.0.113.10", expect=False)
    request(client, "198.19.106.3", expect=False)
    request(uplink, "198.18.106.3", expect=False)
    request(client, "198.19.106.2", expect=False)
    request(client, "10.201.3.233", port=8081, expect=False)
    request(node, "203.0.113.10", source=("10.201.3.233",0), expect=False)

    # A private address maliciously/mistakenly routed onto mesh must not leak.
    request(node, "192.0.2.10", expect=False)
    execute(client, "python3", "-c", "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1); s.bind(('10.201.1.10',0)); s.sendto(b'probe',('10.255.255.255',4305)); assert s.recvfrom(128)[0]==b'mesh-echo'")
    print("ALLOWED: bmxd UDP broadcast and reply",flush=True)
    ipv6 = docker("exec", client, "ping", "-6", "-c", "1", "-W", "1", node_mesh6, check=False)
    assert ipv6.returncode != 0, "IPv6 mesh unexpectedly bypassed isolation"
    print("BLOCKED after policy: IPv6 mesh echo", flush=True)
    print(execute(node, "iptables", "-nvL", "FFDD_NODE_FORWARD").stdout,flush=True)
    print("PASS: real-packet mesh/uplink isolation, web on both interfaces, Node uplink retained; no real network attached",flush=True)
finally:
    # Remove only resources bearing this invocation's exact ownership label.
    for name in reversed(containers):
        result=docker("inspect",name,check=False)
        if result.returncode==0 and json.loads(result.stdout)[0]["Config"]["Labels"].get(LABEL)==TOKEN:
            docker("rm","-f","-v",name,check=False)
    for name in reversed(networks):
        result=docker("network","inspect",name,check=False)
        if result.returncode==0 and json.loads(result.stdout)[0].get("Labels",{}).get(LABEL)==TOKEN:
            docker("network","rm",name,check=False)
