"""Read only sanitized exit metadata and real interface counters for history."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import ipaddress


def read_node_primary() -> str | None:
    """Use registrar-published identity metadata, never generate/register identity."""
    explicit = os.environ.get('EXIT_NODE_IP', '').strip()
    if not explicit:
        path = Path(os.environ.get('BMXD_ENV_FILE', '/run/freifunk/bmxd/bmxd.env'))
        try:
            for line in path.read_text().splitlines():
                if line.startswith('BMXD_PRIMARY_IP='):
                    explicit = line.split('=', 1)[1].strip().strip('"')
                    break
        except OSError:
            return None
    try:
        address = ipaddress.IPv4Address(explicit)
        return str(address) if address in ipaddress.IPv4Network('10.200.0.0/16') else None
    except ValueError:
        return None

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
INTERFACE = re.compile(r'^wgx[0-9a-f]{10}$')
RESERVED = {'__proto__', 'prototype', 'constructor'}


def read_exit_generations(path: Path | None = None, sysfs: Path = Path('/sys/class/net')) -> dict:
    """Private accounting identity: namespace/boot/interface, never public history."""
    path = path or Path(os.environ.get('EXIT_STATUS_FILE', '/run/freifunk/www/exits.json'))
    try:
        if path.stat().st_size > 32768:
            return {}
        profiles = json.loads(path.read_text()).get('exits', [])
        namespace = os.stat('/proc/self/ns/net').st_ino
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(profiles, list):
        return {}
    result = {}
    for profile in profiles[:8]:
        if not isinstance(profile, dict): continue
        name, interface = profile.get('name'), profile.get('interface')
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED: continue
        if not isinstance(interface, str) or not INTERFACE.fullmatch(interface): continue
        try:
            index = int((sysfs / interface / 'ifindex').read_text().strip())
            if index > 0: result[name] = f'{boot}:{namespace}:{index}'
        except (OSError, ValueError): pass
    return result


def read_exit_counters(path: Path | None = None, sysfs: Path = Path('/sys/class/net')) -> dict:
    path = path or Path(os.environ.get('EXIT_STATUS_FILE', '/run/freifunk/www/exits.json'))
    try:
        if path.stat().st_size > 32768:
            return {}
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    result = {}
    profiles = status.get('exits', []) if isinstance(status, dict) else []
    if not isinstance(profiles, list):
        return {}
    for profile in profiles[:8]:
        if not isinstance(profile, dict):
            continue
        name, interface = profile.get('name'), profile.get('interface')
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED:
            continue
        if not isinstance(interface, str) or not INTERFACE.fullmatch(interface):
            continue
        counters = {}
        for key in ('rx_bytes', 'tx_bytes'):
            try:
                value = int((sysfs / interface / 'statistics' / key).read_text().strip())
                if value >= 0:
                    counters[key] = value
            except (OSError, ValueError):
                pass
        result[name] = counters
    return result
