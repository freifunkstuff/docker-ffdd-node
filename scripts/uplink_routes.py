"""Opt-in IPv4 uplink handoff; call after the entry guard and before mesh-up.

Only main-table routes are changed. No files, links, addresses, sysctls or
firewall rules are written. Failure must leave the caller's mesh guard closed.
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import dataclass

from mesh_interfaces import NAME
from node_config import MESH_CONFIG_SCHEMA, require_valid_config

_FIELDS = ("interface", "gateway", "management_interface", "management_gateway", "management_routes")
UPLINK_CONFIG_SCHEMA = tuple(
    {"env": "NODE_UPLINK_" + field.upper() if not field.startswith("management_")
     else "NODE_" + field.upper(), "default_key": "NODE_UPLINK_" + field.upper()
     if not field.startswith("management_") else "NODE_" + field.upper(),
     "path": ("uplink", field), "type": "str", "default": "", "allow_blank": True}
    for field in _FIELDS
)


@dataclass(frozen=True)
class UplinkConfig:
    interface: str = ""
    gateway: str = ""
    management_interface: str = ""
    management_gateway: str = ""
    management_routes: tuple[str, ...] = ()


def _interface(name):
    if not NAME.fullmatch(name) or name == "lo" or name.startswith(("bmx", "tbb")):
        raise ValueError(f"unsafe uplink/management interface: {name!r}")
    return name


def parse_uplink_config(config):
    if isinstance(config, UplinkConfig):
        raw = {field: getattr(config, field) for field in _FIELDS}
    else:
        raw = config.get("uplink", config)
    values = {field: raw.get(field, "") for field in _FIELDS}
    if isinstance(config, dict):
        mesh_interfaces = config.get("mesh", {}).get("interfaces", "").split()
        for field in ("interface", "management_interface"):
            if values[field] and values[field] in mesh_interfaces:
                raise ValueError(f"{field} conflicts with configured mesh interface: {values[field]}")
    routes = values.pop("management_routes")
    routes = routes.split() if isinstance(routes, str) else routes
    prefixes = []
    for prefix in routes:
        network = ipaddress.IPv4Network(prefix, strict=True)
        if network.prefixlen == 0 or any(network.overlaps(ipaddress.IPv4Network(n))
                                        for n in ("10.200.0.0/15", "10.203.0.0/16")):
            raise ValueError(f"unsafe management prefix: {prefix}")
        prefixes.append(str(network))
    if any(values.values()) or prefixes:
        if not values["interface"] or not values["gateway"]:
            raise ValueError("uplink interface and gateway must both be supplied")
        _interface(values["interface"])
        if values["management_interface"]:
            _interface(values["management_interface"])
            if values["management_interface"] == values["interface"]:
                raise ValueError("management and uplink interfaces must differ")
        for field in ("gateway", "management_gateway"):
            if values[field]:
                values[field] = str(ipaddress.IPv4Address(values[field]))
    return UplinkConfig(**values, management_routes=tuple(dict.fromkeys(prefixes)))


def _ip(*args):
    return subprocess.run(["ip", *args], check=True, capture_output=True, text=True)


def _json(*args):
    return json.loads(_ip("-j", "-4", *args).stdout)


def _matches(route, dev, gateway):
    return (route.get("dev") == dev and route.get("gateway") == gateway
            and not route.get("nexthops") and route.get("type", "unicast") == "unicast")


def _onlink(interface, gateway):
    addresses = _json("addr", "show", "dev", interface)
    ip = ipaddress.IPv4Address(gateway)
    if not any(ip in ipaddress.IPv4Network(f"{a['local']}/{a['prefixlen']}", strict=False)
               and ip != ipaddress.IPv4Address(a["local"])
               for link in addresses for a in link.get("addr_info", [])
               if a.get("family") == "inet"):
        raise ValueError(f"gateway {gateway} is not on-link on {interface}")


def _default_args(route):
    args = ["default", "via", route["gateway"], "dev", route["dev"]]
    for key, keyword in (("metric", "metric"), ("protocol", "proto"), ("prefsrc", "src"), ("mtu", "mtu")):
        if key in route:
            args += [keyword, str(route[key])]
    return args


def configure_uplink_routes(config):
    """Return whether routes changed; validate the entire plan before mutation.

    With an already-moved default, management gateway recovery requires an
    explicit gateway or a unique existing via route on management_interface.
    There is intentionally no guessed subnet gateway or persistent route file.
    """
    config = parse_uplink_config(config)
    if not config.interface:
        return False
    routes = _json("route", "show", "table", "main")
    defaults = [r for r in routes if r.get("dst") in ("default", "0.0.0.0/0")]
    if len(defaults) != 1:
        raise ValueError("expected exactly one main-table IPv4 default")
    original = defaults[0]
    if not original.get("gateway") or not original.get("dev") or original.get("nexthops"):
        raise ValueError("unsupported original default route")
    moved = _matches(original, config.interface, config.gateway)
    management = config.management_interface or (original["dev"] if not moved else "")
    if management:
        _interface(management)
    if management == config.interface:
        raise ValueError("management and uplink interfaces must differ")
    _onlink(config.interface, config.gateway)
    additions = []
    if config.management_routes:
        if not management:
            raise ValueError("management interface required after default handoff")
        gateway = config.management_gateway
        if not gateway:
            if original["dev"] == management:
                gateway = original["gateway"]
            else:
                candidates = {r["gateway"] for r in routes if r.get("dev") == management
                              and r.get("gateway") and not r.get("nexthops")}
                if len(candidates) != 1:
                    raise ValueError("management gateway missing or ambiguous; supply NODE_MANAGEMENT_GATEWAY")
                gateway = candidates.pop()
        _onlink(management, gateway)
        for prefix in config.management_routes:
            existing = [r for r in routes if r.get("dst") not in (None, "default")
                        and str(ipaddress.IPv4Network(r["dst"], strict=False)) == prefix]
            if existing and not all(_matches(r, management, gateway) for r in existing):
                raise ValueError(f"conflicting existing management route: {prefix}")
            if not existing:
                additions.append([prefix, "via", gateway, "dev", management])
    # Never replace unrelated routes; route add also detects races safely.
    for args in additions:
        _ip("-4", "route", "add", *args)
    if moved:
        return bool(additions)
    desired = dict(original, gateway=config.gateway, dev=config.interface)
    # Source selection must follow the LAN address, not the original Pod address.
    desired.pop("prefsrc", None)
    try:
        _ip("-4", "route", "replace", *_default_args(desired))
        after = _json("route", "show", "table", "main")
        verified = [r for r in after if r.get("dst") in ("default", "0.0.0.0/0")]
        if len(verified) != 1 or not _matches(verified[0], config.interface, config.gateway):
            raise ValueError("uplink default verification failed")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        try:
            _ip("-4", "route", "replace", *_default_args(original))
        except (OSError, subprocess.SubprocessError) as rollback:
            raise RuntimeError(f"uplink handoff failed ({exc}); default rollback failed ({rollback}); keep mesh down") from exc
        raise RuntimeError(f"uplink handoff failed; original default restored: {exc}") from exc
    return True


def main():
    config = require_valid_config(schema=UPLINK_CONFIG_SCHEMA + MESH_CONFIG_SCHEMA)
    try:
        configure_uplink_routes(config)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"uplink routes: {exc}") from exc


if __name__ == "__main__":
    main()
