"""Tier 2 Loopback HTTP/IPC Bridge Server matching `auragrid-api/openapi/ipc-bridge.openapi.json`.

Serves local MAS processes over `127.0.0.1` with constant-time `X-AuraGrid-IPC-Token`
validation, strict loopback Host enforcement, Browser Origin rejection, and
shared-filesystem backing via `SproutPaths`.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from aurasprout.contracts.paths import SproutPaths
from aurasprout.coordination.batches import CollaborativeBatchCoordinator
from aurasprout.coordination.leases import VolunteerLeaseManager
from aurasprout.storage.cell_state import (
    CellConfigManager,
    FencedStorageManager,
    FencingError,
    GovernanceManager,
    NodePresenceManager,
    ServiceRegistryManager,
)
from aurasprout.storage.events import SqliteEventStore

_LOG = logging.getLogger(__name__)


def _is_loopback_host_header(host_header: Optional[str]) -> bool:
    if not host_header:
        return True
    raw = host_header.strip().lower()
    if raw.startswith("[::1]"):
        return True
    host_only = raw.split(":", 1)[0]
    return host_only in ("127.0.0.1", "localhost", "::1")


def _parse_bool(raw: Optional[str], default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class IpcBridgeState:
    """Thread-safe container holding Tier 2 bridge stores, chaos flags, and callbacks."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        ipc_token: Optional[str] = None,
        node_id: Optional[str] = None,
        mode_resolver: Optional[Callable[[str], str]] = None,
        bind_host: str = "localhost",
    ) -> None:
        self.paths = paths or SproutPaths()
        self.ipc_token = ipc_token or os.environ.get("AURAGRID_IPC_TOKEN") or secrets.token_urlsafe(32)
        self.node_id = node_id or os.environ.get("AURAGRID_NODE_ID") or socket.gethostname() or "sprout-node"
        self.host_name = socket.gethostname() or "localhost"
        self.bind_host = bind_host
        self.mode_resolver = mode_resolver

        self.lease_manager = VolunteerLeaseManager(paths=self.paths, node_id=self.node_id)
        self.batch_coordinator = CollaborativeBatchCoordinator(
            paths=self.paths, lease_manager=self.lease_manager
        )
        self.config_manager = CellConfigManager(paths=self.paths)
        self.registry_manager = ServiceRegistryManager(paths=self.paths)
        self.presence_manager = NodePresenceManager(paths=self.paths)
        self.governance_manager = GovernanceManager(paths=self.paths)
        self.event_store = SqliteEventStore(paths=self.paths)
        self.storage_manager = FencedStorageManager(
            paths=self.paths,
            fencing_validator=self.lease_manager.is_valid_fencing_token,
        )

        self._lock = threading.Lock()
        self.tattles: List[Dict[str, Any]] = []
        self.local_service_ports: Dict[str, Dict[str, Any]] = {}
        self.chaos_gossip_suspended = False
        self.chaos_storage_fault_enabled = False
        self.chaos_network_faults: Dict[str, Dict[str, Any]] = {}

    def resolve_mode(self, mas_id: str) -> str:
        if self.mode_resolver is not None:
            resolved = self.mode_resolver(mas_id)
            if resolved:
                return resolved
        lease = self.lease_manager.get_lease(mas_id, active_only=False)
        if lease and lease.get("mode"):
            return str(lease["mode"])
        return "CellSingleton"

    def local_node_record(self) -> Dict[str, Any]:
        # Strictly avoid writing .sprout/nodes/ if LocalOnly/non-volunteering; return existing or synthetic record
        existing = self.presence_manager.get_member(self.node_id)
        if existing is not None:
            return existing
        return {
            "nodeId": self.node_id,
            "hostName": self.host_name,
            "state": "Alive",
            "address": self.bind_host,
            "joinedAt": datetime.now(timezone.utc).isoformat(),
            "lastHeartbeat": datetime.now(timezone.utc).isoformat(),
            "registeredServices": list(self.local_service_ports.keys()),
            "metadata": {"tier": "AuraSproutLocal"},
            "incarnation": 1,
        }


class _IpcRequestHandler(BaseHTTPRequestHandler):
    server_version = "AuraSproutIPC/4.3.0"
    protocol_version = "HTTP/1.1"

    @property
    def bridge(self) -> IpcBridgeState:
        return self.server.bridge_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.debug("IPC: " + format, *args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Expected JSON object payload")
        return parsed

    def _check_security(self, path: str) -> bool:
        if not _is_loopback_host_header(self.headers.get("Host")):
            self._send_json(403, {"error": "ForbiddenHost", "message": "Only loopback Host is permitted"})
            return False

        # Reject Tier-3 browser Origin requests against Tier-2 IPC bridge
        if self.headers.get("Origin") is not None:
            self._send_json(403, {"error": "BrowserOriginRejected", "message": "Tier 2 IPC rejects browser Origin"})
            return False

        if path in ("/health", "/api/health", "/ready"):
            return True

        token = self.headers.get("X-AuraGrid-IPC-Token") or self.headers.get("X-AuraGrid-Ipc-Token") or ""
        if not hmac.compare_digest(token, self.bridge.ipc_token):
            self._send_json(401, {"error": "Unauthorized", "message": "Missing or invalid X-AuraGrid-IPC-Token"})
            return False
        return True

    def _parse_fencing_token(self, query: Dict[str, List[str]]) -> Optional[int]:
        raw = query.get("fencingToken", [None])[0]
        if raw is None:
            raw = self.headers.get("X-AuraGrid-Fencing-Token")
        if raw is None or not str(raw).strip():
            return None
        return int(str(raw).strip())

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        # No CORS preflight / wildcard CORS on Tier 2 IPC bridge
        self._send_json(403, {"error": "Forbidden", "message": "CORS not permitted on IPC bridge"})

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if not self._check_security(path):
            return

        try:
            self._route(method, path, query)
        except KeyError as exc:
            self._send_json(404, {"error": "NotFound", "message": str(exc), "path": path})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": "NotFound", "message": str(exc), "path": path})
        except FencingError as exc:
            self._send_json(403, {"error": "FencingRejected", "message": str(exc), "path": path})
        except ValueError as exc:
            self._send_json(400, {"error": "BadRequest", "message": str(exc), "path": path})
        except Exception as exc:
            _LOG.exception("Unhandled IPC bridge error on %s %s", method, path)
            self._send_json(500, {"error": "InternalError", "message": str(exc), "path": path})

    def _route(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        b = self.bridge

        # Health / readiness
        if method == "GET" and path in ("/health", "/api/health", "/ready"):
            self._send_json(200, {"status": "Healthy", "nodeId": b.node_id, "version": "4.3.0"})
            return

        # Cell Config
        if method == "GET" and path == "/cell/config":
            self._send_json(200, b.config_manager.get_all())
            return
        if method == "GET" and path == "/cell/config/_metadata":
            self._send_json(200, b.config_manager.get_metadata())
            return
        if path.startswith("/cell/config/"):
            key = path[len("/cell/config/"):]
            if method == "GET":
                found, val = b.config_manager.get(key)
                if not found:
                    self._send_json(404, {"error": "NotFound", "message": f"Key {key!r} not found"})
                    return
                self._send_json(200, {"key": key, "value": val})
                return
            if method == "PUT":
                body = self._read_json()
                b.config_manager.put(key, body.get("value"), node_id=b.node_id)
                self._send_empty(204)
                return
            if method == "DELETE":
                removed = b.config_manager.delete(key, node_id=b.node_id)
                if not removed:
                    self._send_json(404, {"error": "NotFound", "message": f"Key {key!r} not found"})
                    return
                self._send_json(200, {"removed": True})
                return

        # Cell Members
        if method == "GET" and path == "/cell/members":
            include_unhealthy = _parse_bool(query.get("includeUnhealthy", [None])[0], default=False)
            members = b.presence_manager.list_members(include_unhealthy=include_unhealthy)
            self._send_json(200, {"members": members})
            return
        if method == "GET" and path == "/cell/members/_healthy-count":
            self._send_json(200, {"count": b.presence_manager.healthy_count()})
            return
        if method == "GET" and path == "/cell/members/_metadata":
            self._send_json(200, b.presence_manager.membership_metadata())
            return
        if method == "GET" and path.startswith("/cell/members/"):
            nid = path[len("/cell/members/"):]
            member = b.presence_manager.get_member(nid)
            if member is None:
                self._send_json(404, {"error": "NotFound", "message": f"Member {nid!r} not found"})
                return
            self._send_json(200, member)
            return
        if method == "GET" and path == "/cell/local-node":
            self._send_json(200, b.local_node_record())
            return

        # Governance
        if method == "POST" and path == "/cell/governance/can-deploy":
            body = self._read_json()
            self._send_json(
                200,
                b.governance_manager.evaluate("can-deploy", str(body.get("appId", "")), body.get("userId")),
            )
            return
        if method == "POST" and path == "/cell/governance/can-remove":
            body = self._read_json()
            self._send_json(
                200,
                b.governance_manager.evaluate("can-remove", str(body.get("appId", "")), body.get("userId")),
            )
            return
        if method == "POST" and path == "/cell/governance/can-reconfigure":
            body = self._read_json()
            self._send_json(
                200,
                b.governance_manager.evaluate("can-reconfigure", str(body.get("resourceId", "")), body.get("userId")),
            )
            return
        if method == "GET" and path.startswith("/cell/governance/roles/"):
            uid = path[len("/cell/governance/roles/"):]
            self._send_json(200, {"roles": b.governance_manager.get_roles(uid)})
            return
        if method == "GET" and path == "/cell/governance/audit":
            limit = int(query.get("limit", ["100"])[0])
            self._send_json(200, b.governance_manager.get_audit_trail(limit=limit))
            return

        # Events (SQLite TRUNCATE WAL-free)
        if method == "GET" and path == "/cell/events/topics":
            self._send_json(200, {"topics": b.event_store.list_topics()})
            return
        if method == "GET" and path.startswith("/cell/events/topics/") and path.endswith("/exists"):
            topic_id = path[len("/cell/events/topics/") : -len("/exists")]
            self._send_json(200, {"exists": b.event_store.topic_exists(topic_id)})
            return
        if path.startswith("/cell/events/markers/"):
            rest = path[len("/cell/events/markers/"):]
            parts = rest.split("/", 1)
            if len(parts) != 2:
                raise ValueError("Expected /cell/events/markers/{consumerId}/{topicId}")
            consumer_id, topic_id = parts
            if method == "GET":
                self._send_json(200, b.event_store.get_marker(consumer_id, topic_id))
                return
            if method == "PUT":
                body = self._read_json()
                b.event_store.set_marker(
                    consumer_id=consumer_id,
                    topic_id=topic_id,
                    sequence_number=int(body["sequenceNumber"]),
                    custom_state=body.get("customState"),
                )
                self._send_empty(204)
                return
        if path.startswith("/cell/events/") and path.count("/") == 3:
            topic_id = path[len("/cell/events/"):]
            if method == "POST":
                body = self._read_json()
                evt = b.event_store.publish(
                    topic_id=topic_id,
                    payload_b64=str(body["payload"]),
                    source_node_id=b.node_id,
                    event_type=body.get("eventType"),
                )
                self._send_json(200, evt)
                return
            if method == "GET":
                after_raw = query.get("afterSequence", query.get("after", ["0"]))[0]
                count_raw = query.get("count", ["100"])[0]
                events = b.event_store.consume(
                    topic_id=topic_id,
                    after_sequence=int(after_raw or 0),
                    count=int(count_raw or 100),
                )
                self._send_json(200, {"events": events})
                return

        # Leases
        if method == "POST" and path == "/cell/lease/acquire":
            body = self._read_json()
            mas_id = str(body["masId"])
            bid = body.get("bid") or {}
            bid_node = bid.get("nodeId") or b.node_id
            mode = b.resolve_mode(mas_id)
            outcome = b.lease_manager.try_acquire_lease(
                mas_id=mas_id,
                mode=mode,
                node_id=bid_node,
                volunteer_enabled=True,
            )
            self._send_json(200, outcome)
            return
        if method == "POST" and path == "/cell/lease/renew":
            body = self._read_json()
            res = b.lease_manager.renew_lease(
                mas_id=str(body["masId"]),
                fencing_token=int(body["fencingToken"]),
                marker=body.get("marker"),
            )
            self._send_json(200, res)
            return
        if method == "POST" and path == "/cell/lease/release":
            body = self._read_json()
            b.lease_manager.release_lease(
                mas_id=str(body["masId"]),
                fencing_token=int(body["fencingToken"]),
            )
            self._send_empty(204)
            return
        if method == "GET" and path.startswith("/cell/lease/marker/"):
            mas_id = path[len("/cell/lease/marker/"):]
            self._send_json(200, {"marker": b.lease_manager.get_last_marker(mas_id)})
            return
        if method == "GET" and path.startswith("/cell/lease/") and path.count("/") == 3:
            mas_id = path[len("/cell/lease/"):]
            lease = b.lease_manager.get_lease(mas_id, active_only=True)
            if lease is None:
                self._send_json(404, {"error": "NotFound", "message": f"No active lease for {mas_id!r}"})
                return
            self._send_json(200, lease)
            return

        # Collaboration & Conflicts
        if method == "GET" and path.startswith("/cell/collaboration/status/"):
            mas_id = path[len("/cell/collaboration/status/"):]
            lease = b.lease_manager.get_lease(mas_id, active_only=False)
            if lease is None:
                self._send_json(404, {"error": "NotFound", "message": f"No status found for {mas_id!r}"})
                return
            self._send_json(200, lease)
            return
        if method == "POST" and path == "/cell/collaboration/volunteer":
            body = self._read_json()
            mas_id = str(body["masId"])
            enabled = bool(body["enabled"])
            mode = b.resolve_mode(mas_id)
            updated = b.lease_manager.set_volunteer_enabled(
                mas_id=mas_id,
                enabled=enabled,
                mode=mode,
                user=body.get("user"),
            )
            if enabled and mode != "LocalOnly":
                b.presence_manager.heartbeat(b.node_id, registered_services=[mas_id])
            self._send_json(200, updated)
            return
        if method == "POST" and path == "/cell/collaboration/batches/claim":
            body = self._read_json()
            try:
                claimed = b.batch_coordinator.claim_batch(
                    mas_id=str(body["masId"]),
                    item_ids=list(body.get("itemIds") or []),
                    ttl_seconds=float(body.get("ttlSeconds", 60.0)),
                )
            except ValueError as exc:
                self._send_json(409, {"error": "BatchAlreadyClaimed", "message": str(exc)})
                return
            self._send_json(200, claimed)
            return
        if (
            method == "POST"
            and path.startswith("/cell/collaboration/batches/")
            and path.endswith("/commit")
        ):
            batch_id = path[len("/cell/collaboration/batches/") : -len("/commit")]
            body = self._read_json()
            ok, payload = b.batch_coordinator.commit_batch(
                batch_id=batch_id,
                fencing_token=int(body["fencingToken"]),
                checkpoint_metadata=body.get("checkpointMetadata"),
            )
            if not ok:
                self._send_json(409, payload)
                return
            self._send_json(200, payload)
            return
        if method == "GET" and path == "/cell/collaboration/conflicts":
            mas_filter = query.get("masId", [None])[0]
            self._send_json(200, {"conflicts": b.lease_manager.list_conflicts(mas_id=mas_filter)})
            return
        if (
            method == "POST"
            and path.startswith("/cell/collaboration/conflicts/")
            and path.endswith("/resolve")
        ):
            conflict_id = path[len("/cell/collaboration/conflicts/") : -len("/resolve")]
            body = self._read_json()
            resolved = b.lease_manager.resolve_conflict(
                conflict_id=conflict_id,
                action=str(body["action"]),
            )
            self._send_json(200, resolved)
            return

        # Service Registry & Service Port
        if method == "GET" and path == "/cell/registry/discover":
            svc_name = query.get("serviceName", [""])[0]
            self._send_json(200, {"endpoints": b.registry_manager.discover(svc_name)})
            return
        if path == "/cell/registry/services":
            if method == "GET":
                self._send_json(200, {"endpoints": b.registry_manager.list_all()})
                return
            if method == "POST":
                body = self._read_json()
                saved = b.registry_manager.register(body)
                self._send_json(200, saved)
                return
        if method == "GET" and path.startswith("/cell/registry/services/"):
            svc_name = path[len("/cell/registry/services/"):]
            self._send_json(200, {"endpoints": b.registry_manager.discover(svc_name)})
            return
        if method == "POST" and path == "/cell/service-port":
            body = self._read_json()
            svc_name = str(body["serviceName"])
            port = int(body["port"])
            with b._lock:
                b.local_service_ports[svc_name] = body
            mode = b.resolve_mode(svc_name)
            if mode != "LocalOnly":
                scheme = str(body.get("scheme") or "http")
                b.registry_manager.register(
                    {
                        "serviceId": f"{svc_name}-{b.node_id}",
                        "serviceName": svc_name,
                        "nodeId": b.node_id,
                        "endpointUrl": f"{scheme}://127.0.0.1:{port}",
                        "capabilities": list(body.get("methods") or []),
                        "metadata": {"mode": mode},
                    }
                )
            self._send_json(200, {"registered": True, "serviceName": svc_name, "port": port})
            return

        # Shared Storage
        if path.startswith("/cell/storage/"):
            if b.chaos_storage_fault_enabled:
                self._send_json(503, {"error": "ChaosStorageFault", "message": "Injected storage fault active"})
                return
            if method == "GET" and path == "/cell/storage/read":
                rel = query.get("path", [""])[0]
                data = b.storage_manager.read(rel)
                if data is None:
                    self._send_json(404, {"error": "NotFound", "path": rel})
                    return
                self._send_bytes(200, data)
                return
            if method == "GET" and path == "/cell/storage/range":
                rel = query.get("path", [""])[0]
                off = int(query.get("offset", ["0"])[0])
                cnt = int(query.get("count", ["0"])[0])
                data = b.storage_manager.read_range(rel, off, cnt)
                if data is None:
                    self._send_json(404, {"error": "NotFound", "path": rel})
                    return
                self._send_bytes(200, data)
                return
            if method == "GET" and path == "/cell/storage/exists":
                rel = query.get("path", [""])[0]
                self._send_json(200, {"exists": b.storage_manager.exists(rel)})
                return
            if method == "GET" and path == "/cell/storage/metadata":
                rel = query.get("path", [""])[0]
                meta = b.storage_manager.metadata(rel)
                if meta is None:
                    self._send_json(404, {"error": "NotFound", "path": rel})
                    return
                self._send_json(200, meta)
                return
            if method == "POST" and path == "/cell/storage/write":
                rel = query.get("path", [""])[0]
                token = self._parse_fencing_token(query)
                b.storage_manager.write(rel, self._read_body(), fencing_token=token)
                self._send_empty(204)
                return
            if method == "POST" and path == "/cell/storage/append":
                rel = query.get("path", [""])[0]
                token = self._parse_fencing_token(query)
                new_len = b.storage_manager.append(rel, self._read_body(), fencing_token=token)
                self._send_json(200, {"length": new_len})
                return
            if method == "DELETE" and path == "/cell/storage/delete":
                rel = query.get("path", [""])[0]
                recursive = _parse_bool(query.get("recursive", [None])[0], default=False)
                deleted = b.storage_manager.delete(rel, recursive=recursive)
                self._send_json(200, {"deleted": deleted})
                return
            if method == "GET" and path == "/cell/storage/list":
                prefix = query.get("prefix", [""])[0]
                self._send_json(200, {"files": b.storage_manager.list_files(prefix)})
                return

        # Tattle & Chaos
        if method == "POST" and path == "/cell/tattle":
            body = self._read_json()
            with b._lock:
                b.tattles.append({**body, "receivedAt": datetime.now(timezone.utc).isoformat()})
            self._send_json(200, {"filed": True})
            return
        if method == "GET" and path == "/cell/chaos/status":
            self._send_json(
                200,
                {
                    "gossipSuspended": b.chaos_gossip_suspended,
                    "storageFaultEnabled": b.chaos_storage_fault_enabled,
                    "faultedNodes": list(b.chaos_network_faults.keys()),
                },
            )
            return
        if method == "POST" and path == "/cell/chaos/gossip":
            suspended = _parse_bool(query.get("suspended", ["false"])[0], default=False)
            b.chaos_gossip_suspended = suspended
            self._send_json(200, {"success": True, "suspended": suspended})
            return
        if method == "POST" and path == "/cell/chaos/network":
            nid = query.get("nodeId", [None])[0]
            code_raw = query.get("statusCode", [None])[0]
            lat_raw = query.get("latencyMs", [None])[0]
            code = int(code_raw) if code_raw else None
            lat = int(lat_raw) if lat_raw else None
            if nid:
                b.chaos_network_faults[nid] = {"statusCode": code, "latencyMs": lat}
            self._send_json(200, {"success": True, "nodeId": nid, "statusCode": code, "latencyMs": lat})
            return
        if method == "POST" and path == "/cell/chaos/storage":
            enabled = _parse_bool(query.get("enabled", ["false"])[0], default=False)
            b.chaos_storage_fault_enabled = enabled
            self._send_json(200, {"success": True, "enabled": enabled})
            return

        self._send_json(404, {"error": "NotFound", "message": f"Unknown IPC route {method} {path}"})


class IpcBridgeServer:
    """Loopback Tier-2 HTTP IPC bridge server with background thread lifecycle."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        paths: Optional[SproutPaths] = None,
        ipc_token: Optional[str] = None,
        node_id: Optional[str] = None,
        mode_resolver: Optional[Callable[[str], str]] = None,
    ) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"Tier 2 IPC bridge must bind strictly to loopback, got {host!r}")
        self.host = host
        self.state = IpcBridgeState(
            paths=paths,
            ipc_token=ipc_token,
            node_id=node_id,
            mode_resolver=mode_resolver,
            bind_host=self.host,
        )
        self._httpd = ThreadingHTTPServer((self.host, int(port)), _IpcRequestHandler)
        self._httpd.daemon_threads = True
        self._httpd.bridge_state = self.state  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ipc_token(self) -> str:
        return self.state.ipc_token

    def start(self) -> "IpcBridgeServer":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                name="aurasprout-ipc-bridge",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "IpcBridgeServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        del exc_type, exc_val, exc_tb
        self.stop()


def start_ipc_bridge(
    paths: Optional[SproutPaths] = None,
    port: int = 0,
    ipc_token: Optional[str] = None,
    node_id: Optional[str] = None,
) -> Tuple[IpcBridgeServer, str, str]:
    srv = IpcBridgeServer(paths=paths, port=port, ipc_token=ipc_token, node_id=node_id).start()
    return srv, srv.base_url, srv.ipc_token
