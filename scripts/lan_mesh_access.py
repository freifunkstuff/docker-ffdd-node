"""Pure configuration helpers for opt-in LAN access to the FFDD mesh.

No interface discovery or network mutation belongs here. Callers must validate
sources before installing any rules, including during entrypoint initialization.
"""
from __future__ import annotations

import ipaddress
from typing import Any

from node_config import LAN_MESH_ACCESS_CONFIG_SCHEMA, require_valid_config

LAN_MESH_TARGET = "10.200.0.0/16"

# Reject overlaps, not just networks whose first address falls in these ranges.
# Public unicast ranges may be explicitly trusted; RFC1918 is not a requirement.
_FORBIDDEN_SOURCES = tuple(ipaddress.IPv4Network(value) for value in (
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "224.0.0.0/4", "240.0.0.0/4",
    "10.200.0.0/15", "10.203.0.0/16",
))


def parse_allowed_sources(value: str, enabled: bool) -> list[str]:
    """Validate whitespace-separated canonical IPv4 CIDRs, preserving order.

    An empty list is allowed only while disabled. Nonempty input is validated
    even when disabled so a later enable cannot activate an invalid trust list.
    Trust is an explicit operator assertion, not inferred from private addressing.
    """
    if not isinstance(value, str):
        raise ValueError("LAN_MESH_ACCESS_SOURCES must be whitespace-separated IPv4 CIDRs")
    sources = value.split()
    if enabled and not sources:
        raise ValueError("LAN_MESH_ACCESS_SOURCES is required when LAN_MESH_ACCESS_ENABLED=true")
    result: list[str] = []
    for source in sources:
        try:
            network = ipaddress.IPv4Network(source, strict=True)
        except ValueError as exc:
            raise ValueError(f"LAN_MESH_ACCESS_SOURCES: invalid canonical IPv4 CIDR: {source}") from exc
        if source != str(network):
            raise ValueError(f"LAN_MESH_ACCESS_SOURCES: use canonical IPv4 CIDR: {network}")
        if any(network.overlaps(forbidden) for forbidden in _FORBIDDEN_SOURCES):
            raise ValueError(f"LAN_MESH_ACCESS_SOURCES: unsafe or mesh-overlapping source: {source}")
        if source not in result:
            result.append(source)
    return result


def allowed_sources(config: dict[str, Any]) -> list[str]:
    """Validate resolved registrar config and return only active trust sources."""
    access = config.get("lan_mesh_access", {"enabled": False, "sources": ""})
    sources = parse_allowed_sources(access["sources"], access["enabled"])
    return sources if access["enabled"] else []


def resolve_environment(
    *, defaults: dict[str, Any] | None = None, env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve the shared schema/defaults and validate before network setup.

    Returns the same nested configuration shape as the registrar schema loader;
    sources remain a string and can be converted using parse_allowed_sources.
    Invalid schema values raise SystemExit; invalid source trust raises ValueError.
    """
    config = require_valid_config(
        schema=LAN_MESH_ACCESS_CONFIG_SCHEMA, defaults=defaults, env=env, base_values={},
    )
    access = config["lan_mesh_access"]
    parse_allowed_sources(access["sources"], access["enabled"])
    return config
