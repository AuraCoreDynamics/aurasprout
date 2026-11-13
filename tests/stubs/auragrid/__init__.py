"""AuraGrid client SDK stub for test and cleanroom environments without proprietary AuraGrid binaries.

Matches the exact HTTP/IPC API contracts exposed by AuraSprout Tier 2 IPC Bridge:
- IpcOptions / IpcClient
- ConflictError / ConflictResolutionAction
- GovernanceClient
- EventClient
- LeaseClient
- StorageClient
- RegistryClient
- CollaborationClient
"""

from __future__ import annotations

import base64
import enum
import os
import urllib.parse
from typing import Any, Dict, List, Optional
import httpx


class ConflictResolutionAction(str, enum.Enum):
    YIELD_TO_PEER = "YieldToPeer"
    RETRY_WITH_NEW_TOKEN = "RetryWithNewToken"
    OVERWRITE_FORCEFULLY = "OverwriteForcefully"
    ABORT_BATCH = "AbortBatch"


class ConflictError(Exception):
    def __init__(self, message: str = "", conflict: Any = None):
        super().__init__(message)
        self.conflict = conflict


class IpcOptions:
    def __init__(
        self,
        port: Optional[int] = None,
        token: Optional[str] = None,
        timeout: float = 5.0,
        max_retries: int = 0,
    ) -> None:
        raw_port = port or os.environ.get("AURAGRID_IPC_PORT")
        if not raw_port and os.environ.get("AURAGRID_LOCAL_URL"):
            parsed = urllib.parse.urlparse(os.environ["AURAGRID_LOCAL_URL"])
            raw_port = parsed.port
        self.port = int(raw_port or 0)
        self.token = token or os.environ.get("AURAGRID_IPC_TOKEN", "")
        self.timeout = timeout
        self.max_retries = max_retries


class IpcClient:
    def __init__(self, options: Optional[IpcOptions] = None) -> None:
        self.options = options or IpcOptions()
        self.base_url = f"http://127.0.0.1:{self.options.port}"
        self.client = httpx.Client(base_url=self.base_url, timeout=self.options.timeout)

    def __enter__(self) -> "IpcClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.client.close()

    def _headers(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self.options.token:
            h["X-AuraGrid-IPC-Token"] = self.options.token
        if headers:
            h.update(headers)
        return h

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        return self.client.get(path, params=params, headers=self._headers(headers))

    def post(self, path: str, json: Any = None, content: Any = None, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        return self.client.post(path, json=json, content=content, headers=self._headers(headers))

    def put(self, path: str, json: Any = None, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        return self.client.put(path, json=json, headers=self._headers(headers))

    def delete(self, path: str, headers: Optional[Dict[str, str]] = None) -> httpx.Response:
        return self.client.delete(path, headers=self._headers(headers))


class GovernanceClient:
    def __init__(self, ipc: IpcClient) -> None:
        self.ipc = ipc

    def can_deploy(self, app_id: str, user: str) -> Any:
        resp = self.ipc.post("/cell/governance/can-deploy", json={"appId": app_id, "userId": user})
        resp.raise_for_status()
        data = resp.json()

        class CanDeployResult:
            def __init__(self, d: Dict[str, Any]):
                self.is_permitted = d.get("isPermitted", True)

        return CanDeployResult(data)

    def get_audit_trail(self, limit: int = 50) -> List[Dict[str, Any]]:
        resp = self.ipc.get(f"/cell/governance/audit?limit={limit}")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("auditEntries", [])


class EventClient:
    def __init__(self, ipc: IpcClient) -> None:
        self.ipc = ipc

    def publish(self, topic: str, payload: bytes, event_type: str = "Generic") -> Any:
        b64_payload = base64.b64encode(payload).decode("ascii")
        resp = self.ipc.post(
            f"/cell/events/{topic}",
            json={"payload": b64_payload, "eventType": event_type},
        )
        resp.raise_for_status()
        data = resp.json()

        class PubResult:
            def __init__(self, d: Dict[str, Any]):
                self.sequence_number = d.get("sequenceNumber", 1)

        return PubResult(data)

    def topic_exists(self, topic: str) -> bool:
        resp = self.ipc.get(f"/cell/events/topics/{topic}/exists")
        if resp.status_code == 200:
            return bool(resp.json().get("exists", False))
        return topic in self.list_topics()

    def list_topics(self) -> List[str]:
        resp = self.ipc.get("/cell/events/topics")
        resp.raise_for_status()
        return resp.json().get("topics", [])

    def consume(self, topic: str, after: int = 0, count: int = 10) -> List[Any]:
        resp = self.ipc.get(f"/cell/events/{topic}?afterSequence={after}&count={count}")
        resp.raise_for_status()
        events = resp.json().get("events", [])

        class ConsumedEvent:
            def __init__(self, d: Dict[str, Any]):
                self.payload = d.get("payload", "")
                self.sequence_number = d.get("sequenceNumber", 0)
                self.event_type = d.get("eventType", "")

        return [ConsumedEvent(e) for e in events]

    def get_marker(self, topic: str, consumer_id: str) -> Optional[Any]:
        resp = self.ipc.get(f"/cell/events/markers/{consumer_id}/{topic}")
        if resp.status_code == 404 or not resp.json():
            return None
        marker_data = resp.json()
        if marker_data.get("sequenceNumber") is None:
            return None

        class Marker:
            def __init__(self, d: Dict[str, Any]):
                self.sequence_number = d.get("sequenceNumber", 0)

        return Marker(marker_data)

    def set_marker(self, topic: str, seq: int, consumer_id: str) -> None:
        resp = self.ipc.put(
            f"/cell/events/markers/{consumer_id}/{topic}",
            json={"sequenceNumber": seq},
        )
        resp.raise_for_status()


class LeaseClient:
    def __init__(self, ipc: IpcClient) -> None:
        self.ipc = ipc

    def try_acquire_lease(self, lease_id: str, data: Dict[str, Any]) -> Any:
        payload = {"masId": lease_id, "bid": data}
        resp = self.ipc.post("/cell/lease/acquire", json=payload)
        resp_data = resp.json()

        class LeaseInfo:
            def __init__(self, ld: Dict[str, Any]):
                self.fencing_token = ld.get("fencingToken", 0)

        class AcqResult:
            def __init__(self, rd: Dict[str, Any]):
                self.acquired = rd.get("acquired", False)
                self.lease = LeaseInfo(rd["lease"]) if rd.get("lease") else None

        return AcqResult(resp_data)

    def release_lease(self, lease_id: str, fencing_token: int) -> None:
        resp = self.ipc.post(
            "/cell/lease/release",
            json={"masId": lease_id, "fencingToken": fencing_token},
        )
        resp.raise_for_status()


class StorageClient:
    def __init__(self, ipc: IpcClient) -> None:
        self.ipc = ipc

    def write(self, path: str, data: bytes, fencing_token: Optional[int] = None) -> None:
        headers = {}
        if fencing_token is not None:
            headers["X-AuraGrid-Fencing-Token"] = str(fencing_token)
        resp = self.ipc.post(f"/cell/storage/write?path={path}", content=data, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"Storage write failed: {resp.status_code} {resp.text}")

    def read(self, path: str) -> bytes:
        resp = self.ipc.get(f"/cell/storage/read?path={path}")
        resp.raise_for_status()
        return resp.content

    def read_range(self, path: str, offset: int, count: int) -> bytes:
        resp = self.ipc.get(f"/cell/storage/range?path={path}&offset={offset}&count={count}")
        resp.raise_for_status()
        return resp.content

    def exists(self, path: str) -> bool:
        resp = self.ipc.get(f"/cell/storage/exists?path={path}")
        return resp.status_code == 200 and resp.json().get("exists", False)


class RegistryClient:
    def __init__(self, ipc: IpcClient) -> None:
        self.ipc = ipc

    def discover(self, service_name: str) -> List[Any]:
        resp = self.ipc.get(f"/cell/registry/discover?serviceName={service_name}")
        resp.raise_for_status()
        entries = resp.json().get("endpoints", [])

        class ServiceEntry:
            def __init__(self, d: Dict[str, Any]):
                self.service_name = d.get("serviceName", "")
                self.node_id = d.get("nodeId", "")
                ep_url = d.get("endpointUrl", "")
                if ":" in ep_url:
                    try:
                        self.port = int(ep_url.rsplit(":", 1)[1])
                    except ValueError:
                        self.port = 0
                else:
                    self.port = 0

        return [ServiceEntry(e) for e in entries]


class CollaborationClient:
    def __init__(self, ipc: IpcClient) -> None:
        self.ipc = ipc

    def claim_work_batch(self, batch_id: str, items: List[str]) -> Any:
        resp = self.ipc.post("/cell/collaboration/batches/claim", json={"masId": batch_id, "itemIds": items})
        resp.raise_for_status()
        data = resp.json()

        class BatchResult:
            def __init__(self, d: Dict[str, Any]):
                self.batch_id = d.get("batchId", batch_id)
                self.fencing_token = d.get("fencingToken", 1)

        return BatchResult(data)

    def commit_work_batch(self, batch_id: str, fencing_token: int) -> Any:
        resp = self.ipc.post(
            f"/cell/collaboration/batches/{batch_id}/commit",
            json={"fencingToken": fencing_token},
        )
        if resp.status_code == 409:
            data = resp.json()
            conflict_data = data.get("conflict") or data

            class ConflictInfo:
                def __init__(self, cd: Dict[str, Any]):
                    self.conflict_id = cd.get("conflictId", "")

            raise ConflictError("Conflict detected", conflict=ConflictInfo(conflict_data))
        resp.raise_for_status()
        data = resp.json()

        class CommitResult:
            def __init__(self, d: Dict[str, Any]):
                self.status = d.get("status", "Committed")

        return CommitResult(data)

    def resolve_conflict(self, conflict_id: str, action: Any) -> Any:
        action_val = action.value if hasattr(action, "value") else str(action)
        resp = self.ipc.post(
            f"/cell/collaboration/conflicts/{conflict_id}/resolve",
            json={"action": action_val},
        )
        resp.raise_for_status()
        data = resp.json()

        class ResolutionResult:
            def __init__(self, d: Dict[str, Any]):
                self.mas_id = d.get("masId", "")

        return ResolutionResult(data)
