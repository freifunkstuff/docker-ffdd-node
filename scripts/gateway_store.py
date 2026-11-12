"""Incoming-only WireGuard desired state. No secrets, network calls or kernel writes.

Expiry is deletion (allows later readmission); revocation retains a tombstone.
All timestamps are UTC Unix seconds. Runtime is owned by the parent reconciler.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time


class StoreError(ValueError):
    """Invalid input or unavailable/corrupt state; safe generic API response."""


def validate_node(node):
    if isinstance(node, str) and re.fullmatch(r"[0-9]{1,5}", node):
        node = int(node)
    if type(node) is not int or not 0 <= node <= 65279:
        raise StoreError("invalid node")
    return node


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9+/]{43}=", key):
        raise StoreError("invalid public key")
    try:
        raw = base64.b64decode(key, validate=True)
    except ValueError:
        raise StoreError("invalid public key") from None
    if len(raw) != 32 or base64.b64encode(raw).decode() != key:
        raise StoreError("invalid public key")
    return key


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class GatewayStore:
    def __init__(self, state_dir="/data/gateway", max_peers=512, clock=time.time):
        if type(max_peers) is not int or not 1 <= max_peers <= 65536:
            raise StoreError("invalid peer limit")
        self.directory = Path(state_dir)
        self.path = self.directory / "wg-peers.json"
        self.max_peers = max_peers
        self.clock = clock
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._thread_lock:
            fd = os.open(self.directory / "wg-peers.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self):
        if not self.path.exists():
            return {"version": 1, "peers": []}
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise StoreError("state unavailable")
            state = json.loads(raw)
            if state.get("version") != 1 or not isinstance(state.get("peers"), list):
                raise ValueError()
            nodes, keys = set(), set()
            for peer in state["peers"]:
                node, key = validate_node(peer["node"]), validate_key(peer["publickey"])
                if type(peer["node"]) is not int:
                    raise ValueError()
                if node in nodes or key in keys:
                    raise ValueError()
                nodes.add(node)
                keys.add(key)
                for field in ("created_at", "last_request_at"):
                    if not _timestamp(peer[field]):
                        raise ValueError()
                if peer.get("last_handshake_at") is not None and not _timestamp(peer["last_handshake_at"]):
                    raise ValueError()
                if any(type(peer.get(field, False)) is not bool for field in ("pinned", "revoked")):
                    raise ValueError()
            return state
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise StoreError("state unavailable") from None

    def _write(self, state):
        fd, name = tempfile.mkstemp(prefix=".wg-peers-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(state, stream, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def snapshot(self):
        with self._locked():
            return copy.deepcopy(self._read()["peers"])

    def enroll(self, node, key, own_node, restricted=False):
        node, key, own_node = validate_node(node), validate_key(key), validate_node(own_node)
        if node == own_node:
            return "RequestConflict", None
        with self._locked():
            state = self._read()
            for peer in state["peers"]:
                if peer["node"] == node or peer["publickey"] == key:
                    if peer.get("revoked"):
                        return "RequestRevoked", None
                    if peer["node"] != node:
                        return "RequestKeyConflict", None
                    if peer["publickey"] != key:
                        return "RequestConflict", None
                    # Diagnostic only: cap known-client write amplification and
                    # never let HTTP retries extend handshake-based retention.
                    now = self.clock()
                    if now >= peer["last_request_at"] + 60:
                        peer["last_request_at"] = now
                        self._write(state)
                    return "RequestAlreadyRegistered", copy.deepcopy(peer)
            if restricted:
                return "Restricted", None
            if len(state["peers"]) >= self.max_peers:
                return "RequestLimitReached", None
            now = self.clock()
            peer = dict(node=node, publickey=key, created_at=now, last_request_at=now,
                        last_handshake_at=None, pinned=False, revoked=False,
                        origin="incoming", runtime_state="Pending")
            state["peers"].append(peer)
            self._write(state)
            return "RequestAccepted", copy.deepcopy(peer)

    def update_handshakes(self, handshakes):
        with self._locked():
            state = self._read()
            now = self.clock()
            changed = False
            for peer in state["peers"]:
                value = handshakes.get(peer["publickey"], 0)
                if _timestamp(value) and 0 < value <= now and value > (peer["last_handshake_at"] or 0):
                    peer["last_handshake_at"] = value
                    changed = True
            if changed:
                self._write(state)

    def expired(self, now=None, days=30):
        now = self.clock() if now is None else now
        if not _timestamp(now) or not _timestamp(days) or days <= 0:
            raise StoreError("invalid retention")
        return [p for p in self.snapshot() if not p.get("pinned") and not p.get("revoked")
                and now > max(p["created_at"], p["last_handshake_at"] or 0) + days * 86400]

    def revoke(self, node):
        node = validate_node(node)
        with self._locked():
            state = self._read()
            for peer in state["peers"]:
                if peer["node"] == node:
                    peer["revoked"] = True
                    peer["runtime_state"] = "Pending"
                    self._write(state)
                    return True
            return False

    def expire(self, now=None, days=30, dry_run=True):
        # Recheck under the same lock to prevent deleting a refreshed handshake.
        with self._locked():
            now = self.clock() if now is None else now
            if not _timestamp(now) or not _timestamp(days) or days <= 0:
                raise StoreError("invalid retention")
            state = self._read()
            candidates = [p for p in state["peers"] if not p.get("pinned") and not p.get("revoked")
                          and now > max(p["created_at"], p["last_handshake_at"] or 0) + days * 86400]
            if not dry_run and candidates:
                nodes = {p["node"] for p in candidates}
                state["peers"] = [p for p in state["peers"] if p["node"] not in nodes]
                self._write(state)
            return candidates
