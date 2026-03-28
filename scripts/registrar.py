#!/usr/bin/env python3
import argparse
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from node_config import (  # noqa: E402
    build_base_config,
    load_state,
    node_addresses,
    require_valid_config as require_valid_node_config,
    save_state,
)


FASTD_RUNTIME_DIR = Path(os.environ.get("FASTD_RUNTIME_DIR", "/run/freifunk/fastd"))
PEER_DIR = FASTD_RUNTIME_DIR / "peers"
FASTD_CONFIG = Path(os.environ.get("FASTD_CONFIG", str(FASTD_RUNTIME_DIR / "fastd.conf")))
FASTD_ENV_FILE = Path(os.environ.get("FASTD_ENV_FILE", str(FASTD_RUNTIME_DIR / "fastd.env")))
WIREGUARD_RUNTIME_DIR = Path(os.environ.get("WIREGUARD_RUNTIME_DIR", "/run/freifunk/wireguard"))
WIREGUARD_ENV_FILE = Path(
    os.environ.get("WIREGUARD_ENV_FILE", str(WIREGUARD_RUNTIME_DIR / "wireguard.env"))
)
WIREGUARD_INTERFACE = os.environ.get("WIREGUARD_INTERFACE", "tbbwg")
WIREGUARD_TUNNEL_PREFIX = os.environ.get("WIREGUARD_TUNNEL_PREFIX", "tbb_wg")
BMXD_RUNTIME_DIR = Path(os.environ.get("BMXD_RUNTIME_DIR", "/run/freifunk/bmxd"))
BMXD_ENV_FILE = Path(os.environ.get("BMXD_ENV_FILE", str(BMXD_RUNTIME_DIR / "bmxd.env")))
REGISTER_KEY_PLACEHOLDER = "__NODE_REGISTER_KEY__"
NODE_ID_PLACEHOLDER = "__NODE_ID__"
WG_STATUS_ACCEPTED = {"RequestAccepted", "RequestAlreadyRegistered"}
FASTD_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WIREGUARD_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{43}=$")


REGISTRAR_CONFIG_SCHEMA: tuple[dict[str, object], ...] = (
    {
        "env": "REGISTRAR_INTERVAL",
        "path": ("registrar", "interval"),
        "type": "int",
        "default": 3600,
        "min": 3600,
        "max": 21600,
    },
    {
        "env": "NODE_REGISTRATION_URL",
        "default_key": "NODE_REGISTRATION_URL",
        "path": ("registrar", "registration_url"),
        "type": "str",
        "required": True,
        "allow_blank": False,
        "blank_env_uses_default": True,
    },
    {
        "env": "INITIAL_NODE_ID",
        "default_key": "INITIAL_NODE_ID",
        "path": ("registrar", "initial_node_id"),
        "type": "int",
        "default": 52001,
        "min": 0,
        "blank_env_uses_default": True,
    },
    {
        "env": "BACKBONE_PEERS",
        "aliases": ("FASTD_PEERS",),
        "default_key": "BACKBONE_PEERS",
        "path": ("backbone", "peers"),
        "type": "str",
        "required": True,
        "allow_blank": False,
        "blank_env_uses_default": True,
    },
    {
        "env": "FASTD_PORT",
        "path": ("fastd", "port"),
        "type": "int",
        "default": 5002,
        "min": 1,
        "max": 65535,
    },
    {
        "env": "WIREGUARD_PORT",
        "path": ("wireguard", "port"),
        "type": "int",
        "default": 51820,
        "min": 1,
        "max": 65535,
        "blank_env_uses_default": True,
    },
    {
        "env": "BMXD_PREFERRED_GATEWAY",
        "path": ("bmxd", "preferred_gateway"),
        "type": "str",
        "default": "",
        "allow_blank": True,
    },
)


def log_info(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"{timestamp} [registrar] {message}", flush=True)


def _split_host_port(host_port: str) -> tuple[str, str]:
    host, port = host_port.rsplit(":", 1)
    host = host.strip()
    port = port.strip()
    if not host or not port:
        raise ValueError("missing host or port")
    return host, port


def _fastd_example(host: str = "host.example.org", port: str = "5002") -> str:
    return (
        f"fastd;{host}:{port};"
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )


def _wireguard_example(host: str = "host.example.org", port: str = "51820") -> str:
    return f"wireguard;{host}:{port};AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;52001 or wireguard;{host}:{port}"


def require_valid_registrar_config(
    *,
    defaults: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
    base_values: dict | None = None,
    logger=None,
) -> dict:
    config = require_valid_node_config(
        schema=REGISTRAR_CONFIG_SCHEMA,
        defaults=defaults,
        env=env,
        logger=logger,
        base_values=build_base_config() if base_values is None else base_values,
    )

    issues: list[str] = []
    registration_url = str(config["registrar"]["registration_url"])
    if REGISTER_KEY_PLACEHOLDER not in registration_url or NODE_ID_PLACEHOLDER not in registration_url:
        issues.append(
            f"NODE_REGISTRATION_URL: NODE_REGISTRATION_URL must contain {REGISTER_KEY_PLACEHOLDER} and {NODE_ID_PLACEHOLDER}"
        )

    _source, peer_string = requested_peer_string(config, env=env)
    parsed_peers, parse_issues = parse_peer_string(peer_string)
    usable_peers, peer_issues = validate_requested_peers(parsed_peers)

    for issue in parse_issues:
        if logger is not None:
            logger(f"config warning: {issue}")
    if any(peer["type"] == "wireguard" for peer in usable_peers):
        wireguard_probe_error = probe_wireguard_support()
        if wireguard_probe_error is not None:
            issues.append(f"WIREGUARD_BACKBONE: {wireguard_probe_error}")

    if not usable_peers:
        for issue in peer_issues:
            if logger is not None:
                logger(f"config error: {issue}")
        issues.append(
            "BACKBONE_PEERS: no usable backbone peers remain after validation; expected at least one valid fastd or wireguard peer"
        )

    if issues:
        for issue in issues:
            if logger is not None:
                logger(f"config error: {issue}")
        raise SystemExit(1)

    return config


def write_text_atomic(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = None
    if path.exists():
        current = path.read_text(encoding="utf-8")
    if current == content:
        return False

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)
    return True


def run_fastd(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["fastd", *args],
        input=input_text,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def run_wg(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["wg", *args],
        input=input_text,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def ensure_secret(state: dict) -> tuple[str, bool]:
    fastd_state = state.setdefault("fastd", {})
    secret = fastd_state.get("secret")
    if secret:
        return str(secret), False

    secret = run_fastd("--machine-readable", "--generate-key")
    fastd_state.clear()
    fastd_state["secret"] = secret
    return secret, True


def ensure_wireguard_secret(state: dict) -> tuple[str, bool]:
    wireguard_state = state.setdefault("wireguard", {})
    secret = wireguard_state.get("secret")
    if secret:
        return str(secret), False

    secret = run_wg("genkey")
    wireguard_state["secret"] = secret
    return secret, True


def format_colon_hex(raw: str) -> str:
    return ":".join(raw[i : i + 2] for i in range(0, len(raw), 2))


def ensure_register_key(state: dict) -> str:
    registration_state = state.setdefault("registration", {})
    register_key = registration_state.get("register_key")
    if register_key:
        return str(register_key)

    register_key = format_colon_hex(secrets.token_hex(32))
    registration_state["register_key"] = register_key
    return register_key


def ensure_node_id(state: dict, initial_node_id: int) -> int:
    registration_state = state.setdefault("registration", {})
    node_id = registration_state.get("node_id")
    if node_id is not None:
        try:
            return int(node_id)
        except (TypeError, ValueError) as exc:
            raise SystemExit("invalid registration.node_id in state file") from exc

    registration_state["node_id"] = int(initial_node_id)
    return int(initial_node_id)


def derive_public_key(secret: str) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(f'secret "{secret}";\n')
        config_path = handle.name
    try:
        return run_fastd("--machine-readable", "--show-key", "--config", config_path)
    finally:
        Path(config_path).unlink(missing_ok=True)


def derive_wireguard_public_key(secret: str) -> str:
    return run_wg("pubkey", input_text=secret + "\n")


def requested_peer_string(config: dict, env: dict[str, str] | None = None) -> tuple[str, str]:
    env_map = os.environ if env is None else env
    for env_name in ("BACKBONE_PEERS", "FASTD_PEERS"):
        raw = str(env_map.get(env_name, "")).strip()
        if raw:
            return "env", raw
    return "default", str(config["backbone"]["peers"])


def parse_peer_string(raw: str) -> tuple[list[dict[str, str]], list[str]]:
    peers: list[dict[str, str]] = []
    issues: list[str] = []
    seen: set[str] = set()

    items = re.split(r"[\s,]+", raw.strip())
    for item in items:
        if not item:
            continue

        parts = [part.strip() for part in item.split(";")]
        if len(parts) < 2:
            issues.append(
                f"BACKBONE_PEERS entry '{item}' is invalid; expected {_fastd_example()} or {_wireguard_example()}"
            )
            continue

        if ":" in parts[0] and len(parts) == 2:
            peer_type = "fastd"
            host_port = parts[0]
            rest = parts[1:]
        else:
            peer_type = parts[0].lower()
            host_port = parts[1]
            rest = parts[2:]

        if peer_type not in {"fastd", "wireguard"}:
            issues.append(
                f"BACKBONE_PEERS entry '{item}' uses unsupported type '{parts[0]}'; expected 'fastd' or 'wireguard'"
            )
            continue

        try:
            host, port = _split_host_port(host_port)
        except ValueError:
            issues.append(
                f"BACKBONE_PEERS entry '{item}' has invalid host:port; expected {_fastd_example()} or {_wireguard_example()}"
            )
            continue

        identity = f"{peer_type}:{host}:{port}"
        if identity in seen:
            continue
        seen.add(identity)

        peer: dict[str, str] = {
            "type": peer_type,
            "host": host,
            "port": port,
            "raw": item,
        }

        if peer_type == "fastd":
            if len(rest) != 1 or not rest[0]:
                issues.append(
                    f"BACKBONE_PEERS entry '{item}' is invalid; fastd peers must use {_fastd_example(host, port)}"
                )
                continue
            peer["key"] = rest[0]
        else:
            if len(rest) == 0:
                peer["key"] = ""
                peer["node"] = ""
            elif len(rest) == 2 and rest[0] and rest[1]:
                peer["key"] = rest[0]
                peer["node"] = rest[1]
            else:
                issues.append(
                    f"BACKBONE_PEERS entry '{item}' is invalid; wireguard peers must use {_wireguard_example(host, port)}"
                )
                continue

        peers.append(peer)

    return peers, issues


def validate_requested_peers(peers: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    usable: list[dict[str, str]] = []
    issues: list[str] = []

    for peer in peers:
        if peer["type"] == "fastd":
            key = peer.get("key", "")
            if not FASTD_KEY_PATTERN.fullmatch(key):
                issues.append(
                    f"backbone peer error: '{peer['raw']}': fastd public key must be 64 lowercase hex chars; discarding peer; expected {_fastd_example(peer['host'], peer['port'])}"
                )
                continue
            usable.append(peer)
            continue

        key = peer.get("key", "")
        node = peer.get("node", "")
        if not key and not node:
            candidate = dict(peer)
            candidate["metadata_missing"] = "1"
            usable.append(candidate)
            continue

        if not WIREGUARD_KEY_PATTERN.fullmatch(key):
            issues.append(
                f"backbone peer error: '{peer['raw']}': wireguard public key must be base64 encoded (44 chars, ending with '='); discarding peer; expected {_wireguard_example(peer['host'], peer['port'])}"
            )
            continue

        try:
            node_id = int(node)
        except ValueError:
            issues.append(
                f"backbone peer error: '{peer['raw']}': wireguard node must be an integer; discarding peer; expected {_wireguard_example(peer['host'], peer['port'])}"
            )
            continue

        if node_id <= 0:
            issues.append(
                f"backbone peer error: '{peer['raw']}': wireguard node must be > 0; discarding peer; expected {_wireguard_example(peer['host'], peer['port'])}"
            )
            continue

        candidate = dict(peer)
        candidate["node"] = str(node_id)
        usable.append(candidate)

    return usable, issues


def probe_wireguard_support() -> str | None:
    probe_ifname = f"wgprobe{os.getpid()}"
    result = _run_ip("link", "add", probe_ifname, "type", "wireguard", check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
        hint = ""
        if "Unknown device type" in details:
            hint = "; likely cause: wireguard kernel support is missing on the Docker host or not exposed to the container"
        return (
            f"wireguard backbone configured but kernel probe failed: {details}; "
            f"attempted command: ip link add {probe_ifname} type wireguard{hint}"
        )

    _run_ip("link", "del", probe_ifname, check=False)
    return None


def sync_peer_dir(peers: list[dict[str, str]]) -> bool:
    desired: dict[str, str] = {}
    PEER_DIR.mkdir(parents=True, exist_ok=True)

    for peer in peers:
        host_token = re.sub(r"[^A-Za-z0-9.-]", "_", peer["host"])
        peer_file = f"connect_{host_token}_{peer['port']}.conf"
        desired[peer_file] = f'key "{peer["key"]}";\nremote ipv4 "{peer["host"]}":{peer["port"]};\n'

    current: dict[str, str] = {}
    for child in PEER_DIR.glob("*.conf"):
        current[child.name] = child.read_text(encoding="utf-8")

    changed = current != desired
    if not changed:
        return False

    for child in PEER_DIR.glob("*.conf"):
        if child.name not in desired:
            child.unlink()

    for name, content in desired.items():
        write_text_atomic(PEER_DIR / name, content)

    return changed


def _clear_fastd_runtime() -> bool:
    changed = False

    if FASTD_CONFIG.exists():
        FASTD_CONFIG.unlink()
        changed = True

    if PEER_DIR.exists():
        for child in PEER_DIR.glob("*.conf"):
            child.unlink()
            changed = True

    return changed


def _build_wireguard_registration_url(host: str, node_id: int, public_key: str) -> str:
    query = urllib.parse.urlencode({"node": str(node_id), "key": public_key})
    return f"http://{host}/wg.cgi?{query}"


def extract_json_payload(raw: str) -> dict:
    start = raw.find("{")
    if start == -1:
        raise SystemExit("registration response does not contain JSON")

    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise SystemExit("registration response contains invalid JSON") from exc

    if not isinstance(payload, dict):
        raise SystemExit("registration response has invalid format")

    return payload


def fetch_wireguard_peer_info(host: str, node_id: int, public_key: str) -> dict[str, str] | None:
    url = _build_wireguard_registration_url(host, node_id, public_key)
    request = urllib.request.Request(url, headers={"User-Agent": "dockernode-registrar/1"})

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError:
        return None

    try:
        payload = extract_json_payload(body)
    except SystemExit:
        return None

    status = str(payload.get("status", ""))
    if status not in WG_STATUS_ACCEPTED:
        return None

    server = payload.get("server")
    if not isinstance(server, dict):
        return None

    key = str(server.get("key", "")).strip()
    node = str(server.get("node", "")).strip()
    port = str(server.get("port", "")).strip()
    if not key or not node:
        return None

    return {
        "key": key,
        "node": node,
        "port": port,
        "status": status,
        "url": url,
    }


def resolve_requested_peers(
    peers: list[dict[str, str]],
    assigned_node_id: int,
    wireguard_public_key: str,
    logger=None,
) -> list[dict[str, str]]:
    usable: list[dict[str, str]] = []

    for peer in peers:
        if peer["type"] == "fastd":
            usable.append(peer)
            continue

        fetched = fetch_wireguard_peer_info(peer["host"], assigned_node_id, wireguard_public_key)
        if fetched is None:
            if logger is not None:
                if peer.get("metadata_missing"):
                    logger(
                        "wireguard peer error: "
                        f"{peer['host']}:{peer['port']}: missing public key/node and API lookup failed; discarding peer; "
                        f"expected {_wireguard_example(peer['host'], peer['port'])}"
                    )
                else:
                    logger(
                        "wireguard peer error: "
                        f"{peer['host']}:{peer['port']}: API lookup failed; discarding peer; "
                        f"expected {_wireguard_example(peer['host'], peer['port'])}"
                    )
            continue

        config_key = str(peer.get("key", "")).strip()
        config_node = str(peer.get("node", "")).strip()
        config_port = str(peer.get("port", "")).strip()
        fetched_key = str(fetched.get("key", "")).strip()
        fetched_node = str(fetched.get("node", "")).strip()
        fetched_port = str(fetched.get("port", "") or peer["port"]).strip()

        if peer.get("metadata_missing"):
            if logger is not None:
                logger(
                    "wireguard peer error: "
                    f"{peer['host']}:{peer['port']}: missing public key/node; discarding peer; "
                    f"API reports wireguard;{peer['host']}:{fetched_port};{fetched_key};{fetched_node}"
                )
            continue

        if config_key != fetched_key or config_node != fetched_node or config_port != fetched_port:
            if logger is not None:
                logger(
                    "wireguard peer error: "
                    f"{peer['host']}:{peer['port']}: config differs from API; discarding peer; "
                    f"configured wireguard;{peer['host']}:{config_port};{config_key};{config_node}; "
                    f"API reports wireguard;{peer['host']}:{fetched_port};{fetched_key};{fetched_node}"
                )
            continue

        effective = dict(peer)
        effective["key"] = fetched_key
        effective["node"] = fetched_node
        effective["endpoint_port"] = fetched_port

        if logger is not None:
            logger(
                "validated wireguard peer via API: "
                f"wireguard;{peer['host']}:{fetched_port};{fetched_key};{fetched_node}"
            )

        effective_key = fetched_key
        effective_node = fetched_node
        effective_port = str(effective.get("endpoint_port", peer["port"]))

        if not WIREGUARD_KEY_PATTERN.fullmatch(effective_key):
            continue
        try:
            node_id = int(effective_node)
        except ValueError:
            continue
        if node_id <= 0:
            continue

        effective["key"] = effective_key
        effective["node"] = str(node_id)
        effective["endpoint_port"] = effective_port
        effective["ifname"] = f"{WIREGUARD_TUNNEL_PREFIX}{node_id}"
        usable.append(effective)

    return usable


def render_fastd_runtime(config: dict, secret: str, peers: list[dict[str, str]], assigned_node_id: int) -> bool:
    interface = str(config["fastd"]["interface"])
    port = int(config["fastd"]["port"])
    mtu = int(config["fastd"]["mtu"])
    method = str(config["fastd"]["method"])
    log_level = str(config["fastd"]["log_level"])
    addresses = node_addresses(assigned_node_id)

    FASTD_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    if not peers:
        cleared = _clear_fastd_runtime()
        env_content = "\n".join(
            [
                f'FASTD_INTERFACE="{interface}"',
                f'FASTD_MTU="{mtu}"',
                f'FASTD_NONPRIMARY_IP="{addresses["nonprimary_ip"]}"',
                f'FASTD_MESH_PREFIX="{addresses["mesh_prefix"]}"',
                f'FASTD_MESH_BROADCAST="{addresses["mesh_broadcast"]}"',
                "",
            ]
        )
        env_changed = write_text_atomic(FASTD_ENV_FILE, env_content)
        return cleared or env_changed

    peer_changed = sync_peer_dir(peers)

    fastd_config = f'''log level {log_level};
log to stderr level {log_level};
mode tap;
interface "{interface}";
method "{method}";
bind any:{port};
secret "{secret}";
mtu {mtu};
packet mark 0x5002;
include peers from "{PEER_DIR}";
forward no;
on up sync "/usr/lib/fastd/backbone-cmd.sh up";
on down sync "/usr/lib/fastd/backbone-cmd.sh down";
on connect sync "/usr/lib/fastd/backbone-cmd.sh connect";
on establish sync "/usr/lib/fastd/backbone-cmd.sh establish";
on disestablish sync "/usr/lib/fastd/backbone-cmd.sh disestablish";
'''
    config_changed = write_text_atomic(FASTD_CONFIG, fastd_config)
    env_content = "\n".join(
        [
            f'FASTD_INTERFACE="{interface}"',
            f'FASTD_MTU="{mtu}"',
            f'FASTD_NONPRIMARY_IP="{addresses["nonprimary_ip"]}"',
            f'FASTD_MESH_PREFIX="{addresses["mesh_prefix"]}"',
            f'FASTD_MESH_BROADCAST="{addresses["mesh_broadcast"]}"',
            "",
        ]
    )
    env_changed = write_text_atomic(FASTD_ENV_FILE, env_content)
    return peer_changed or config_changed or env_changed


def _run_ip(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["ip", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        details = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
        command = " ".join(["ip", *args])
        raise SystemExit(f"ip command failed: {command}: {details}")
    return result


def _run_wg_command(*args: str) -> None:
    result = subprocess.run(["wg", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
        command = " ".join(["wg", *args])
        raise SystemExit(f"wg command failed: {command}: {details}")


def _link_exists(ifname: str) -> bool:
    return _run_ip("link", "show", ifname, check=False).returncode == 0


def _list_wireguard_tunnels() -> list[str]:
    result = _run_ip("-o", "link", "show", check=False)
    if result.returncode != 0:
        return []

    interfaces: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        ifname = parts[1].strip().split("@", 1)[0]
        if ifname.startswith(WIREGUARD_TUNNEL_PREFIX):
            interfaces.append(ifname)
    return interfaces


def _sync_wireguard_peers(interface: str, peers: list[dict[str, str]]) -> None:
    result = subprocess.run(["wg", "show", interface, "peers"], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        for peer_key in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            subprocess.run(["wg", "set", interface, "peer", peer_key, "remove"], check=False)

    for peer in peers:
        remote_wg_ip = node_addresses(int(peer["node"]))["wireguard_ip"]
        subprocess.run(
            [
                "wg",
                "set",
                interface,
                "peer",
                peer["key"],
                "persistent-keepalive",
                "25",
                "allowed-ips",
                f"{remote_wg_ip}/32",
                "endpoint",
                f"{peer['host']}:{peer['endpoint_port']}",
            ],
            check=True,
        )


def _ensure_wireguard_interface(secret: str, assigned_node_id: int, listen_port: int) -> bool:
    changed = False
    local_wg_ip = node_addresses(assigned_node_id)["wireguard_ip"]
    if not _link_exists(WIREGUARD_INTERFACE):
        create_result = _run_ip("link", "add", WIREGUARD_INTERFACE, "type", "wireguard", check=False)
        if create_result.returncode != 0:
            details = (create_result.stderr or create_result.stdout).strip() or f"exit status {create_result.returncode}"
            hint = ""
            if "Unknown device type" in details:
                hint = " likely cause: wireguard kernel support is missing on the Docker host or not exposed to the container"
            raise SystemExit(
                f"failed to create wireguard interface '{WIREGUARD_INTERFACE}': {details};"
                f" attempted command: ip link add {WIREGUARD_INTERFACE} type wireguard;{hint}"
            )
        changed = True

    _run_ip("link", "set", WIREGUARD_INTERFACE, "mtu", "1320")
    _run_ip("addr", "replace", f"{local_wg_ip}/32", "dev", WIREGUARD_INTERFACE)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(secret + "\n")
        secret_file = handle.name
    try:
        _run_wg_command(
            "set",
            WIREGUARD_INTERFACE,
            "private-key",
            secret_file,
            "listen-port",
            str(listen_port),
        )
    finally:
        Path(secret_file).unlink(missing_ok=True)

    _run_ip("link", "set", WIREGUARD_INTERFACE, "up")
    _run_ip("route", "replace", "10.203.0.0/16", "dev", WIREGUARD_INTERFACE, "src", local_wg_ip)
    return changed


def _recreate_ipip_interface(ifname: str, local_wg_ip: str, remote_wg_ip: str, nonprimary_cidr: str, broadcast_ip: str) -> None:
    if _link_exists(ifname):
        _run_ip("link", "del", ifname, check=False)
    _run_ip("link", "add", ifname, "type", "ipip", "remote", remote_wg_ip, "local", local_wg_ip)
    _run_ip("addr", "replace", nonprimary_cidr, "broadcast", broadcast_ip, "dev", ifname)
    _run_ip("link", "set", ifname, "up")


def reconcile_wireguard_runtime(
    config: dict,
    secret: str,
    peers: list[dict[str, str]],
    assigned_node_id: int,
    wireguard_public_key: str,
) -> tuple[bool, list[str]]:
    addresses = node_addresses(assigned_node_id)
    listen_port = int(config["wireguard"]["port"])
    desired_ifaces = [peer["ifname"] for peer in peers]
    desired_content = "\n".join(
        [
            f'WIREGUARD_INTERFACE="{WIREGUARD_INTERFACE}"',
            f'WIREGUARD_LISTEN_PORT="{listen_port}"',
            f'WIREGUARD_PUBLIC_KEY="{wireguard_public_key}"',
            f'WIREGUARD_LOCAL_IP="{addresses["wireguard_ip"]}"',
            'WIREGUARD_PEERS="'
            + " ".join(
                f"{peer['host']}:{peer['endpoint_port']};{peer['key']};{peer['node']};{peer['ifname']}"
                for peer in peers
            )
            + '"',
            "",
        ]
    )

    WIREGUARD_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    config_changed = write_text_atomic(WIREGUARD_ENV_FILE, desired_content)

    if not peers:
        stale_ifaces = _list_wireguard_tunnels()
        for ifname in stale_ifaces:
            _run_ip("link", "del", ifname, check=False)
        if _link_exists(WIREGUARD_INTERFACE):
            _run_ip("link", "del", WIREGUARD_INTERFACE, check=False)
            return True, []
        return config_changed or bool(stale_ifaces), []

    interface_changed = _ensure_wireguard_interface(secret, assigned_node_id, listen_port)
    current_ifaces = set(_list_wireguard_tunnels())
    desired_ifaces_set = set(desired_ifaces)
    stale_ifaces = sorted(current_ifaces - desired_ifaces_set)
    missing_ifaces = sorted(desired_ifaces_set - current_ifaces)
    needs_sync = config_changed or interface_changed or bool(stale_ifaces) or bool(missing_ifaces)

    if needs_sync:
        _sync_wireguard_peers(WIREGUARD_INTERFACE, peers)

    for ifname in stale_ifaces:
        _run_ip("link", "del", ifname, check=False)

    for peer in peers:
        ifname = peer["ifname"]
        if needs_sync or ifname in missing_ifaces:
            remote_wg_ip = node_addresses(int(peer["node"]))["wireguard_ip"]
            _recreate_ipip_interface(
                ifname,
                addresses["wireguard_ip"],
                remote_wg_ip,
                f"{addresses['nonprimary_ip']}/{addresses['mesh_prefix']}",
                addresses["mesh_broadcast"],
            )

    return config_changed or interface_changed or bool(stale_ifaces) or bool(missing_ifaces), desired_ifaces


def render_bmxd_runtime(config: dict, assigned_node_id: int, backbone_interfaces: list[str] | None = None) -> bool:
    addresses = node_addresses(assigned_node_id)
    bmxd_config = config["bmxd"]
    preferred_gateway = bmxd_config.get("preferred_gateway") or ""
    interfaces = [config["fastd"]["interface"]] if backbone_interfaces is None else backbone_interfaces
    content = "\n".join(
        [
            f'BMXD_NODE_ID="{assigned_node_id}"',
            f'BMXD_PRIMARY_IP="{addresses["primary_ip"]}"',
            f'BMXD_DAEMON_RUNTIME_DIR="{bmxd_config["daemon_runtime_dir"]}"',
            f'BMXD_GATEWAY_USAGE_FILE="{bmxd_config["gateway_usage_file"]}"',
            f'BMXD_PRIMARY_INTERFACE="{bmxd_config["primary_interface"]}"',
            f'BMXD_FASTD_INTERFACE="{config["fastd"]["interface"]}"',
            f'BMXD_BACKBONE_INTERFACES="{" ".join(interfaces)}"',
            f'BMXD_MESH_NETWORK="{bmxd_config["mesh_network"] or addresses["mesh_network"]}"',
            f'BMXD_POLICY_RULE_TO="{bmxd_config["policy_rule_to"]}"',
            f'BMXD_POLICY_RULE_PRIORITY="{bmxd_config["policy_rule_priority"]}"',
            f'BMXD_POLICY_RULE_TABLE="{bmxd_config["policy_rule_table"]}"',
            f'BMXD_NETID="{bmxd_config["netid"]}"',
            f'BMXD_ONLY_COMMUNITY_GW="{bmxd_config["only_community_gw"]}"',
            f'BMXD_ROUTING_CLASS="{bmxd_config["routing_class"]}"',
            f'BMXD_PREFERRED_GATEWAY="{preferred_gateway}"',
            f'BMXD_GATEWAY_HYSTERESIS="{bmxd_config["gateway_hysteresis"]}"',
            f'BMXD_PATH_HYSTERESIS="{bmxd_config["path_hysteresis"]}"',
            f'BMXD_HOP_PENALTY="{bmxd_config["hop_penalty"]}"',
            f'BMXD_LATENESS_PENALTY="{bmxd_config["lateness_penalty"]}"',
            f'BMXD_WIRELESS_OGM_CLONE="{bmxd_config["wireless_ogm_clone"]}"',
            f'BMXD_UDP_DATA_SIZE="{bmxd_config["udp_data_size"]}"',
            f'BMXD_OGM_INTERVAL="{bmxd_config["ogm_interval"]}"',
            f'BMXD_PURGE_TIMEOUT="{bmxd_config["purge_timeout"]}"',
            f'BMXD_GATEWAY_SCRIPT="{bmxd_config["gateway_script"]}"',
            "",
        ]
    )
    return write_text_atomic(BMXD_ENV_FILE, content)


def build_registration_url(config: dict, register_key: str, node_id: int) -> str:
    template = str(config["registrar"]["registration_url"])
    url = template.replace(REGISTER_KEY_PLACEHOLDER, register_key)
    url = url.replace(NODE_ID_PLACEHOLDER, str(node_id))

    if REGISTER_KEY_PLACEHOLDER in url or NODE_ID_PLACEHOLDER in url:
        raise SystemExit("registration URL still contains unresolved placeholders")

    return url


def register_once(config: dict, state: dict) -> tuple[int, str]:
    register_key = ensure_register_key(state)
    node_id = ensure_node_id(state, int(config["registrar"]["initial_node_id"]))
    url = build_registration_url(config, register_key, node_id)

    request = urllib.request.Request(url, headers={"User-Agent": "dockernode-registrar/1"})
    context = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(request, context=context, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise SystemExit(f"registration request failed: {exc}") from exc

    payload = extract_json_payload(body)
    registration = payload.get("registration")
    if not isinstance(registration, dict):
        raise SystemExit("registration response misses registration object")

    status = str(registration.get("status", ""))
    if status != "ok":
        error = registration.get("error") or "unknown registration error"
        raise SystemExit(f"registration failed: {error}")

    try:
        assigned_node_id = int(registration["node"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit("registration response misses valid node id") from exc

    state.setdefault("registration", {})["node_id"] = assigned_node_id
    return assigned_node_id, build_registration_url(config, register_key, assigned_node_id)


def restart_service(name: str) -> None:
    subprocess.run(["sv", "restart", f"/etc/service/{name}"], check=False)


def run_iteration(
    loop_mode: bool,
    first_iteration: bool,
    config_override: dict | None = None,
) -> tuple[int, bool, bool]:
    config = config_override if config_override is not None else require_valid_registrar_config(logger=log_info)
    state = load_state()
    previous_node_id = state.get("registration", {}).get("node_id")
    fastd_secret, fastd_secret_created = ensure_secret(state)
    wireguard_secret, wireguard_secret_created = ensure_wireguard_secret(state)
    ensure_register_key(state)
    requested_node_id = ensure_node_id(state, int(config["registrar"]["initial_node_id"]))
    save_state(state)

    assigned_node_id, _registration_url = register_once(config, state)
    save_state(state)

    _source, peer_string = requested_peer_string(config)
    parsed_peers, parse_issues = parse_peer_string(peer_string)
    for issue in parse_issues:
        log_info(issue)

    candidate_peers, peer_issues = validate_requested_peers(parsed_peers)
    for issue in peer_issues:
        log_info(issue)

    wireguard_public = derive_wireguard_public_key(wireguard_secret)
    peers = resolve_requested_peers(candidate_peers, assigned_node_id, wireguard_public, logger=log_info)
    if not peers:
        raise SystemExit("no usable backbone peers remain after validation and wireguard peer resolution")

    fastd_peers = [peer for peer in peers if peer["type"] == "fastd"]
    wireguard_peers = [peer for peer in peers if peer["type"] == "wireguard"]
    save_state(state)

    fastd_changed = render_fastd_runtime(config, fastd_secret, fastd_peers, assigned_node_id)
    wireguard_changed, wireguard_ifaces = reconcile_wireguard_runtime(
        config,
        wireguard_secret,
        wireguard_peers,
        assigned_node_id,
        wireguard_public,
    )
    backbone_interfaces: list[str] = []
    if fastd_peers:
        backbone_interfaces.append(config["fastd"]["interface"])
    backbone_interfaces.extend(wireguard_ifaces)

    bmxd_changed = render_bmxd_runtime(
        config,
        assigned_node_id,
        backbone_interfaces,
    )

    if loop_mode and not first_iteration:
        if fastd_changed:
            restart_service("fastd")
        if bmxd_changed or wireguard_changed or str(previous_node_id) != str(assigned_node_id):
            restart_service("bmxd")

    addresses = node_addresses(assigned_node_id)
    if fastd_secret_created:
        log_info(f"fastd public key {derive_public_key(fastd_secret)}")
    if wireguard_secret_created:
        log_info(f"wireguard public key {wireguard_public}")
    log_info(f"requested node id {requested_node_id}")
    log_info(f"assigned node id {assigned_node_id}")
    log_info(f"primary ip {addresses['primary_ip']}")
    if fastd_changed:
        log_info(f"updated {FASTD_CONFIG}")
        log_info(f"updated peer set in {PEER_DIR} ({len(fastd_peers)} files)")
    if wireguard_changed:
        log_info(f"updated {WIREGUARD_ENV_FILE}")
    if bmxd_changed:
        log_info(f"updated {BMXD_ENV_FILE}")
    return assigned_node_id, fastd_changed, bmxd_changed or wireguard_changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkconfig", action="store_true", help="Validate config and exit")
    parser.add_argument("--loop", action="store_true", help="Run registrar in periodic reconcile mode")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.checkconfig:
        require_valid_registrar_config(logger=log_info)
        log_info("config check passed")
        return 0

    if not args.loop:
        log_info("running single reconcile iteration")
        run_iteration(loop_mode=False, first_iteration=True)
        return 0

    initial_config = require_valid_registrar_config(logger=log_info)
    interval = int(initial_config["registrar"]["interval"])
    log_info(f"starting reconcile loop with interval {interval}s")
    first_iteration = True
    while True:
        run_iteration(loop_mode=True, first_iteration=first_iteration, config_override=initial_config if first_iteration else None)
        first_iteration = False
        log_info(f"sleeping {interval}s until next reconcile")
        time.sleep(interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
