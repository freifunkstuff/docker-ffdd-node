#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


WIREGUARD_ENV_FILE = Path(os.environ.get("WIREGUARD_ENV_FILE", "/run/freifunk/wireguard/wireguard.env"))
DEFAULT_INTERVAL = int(os.environ.get("WIREGUARD_STATUS_INTERVAL", "10"))
DEFAULT_STALE_AFTER = int(os.environ.get("WIREGUARD_STATUS_STALE_AFTER", "180"))


@dataclass(frozen=True)
class ConfiguredPeer:
    host: str
    port: str
    public_key: str
    node: str
    ifname: str

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port} node {self.node} via {self.ifname}"


@dataclass(frozen=True)
class LivePeer:
    endpoint: str
    latest_handshake: int
    transfer_rx: int
    transfer_tx: int
    persistent_keepalive: int


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"{timestamp} [wireguard] {message}", flush=True)


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
        endpoint = parts[2].strip()
        latest_handshake = _parse_int(parts[4])
        transfer_rx = _parse_int(parts[5])
        transfer_tx = _parse_int(parts[6])
        persistent_keepalive = _parse_int(parts[7])
        peers[public_key] = LivePeer(
            endpoint=endpoint,
            latest_handshake=latest_handshake,
            transfer_rx=transfer_rx,
            transfer_tx=transfer_tx,
            persistent_keepalive=persistent_keepalive,
        )

    return peers


def _parse_int(value: str) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError, AttributeError):
        return 0


def determine_status(live_peer: LivePeer | None, previous_live_peer: LivePeer | None, now: int, stale_after: int) -> str:
    if live_peer is None:
        return "never-seen"

    if live_peer.latest_handshake > 0 and now - live_peer.latest_handshake <= stale_after:
        return "connected"

    if previous_live_peer is None:
        if live_peer.latest_handshake == 0 and live_peer.transfer_rx == 0 and live_peer.transfer_tx == 0:
            return "never-seen"
        return "stale"

    if live_peer.transfer_rx != previous_live_peer.transfer_rx or live_peer.transfer_tx != previous_live_peer.transfer_tx:
        return "connected"

    if live_peer.latest_handshake == 0 and live_peer.transfer_rx == 0 and live_peer.transfer_tx == 0:
        return "never-seen"

    return "stale"


def format_peer_message(peer: ConfiguredPeer, live_peer: LivePeer | None) -> str:
    endpoint = live_peer.endpoint if live_peer and live_peer.endpoint else f"{peer.host}:{peer.port}"
    keepalive = live_peer.persistent_keepalive if live_peer else 0
    keepalive_text = str(keepalive) if keepalive > 0 else "off"
    return (
        f"peer {peer.label} endpoint {endpoint} keepalive {keepalive_text}s "
        f"key {peer.public_key[:12]}..."
    )


def snapshot_config() -> tuple[str, dict[str, ConfiguredPeer]]:
    return parse_configured_peers(load_runtime_env(WIREGUARD_ENV_FILE))


def wait_for_runtime_config(interval: int) -> None:
    while not WIREGUARD_ENV_FILE.exists():
        time.sleep(interval)


def run_loop(interval: int, stale_after: int) -> int:
    wait_for_runtime_config(interval)
    previous_config_keys: set[str] | None = None
    previous_statuses: dict[str, str] = {}
    previous_live_peers: dict[str, LivePeer] = {}
    previous_endpoints: dict[str, str] = {}
    waiting_logged = False

    while True:
        interface, configured_peers = snapshot_config()
        config_keys = set(configured_peers)
        config_changed = previous_config_keys != config_keys

        if not configured_peers:
            if not waiting_logged:
                log("no wireguard peers configured; waiting")
                waiting_logged = True
            previous_config_keys = None
            previous_statuses = {}
            previous_live_peers = {}
            previous_endpoints = {}
            time.sleep(interval)
            continue

        waiting_logged = False
        live_peers = read_live_peers(interface)
        now = int(time.time())

        if config_changed:
            log(f"watching {len(configured_peers)} wireguard peer(s) on {interface}")
            for public_key in sorted(configured_peers):
                peer = configured_peers[public_key]
                live_peer = live_peers.get(public_key)
                status = determine_status(live_peer, previous_live_peers.get(public_key), now, stale_after)
                log(f"{format_peer_message(peer, live_peer)} status {status}")

        for removed_key in sorted(set(previous_statuses) - config_keys):
            log(f"peer {removed_key[:12]}... removed from configuration")

        for public_key in sorted(configured_peers):
            peer = configured_peers[public_key]
            live_peer = live_peers.get(public_key)
            previous_live_peer = previous_live_peers.get(public_key)
            status = determine_status(live_peer, previous_live_peer, now, stale_after)
            previous_status = previous_statuses.get(public_key)
            endpoint = live_peer.endpoint if live_peer and live_peer.endpoint else f"{peer.host}:{peer.port}"
            previous_endpoint = previous_endpoints.get(public_key)

            if not config_changed and previous_status is not None and endpoint != previous_endpoint:
                log(f"peer {peer.label} endpoint changed to {endpoint}")

            if not config_changed and previous_status != status:
                log(f"peer {peer.label} status {status}")

            previous_statuses[public_key] = status
            previous_endpoints[public_key] = endpoint

        previous_live_peers = live_peers
        previous_config_keys = config_keys
        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Polling interval in seconds")
    parser.add_argument("--stale-after", type=int, default=DEFAULT_STALE_AFTER, help="Seconds without handshake or traffic before a peer becomes stale")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_loop(interval=max(1, args.interval), stale_after=max(1, args.stale_after))


if __name__ == "__main__":
    raise SystemExit(main())