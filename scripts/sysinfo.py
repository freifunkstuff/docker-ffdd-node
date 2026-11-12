#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from backbone_runtime import (
    LivePeer, WIREGUARD_ENV_FILE, build_backbone_payload,
    load_runtime_env, parse_configured_peers,
)
from node_identity import effective_node_id, registration_mode
from system_resources import ResourceSnapshot, read_resources
from exit_observation import read_exit_counters
from node_config import (
    IDENTITY_CONFIG_SCHEMA,
    NODE_TYPE_CONFIG_SCHEMA,
    build_base_config,
    format_issues,
    load_defaults,
    load_state,
    node_addresses,
    resolve_config,
)


SYSINFO_RUNTIME_DIR = Path(os.environ.get("SYSINFO_RUNTIME_DIR", "/run/freifunk/sysinfo"))
SYSINFO_WEBROOT = Path(os.environ.get("SYSINFO_WEBROOT", "/run/freifunk/www"))
SYSINFO_OUTPUT = Path(os.environ.get("SYSINFO_OUTPUT", str(SYSINFO_RUNTIME_DIR / "sysinfo.json")))
NODES_OUTPUT = Path(os.environ.get("NODES_OUTPUT", str(SYSINFO_RUNTIME_DIR / "nodes.json")))
BACKBONE_OUTPUT = Path(os.environ.get("BACKBONE_OUTPUT", str(SYSINFO_RUNTIME_DIR / "backbone.json")))
SYSINFO_WEB_LINKS = {
    "sysinfo.json": "sysinfo",
    "sysinfo-json.cgi": "sysinfo",
    "nodes.json": "nodes",
    "backbone.json": "backbone",
}
GATEWAY_USAGE_PATH = Path(os.environ.get("GATEWAY_USAGE_PATH", "/data/statistic/gateway_usage"))
COMMUNITY_DEFAULT_DOMAINS = {
    "Dresden": "freifunk-dresden.de",
    "Leipzig": "freifunk-leipzig.de",
}

SYSINFO_CONFIG_SCHEMA: tuple[dict[str, object], ...] = (
    *IDENTITY_CONFIG_SCHEMA,
    *NODE_TYPE_CONFIG_SCHEMA,
    {
        "env": "SYSINFO_RESOURCE_SCOPE",
        "required": True,
        "default_key": "SYSINFO_RESOURCE_SCOPE",
        "path": ("resources", "scope"),
        "type": "str",
        "default": "auto",
        "enum": ("auto", "host", "container"),
        "allow_blank": False,
    },
    {
        "env": "NODE_CONTACT_EMAIL",
        "path": ("node", "contact", "email"),
        "type": "str",
        "required": True,
        "allow_blank": False,
    },
    {
        "env": "NODE_NAME",
        "aliases": ("NODE_CONTACT_NAME",),
        "path": ("node", "contact", "name"),
        "type": "str",
        "required": True,
        "allow_blank": False,
    },
    {
        "env": "NODE_CONTACT_LOCATION",
        "path": ("node", "contact", "location"),
        "type": "str",
        "default": "",
        "allow_blank": True,
    },
    {
        "env": "NODE_CONTACT_NOTE",
        "path": ("node", "contact", "note"),
        "type": "str",
        "default": "",
        "allow_blank": True,
    },
    {
        "env": "NODE_COMMUNITY",
        "path": ("node", "community"),
        "type": "str",
        "default": "Dresden",
        "enum": ("Dresden", "Leipzig"),
        "allow_blank": False,
    },
    {
        "env": "NODE_DOMAIN",
        "path": ("node", "common", "domain"),
        "type": "str",
        "default": "freifunk-dresden.de",
        "allow_blank": False,
    },
    {
        "env": "NODE_GROUP_ID",
        "path": ("node", "common", "group_id"),
        "type": "str",
        "default": "",
        "allow_blank": True,
    },
    {
        "env": "NODE_NETWORK_ID",
        "path": ("node", "common", "network_id"),
        "type": "str",
        "default": "0",
        "allow_blank": False,
    },
    {
        "env": "NODE_GPS_LATITUDE",
        "path": ("node", "gps", "latitude"),
        "type": "float",
        "required": True,
        "required_level": "warning",
        "min": -90.0,
        "max": 90.0,
    },
    {
        "env": "NODE_GPS_LONGITUDE",
        "path": ("node", "gps", "longitude"),
        "type": "float",
        "required": True,
        "required_level": "warning",
        "min": -180.0,
        "max": 180.0,
    },
    {
        "env": "NODE_GPS_ALTITUDE",
        "path": ("node", "gps", "altitude"),
        "type": "int",
        "default": 0,
        "min": -1000,
        "max": 10000,
    },
)


def log_info(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"{timestamp} [sysinfo] {message}", flush=True)


def require_valid_sysinfo_config(
    *,
    defaults: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
    base_values: dict | None = None,
    log_warnings: bool = True,
    logger=None,
) -> dict:
    defaults_map = load_defaults() if defaults is None else defaults
    env_map = dict(os.environ) if env is None else dict(env)

    effective_schema = _apply_community_domain_default(
        schema=SYSINFO_CONFIG_SCHEMA,
        defaults=defaults_map,
        env=env_map,
    )

    result = resolve_config(
        schema=effective_schema,
        defaults=defaults_map,
        env=env_map,
        base_values=build_base_config() if base_values is None else base_values,
    )

    if log_warnings:
        for warning in format_issues(result.warnings):
            if logger is not None:
                logger(f"config warning: {warning}")

    if result.errors:
        for error in format_issues(result.errors):
            if logger is not None:
                logger(f"config error: {error}")
        raise SystemExit(1)

    return result.values


def _apply_community_domain_default(*, schema: tuple[dict[str, object], ...], defaults: dict[str, object], env: dict[str, str]) -> tuple[dict[str, object], ...]:
    if _has_nonblank_value(env.get("NODE_DOMAIN")):
        return schema

    community = str(env.get("NODE_COMMUNITY") or "Dresden").strip()
    domain_default = COMMUNITY_DEFAULT_DOMAINS.get(community)
    if not domain_default:
        return schema

    updated_schema: list[dict[str, object]] = []
    for spec in schema:
        if spec.get("env") == "NODE_DOMAIN":
            updated_spec = dict(spec)
            updated_spec["default"] = domain_default
            updated_schema.append(updated_spec)
            continue
        updated_schema.append(spec)
    return tuple(updated_schema)


def _has_nonblank_value(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def render_stub_payload(config: dict, state: dict, nodes_payload: dict | None = None) -> dict:
    resources = _resource_snapshot(config)
    node_meta = _build_node_meta(config, state)
    os_release = _read_os_release()
    nodes_payload = build_nodes_payload(config, state) if nodes_payload is None else nodes_payload
    bmxd_data = _build_bmxd_block(nodes_payload["bmxd"])

    payload = {
        "version": "17",
        "timestamp": str(int(time.time())),
        "data": {
            "firmware": _build_firmware_block(os_release),
            "system": _build_system_block(config, resources),
            "resources": _public_resources(resources),
            "common": {
                "community": config["node"]["community"],
                "group_id": str(config["node"]["common"]["group_id"]),
                "node": node_meta["node"],
                "registration_mode": registration_mode(config),
                "domain": str(config["node"]["common"]["domain"]),
                "ip": node_meta["ip"],
                "network_id": str(config["node"]["common"]["network_id"]),
            },
            "backbone": {
                "fastd_pubkey": _derive_fastd_pubkey(state),
                "wg_pubkey": _wireguard_pubkey(state),
            },
            "gps": config["node"]["gps"],
            "contact": config["node"]["contact"],
            "statistic": _build_statistic_block(resources),
            "bmxd": bmxd_data,
        },
    }

    return payload


def build_nodes_payload(config: dict, state: dict) -> dict:
    node_meta = _build_node_meta(config, state)
    bmxd_data = _collect_bmxd_nodes_data()

    return {
        "timestamp": str(int(time.time())),
        "node": {
            "community": config["node"]["community"],
            "id": node_meta["node"],
            "registration_mode": registration_mode(config),
            "ip": node_meta["ip"],
            "domain": str(config["node"]["common"]["domain"]),
            "network_id": str(config["node"]["common"]["network_id"]),
        },
        "bmxd": bmxd_data,
    }


def _build_node_meta(config: dict, state: dict) -> dict[str, str]:
    addresses = node_addresses(0)
    node_id = effective_node_id(config, state)
    node_id_text = ""
    primary_ip = ""

    if node_id is not None:
        try:
            assigned_node_id = int(node_id)
        except (TypeError, ValueError):
            assigned_node_id = None
        else:
            node_id_text = str(assigned_node_id)
            addresses = node_addresses(assigned_node_id)
            primary_ip = addresses["primary_ip"]

    return {
        "node": node_id_text,
        "ip": primary_ip,
        "community": str(config["node"]["community"]),
    }


BUILD_IDENTITY_PATH = Path("/usr/local/share/freifunk/build-identity.json")


def _firmware_version() -> str:
    """Read image-owned identity; legacy explicit runtime override remains supported."""
    if "SYSINFO_FIRMWARE_VERSION" in os.environ:
        return os.environ["SYSINFO_FIRMWARE_VERSION"]
    try:
        identity = json.loads(BUILD_IDENTITY_PATH.read_text())
    except (OSError, ValueError):
        identity = {}
    if not isinstance(identity, dict):
        identity = {}
    ref_type = str(identity.get("ref_type") or "")
    ref = str(identity.get("ref") or "")
    commit = str(identity.get("commit") or "")
    version = str(identity.get("version") or "")
    if ref_type == "tag" and (version or ref):
        return "dockernode / " + (version or ref).removeprefix("v")
    if ref_type == "branch" and ref and commit:
        return f"dockernode / {ref} / {commit[:7]}"
    return "dockernode / development"


def _build_firmware_block(os_release: dict[str, str]) -> dict:
    distrib_id = os_release.get("ID") or os_release.get("NAME", "Alpine")
    distrib_release = os_release.get("VERSION_ID") or ""
    target = os.uname().machine
    description = os_release.get("PRETTY_NAME") or ""

    block = {
        "name": "dockernode",
        "version": _firmware_version(),
        "DISTRIB_ID": distrib_id,
        "DISTRIB_RELEASE": distrib_release,
        "DISTRIB_REVISION": "",
        "DISTRIB_CODENAME": "",
        "DISTRIB_TARGET": target,
        "DISTRIB_DESCRIPTION": description,
    }

    git_branch = os.environ.get("SYSINFO_GIT_DDMESH_BRANCH", "")
    if git_branch:
        block["git-ddmesh-branch"] = git_branch

    return block


def _resource_snapshot(config: dict | None = None) -> ResourceSnapshot:
    """Use validated configuration, retaining env fallback for manual callers."""
    scope = (config or {}).get("resources", {}).get("scope")
    if scope is None:
        scope = os.environ.get("SYSINFO_RESOURCE_SCOPE", "auto")
    return read_resources(scope=scope)


def _public_resources(snapshot: ResourceSnapshot) -> dict:
    result = {key: value for key, value in snapshot.as_dict().items()
              if value is not None and key != "sampled_at"}
    # Cgroup membership paths can contain workload identifiers. Publish only
    # source kinds, never absolute paths or hidden host topology.
    result["sources"] = {key: value.split(":", 1)[0]
                         for key, value in snapshot.sources.items()}
    return result


def _build_system_block(config: dict, snapshot: ResourceSnapshot | None = None) -> dict:
    snapshot = _resource_snapshot(config) if snapshot is None else snapshot
    block = {
        "uname": _safe_command(["uname", "-a"]),
        "nameserver": _read_nameservers(),
        "date": datetime.now().astimezone().strftime("%c %Z"),
        "board": os.environ.get("SYSINFO_BOARD", "docker"),
        "model": os.environ.get("SYSINFO_MODEL", "docker-alpine"),
        "model2": os.environ.get("SYSINFO_MODEL2", ""),
        "cpuinfo": _read_cpuinfo(),
        "cpuinfo_scope": "hardware-host",
        "bmxd": _safe_command(["bmxd", "-c", "status"]),
        "node_type": config["system"]["node_type"],
        "node_role": config['system'].get('node_role', 'node'),
        "autoupdate": config["system"]["autoupdate"],
    }
    # Legacy integer count rounds quota capacity up; exact CPU equivalents and
    # visible cpuset cardinality remain separately available in data.resources.
    count = (math.ceil(snapshot.cpu_capacity) if snapshot.scope == "container"
             and snapshot.cpu_capacity is not None else snapshot.cpu_count)
    if count is not None:
        block["cpucount"] = str(count)
    if snapshot.uptime_seconds is not None:
        block["uptime"] = _read_uptime_raw(snapshot)
        block["uptime_string"] = _format_uptime_string(snapshot.uptime_seconds)
    return block


def _build_statistic_block(snapshot: ResourceSnapshot | None = None) -> dict:
    snapshot = _resource_snapshot() if snapshot is None else snapshot
    block = {"meminfo_" + key: value for key, value in _effective_meminfo(snapshot).items()}
    block["gateway_usage"] = _read_gateway_usage()
    if snapshot.load_average is not None:
        block["cpu_load"] = " ".join(str(value) for value in snapshot.load_average)
    # Host /proc/stat is never a container CPU counter. Structured CPU seconds
    # are published from this same snapshot in data.resources instead.
    interfaces = _read_interface_stats()
    interfaces.update(_read_exit_interface_stats())
    if interfaces:
        block["interfaces"] = interfaces
    volume_path = Path(os.environ.get('EXIT_TRAFFIC_PUBLIC_FILE', '/run/freifunk/www/exit-traffic.json'))
    try:
        if volume_path.stat().st_size <= 65536:
            volume = json.loads(volume_path.read_text())
            totals = volume.get('totals', {})
            timestamps_valid = all(isinstance(volume.get(k), (int, float)) and not isinstance(volume.get(k), bool) and math.isfinite(volume[k]) and volume[k] >= 0 for k in ('started_at', 'updated_at'))
            if volume.get('version') == 1 and timestamps_valid and all(isinstance(totals.get(k), int) and not isinstance(totals.get(k), bool) and totals[k] >= 0 for k in ('rx_bytes', 'tx_bytes')):
                block['exit_traffic'] = {'rx_bytes_total': totals['rx_bytes'], 'tx_bytes_total': totals['tx_bytes'], 'started_at': volume.get('started_at'), 'updated_at': volume.get('updated_at')}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return block


def _build_bmxd_block(nodes_data: dict | None = None) -> dict:
    if nodes_data is None:
        nodes_data = _collect_bmxd_nodes_data()

    block = {
        "links": [],
        "gateways": {
            "selected": "",
            "preferred": "",
            "gateways": [],
        },
        "info": [],
    }

    block["links"] = [
        {
            "node": str(link["node"]),
            "ip": str(link["ip"]),
            "interface": str(link["interface"]),
            "rtq": str(link["rtq"]),
            "rq": str(link["rq"]),
            "tq": str(link["tq"]),
            "type": str(link["type"]),
        }
        for link in nodes_data["links"]
    ]
    block["gateways"] = {
        "selected": str(nodes_data["gateways"]["selected"]),
        "preferred": str(nodes_data["gateways"]["preferred"]),
        "gateways": [{"ip": str(gateway["ip"])} for gateway in nodes_data["gateways"]["gateways"]],
    }
    block["info"] = [str(line) for line in nodes_data["info"]]
    return block


def _collect_bmxd_nodes_data() -> dict:
    links = _parse_bmxd_links(_safe_command(["bmxd", "-c", "--links"]))
    gateway_usage = _read_gateway_usage_map()
    selected, preferred, gateways = _parse_bmxd_gateways(
        _safe_command(["bmxd", "-c", "--gateways"]),
        gateway_usage,
    )
    originators = _parse_bmxd_originators(_safe_command(["bmxd", "-c", "--originators"]))
    info = [line.strip() for line in _safe_command(["bmxd", "-c", "options"]).splitlines() if line.strip()]

    return {
        "links": links,
        "gateways": {
            "selected": selected,
            "preferred": preferred,
            "gateways": gateways,
        },
        "originators": originators,
        "info": info,
    }


def _parse_bmxd_links(raw: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    pattern = re.compile(
        r"^(?P<neighbor>\d+\.\d+\.\d+\.\d+)\s+(?P<iface>\S+)\s+(?P<originator>\d+\.\d+\.\d+\.\d+)\s+(?P<rtq>\d+)\s+(?P<rq>\d+)\s+(?P<tq>\d+)"
    )

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = pattern.match(stripped)
        if not match:
            continue
        originator = match.group("originator")
        interface = match.group("iface")
        links.append(
            {
                "node": str(_node_id_from_ip(originator)),
                "ip": originator,
                "neighbor_ip": match.group("neighbor"),
                "interface": interface,
                "rtq": match.group("rtq"),
                "rq": match.group("rq"),
                "tq": match.group("tq"),
                "type": _link_type_for_interface(interface),
            }
        )

    return links


def _parse_bmxd_gateways(raw: str, gateway_usage: dict[str, str] | None = None) -> tuple[str, str, list[dict[str, str | bool]]]:
    selected = ""
    preferred = ""
    gateways: list[dict[str, str | bool]] = []
    gateway_usage = {} if gateway_usage is None else gateway_usage

    gateway_pattern = re.compile(
        r"^(?P<selected>=?>)?\s*(?P<originator>\d+\.\d+\.\d+\.\d+)\s+"
        r"(?P<best_next_hop>\d+\.\d+\.\d+\.\d+)\s+"
        r"(?P<brc>\d+),\s*(?P<community>[01]),\s*(?P<speed>\S+)"
    )

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "preferred gateway" in stripped.lower():
            match = re.search(r"(\d+\.\d+\.\d+\.\d+)", stripped)
            if match:
                preferred = match.group(1)
            continue
        match = gateway_pattern.match(stripped)
        if match:
            originator = match.group("originator")
            is_selected = bool(match.group("selected"))
            if is_selected:
                selected = originator
            gateways.append(
                {
                    "node": str(_node_id_from_ip(originator)),
                    "ip": originator,
                    "best_next_hop": match.group("best_next_hop"),
                    "brc": match.group("brc"),
                    "community": match.group("community"),
                    "speed": match.group("speed"),
                    "selected": is_selected,
                    "preferred": originator == preferred,
                    "usage": gateway_usage.get(originator, "0"),
                }
            )

    return selected, preferred, gateways


def _parse_bmxd_originators(raw: str) -> list[dict[str, str]]:
    originators: list[dict[str, str]] = []
    pattern = re.compile(
        r"^(?P<originator>\d+\.\d+\.\d+\.\d+)\s+"
        r"(?P<iface>\S+)\s+"
        r"(?P<best_next_hop>\d+\.\d+\.\d+\.\d+)\s+"
        r"(?P<brc>\d+)"
    )

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = pattern.match(stripped)
        if not match:
            continue
        originator = match.group("originator")
        interface = match.group("iface")
        originators.append(
            {
                "node": str(_node_id_from_ip(originator)),
                "ip": originator,
                "interface": interface,
                "best_next_hop": match.group("best_next_hop"),
                "brc": match.group("brc"),
                "type": _link_type_for_interface(interface),
            }
        )

    return originators


def _wireguard_pubkey(state: dict) -> str:
    # Never derive or expose a private key here; registrar publishes the public
    # key in its runtime env even when persistent state contains only a secret.
    public_key = state.get("wireguard", {}).get("public_key")
    if not public_key:
        public_key = load_runtime_env(WIREGUARD_ENV_FILE).get("WIREGUARD_PUBLIC_KEY", "")
    return str(public_key).strip()


def _derive_fastd_pubkey(state: dict) -> str:
    secret = state.get("fastd", {}).get("secret")
    if not secret:
        return ""

    secret_text = str(secret).strip()
    if not secret_text:
        return ""

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(f'secret "{secret_text}";\n')
        config_path = handle.name

    try:
        result = subprocess.run(
            ["fastd", "--machine-readable", "--show-key", "--config", config_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    finally:
        Path(config_path).unlink(missing_ok=True)


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in _read_text(Path("/etc/os-release")).splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def _container_uptime_seconds(snapshot: ResourceSnapshot | None = None) -> float | None:
    """Compatibility wrapper; unknown PID 1 age never falls back to host age."""
    return (_resource_snapshot() if snapshot is None else snapshot).uptime_seconds


def _format_uptime_string(seconds: float) -> str:
    """Format uptime seconds as a human-readable string similar to `uptime`."""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    parts.append(f"{hours:02d}:{minutes:02d}")
    return "up " + ", ".join(parts)


def _read_uptime_raw(snapshot: ResourceSnapshot | None = None) -> str | None:
    """Legacy first uptime token only: idle accounting is unavailable."""
    uptime = _container_uptime_seconds(snapshot)
    return f"{uptime:.2f}" if uptime is not None else None


def _read_nameservers() -> list[str]:
    nameservers: list[str] = []
    for line in _read_text(Path("/etc/resolv.conf")).splitlines():
        stripped = line.strip()
        if not stripped.startswith("nameserver"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            nameservers.append(parts[1])
    return nameservers


def _read_cpuinfo() -> str:
    cpuinfo = _read_text(Path("/proc/cpuinfo"))
    values: dict[str, str] = {}
    for line in cpuinfo.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        text = value.strip()
        if text:
            values.setdefault(key.strip().lower(), text)
    for key in ("model name", "hardware", "system type", "processor"):
        if key in values:
            return values[key]
    return ""


def _effective_meminfo(snapshot: ResourceSnapshot | None = None) -> dict[str, str]:
    """Legacy kB fields, exclusively from the resource snapshot.

    MemFree means remaining limit-current for containers and MemAvailable for
    hosts. Buffers are omitted: the snapshot has no scope-consistent equivalent.
    """
    snapshot = _resource_snapshot() if snapshot is None else snapshot
    fields = {"MemTotal": snapshot.memory_limit_bytes,
              "MemFree": snapshot.memory_remaining_bytes,
              "Cached": snapshot.memory_cache_bytes}
    return {key: f"{value // 1024} kB" for key, value in fields.items()
            if value is not None}


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _read_gateway_usage() -> list[dict[str, str]]:
    content = _read_text(GATEWAY_USAGE_PATH)
    usage: list[dict[str, str]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            usage.append({key: value})
    return usage


def _read_gateway_usage_map() -> dict[str, str]:
    usage: dict[str, str] = {}
    for entry in _read_gateway_usage():
        usage.update(entry)
    return usage


def _read_exit_interface_stats() -> dict[str, str]:
    """Legacy VPN/FFGW interface counters; never fold them into backbone totals."""
    result: dict[str, str] = {}
    observed = read_exit_counters()
    for direction in ('rx_bytes', 'tx_bytes'):
        if observed and all(direction in counters for counters in observed.values()):
            result['vpn_' + direction[:2]] = str(sum(counters[direction] for counters in observed.values()))
    if Path('/sys/class/net/ffgw').exists():
        for direction in ('rx', 'tx'):
            value = _parse_int(_read_text(Path('/sys/class/net/ffgw/statistics') / (direction + '_bytes')).strip())
            if value is not None and value >= 0:
                result['ffgw_' + direction] = str(value)
    return result


def _read_interface_stats() -> dict[str, str]:
    interfaces: dict[str, str] = {}
    candidates = {
        "tbb_fastd",
    }

    for ifname in sorted(candidates):
        base = Path("/sys/class/net") / ifname / "statistics"
        rx = _read_text(base / "rx_bytes").strip()
        tx = _read_text(base / "tx_bytes").strip()
        if not rx or not tx:
            continue
        interfaces[f"{ifname}_rx"] = rx
        interfaces[f"{ifname}_tx"] = tx

    # Count the configured IPIP mesh tunnels, not the encrypted tbbwg
    # underlay as well. Preserve the legacy aggregate keys for consumers.
    _, peers = parse_configured_peers(load_runtime_env(WIREGUARD_ENV_FILE))
    tunnel_names = {peer.ifname for peer in peers.values()
                    if re.fullmatch(r"tbb_wg\d+", peer.ifname)}
    if os.environ.get('NODE_ROLE', 'node') == 'gateway':
        try:
            gateway_status = Path(os.environ.get('GATEWAY_RUNTIME_STATUS_FILE', '/run/freifunk/gateway/runtime.json'))
            if gateway_status.stat().st_size <= 1024 * 1024:
                applied = json.loads(gateway_status.read_text()).get('peers', {})
                if isinstance(applied, dict):
                    for entry in list(applied.values())[:512]:
                        if not isinstance(entry, dict):
                            continue
                        name = entry.get('ifname')
                        if entry.get('applied') is True and isinstance(name, str) and re.fullmatch(r'tbb_wg[0-9]{1,5}', name):
                            tunnel_names.add(name)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    wg_rx = wg_tx = 0
    have_wg = False
    for ifname in sorted(tunnel_names):
        base = Path("/sys/class/net") / ifname / "statistics"
        rx = _parse_int(_read_text(base / "rx_bytes").strip())
        tx = _parse_int(_read_text(base / "tx_bytes").strip())
        if rx is None or tx is None or rx < 0 or tx < 0:
            continue
        wg_rx += rx
        wg_tx += tx
        have_wg = True
    if have_wg:
        interfaces["tbb_wg_rx"] = str(wg_rx)
        interfaces["tbb_wg_tx"] = str(wg_tx)
    return interfaces


def _node_id_from_ip(ip_address: str) -> int:
    parts = ip_address.split(".")
    if len(parts) != 4:
        return 0
    try:
        third = int(parts[2])
        fourth = int(parts[3])
    except ValueError:
        return 0
    return third * 255 + (fourth - 1)


def _link_type_for_interface(interface: str) -> str:
    if re.match(r"^tbb_wg\d+$", interface):
        return "backbone"

    mapping = {
        "mesh_lan": "lan",
        "mesh_wan": "lan",
        "mesh_vlan": "lan",
        "tbb_fastd": "backbone",
        "tbb_wg": "backbone",
        "mesh2g-80211s": "wifi_mesh",
        "mesh5g-80211s": "wifi_mesh",
        "wifi_adhoc": "wifi_adhoc",
    }
    if interface in mapping:
        return mapping[interface]
    if interface in os.environ.get("MESH_INTERFACES", "").split():
        return "lan"
    return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _safe_command(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as handle:
        handle.write(content)
        temp_name = handle.name
    os.chmod(temp_name, 0o644)
    os.replace(temp_name, path)


def publish_web_links(output_path: Path, nodes_output_path: Path, backbone_output_path: Path, webroot: Path) -> None:
    webroot.mkdir(parents=True, exist_ok=True)
    targets = {
        "sysinfo": output_path,
        "nodes": nodes_output_path,
        "backbone": backbone_output_path,
    }

    for name, target_name in SYSINFO_WEB_LINKS.items():
        link_path = webroot / name
        if link_path.is_symlink() or link_path.exists():
            link_path.unlink()
        link_path.symlink_to(targets[target_name])


def render_once(
    output_path: Path,
    webroot: Path,
    nodes_output_path: Path,
    backbone_output_path: Path,
    config: dict | None = None,
    previous_wireguard_live_peers: dict[str, LivePeer] | None = None,
) -> dict[str, LivePeer]:
    config = require_valid_sysinfo_config(logger=log_info, log_warnings=False) if config is None else config
    state = load_state()
    nodes_payload = build_nodes_payload(config, state)
    payload = render_stub_payload(config, state, nodes_payload=nodes_payload)
    backbone_payload, wireguard_live_peers = build_backbone_payload(previous_live_peers=previous_wireguard_live_peers)
    write_json_atomic(output_path, payload)
    write_json_atomic(nodes_output_path, nodes_payload)
    write_json_atomic(backbone_output_path, backbone_payload)
    publish_web_links(output_path, nodes_output_path, backbone_output_path, webroot)
    return wireguard_live_peers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkconfig", action="store_true", help="Validate sysinfo config and exit")
    parser.add_argument("--loop", action="store_true", help="Refresh sysinfo data periodically")
    parser.add_argument("--interval", type=int, default=60, help="Refresh interval in seconds for --loop")
    parser.add_argument("--output", default=str(SYSINFO_OUTPUT), help="Target path for rendered sysinfo JSON")
    parser.add_argument("--nodes-output", default=str(NODES_OUTPUT), help="Target path for rendered nodes JSON")
    parser.add_argument("--backbone-output", default=str(BACKBONE_OUTPUT), help="Target path for rendered backbone JSON")
    parser.add_argument("--webroot", default=str(SYSINFO_WEBROOT), help="Directory that exposes sysinfo.json, sysinfo-json.cgi, nodes.json and backbone.json symlinks")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.output)
    nodes_output_path = Path(args.nodes_output)
    backbone_output_path = Path(args.backbone_output)
    webroot = Path(args.webroot)

    if args.checkconfig:
        require_valid_sysinfo_config(logger=log_info)
        log_info("config check passed")
        return 0

    if not args.loop:
        config = require_valid_sysinfo_config(logger=log_info, log_warnings=False)
        render_once(output_path, webroot, nodes_output_path, backbone_output_path, config=config)
        return 0

    if args.interval < 1:
        raise SystemExit("--interval must be >= 1")

    config = require_valid_sysinfo_config(logger=log_info, log_warnings=False)
    log_info(f"starting sysinfo loop with interval {args.interval}s")
    previous_wireguard_live_peers: dict[str, LivePeer] = {}
    while True:
        previous_wireguard_live_peers = render_once(
            output_path,
            webroot,
            nodes_output_path,
            backbone_output_path,
            config=config,
            previous_wireguard_live_peers=previous_wireguard_live_peers,
        )
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())