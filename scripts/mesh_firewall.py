"""Mesh/uplink isolation in the Node's private network namespace.

Only owned chains are replaced. Startup policy precedes mesh L3 activation;
the runtime provides its namespace and initial ingress protection.
The uplink is never an Internet exit. Opt-in trusted LAN access is limited to
10.200.0.0/16, with connection-authorized return traffic and primary-IP SNAT.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess

from mesh_interfaces import parse_mesh_interfaces
from lan_mesh_access import LAN_MESH_TARGET, parse_allowed_sources

CHAIN = "FFDD_NODE_FORWARD"
INPUT_CHAIN = "FFDD_NODE_INPUT"
OUTPUT_CHAIN = "FFDD_NODE_OUTPUT"
MESH_NETWORKS = ("10.200.0.0/15", "10.203.0.0/16")
LAN_NAT_CHAIN = "FFDD_NODE_LAN_NAT"
LAN_AUTH_CHAIN = "FFDD_NODE_LAN_AUTH"
LAN_AUTH_MARK = "0x40000000/0x40000000"


def _uplink_interfaces() -> set[str]:
    uplinks = set()
    for family in ("-4", "-6"):
        result = subprocess.run(
            ["ip", "-j", family, "route", "show", "table", "all"],
            capture_output=True, text=True, check=True,
        )
        for route in json.loads(result.stdout):
            if route.get("dst") in ("default", "0.0.0.0/0", "::/0"):
                if route.get("dev"):
                    uplinks.add(route["dev"])
                uplinks.update(hop["dev"] for hop in route.get("nexthops", []) if hop.get("dev"))
    return uplinks


def _apply_filter(binary: str, restore_binary: str, rules: dict[str, tuple[str, list[str]]], *, table: str = "filter") -> None:
    """Atomically replace our chains without flushing host/runtime rules."""
    result = subprocess.run(
        [binary, "-w", "5", "-t", table, "-S"], capture_output=True, text=True, check=True,
    )
    existing = result.stdout.splitlines()
    lines = [f"*{table}"]
    for chain in rules:
        lines.extend([f":{chain} - [0:0]", f"-F {chain}"])
    for chain, (parent, entries) in rules.items():
        lines.extend(f"-A {chain} {entry}" for entry in entries)
        jump = f"-A {parent} -j {chain}"
        parent_rules = [rule for rule in existing if rule.startswith(f"-A {parent} ")]
        if not parent_rules or parent_rules[0] != jump or parent_rules.count(jump) != 1:
            # Reassert first-rule precedence, also after another component has
            # inserted an ACCEPT above our jump. Touch only our exact jump.
            lines.extend(f"-D {parent} -j {chain}" for _ in range(parent_rules.count(jump)))
            lines.append(f"-I {parent} 1 -j {chain}")
    lines.extend(["COMMIT", ""])
    subprocess.run(
        [restore_binary, "--wait", "5", "--noflush"],
        input="\n".join(lines), text=True, capture_output=True, check=True,
    )


def _primary_uplink() -> str:
    result = subprocess.run(["ip", "-j", "-4", "route", "show", "default"], capture_output=True, text=True, check=True)
    routes = json.loads(result.stdout)
    if not routes:
        raise ValueError("LAN mesh access needs a MAIN IPv4 default route")
    metric = min(route.get("metric", 0) for route in routes)
    devices = set()
    for route in routes:
        if route.get("metric", 0) == metric:
            if route.get("dev"):
                devices.add(route["dev"])
            devices.update(hop["dev"] for hop in route.get("nexthops", []) if hop.get("dev"))
    if len(devices) != 1:
        raise ValueError("LAN mesh access needs one unambiguous primary uplink")
    interface = devices.pop()
    # Reject reserved/backbone names and unsafe characters before rule rendering.
    parse_mesh_interfaces(interface)
    return interface


def apply_mesh_forwarding_guard(names: list[str], backbone_interfaces=(), *, lan_sources=(), node_ip: str | None = None) -> None:
    """Enable IPv4 mesh transit plus scoped local services; never an uplink exit.

    The web server remains available on mesh IPs and the ordinary uplink. Mesh
    ingress cannot address the Node's Docker/uplink IPs or unrelated services.
    New gateway/client features must explicitly extend this fail-closed policy.
    IPv6 mesh is currently unsupported and denied in both directions.
    """
    names = parse_mesh_interfaces(" ".join(names), backbone_interfaces=backbone_interfaces)
    conflicts = set(names) & _uplink_interfaces()
    if conflicts:
        raise ValueError("MESH_INTERFACES: refusing default-route uplink: " + ", ".join(sorted(conflicts)))
    interfaces = [*names, "tbb+"]

    incoming = []
    outgoing = ["-o lo -j RETURN"]
    forwarding = []
    authorization = []
    snat = []
    if lan_sources:
        sources = parse_allowed_sources(" ".join(lan_sources), True)
        if node_ip is None or ipaddress.IPv4Address(node_ip) not in ipaddress.IPv4Network(LAN_MESH_TARGET):
            raise ValueError("LAN mesh access requires the Node's primary FFDD IPv4 address")
        uplink = _primary_uplink()
        if uplink in names:
            raise ValueError("LAN mesh ingress cannot also be a mesh interface")
        for source in sources:
            # Only an original trusted uplink request can authorize return traffic.
            # Matching source IPs from a mesh peer must not gain this connmark.
            authorization.append(
                f"-i {uplink} -s {source} -d {LAN_MESH_TARGET} -m conntrack --ctstate NEW --ctdir ORIGINAL "
                f"-j CONNMARK --set-xmark {LAN_AUTH_MARK}"
            )
            reply = f"-o {uplink} -d {source} -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -m connmark --mark {LAN_AUTH_MARK}"
            # Replies from the Node's own FFDD web address need the same exception.
            outgoing.append(f"{reply} -j RETURN")
            for interface in interfaces:
                original = f"-i {uplink} -o {interface} -s {source} -d {LAN_MESH_TARGET} -m conntrack --ctdir ORIGINAL -m connmark --mark {LAN_AUTH_MARK}"
                forwarding.extend([f"{original} -j ACCEPT", f"-i {interface} {reply} -j ACCEPT"])
                snat.append(
                    f"-o {interface} -s {source} -d {LAN_MESH_TARGET} -m connmark --mark {LAN_AUTH_MARK} "
                    f"-j SNAT --to-source {node_ip}"
                )
    for interface in interfaces:
        # Link-local routing announcements are broadcast. All other local mesh
        # services are restricted to the Node's FFDD address realms.
        for broadcast in ("10.255.255.255/32", "255.255.255.255/32"):
            incoming.append(f"-i {interface} -d {broadcast} -p udp --dport 4305:4307 -j ACCEPT")
        for network in MESH_NETWORKS:
            incoming.extend([
                f"-i {interface} -d {network} -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
                f"-i {interface} -d {network} -p icmp -j ACCEPT",
                f"-i {interface} -d {network} -p udp --dport 4305:4307 -j ACCEPT",
                f"-i {interface} -d {network} -p tcp --dport 80 -j ACCEPT",
            ])
            outgoing.append(f"-o {interface} -d {network} -j RETURN")
        # WireGuard backbone transports the per-peer mesh adjacency via IPIP.
        incoming.append(f"-i {interface} -d 10.203.0.0/16 -p 4 -j ACCEPT")
        incoming.append(f"-i {interface} -j DROP")
        for broadcast in ("10.255.255.255/32", "255.255.255.255/32"):
            outgoing.append(f"-o {interface} -d {broadcast} -p udp --dport 4305:4307 -j RETURN")
        outgoing.append(f"-o {interface} -j DROP")
        for destination in interfaces:
            forwarding.append(f"-i {interface} -o {destination} -j ACCEPT")
    incoming.append("-j RETURN")
    # ICMP errors or explicitly bound sockets must not leak mesh source IPs to
    # the ordinary uplink when a mesh route is missing.
    outgoing.extend(f"-s {network} -j DROP" for network in MESH_NETWORKS)
    outgoing.append("-j RETURN")
    forwarding.append("-j DROP")

    ipv6_incoming = [f"-i {interface} -j DROP" for interface in interfaces] + ["-j RETURN"]
    ipv6_outgoing = [f"-o {interface} -j DROP" for interface in interfaces] + ["-j RETURN"]
    _apply_filter("ip6tables", "ip6tables-restore", {
        INPUT_CHAIN: ("INPUT", ipv6_incoming),
        OUTPUT_CHAIN: ("OUTPUT", ipv6_outgoing),
        CHAIN: ("FORWARD", ["-j DROP"]),
    })
    _apply_filter("iptables", "iptables-restore", {
        INPUT_CHAIN: ("INPUT", incoming),
        OUTPUT_CHAIN: ("OUTPUT", outgoing),
        CHAIN: ("FORWARD", forwarding),
    })
    _apply_filter("iptables", "iptables-restore", {
        LAN_NAT_CHAIN: ("POSTROUTING", [*snat, "-j RETURN"]),
    }, table="nat")
    # Authorize new flows last: forwarding and SNAT must be installed before a
    # first trusted packet can receive the mark. Disabled mode clears both chains.
    _apply_filter("iptables", "iptables-restore", {
        LAN_AUTH_CHAIN: ("PREROUTING", [*authorization, "-j RETURN"]),
    }, table="mangle")


def prepare_mesh_startup(names: list[str]) -> None:
    """Quiesce supplied mesh links until policy and L3 configuration are ready."""
    names = parse_mesh_interfaces(" ".join(names))
    conflicts = set(names) & _uplink_interfaces()
    if conflicts:
        raise ValueError("MESH_INTERFACES: refusing default-route uplink: " + ", ".join(sorted(conflicts)))
    for name in names:
        result = subprocess.run(["ip", "-j", "link", "show", "dev", name], capture_output=True, text=True, check=False)
        if result.returncode == 0 and json.loads(result.stdout):
            subprocess.run(["ip", "link", "set", "dev", name, "down"], capture_output=True, text=True, check=True)
    apply_mesh_forwarding_guard(names)


if __name__ == "__main__":
    prepare_mesh_startup(parse_mesh_interfaces(os.environ.get("MESH_INTERFACES", "")))
