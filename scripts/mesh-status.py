#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path


STARTUP_INTERVAL = int(os.environ.get("MESH_STATUS_STARTUP_INTERVAL", "1"))
STEADY_INTERVAL = int(os.environ.get("MESH_STATUS_INTERVAL", "5"))
PING_TIMEOUT = int(os.environ.get("MESH_STATUS_PING_TIMEOUT", "1"))
MAX_LINK_TARGETS = int(os.environ.get("MESH_STATUS_MAX_LINK_TARGETS", "3"))
STABLE_AFTER = int(os.environ.get("MESH_STATUS_STABLE_AFTER", "30"))
OUTPUT_PATH = Path(os.environ.get("MESH_STATUS_OUTPUT", "/run/freifunk/state/mesh-status.json"))
WEBROOT = Path(os.environ.get("MESH_STATUS_WEBROOT", "/run/freifunk/www"))
WEB_LINK = os.environ.get("MESH_STATUS_WEB_LINK", "mesh-status.json")

LINK_PATTERN = re.compile(
    r"^(?P<neighbor>\d+\.\d+\.\d+\.\d+)\s+(?P<iface>\S+)\s+(?P<originator>\d+\.\d+\.\d+\.\d+)\s+(?P<rtq>\d+)\s+(?P<rq>\d+)\s+(?P<tq>\d+)"
)
GATEWAY_PATTERN = re.compile(
    r"^(?P<selected>=?>)?\s*(?P<originator>\d+\.\d+\.\d+\.\d+)\s+"
    r"(?P<best_next_hop>\d+\.\d+\.\d+\.\d+)\s+"
    r"(?P<brc>\d+),\s*(?P<community>[01]),\s*(?P<speed>\S+)"
)


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"{timestamp} [mesh-status] {message}", flush=True)


def read_links() -> list[dict[str, str]]:
    result = subprocess.run(["bmxd", "-c", "--links"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return []

    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = LINK_PATTERN.match(line)
        if not match:
            continue
        originator = match.group("originator")
        interface = match.group("iface")
        key = (originator, interface)
        if key in seen:
            continue
        seen.add(key)
        links.append(
            {
                "originator": originator,
                "neighbor": match.group("neighbor"),
                "interface": interface,
            }
        )
    return links


def select_link_targets(links: list[dict[str, str]], max_targets: int) -> list[dict[str, str]]:
    if max_targets <= 0:
        return []
    return links[:max_targets]


def read_selected_gateway() -> str:
    result = subprocess.run(["bmxd", "-c", "--gateways"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return ""

    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = GATEWAY_PATTERN.match(line)
        if match and match.group("selected"):
            return match.group("originator")
    return ""


def ping(ip_address: str, timeout: int) -> bool:
    result = subprocess.run(
        ["ping", "-c", "1", "-W", str(timeout), ip_address],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def build_status_payload(
    *,
    selected_gateway: str,
    gateway_connected: bool,
    link_targets: list[dict[str, str]],
    reachable_targets: set[str],
    connected_duration: int,
) -> dict[str, object]:
    checked_links = len(link_targets)
    reachable_count = len(reachable_targets)
    mesh_connected = reachable_count > 0
    mesh_stable = mesh_connected and connected_duration >= STABLE_AFTER

    return {
        "updated_at": datetime.now().astimezone().isoformat(),
        "mesh": {
            "connected": mesh_connected,
            "stable": mesh_stable,
            "checked_links": checked_links,
            "reachable_links": reachable_count,
            "connected_duration": connected_duration,
            "stable_after": STABLE_AFTER,
        },
        "gateway": {
            "selected": selected_gateway,
            "connected": gateway_connected,
        },
    }


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o644)
    os.replace(temp_name, path)


def publish_web_link(output_path: Path, webroot: Path, link_name: str) -> None:
    webroot.mkdir(parents=True, exist_ok=True)
    link_path = webroot / link_name
    if link_path.is_symlink() or link_path.exists():
        link_path.unlink()
    link_path.symlink_to(output_path)


def state_signature(payload: dict[str, object]) -> tuple[object, ...]:
    return mesh_state(payload), gateway_state(payload)


def mesh_state(payload: dict[str, object]) -> str:
    mesh = payload["mesh"]
    if mesh["stable"]:
        return "stable"
    if mesh["connected"]:
        return "connected"
    return "disconnected"


def gateway_state(payload: dict[str, object]) -> tuple[str, str]:
    gateway = payload["gateway"]
    selected = str(gateway["selected"] or "")
    status = "connected" if gateway["connected"] else "disconnected"
    return selected, status


def describe_state(payload: dict[str, object]) -> str:
    gateway_ip, gateway_status = gateway_state(payload)
    if gateway_ip:
        gateway_text = f"Gateway {gateway_ip} {gateway_status}"
    else:
        gateway_text = f"Gateway {gateway_status}"
    return f"Mesh {mesh_state(payload)}, {gateway_text}"


def run_loop(
    startup_interval: int,
    steady_interval: int,
    timeout: int,
    max_link_targets: int,
    stable_after: int,
) -> None:
    log(
        f"starting mesh status loop with startup interval {startup_interval}s, steady interval {steady_interval}s, stable after {stable_after}s and {max_link_targets} link target(s)"
    )
    previous_signature: tuple[object, ...] | None = None
    startup_mode = True
    mesh_connected_since: float | None = None

    while True:
        loop_started = time.monotonic()
        links = read_links()
        link_targets = select_link_targets(links, max_link_targets)
        selected_gateway = read_selected_gateway()

        reachable_targets: set[str] = set()
        for entry in link_targets:
            if ping(entry["originator"], timeout):
                reachable_targets.add(entry["originator"])

        gateway_connected = False
        if selected_gateway:
            if selected_gateway in reachable_targets:
                gateway_connected = True
            else:
                gateway_connected = ping(selected_gateway, timeout)

        mesh_connected = bool(reachable_targets)
        if mesh_connected:
            if mesh_connected_since is None:
                mesh_connected_since = loop_started
            connected_duration = int(loop_started - mesh_connected_since)
        else:
            mesh_connected_since = None
            connected_duration = 0

        payload = build_status_payload(
            selected_gateway=selected_gateway,
            gateway_connected=gateway_connected,
            link_targets=link_targets,
            reachable_targets=reachable_targets,
            connected_duration=connected_duration,
        )
        write_json_atomic(OUTPUT_PATH, payload)
        publish_web_link(OUTPUT_PATH, WEBROOT, WEB_LINK)

        signature = state_signature(payload)
        if signature != previous_signature:
            log(describe_state(payload))
            previous_signature = signature

        if startup_mode and (payload["mesh"]["connected"] or payload["gateway"]["connected"]):
            startup_mode = False

        time.sleep(startup_interval if startup_mode else steady_interval)


def main() -> int:
    run_loop(STARTUP_INTERVAL, STEADY_INTERVAL, PING_TIMEOUT, MAX_LINK_TARGETS, STABLE_AFTER)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())