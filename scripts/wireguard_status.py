#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from backbone_runtime import ConfiguredPeer, LivePeer, WIREGUARD_ENV_FILE, determine_status, load_runtime_env, parse_configured_peers, read_live_peers


DEFAULT_INTERVAL = int(os.environ.get("WIREGUARD_STATUS_INTERVAL", "10"))
DEFAULT_STALE_AFTER = int(os.environ.get("WIREGUARD_STATUS_STALE_AFTER", "180"))


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"{timestamp} [wireguard] {message}", flush=True)


def format_peer_message(peer: ConfiguredPeer, live_peer: LivePeer | None) -> str:
    endpoint = live_peer.endpoint if live_peer and live_peer.endpoint else f"{peer.host}:{peer.port}"
    keepalive = live_peer.persistent_keepalive if live_peer else 0
    keepalive_text = str(keepalive) if keepalive > 0 else "off"
    return (
        f"peer {peer.host}:{peer.port} node {peer.node} via {peer.ifname} endpoint {endpoint} keepalive {keepalive_text}s "
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