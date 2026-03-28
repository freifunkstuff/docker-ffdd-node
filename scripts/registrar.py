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
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from node_config import (
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
BMXD_RUNTIME_DIR = Path(os.environ.get("BMXD_RUNTIME_DIR", "/run/freifunk/bmxd"))
BMXD_ENV_FILE = Path(os.environ.get("BMXD_ENV_FILE", str(BMXD_RUNTIME_DIR / "bmxd.env")))
REGISTER_KEY_PLACEHOLDER = "__NODE_REGISTER_KEY__"
NODE_ID_PLACEHOLDER = "__NODE_ID__"


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
        "env": "FASTD_PEERS",
        "default_key": "FASTD_PEERS",
        "path": ("fastd", "peers"),
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
        "env": "BMXD_PREFERRED_GATEWAY",
        "path": ("bmxd", "preferred_gateway"),
        "type": "str",
        "default": "",
        "allow_blank": True,
    },
)


def log_info(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"{timestamp} [registrar] {message}", flush=True)


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

    issues = []
    registration_url = str(config["registrar"]["registration_url"])
    if REGISTER_KEY_PLACEHOLDER not in registration_url or NODE_ID_PLACEHOLDER not in registration_url:
        issues.append(
            f"NODE_REGISTRATION_URL: NODE_REGISTRATION_URL must contain {REGISTER_KEY_PLACEHOLDER} and {NODE_ID_PLACEHOLDER}"
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


def ensure_secret(state: dict) -> str:
    fastd_state = state.setdefault("fastd", {})
    secret = fastd_state.get("secret")
    if secret:
        return str(secret)

    secret = run_fastd("--machine-readable", "--generate-key")
    fastd_state.clear()
    fastd_state["secret"] = secret
    return secret


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


def requested_peer_string(config: dict) -> tuple[str, str]:
    raw = os.environ.get("FASTD_PEERS", "").strip()
    if raw:
        return "env", raw
    return "default", str(config["fastd"]["peers"])


def parse_peer_string(raw: str) -> list[dict[str, str]]:
    peers: list[dict[str, str]] = []
    seen: set[str] = set()

    items = re.split(r"[\s,]+", raw.strip())
    for item in items:
        if not item:
            continue

        try:
            host_port, key = item.split(";", 1)
            host, port = host_port.rsplit(":", 1)
        except ValueError as exc:
            raise SystemExit(f"invalid FASTD_PEERS entry: {item}") from exc

        host = host.strip()
        port = port.strip()
        key = key.strip()

        if not host or not port or not key:
            raise SystemExit(f"invalid FASTD_PEERS entry: {item}")

        host_key = f"{host}:{port}"
        if host_key in seen:
            continue
        seen.add(host_key)

        peers.append(
            {
                "host": host,
                "port": port,
                "key": key,
            }
        )

    return peers


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

    return True


def render_fastd_runtime(config: dict, secret: str, peers: list[dict[str, str]], assigned_node_id: int) -> bool:
    interface = str(config["fastd"]["interface"])
    port = int(config["fastd"]["port"])
    mtu = int(config["fastd"]["mtu"])
    method = str(config["fastd"]["method"])
    log_level = str(config["fastd"]["log_level"])
    addresses = node_addresses(assigned_node_id)

    FASTD_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    peer_changed = sync_peer_dir(peers)

    config = f'''log level {log_level};
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
    config_changed = write_text_atomic(FASTD_CONFIG, config)
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


def render_bmxd_runtime(config: dict, assigned_node_id: int) -> bool:
    addresses = node_addresses(assigned_node_id)
    bmxd_config = config["bmxd"]
    preferred_gateway = bmxd_config.get("preferred_gateway") or ""
    content = "\n".join(
        [
            f'BMXD_NODE_ID="{assigned_node_id}"',
            f'BMXD_PRIMARY_IP="{addresses["primary_ip"]}"',
            f'BMXD_DAEMON_RUNTIME_DIR="{bmxd_config["daemon_runtime_dir"]}"',
            f'BMXD_GATEWAY_USAGE_FILE="{bmxd_config["gateway_usage_file"]}"',
            f'BMXD_PRIMARY_INTERFACE="{bmxd_config["primary_interface"]}"',
            f'BMXD_FASTD_INTERFACE="{config["fastd"]["interface"]}"',
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


def run_iteration(loop_mode: bool, first_iteration: bool) -> tuple[int, bool, bool]:
    config = require_valid_registrar_config(logger=log_info)
    state = load_state()
    previous_node_id = state.get("registration", {}).get("node_id")
    secret = ensure_secret(state)
    ensure_register_key(state)
    requested_node_id = ensure_node_id(state, int(config["registrar"]["initial_node_id"]))
    save_state(state)

    assigned_node_id, _registration_url = register_once(config, state)
    save_state(state)

    _source, peer_string = requested_peer_string(config)
    peers = parse_peer_string(peer_string)
    fastd_changed = render_fastd_runtime(config, secret, peers, assigned_node_id)
    bmxd_changed = render_bmxd_runtime(config, assigned_node_id)

    if loop_mode and not first_iteration:
        if fastd_changed:
            restart_service("fastd")
        if bmxd_changed or str(previous_node_id) != str(assigned_node_id):
            restart_service("bmxd")

    public = derive_public_key(secret)
    addresses = node_addresses(assigned_node_id)
    log_info(f"fastd public key {public}")
    log_info(f"requested node id {requested_node_id}")
    log_info(f"assigned node id {assigned_node_id}")
    log_info(f"primary ip {addresses['primary_ip']}")
    if fastd_changed:
        log_info(f"updated {FASTD_CONFIG}")
        log_info(f"updated peer set in {PEER_DIR} ({len(peers)} files)")
    if bmxd_changed:
        log_info(f"updated {BMXD_ENV_FILE}")
    return assigned_node_id, fastd_changed, bmxd_changed


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

    interval = int(require_valid_registrar_config(logger=log_info)["registrar"]["interval"])
    log_info(f"starting reconcile loop with interval {interval}s")
    first_iteration = True
    while True:
        run_iteration(loop_mode=True, first_iteration=first_iteration)
        first_iteration = False
        log_info(f"sleeping {interval}s until next reconcile")
        time.sleep(interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())