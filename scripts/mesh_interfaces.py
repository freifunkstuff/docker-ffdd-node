"""Reconcile FFDD addresses on runtime-owned links, never create/delete links.

Only addresses recorded in our runtime state are removed on readdressing. Other
addresses and routes belong to the runtime. A missing link is a retryable state.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from node_config import node_addresses

NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}\Z")


def parse_mesh_interfaces(value: str, *, primary_interface: str = "bmx_prime",
                          backbone_interfaces=(), wireguard_interface="tbbwg",
                          wireguard_prefix="tbb_wg") -> list[str]:
    names = list(dict.fromkeys(value.split()))
    reserved = {"lo", primary_interface, wireguard_interface, "tbb_fastd", *backbone_interfaces}
    for name in names:
        if not NAME.fullmatch(name):
            raise ValueError(f"MESH_INTERFACES: invalid interface name {name!r}")
        if name in reserved or name.startswith(wireguard_prefix):
            raise ValueError(f"MESH_INTERFACES: {name} conflicts with a primary/backbone interface")
    return names


@dataclass
class MeshResult:
    ready: bool
    changed: bool
    state: dict


def _ip(*args):
    return subprocess.run(["ip", *args], check=True, capture_output=True, text=True)


def _json(*args):
    return json.loads(_ip("-j", *args).stdout)


def ensure_mesh_interfaces(names: list[str], node_id: int, *, primary_interface="bmx_prime",
                           backbone_interfaces=(), previous: dict | None = None,
                           mtu: int = 1200, logger: Callable[[str], None] | None = None) -> MeshResult:
    previous = previous or {}
    parse_mesh_interfaces(" ".join(names), primary_interface=primary_interface,
                          backbone_interfaces=backbone_interfaces)
    if not names:
        return MeshResult(True, False, {})
    state = {}
    changed = False
    desired = node_addresses(node_id)["nonprimary_ip"]
    try:
        routes = [*_json("-4", "route", "show", "table", "all"),
                  *_json("-6", "route", "show", "table", "all")]
        uplinks = set()
        for route in routes:
            if route.get("dst") in ("default", "0.0.0.0/0", "::/0"):
                uplinks.add(route.get("dev"))
                uplinks.update(hop.get("dev") for hop in route.get("nexthops", []))
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        state = {name: {"status": f"waiting: cannot inspect uplink routes: {exc}"} for name in names}
        if logger:
            logger("mesh interfaces waiting: cannot inspect uplink routes")
        return MeshResult(False, state != previous, state)
    for name in names:
        old = previous.get(name, {})
        entry = dict(old)
        state[name] = entry
        try:
            if name in uplinks:
                raise ValueError("interface carries a default route; refusing uplink conflict")
            try:
                links = _json("link", "show", "dev", name)
            except subprocess.CalledProcessError as exc:
                raise ValueError("external interface is absent or unavailable; retrying") from exc
            if not links:
                raise ValueError("interface is absent")
            link = links[0]
            index = link["ifindex"]
            if link.get("master"):
                raise ValueError("interface is enslaved; supply the mesh L3 interface instead")
            # ip -4 addr may return [] for an existing link with no IPv4 yet.
            address_links = _json("-4", "addr", "show", "dev", name)
            addresses = address_links[0].get("addr_info", []) if address_links else []
            # Do not flush runtime IPAM addresses. Remove only our recorded old IP.
            owned = old.get("address")
            if owned and owned != desired and old.get("ifindex") == index:
                if any(a.get("local") == owned and a.get("prefixlen") == 16 for a in addresses):
                    _ip("addr", "del", f"{owned}/16", "dev", name)
                    changed = True
            if link.get("mtu") != mtu or "UP" not in link.get("flags", []):
                _ip("link", "set", "dev", name, "mtu", str(mtu), "up")
                changed = True
            if not any(a.get("local") == desired and a.get("prefixlen") == 16
                       and a.get("broadcast") == "10.255.255.255" for a in addresses):
                _ip("addr", "replace", f"{desired}/16", "broadcast", "10.255.255.255",
                    "dev", name)
                changed = True
            state[name] = {"status": "ready", "ifindex": index, "address": desired}
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError) as exc:
            entry["status"] = f"waiting: {exc}"
        if logger and state[name] != old:
            logger(f"mesh interface {name}: {state[name]['status']}")
    return MeshResult(all(item["status"] == "ready" for item in state.values()),
                      changed or state != previous, state)
