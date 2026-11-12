"""Loopback-only wg.cgi-compatible admission. Never reads private-key files.

Restricted mode intentionally permits exact known node/key reconnects, unlike the
legacy CGI. Success requires reconciler confirmation, not a WireGuard handshake.
Runtime confirmation: {"peers":{"<node>":{"key":"...","applied":true}}}.
"""
from __future__ import annotations
import collections
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import parse_qsl, urlsplit
from gateway_store import GatewayStore, StoreError, validate_key, validate_node
from gateway_health import fresh_ready


def bounded_json(path, limit=1048576):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise StoreError("metadata unavailable")
    return json.loads(data)


class GatewayApplication:
    def __init__(self, store=None, role=None, identity_file="/run/freifunk/gateway/identity.json",
                 runtime_file=None, restricted=False, apply=None, wait_seconds=3, rate_limit=120,
                 outgoing_file=None):
        self.role = role if role is not None else os.environ.get("NODE_ROLE", "node")
        # Role-off must not even parse gateway settings or construct its store.
        self.store = store
        if self.role == "gateway" and self.store is None:
            self.store = GatewayStore(os.environ.get("GATEWAY_STATE_DIR", "/data/gateway"),
                                      int(os.environ.get("GATEWAY_MAX_PEERS", "512")))
        self.identity_file = identity_file
        self.runtime_file = runtime_file or os.environ.get("GATEWAY_RUNTIME_STATUS_FILE", "/run/freifunk/gateway/runtime.json")
        self.outgoing_file = outgoing_file or os.environ.get("GATEWAY_OUTGOING_FILE", "/run/freifunk/gateway/outgoing.json")
        self.restricted = restricted
        self.apply = apply
        self.wait_seconds = min(3, max(0, wait_seconds))
        self.rate_limit = rate_limit
        self.requests = collections.deque()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(16)

    def _applied(self, peer):
        if self.apply is not None:
            return self.apply(dict(peer)) is True
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                state = bounded_json(self.runtime_file)
                entry = state["peers"][str(peer["node"])]
                if fresh_ready(state) and entry.get("key") == peer["publickey"] and entry.get("applied") is True:
                    return True
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(.05, remaining))

    def handle(self, target, method="GET"):
        if self.role != "gateway":
            return 404, {"status": "NotFound"}
        if len(target) > 2048:
            return 414, {"status": "RequestFailed"}
        parsed = urlsplit(target)
        if parsed.path != "/wg.cgi":
            return 404, {"status": "NotFound"}
        if method != "GET":
            return 405, {"status": "RequestFailed"}
        if not self.slots.acquire(blocking=False):
            return 503, {"status": "RequestFailed"}
        try:
            return self._handle(parsed)
        except Exception:
            # Do not return raw exceptions, key paths or any private configuration.
            return 200, {"status": "RequestFailed"}
        finally:
            self.slots.release()

    def _handle(self, parsed):
        try:
            identity = bounded_json(self.identity_file, 4096)
            server = {"node": validate_node(identity["node"]), "key": validate_key(identity["key"]), "port": identity["port"]}
            if type(server["port"]) is not int or not 1 <= server["port"] <= 65535:
                raise StoreError("invalid port")
        except (OSError, ValueError, KeyError, TypeError):
            return 200, {"status": "NotConfigured"}
        if not parsed.query:
            return 200, {"status": "Restricted" if self.restricted else "NotRestricted", "server": server}
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        params = {}
        for name, value in pairs:
            name = name.lower()
            if name not in ("node", "key") or name in params:
                raise StoreError("invalid query")
            params[name] = value
        if set(params) != {"node", "key"}:
            raise StoreError("invalid query")
        node, key = validate_node(params["node"]), validate_key(params["key"])
        known = any(p["node"] == node and p["publickey"] == key and not p.get("revoked")
                    for p in self.store.snapshot())
        # Discovery and exact reconnects must survive a full-fleet cold boot.
        # New claims and conflicts share a bounded mutation-attempt budget.
        if not known:
            with self.lock:
                now = time.monotonic()
                while self.requests and self.requests[0] <= now - 60:
                    self.requests.popleft()
                if len(self.requests) >= self.rate_limit:
                    return 429, {"status": "RequestFailed"}
                self.requests.append(now)
        if key == server["key"]:
            return 200, {"status": "RequestKeyConflict"}
        # Authoritative public-only outgoing desired state must exist even if empty.
        # Check before persistence: a rejected claim must not poison the runtime union.
        outgoing = bounded_json(self.outgoing_file)
        if not isinstance(outgoing, dict) or not isinstance(outgoing.get("peers"), list):
            raise StoreError("outgoing metadata unavailable")
        outgoing_nodes, outgoing_keys = {}, {}
        for entry in outgoing["peers"]:
            other_node, other_key = validate_node(entry["node"]), validate_key(entry["key"])
            if (other_node in outgoing_nodes and outgoing_nodes[other_node] != other_key
                    or other_key in outgoing_keys and outgoing_keys[other_key] != other_node):
                raise StoreError("outgoing metadata conflicts")
            outgoing_nodes[other_node] = other_key
            outgoing_keys[other_key] = other_node
        if node in outgoing_nodes and outgoing_nodes[node] != key:
            return 200, {"status": "RequestConflict"}
        if key in outgoing_keys and outgoing_keys[key] != node:
            return 200, {"status": "RequestKeyConflict"}
        # An exact static outgoing pair is already operator-authorized, including
        # after its incoming record expired. Revocation still wins inside enroll.
        authorized_outgoing = outgoing_nodes.get(node) == key
        status, peer = self.store.enroll(node, key, server["node"],
                                         restricted=self.restricted and not authorized_outgoing)
        if peer is not None:
            if not self._applied(peer):
                return 200, {"status": "RequestFailed"}
            return 200, {"status": status, "server": server, "client": {"node": node, "key": key}}
        result = {"status": status}
        if status == "Restricted":
            result["server"] = server
        return 200, result


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def process_request(self, request, client_address):
        # Bound threads, including clients stalled before sending a request line.
        if not self.worker_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.worker_slots.release()


def make_server(application, port=9081):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(4)

        def do_GET(self):
            code, result = application.handle(self.path, self.command)
            body = json.dumps(result, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET
        do_PUT = do_GET
        do_DELETE = do_GET

        def log_message(self, *args):
            pass
    server = GatewayHTTPServer(("127.0.0.1", port), Handler)
    server.worker_slots = threading.BoundedSemaphore(32)
    return server


def main():
    restricted = os.environ.get("GATEWAY_WG_RESTRICTED", "0").lower() in ("1", "true", "yes")
    make_server(GatewayApplication(restricted=restricted)).serve_forever()


if __name__ == "__main__":
    main()
