"""Effective node identity, keeping temporary and Registry state independent."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess


def registration_mode(config: dict) -> str:
    mode = config.get("registrar", {}).get("mode", "registered")
    if mode not in ("registered", "temporary"):
        raise ValueError("NODE_REGISTRATION_MODE must be registered or temporary")
    return mode


def container_uptime_seconds() -> float:
    """PID 1 lifetime, not the host's uptime (stat field 22 is starttime)."""
    host_uptime = float(Path("/proc/uptime").read_text().split()[0])
    stat = Path("/proc/1/stat").read_text()
    # comm can contain spaces and parentheses; fields after its final ')' start at 3.
    start_ticks = int(stat[stat.rfind(")") + 1:].split()[19])
    return max(0.0, host_uptime - start_ticks / os.sysconf("SC_CLK_TCK"))


def temporary_node_id(uptime_seconds: float, ip_addr_output: str) -> int:
    digest = hashlib.md5(ip_addr_output.encode("utf-8")).hexdigest()
    digits = "".join(char for char in digest if char in "123456789")[:6]
    # A digest with no nonzero digits contributes zero, rather than randomness.
    return 900 + ((int(uptime_seconds) + int(digits or "0")) % 100)


def ensure_temporary_node_id(state: dict) -> int:
    temporary = state.setdefault("temporary", {})
    if "node_id" in temporary:
        try:
            node_id = int(temporary["node_id"])
        except (TypeError, ValueError) as exc:
            raise SystemExit("invalid temporary.node_id in state file") from exc
        if not 900 <= node_id <= 999:
            raise SystemExit("invalid temporary.node_id in state file (expected 900..999)")
        return node_id
    output = subprocess.run(["ip", "addr"], capture_output=True, text=True, check=True).stdout
    node_id = temporary_node_id(container_uptime_seconds(), output)
    temporary["node_id"] = node_id
    return node_id


def effective_node_id(config: dict, state: dict):
    section = "temporary" if registration_mode(config) == "temporary" else "registration"
    return state.get(section, {}).get("node_id")
