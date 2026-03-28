from __future__ import annotations

import copy
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DATA_FILE = Path(os.environ.get("NODE_YAML", "/data/node.yaml"))
DEFAULTS_FILE = Path(os.environ.get("DEFAULTS_FILE", "/usr/local/share/freifunk/defaults.yaml"))

STATIC_CONFIG: dict[str, Any] = {
    "system": {
        "node_type": "server",
        "autoupdate": 0,
    },
    "fastd": {
        "interface": "tbb_fastd",
        "port": 5002,
        "mtu": 1200,
        "method": "null",
        "log_level": "info",
    },
    "bmxd": {
        "daemon_runtime_dir": "/var/run/bmx",
        "gateway_usage_file": "/data/statistic/gateway_usage",
        "primary_interface": "bmx_prime",
        "mesh_network": "10.200.0.0/16",
        "policy_rule_to": "10.200.0.0/15",
        "policy_rule_priority": 500,
        "policy_rule_table": 64,
        "netid": 0,
        "only_community_gw": 1,
        "routing_class": 3,
        "preferred_gateway": "",
        "gateway_hysteresis": 20,
        "path_hysteresis": 3,
        "hop_penalty": 5,
        "lateness_penalty": 10,
        "wireless_ogm_clone": 100,
        "udp_data_size": 512,
        "ogm_interval": 5000,
        "purge_timeout": 35,
        "gateway_script": "/usr/lib/bmxd/bmxd-gateway.py",
    },
}


@dataclass(frozen=True)
class ConfigIssue:
    level: str
    key: str
    message: str


@dataclass(frozen=True)
class ConfigResult:
    values: dict[str, Any]
    warnings: list[ConfigIssue]
    errors: list[ConfigIssue]


ConfigSchema = tuple[dict[str, Any], ...]


def node_addresses(node_id: int) -> dict[str, str]:
    middle = (node_id // 255) % 256
    minor = (node_id % 255) + 1
    return {
        "primary_ip": f"10.200.{middle}.{minor}",
        "nonprimary_ip": f"10.201.{middle}.{minor}",
        "wireguard_ip": f"10.203.{middle}.{minor}",
        "mesh_network": "10.200.0.0/16",
        "mesh_prefix": "16",
        "mesh_broadcast": "10.255.255.255",
    }


def load_defaults() -> dict[str, Any]:
    defaults_file = Path(os.environ.get("DEFAULTS_FILE", str(DEFAULTS_FILE)))
    if not defaults_file.exists():
        raise SystemExit(f"defaults file not found: {defaults_file}")

    with defaults_file.open("r", encoding="utf-8") as handle:
        defaults = yaml.safe_load(handle) or {}

    if not isinstance(defaults, dict):
        raise SystemExit(f"invalid defaults file: {defaults_file}")

    return defaults


def load_state() -> dict[str, Any]:
    data_file = Path(os.environ.get("NODE_YAML", str(DATA_FILE)))
    if not data_file.exists():
        return {}

    with data_file.open("r", encoding="utf-8") as handle:
        state = yaml.safe_load(handle) or {}

    if not isinstance(state, dict):
        raise SystemExit(f"ungueltiges YAML in {data_file}")

    return state


def save_state(state: dict[str, Any]) -> None:
    data_file = Path(os.environ.get("NODE_YAML", str(DATA_FILE)))
    data_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(data_file.parent), delete=False
    ) as handle:
        yaml.safe_dump(state, handle, default_flow_style=False, sort_keys=False)
        temp_name = handle.name
    os.replace(temp_name, data_file)


def build_base_config() -> dict[str, Any]:
    return copy.deepcopy(STATIC_CONFIG)


def resolve_config(
    *,
    schema: ConfigSchema,
    defaults: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    base_values: dict[str, Any] | None = None,
) -> ConfigResult:
    defaults = load_defaults() if defaults is None else defaults
    env_map = dict(os.environ) if env is None else dict(env)

    values = build_base_config() if base_values is None else copy.deepcopy(base_values)
    warnings: list[ConfigIssue] = []
    errors: list[ConfigIssue] = []

    for spec in schema:
        env_name = str(spec["env"])
        path = tuple(spec["path"])
        raw_value = _pick_raw_value(spec, env_map, defaults)

        if _is_missing(raw_value, spec):
            level = "warning" if spec.get("required_level") == "warning" else "error"
            if spec.get("required"):
                message = f"{env_name} is required"
                issue = ConfigIssue(level=level, key=env_name, message=message)
                if level == "warning":
                    warnings.append(issue)
                else:
                    errors.append(issue)
            _set_path(values, path, spec.get("missing_value"))
            continue

        try:
            value = _cast_value(raw_value, spec)
        except ValueError as exc:
            errors.append(ConfigIssue(level="error", key=env_name, message=str(exc)))
            _set_path(values, path, spec.get("missing_value"))
            continue

        enum_values = spec.get("enum")
        if enum_values and value not in enum_values:
            errors.append(
                ConfigIssue(
                    level="error",
                    key=env_name,
                    message=f"{env_name} must be one of: {', '.join(str(item) for item in enum_values)}",
                )
            )
            _set_path(values, path, value)
            continue

        minimum = spec.get("min")
        if minimum is not None and value < minimum:
            errors.append(
                ConfigIssue(
                    level="error",
                    key=env_name,
                    message=f"{env_name} must be >= {minimum}",
                )
            )

        maximum = spec.get("max")
        if maximum is not None and value > maximum:
            errors.append(
                ConfigIssue(
                    level="error",
                    key=env_name,
                    message=f"{env_name} must be <= {maximum}",
                )
            )

        _set_path(values, path, value)
    return ConfigResult(values=values, warnings=warnings, errors=errors)


def format_issues(issues: list[ConfigIssue]) -> list[str]:
    return [f"{issue.key}: {issue.message}" for issue in issues]


def require_valid_config(
    *,
    schema: ConfigSchema,
    defaults: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    logger: Callable[[str], None] | None = None,
    base_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = resolve_config(
        schema=schema,
        defaults=defaults,
        env=env,
        base_values=base_values,
    )
    for warning in format_issues(result.warnings):
        if logger is not None:
            logger(f"config warning: {warning}")
    if result.errors:
        for error in format_issues(result.errors):
            if logger is not None:
                logger(f"config error: {error}")
        raise SystemExit(1)
    return result.values


def _pick_raw_value(spec: dict[str, Any], env: dict[str, str], defaults: dict[str, Any]) -> Any:
    env_name = str(spec["env"])
    env_value = env.get(env_name)
    if env_value is not None:
        if spec.get("blank_env_uses_default") and _is_blank_string(env_value):
            env_value = None
        else:
            return env_value

    if env_value is not None:
        return env_value

    for alias in spec.get("aliases", ()):
        alias_value = env.get(str(alias))
        if alias_value is not None:
            return alias_value

    default_key = spec.get("default_key")
    if default_key is not None and default_key in defaults:
        return defaults[default_key]

    return spec.get("default")


def _is_missing(value: Any, spec: dict[str, Any]) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not spec.get("allow_blank", False) and value.strip() == "":
        return True
    return False


def _is_blank_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() == ""


def _cast_value(raw_value: Any, spec: dict[str, Any]) -> Any:
    value_type = spec.get("type", "str")
    env_name = str(spec["env"])

    if value_type == "str":
        return str(raw_value).strip()
    if value_type == "int":
        try:
            return int(str(raw_value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{env_name} must be an integer") from exc
    if value_type == "float":
        try:
            return float(str(raw_value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{env_name} must be a number") from exc

    raise ValueError(f"unsupported config type for {env_name}: {value_type}")


def _set_path(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cursor = target
    for part in path[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[path[-1]] = value
