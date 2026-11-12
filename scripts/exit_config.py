"""Strict, generic exit configuration. Exceptions never contain configuration values."""
from __future__ import annotations
import base64
import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

class ConfigError(ValueError):
    pass

@dataclass(frozen=True)
class Exit:
    name: str
    comment: str
    private_key: str = field(repr=False)
    addresses: tuple[str, ...]
    dns: tuple[str, ...]
    public_key: str = field(repr=False)
    endpoint: str

@dataclass(frozen=True)
class ExitConfig:
    exits: tuple[Exit, ...]

def _fields(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= obj.keys() or obj.keys() - set(required) - set(optional):
        raise ConfigError('invalid configuration fields')

def _key(value):
    try:
        if not isinstance(value, str) or len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError()
    except Exception:
        raise ConfigError('invalid WireGuard key') from None
    return value

def parse_config(data):
    _fields(data, ('exits',))
    if not isinstance(data['exits'], list) or len(data['exits']) > 8:
        raise ConfigError('invalid exits list')
    exits = []
    for item in data['exits']:
        _fields(item, ('name', 'wireguard'), ('comment',))
        name = item['name']
        comment = item.get('comment', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name) or name in ('__proto__', 'constructor', 'prototype') or any(e.name == name for e in exits):
            raise ConfigError('invalid or duplicate exit name')
        if not isinstance(comment, str) or len(comment) > 512:
            raise ConfigError('invalid comment')
        wg = item['wireguard']
        _fields(wg, ('privateKey', 'addresses', 'dns', 'peer'))
        _fields(wg['peer'], ('publicKey', 'endpoint'))
        addresses, dns = [], []
        try:
            for key, result, parser in [('addresses', addresses, ipaddress.ip_interface), ('dns', dns, ipaddress.ip_address)]:
                if not isinstance(wg[key], list) or not 1 <= len(wg[key]) <= 4:
                    raise ValueError()
                for value in wg[key]:
                    if not isinstance(value, str):
                        raise ValueError()
                    parsed = parser(value)
                    ip = parsed.ip if key == 'addresses' else parsed
                    if parsed.version != 4 or ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                        raise ValueError()
                    if key == 'addresses' and (ip in ipaddress.ip_network('10.200.0.0/15') or ip in ipaddress.ip_network('10.203.0.0/16') or any(ipaddress.ip_interface(a).ip == ip for a in result)):
                        raise ValueError()
                    result.append(str(parsed))
        except Exception:
            raise ConfigError('invalid IPv4 addresses or DNS') from None
        if set(dns) & {str(ipaddress.ip_interface(address).ip) for address in addresses}:
            raise ConfigError('DNS must not be a tunnel local address')
        endpoint = wg['peer']['endpoint']
        if not isinstance(endpoint, str) or not re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}', endpoint) or not 1 <= int(endpoint.rsplit(':', 1)[1]) <= 65535:
            raise ConfigError('invalid IPv4 endpoint')
        exits.append(Exit(name, comment, _key(wg['privateKey']), tuple(addresses), tuple(dns), _key(wg['peer']['publicKey']), endpoint))
    return ExitConfig(tuple(exits))

def load_config(path):
    try:
        import yaml
        with Path(path).open('r', encoding='utf-8') as handle:
            text = handle.read(65537)
        if len(text.encode('utf-8')) > 65536:
            raise ValueError()
        data = yaml.safe_load(text)
    except Exception:
        raise ConfigError('cannot load exit configuration') from None
    return parse_config(data)
