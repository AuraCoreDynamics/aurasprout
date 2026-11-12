"""Shared-filesystem cell configuration, membership, governance, registry, and fenced storage."""

from __future__ import annotations

import json
import os
import shutil
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root
from aurasprout.storage.mutex import SproutMutex


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _atomic_write_json(target: Path, payload: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)


class CellConfigManager:
    """Shared cell key-value store backed by `.sprout/config/cell_config.json`."""

    def __init__(self, paths: Optional[SproutPaths] = None) -> None:
        self.paths = paths or SproutPaths()
        self.mutex = SproutMutex(self.paths.lock_dir("cell-config"))

    def _read_state(self) -> Dict[str, Any]:
        if not self.paths.cell_config_file.exists():
            return {"values": {}, "maxVersion": 0, "lastModified": _utc_now_iso(), "lastModifiedByNodeId": None}
        data = json.loads(self.paths.cell_config_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "values" not in data:
            return {"values": {}, "maxVersion": 0, "lastModified": _utc_now_iso(), "lastModifiedByNodeId": None}
        return data

    def get_all(self) -> Dict[str, Any]:
        with self.mutex:
            state = self._read_state()
            return {"config": dict(state.get("values") or {}), "metadata": self._metadata_from_state(state)}

    def _metadata_from_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        values = state.get("values") or {}
        return {
            "keyCount": len(values),
            "maxVersion": int(state.get("maxVersion") or 0),
            "lastModified": str(state.get("lastModified") or _utc_now_iso()),
            "lastModifiedByNodeId": state.get("lastModifiedByNodeId"),
            "synchronizedNodeCount": len(list(self.paths.nodes_dir.glob("*.json"))) if self.paths.nodes_dir.exists() else 1,
            "storagePath": str(self.paths.cell_config_file),
        }

    def get_metadata(self) -> Dict[str, Any]:
        with self.mutex:
            return self._metadata_from_state(self._read_state())

    def get(self, key: str) -> tuple[bool, Any]:
        with self.mutex:
            values = self._read_state().get("values") or {}
            if key not in values:
                return False, None
            return True, values[key]

    def put(self, key: str, value: Any, node_id: Optional[str] = None) -> None:
        if not key or "\x00" in key:
            raise ValueError(f"Invalid config key: {key!r}")
        with self.mutex:
            state = self._read_state()
            values = dict(state.get("values") or {})
            values[key] = value
            state["values"] = values
            state["maxVersion"] = int(state.get("maxVersion") or 0) + 1
            state["lastModified"] = _utc_now_iso()
            state["lastModifiedByNodeId"] = node_id or socket.gethostname()
            _atomic_write_json(self.paths.cell_config_file, state)

    def delete(self, key: str, node_id: Optional[str] = None) -> bool:
        with self.mutex:
            state = self._read_state()
            values = dict(state.get("values") or {})
            if key not in values:
                return False
            del values[key]
            state["values"] = values
            state["maxVersion"] = int(state.get("maxVersion") or 0) + 1
            state["lastModified"] = _utc_now_iso()
            state["lastModifiedByNodeId"] = node_id or socket.gethostname()
            _atomic_write_json(self.paths.cell_config_file, state)
            return True


class ServiceRegistryManager:
    """Shared service discovery registry backed by `.sprout/registry/services.json`."""

    def __init__(self, paths: Optional[SproutPaths] = None) -> None:
        self.paths = paths or SproutPaths()
        self.mutex = SproutMutex(self.paths.lock_dir("service-registry"))

    def _read_services(self) -> List[Dict[str, Any]]:
        if not self.paths.services_file.exists():
            return []
        data = json.loads(self.paths.services_file.read_text(encoding="utf-8"))
        return list(data.get("endpoints") or [])

    def register(self, endpoint: Dict[str, Any]) -> Dict[str, Any]:
        svc_id = str(endpoint.get("serviceId") or "")
        svc_name = str(endpoint.get("serviceName") or "")
        node_id = str(endpoint.get("nodeId") or "")
        if not svc_id or not svc_name or not node_id:
            raise ValueError("ServiceEndpoint requires serviceId, serviceName, and nodeId")

        normalized = {
            "serviceId": svc_id,
            "serviceName": svc_name,
            "nodeId": node_id,
            "endpointUrl": endpoint.get("endpointUrl"),
            "grpcEndpoint": endpoint.get("grpcEndpoint"),
            "openApiEndpoint": endpoint.get("openApiEndpoint"),
            "mcpEndpoint": endpoint.get("mcpEndpoint"),
            "capabilities": list(endpoint.get("capabilities") or []),
            "metadata": {str(k): str(v) for k, v in (endpoint.get("metadata") or {}).items()},
        }
        with self.mutex:
            items = self._read_services()
            filtered = [
                e for e in items
                if not (e.get("serviceId") == svc_id and e.get("nodeId") == node_id)
            ]
            filtered.append(normalized)
            _atomic_write_json(self.paths.services_file, {"endpoints": filtered, "updatedAt": _utc_now_iso()})
        return normalized

    def unregister_node_service(self, service_name: str, node_id: str) -> None:
        with self.mutex:
            if not self.paths.services_file.exists():
                return
            items = self._read_services()
            filtered = [
                e for e in items
                if not (e.get("serviceName") == service_name and e.get("nodeId") == node_id)
            ]
            _atomic_write_json(self.paths.services_file, {"endpoints": filtered, "updatedAt": _utc_now_iso()})

    def discover(self, service_name: str) -> List[Dict[str, Any]]:
        with self.mutex:
            return [e for e in self._read_services() if e.get("serviceName") == service_name]

    def list_all(self) -> List[Dict[str, Any]]:
        with self.mutex:
            return self._read_services()


class NodePresenceManager:
    """Shared node presence beacons under `.sprout/nodes/<nodeId>.json`."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        unhealthy_after_seconds: float = 30.0,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.mutex = SproutMutex(self.paths.lock_dir("nodes-presence"))
        self.unhealthy_after_seconds = float(unhealthy_after_seconds)

    def heartbeat(
        self,
        node_id: str,
        host_name: Optional[str] = None,
        address: Optional[str] = None,
        registered_services: Optional[List[str]] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        safe_node = validate_identifier(node_id, "nodeId")
        node_file = self.paths.node_file(safe_node)
        now = _utc_now_iso()
        with self.mutex:
            existing: Dict[str, Any] = {}
            if node_file.exists():
                try:
                    existing = json.loads(node_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
            record = {
                "nodeId": safe_node,
                "hostName": host_name or existing.get("hostName") or socket.gethostname(),
                "state": "Alive",
                "address": address or existing.get("address"),
                "joinedAt": existing.get("joinedAt") or now,
                "lastHeartbeat": now,
                "registeredServices": list(
                    registered_services
                    if registered_services is not None
                    else (existing.get("registeredServices") or [])
                ),
                "metadata": {
                    **({str(k): str(v) for k, v in (existing.get("metadata") or {}).items()}),
                    **({str(k): str(v) for k, v in (metadata or {}).items()}),
                },
                "incarnation": int(existing.get("incarnation") or 0) + 1,
            }
            _atomic_write_json(node_file, record)
            return record

    def list_members(self, include_unhealthy: bool = False) -> List[Dict[str, Any]]:
        if not self.paths.nodes_dir.exists():
            return []
        now_dt = _utc_now()
        results: List[Dict[str, Any]] = []
        with self.mutex:
            for path in sorted(self.paths.nodes_dir.glob("*.json")):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                hb_raw = str(rec.get("lastHeartbeat") or "")
                healthy = True
                if hb_raw:
                    try:
                        hb_dt = datetime.fromisoformat(hb_raw.replace("Z", "+00:00"))
                        if (now_dt - hb_dt).total_seconds() > self.unhealthy_after_seconds:
                            healthy = False
                            rec["state"] = "Suspect"
                    except ValueError:
                        pass
                if healthy or include_unhealthy:
                    results.append(rec)
        return results

    def get_member(self, node_id: str) -> Optional[Dict[str, Any]]:
        safe_node = validate_identifier(node_id, "nodeId")
        path = self.paths.node_file(safe_node)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def healthy_count(self) -> int:
        return len(self.list_members(include_unhealthy=False))

    def membership_metadata(self) -> Dict[str, Any]:
        all_members = self.list_members(include_unhealthy=True)
        healthy = [m for m in all_members if m.get("state") == "Alive"]
        max_inc = max((int(m.get("incarnation") or 0) for m in all_members), default=0)
        return {
            "healthyCount": len(healthy),
            "totalCount": len(all_members),
            "lastUpdated": _utc_now_iso(),
            "incarnationNumber": max_inc,
            "protocol": "SharedFS",
        }


class GovernanceManager:
    """User-space governance policy & audit trail (`can-deploy`, `can-remove`, `can-reconfigure`)."""

    def __init__(self, paths: Optional[SproutPaths] = None) -> None:
        self.paths = paths or SproutPaths()
        self.mutex = SproutMutex(self.paths.lock_dir("governance"))

    def _append_audit(self, action: str, user_id: Optional[str], resource_id: Optional[str], permitted: bool) -> None:
        entry = {
            "id": uuid.uuid4().hex,
            "timestamp": _utc_now_iso(),
            "userId": user_id or "local-user",
            "action": action,
            "appId": resource_id or "",
            "resourceId": resource_id,
            "permitted": permitted,
            "denyReason": None,
            "source": "AuraSprout",
        }
        with self.mutex:
            entries: List[Dict[str, Any]] = []
            if self.paths.audit_log_file.exists():
                try:
                    entries = list(json.loads(self.paths.audit_log_file.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    entries = []
            entries.insert(0, entry)
            _atomic_write_json(self.paths.audit_log_file, entries[:1000])

    def evaluate(self, action: str, resource_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        decision = {
            "isPermitted": True,
            "denyReason": None,
            "requiresApproval": False,
            "metadata": {"mode": "UserSpaceLocal"},
            "expiresAt": None,
        }
        self._append_audit(action, user_id, resource_id, True)
        return decision

    def get_roles(self, user_id: str) -> List[str]:
        del user_id
        return ["Operator", "Contributor"]

    def get_audit_trail(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.paths.audit_log_file.exists():
            return []
        with self.mutex:
            try:
                entries = list(json.loads(self.paths.audit_log_file.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                return []
            return entries[: max(1, int(limit))]


class FencingError(PermissionError):
    """Raised when a storage mutation fails fencing token validation."""


class FencedStorageManager:
    """Path-traversal-safe shared storage engine under `.sprout/storage/` with fencing validation."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        fencing_validator: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.paths.storage_dir.mkdir(parents=True, exist_ok=True)
        self.mutex = SproutMutex(self.paths.lock_dir("storage"))
        self.fencing_validator = fencing_validator

    def _resolve(self, relative_path: str) -> Path:
        return self.paths.storage_file(relative_path)

    def _enforce_fencing(self, fencing_token: Optional[int]) -> None:
        if fencing_token is None:
            return
        token_int = int(fencing_token)
        if token_int <= 0:
            raise FencingError(f"Invalid fencingToken: {fencing_token}")
        if self.fencing_validator is not None and not self.fencing_validator(token_int):
            raise FencingError(f"Stale or rejected fencingToken: {token_int}")

    def read(self, relative_path: str) -> Optional[bytes]:
        target = self._resolve(relative_path)
        if not target.exists() or not target.is_file():
            return None
        return target.read_bytes()

    def read_range(self, relative_path: str, offset: int, count: int) -> Optional[bytes]:
        target = self._resolve(relative_path)
        if not target.exists() or not target.is_file():
            return None
        off = max(0, int(offset))
        cnt = max(0, int(count))
        with open(target, "rb") as handle:
            handle.seek(off)
            return handle.read(cnt)

    def exists(self, relative_path: str) -> bool:
        target = self._resolve(relative_path)
        return target.exists()

    def metadata(self, relative_path: str) -> Optional[Dict[str, Any]]:
        target = self._resolve(relative_path)
        if not target.exists():
            return None
        st = target.stat()
        return {
            "isDirectory": target.is_dir(),
            "size": 0 if target.is_dir() else int(st.st_size),
            "lastModified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        }

    def write(self, relative_path: str, data: bytes, fencing_token: Optional[int] = None) -> None:
        target = self._resolve(relative_path)
        with self.mutex:
            self._enforce_fencing(fencing_token)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)

    def append(self, relative_path: str, data: bytes, fencing_token: Optional[int] = None) -> int:
        target = self._resolve(relative_path)
        with self.mutex:
            self._enforce_fencing(fencing_token)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "ab") as handle:
                handle.write(data)
            return int(target.stat().st_size)

    def delete(self, relative_path: str, recursive: bool = False) -> bool:
        target = self._resolve(relative_path)
        with self.mutex:
            if not target.exists():
                return False
            if target.is_dir():
                if not recursive:
                    target.rmdir()
                else:
                    shutil.rmtree(target)
                return True
            target.unlink()
            return True

    def list_files(self, prefix: str = "") -> List[str]:
        base = self.paths.storage_dir.resolve()
        if not base.exists():
            return []
        search_root = validate_within_root(base, prefix) if prefix.strip() else base
        if not search_root.exists():
            return []
        if search_root.is_file():
            return [search_root.relative_to(base).as_posix()]
        files: List[str] = []
        for path in sorted(search_root.rglob("*")):
            if path.is_file() and not path.name.endswith(".tmp"):
                files.append(path.relative_to(base).as_posix())
        return files

