"""Explicit functional gateway role; NODE_TYPE remains descriptive metadata."""
from __future__ import annotations
import os
from pathlib import Path
import re
from node_config import load_state

SERVICES = ('gateway-api', 'gateway-runtime', 'gateway-control')


def prepare_role(service_root=Path('/etc/service'), runtime=Path('/run/freifunk/gateway')):
    role = os.environ.get('NODE_ROLE', 'node')
    if role not in ('node', 'gateway'):
        raise ValueError('invalid NODE_ROLE')
    enabled = role == 'gateway'
    if enabled and not os.environ.get('BACKBONE_PEERS', '').strip() and os.environ.get('BACKBONE_ENABLED', '1').lower() not in ('0', 'false', 'no', 'off'):
        raise ValueError('gateway requires explicit outbound peers or BACKBONE_ENABLED=0')
    if enabled and os.environ.get('GATEWAY_ALLOW_IDENTITY_GENERATION', 'false').lower() != 'true':
        try:
            state = load_state()
            if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', state.get('wireguard', {}).get('secret', '')) or not re.fullmatch(r'[0-9a-fA-F]{64}', state.get('fastd', {}).get('secret', '')):
                raise ValueError()
        except Exception:
            raise ValueError('gateway requires pre-existing private identity; initialize explicitly first') from None
    runtime.mkdir(parents=True, exist_ok=True)
    for name in SERVICES:
        directory = service_root / name
        if not directory.is_dir():
            raise ValueError('gateway service unavailable')
        down = directory / 'down'
        if enabled:
            if down.exists():
                assert down.resolve().parent == directory.resolve() and down.name == 'down'
                down.unlink()
        else:
            down.write_text('')
    marker = runtime / 'enabled'
    if enabled:
        marker.write_text('gateway\n')
        marker.chmod(0o644)
    elif marker.exists():
        assert marker.resolve().parent == runtime.resolve() and marker.name == 'enabled'
        marker.unlink()
    return enabled


if __name__ == '__main__':
    try:
        prepare_role()
    except Exception:
        raise SystemExit('gateway role initialization failed (check role and identity availability)') from None
