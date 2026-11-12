#!/usr/bin/env python3
"""Local fastd gateway admission, without private-key handling or HTTP exposure.

Policy JSON: {"allow_keys": [...], "allow_ips": [...], "deny_keys": [...],
"deny_ips": [...]}. Values are exact public keys or IP addresses (no networks).
Deny always wins, including over previously accepted keys. Restricted mode admits
known accepted keys or an explicit allow match; open mode admits unknown keys.
No TTL is applied. Revocation is persistent until an operator calls unrevoke.
"""
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time


class AdmissionError(Exception):
    """Intentionally contains no supplied key, address, or environment values."""


def public_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise AdmissionError("invalid public key")
    return value.lower()


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".gateway-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GatewayFastd:
    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.root = Path(self.env.get("GATEWAY_FASTD_ROOT", "/data/gateway/fastd"))
        self.accepted = self.root / "accepted"
        self.connections = Path(self.env.get("GATEWAY_FASTD_CONNECTION_DIR", "/run/freifunk/gateway/fastd-connections"))
        self.state_path = self.root / "state.json"
        self.maximum = int(self.env.get("GATEWAY_FASTD_MAX_RECORDS", "512"))
        if not 1 <= self.maximum <= 65536:
            raise AdmissionError("invalid record limit")

    def _role(self):
        if self.env.get("NODE_ROLE", "node") != "gateway":
            raise AdmissionError("gateway role required")

    @contextlib.contextmanager
    def _locked(self):
        self._role()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / ".lock").open("a") as lock:
            os.chmod(self.root / ".lock", 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _state(self):
        if not self.state_path.exists():
            return {"version": 1, "records": {}}
        state = json.loads(self.state_path.read_text())
        if state.get("version") != 1 or not isinstance(state.get("records"), dict):
            raise AdmissionError("invalid gateway state")
        for key in state["records"]:
            public_key(key)
        return state

    def _save(self, state):
        atomic_write(self.state_path, json.dumps(state, sort_keys=True) + "\n")

    def _config(self, key):
        return self.accepted / ("accepted_" + hashlib.sha256(key.encode("ascii")).hexdigest() + ".conf")

    def _policy(self):
        path = self.env.get("GATEWAY_FASTD_POLICY_FILE")
        policy = json.loads(Path(path).read_text()) if path else {}
        result = {}
        for field in ("allow_keys", "deny_keys", "allow_ips", "deny_ips"):
            values = policy.get(field, [])
            if not isinstance(values, list):
                raise AdmissionError("invalid gateway policy")
            result[field] = {public_key(v) if field.endswith("keys") else str(ipaddress.ip_address(v)) for v in values}
        return result

    def _allowed(self, key, record):
        own = self.env.get("GATEWAY_FASTD_OWN_PUBLIC_KEY")
        own_file = self.env.get("GATEWAY_FASTD_OWN_PUBLIC_KEY_FILE")
        if own_file:
            own = Path(own_file).read_text().strip()
        if own and key == public_key(own):
            return False
        policy = self._policy()
        address = self.env.get("PEER_ADDRESS", "")
        # fastd normally supplies a bare IP. Ignore non-IP forms, never persist it.
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError:
            address = None
        if record.get("revoked") or key in policy["deny_keys"] or address in policy["deny_ips"]:
            return False
        restricted = self.env.get("GATEWAY_FASTD_RESTRICT", "false").lower()
        if restricted not in ("true", "false", "1", "0", "yes", "no"):
            raise AdmissionError("invalid restriction setting")
        return (restricted in ("false", "0", "no") or bool(record)
                or key in policy["allow_keys"] or address in policy["allow_ips"])

    def verify(self, key):
        key = public_key(key)
        with self._locked():
            state = self._state()
            record = state["records"].get(key, {})
            if not self._allowed(key, record):
                return False
            if not record and len(state["records"]) >= self.maximum:
                return False
            now = int(time.time())
            record = dict(record) if record else {"created": now}
            record["lastSeen"] = now
            state["records"][key] = record
            # Commit metadata before config: interruption cannot lose a revocation.
            self._save(state)
            atomic_write(self._config(key), 'key "' + key + '";\n')
            return True

    def establish(self, key):
        key = public_key(key)
        if not self.verify(key):
            return False
        with self._locked():
            # Recheck under lock in case an operator revoked between operations.
            record = self._state()["records"].get(key, {})
            if not self._allowed(key, record):
                return False
            atomic_write(self.connections / key, "")
        return True

    def disestablish(self, key):
        key = public_key(key)
        with self._locked():
            (self.connections / key).unlink(missing_ok=True)
        return True

    def revoke(self, key):
        key = public_key(key)
        with self._locked():
            state = self._state()
            if key not in state["records"] and len(state["records"]) >= self.maximum:
                raise AdmissionError("record limit reached")
            state["records"].setdefault(key, {"created": int(time.time())})["revoked"] = True
            self._save(state)
            self._config(key).unlink(missing_ok=True)
            # This does not terminate an already loaded fastd session. Keep its
            # marker truthful until a real disestablish hook or daemon reset.
        return True

    def unrevoke(self, key):
        key = public_key(key)
        with self._locked():
            state = self._state()
            # Remove tombstone; fresh admission must satisfy current policy.
            if state["records"].get(key, {}).get("revoked"):
                del state["records"][key]
                self._save(state)
        return True

    def reset(self):
        with self._locked():
            if self.connections.exists():
                for path in self.connections.iterdir():
                    if re.fullmatch(r"[0-9a-f]{64}", path.name) and (path.is_file() or path.is_symlink()):
                        path.unlink()
        return True

    def counts(self):
        """Internal aggregate only; never returns key identities or addresses."""
        with self._locked():
            records = self._state()["records"]
            active = sum(1 for key in records if (self.connections / key).is_file())
            return {"accepted": sum(not rec.get("revoked", False) for rec in records.values()), "connected": active}


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    try:
        if len(argv) != 1 or argv[0] not in ("verify", "establish", "disestablish", "reset", "revoke", "unrevoke"):
            raise AdmissionError("invalid event")
        gateway = GatewayFastd(env)
        event = argv[0]
        result = gateway.reset() if event == "reset" else getattr(gateway, event)(env.get("PEER_KEY", ""))
        if event == "revoke" and result:
            sys.stderr.write("gateway fastd revocation persisted; daemon restart required for runtime enforcement\n")
        return 0 if result else 1
    except Exception:
        # Fixed text only: exceptions may contain paths, keys, or JSON data.
        sys.stderr.write("gateway fastd event failed\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
