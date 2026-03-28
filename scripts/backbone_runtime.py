from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


WIREGUARD_ENV_FILE = Path(os.environ.get("WIREGUARD_ENV_FILE", "/run/freifunk/wireguard/wireguard.env"))
FASTD_ENV_FILE = Path(os.environ.get("FASTD_ENV_FILE", "/run/freifunk/fastd/fastd.env"))
FASTD_PEER_DIR = Path(os.environ.get("FASTD_PEER_DIR", "/run/freifunk/fastd/peers"))
FASTD_STATUS_DIR = Path(os.environ.get("FASTD_STATUS_DIR", "/run/freifunk/fastd/backbone_status"))
DEFAULT_WIREGUARD_STALE_AFTER = int(os.environ.get("WIREGUARD_STATUS_STALE_AFTER", "180"))
DEFAULT_FASTD_INTERFACE = os.environ.get("FASTD_INTERFACE", "tbb_fastd")

FASTD_PEER_PATTERN = re.compile(r'^key\s+"(?P<key>[0-9a-f]+)";\s*remote ipv4\s+"(?P<host>[^"]+)":(?P<port>\d+);\s*$')


@dataclass(frozen=True)
class ConfiguredPeer:
    host: str
    port: str
    public_key: str
    node: str
    ifname: str

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port} via {self.ifname}"


@dataclass(frozen=True)
class LivePeer:
    endpoint: str
    latest_handshake: int
    transfer_rx: int
    transfer_tx: int
    persistent_keepalive: int


@dataclass(frozen=True)
class FastdPeer:
    host: str
    port: str
    public_key: str
    ifname: str


def load_runtime_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(raw_value)
        except ValueError:
            continue
        values[key] = parsed[0] if parsed else ""
    return values


def parse_configured_peers(env_values: dict[str, str]) -> tuple[str, dict[str, ConfiguredPeer]]:
    interface = env_values.get("WIREGUARD_INTERFACE", "tbbwg")
    peers: dict[str, ConfiguredPeer] = {}

    for item in env_values.get("WIREGUARD_PEERS", "").split():
        parts = item.split(";")
        if len(parts) != 4:
            continue
        endpoint, public_key, node, ifname = parts
        if ":" not in endpoint:
            continue
        host, port = endpoint.rsplit(":", 1)
        peers[public_key] = ConfiguredPeer(host=host, port=port, public_key=public_key, node=node, ifname=ifname)

    return interface, peers


def read_live_peers(interface: str) -> dict[str, LivePeer]:
    result = subprocess.run(["wg", "show", interface, "dump"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {}

    peers: dict[str, LivePeer] = {}
    for index, line in enumerate(result.stdout.splitlines()):
        if index == 0:
            continue
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        public_key = parts[0].strip()
        if not public_key:
            continue
        peers[public_key] = LivePeer(
            endpoint=parts[2].strip(),
            latest_handshake=_parse_int(parts[4]),
            transfer_rx=_parse_int(parts[5]),
            transfer_tx=_parse_int(parts[6]),
            persistent_keepalive=_parse_int(parts[7]),
        )

    return peers


def determine_status(live_peer: LivePeer | None, previous_live_peer: LivePeer | None, now: int, stale_after: int) -> str:
    if live_peer is None:
        return "disconnected"

    if live_peer.latest_handshake > 0 and now - live_peer.latest_handshake <= stale_after:
        return "connected"

    if previous_live_peer is not None:
        if live_peer.transfer_rx != previous_live_peer.transfer_rx or live_peer.transfer_tx != previous_live_peer.transfer_tx:
            return "connected"

    if live_peer.latest_handshake == 0 and live_peer.transfer_rx == 0 and live_peer.transfer_tx == 0:
        return "disconnected"

    return "stale"


def load_fastd_peers() -> list[FastdPeer]:
    interface = load_runtime_env(FASTD_ENV_FILE).get("FASTD_INTERFACE", DEFAULT_FASTD_INTERFACE)
    peers: list[FastdPeer] = []

    if not FASTD_PEER_DIR.exists():
        return peers

    for child in sorted(FASTD_PEER_DIR.glob("*.conf")):
        match = FASTD_PEER_PATTERN.fullmatch(child.read_text(encoding="utf-8").strip())
        if not match:
            continue
        peers.append(
            FastdPeer(
                host=match.group("host"),
                port=match.group("port"),
                public_key=match.group("key"),
                ifname=interface,
            )
        )

    return peers


def read_fastd_connected_keys() -> set[str]:
    if not FASTD_STATUS_DIR.exists():
        return set()
    return {child.name for child in FASTD_STATUS_DIR.iterdir() if child.is_file()}


def build_backbone_payload(
    *,
    previous_live_peers: dict[str, LivePeer] | None = None,
    now: int | None = None,
    stale_after: int = DEFAULT_WIREGUARD_STALE_AFTER,
) -> tuple[dict[str, object], dict[str, LivePeer]]:
    previous_live_peers = {} if previous_live_peers is None else previous_live_peers
    now = int(time.time()) if now is None else now

    wireguard_interface, wireguard_peers = parse_configured_peers(load_runtime_env(WIREGUARD_ENV_FILE))
    wireguard_live_peers = read_live_peers(wireguard_interface)
    fastd_peers = load_fastd_peers()
    fastd_connected_keys = read_fastd_connected_keys()

    peers: list[dict[str, str]] = []

    for public_key in sorted(wireguard_peers):
        peer = wireguard_peers[public_key]
        peers.append(
            {
                "type": "wireguard",
                "host": peer.host,
                "port": peer.port,
                "interface": peer.ifname,
                "status": determine_status(
                    wireguard_live_peers.get(public_key),
                    previous_live_peers.get(public_key),
                    now,
                    stale_after,
                ),
            }
        )

    for peer in fastd_peers:
        peers.append(
            {
                "type": "fastd",
                "host": peer.host,
                "port": peer.port,
                "interface": peer.ifname,
                "status": "connected" if peer.public_key in fastd_connected_keys else "disconnected",
            }
        )

    peers.sort(key=lambda peer: (peer["type"], peer["host"], int(peer["port"])))
    return {"timestamp": str(now), "peers": peers}, wireguard_live_peers


def _parse_int(value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError, AttributeError):
        return 0