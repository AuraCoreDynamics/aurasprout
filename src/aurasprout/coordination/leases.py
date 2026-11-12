"""Opt-in Volunteer & CellSingleton Fenced Lease Manager over Shared Filesystems."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aurasprout.catalog.models import MAS_MODES
from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root
from aurasprout.storage.cell_state import _atomic_write_json
from aurasprout.storage.mutex import SproutMutex

CONFLICT_RESOLUTION_ACTIONS = (
    "DiscardLocalAndSync",
    "KeepLocalAndStageMerge",
    "TakeOverStaleLock",
    "YieldToPeer",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).isoformat()


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_node_id() -> str:
    return os.environ.get("AURAGRID_NODE_ID") or socket.gethostname() or "local-node"


def _default_user() -> str:
    try:
        return getpass.getuser()
    except OSError:
        return "user"


class VolunteerLeaseManager:
    """Manages opt-in volunteering, monotonic fenced leases, and split-brain conflicts.

    Invariants enforced:
    1. ``volunteerEnabled`` defaults to ``False``. No ``.sprout/leases/`` files are written
       for ``LocalOnly`` services or disabled services unless already holding an active lease
       that is being explicitly transitioned.
    2. Every lease acquisition or stale takeover monotonically increments ``fencingToken``.
    3. Stale token renewals emit a ``LeaseConflictDto`` under ``.sprout/conflicts/``.
    """

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        node_id: Optional[str] = None,
        user: Optional[str] = None,
        host_name: Optional[str] = None,
        default_ttl_seconds: float = 30.0,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.node_id = node_id or _default_node_id()
        self.user = user or _default_user()
        self.host_name = host_name or socket.gethostname() or "localhost"
        self.default_ttl_seconds = float(default_ttl_seconds)

    def _lease_mutex(self, mas_id: str) -> SproutMutex:
        return SproutMutex(self.paths.lock_dir(f"lease-{validate_identifier(mas_id, 'masId')}"))

    def _token_seq_file(self, mas_id: str) -> Path:
        safe = validate_identifier(mas_id, "masId")
        return validate_within_root(self.paths.leases_dir, f".{safe}.token_seq")

    def _next_fencing_token(self, mas_id: str, observed_floor: int = 0) -> int:
        seq_file = self._token_seq_file(mas_id)
        current = int(observed_floor or 0)
        if seq_file.exists():
            try:
                current = max(current, int(seq_file.read_text(encoding="utf-8").strip()))
            except (OSError, ValueError):
                pass
        existing = self._read_lease_unlocked(mas_id)
        if existing is not None:
            current = max(current, int(existing.get("fencingToken") or 0))
        nxt = current + 1
        seq_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = seq_file.with_name(f"{seq_file.name}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(str(nxt), encoding="utf-8")
        os.replace(tmp, seq_file)
        return nxt

    def _read_lease_unlocked(self, mas_id: str) -> Optional[Dict[str, Any]]:
        path = self.paths.lease_file(mas_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _is_lease_expired(self, record: Dict[str, Any], now: Optional[datetime] = None) -> bool:
        exp = _parse_iso(record.get("expiresAt"))
        if exp is None:
            return True
        return exp <= (now or _utc_now())

    def get_lease(self, mas_id: str, active_only: bool = True) -> Optional[Dict[str, Any]]:
        safe_mas = validate_identifier(mas_id, "masId")
        with self._lease_mutex(safe_mas):
            rec = self._read_lease_unlocked(safe_mas)
            if rec is None:
                return None
            if active_only:
                if rec.get("state") in ("Released", "Stale"):
                    return None
                if self._is_lease_expired(rec):
                    return None
            return rec

    def try_acquire_lease(
        self,
        mas_id: str,
        mode: str = "CellSingleton",
        node_id: Optional[str] = None,
        user: Optional[str] = None,
        host_name: Optional[str] = None,
        pid: Optional[int] = None,
        ttl_seconds: Optional[float] = None,
        volunteer_enabled: bool = True,
        activity_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_mas = validate_identifier(mas_id, "masId")
        if mode not in MAS_MODES:
            raise ValueError(f"Unsupported MAS mode: {mode!r}")

        caller_node = node_id or self.node_id
        caller_user = user or self.user
        caller_host = host_name or self.host_name
        ttl = float(ttl_seconds or self.default_ttl_seconds)

        if mode == "LocalOnly":
            now = _utc_now()
            return {
                "acquired": False,
                "lease": {
                    "masId": safe_mas,
                    "mode": "LocalOnly",
                    "holderNodeId": caller_node,
                    "holderUser": caller_user,
                    "holderHostName": caller_host,
                    "fencingToken": 1,
                    "acquiredAt": now.isoformat(),
                    "expiresAt": (now + timedelta(seconds=ttl)).isoformat(),
                    "lastKnownMarker": None,
                    "pid": pid or os.getpid(),
                    "volunteerEnabled": False,
                    "activitySummary": activity_summary,
                    "progressPct": None,
                    "lastCheckpointUtc": None,
                    "state": "Standby",
                },
                "reason": "LocalOnly services never acquire shared cell leases",
            }

        with self._lease_mutex(safe_mas):
            existing = self._read_lease_unlocked(safe_mas)
            now = _utc_now()
            if existing is not None:
                active = existing.get("state") not in ("Released", "Stale") and not self._is_lease_expired(
                    existing, now=now
                )
                if active and existing.get("holderNodeId") != caller_node:
                    return {
                        "acquired": False,
                        "lease": existing,
                        "reason": f"Lease actively held by {existing.get('holderNodeId')}",
                    }

            new_token = self._next_fencing_token(safe_mas)
            record = {
                "masId": safe_mas,
                "mode": mode,
                "holderNodeId": caller_node,
                "holderUser": caller_user,
                "holderHostName": caller_host,
                "fencingToken": new_token,
                "acquiredAt": now.isoformat(),
                "expiresAt": (now + timedelta(seconds=ttl)).isoformat(),
                "lastKnownMarker": existing.get("lastKnownMarker") if existing else None,
                "pid": pid if pid is not None else os.getpid(),
                "volunteerEnabled": bool(volunteer_enabled),
                "activitySummary": activity_summary,
                "progressPct": existing.get("progressPct") if existing else 0.0,
                "lastCheckpointUtc": existing.get("lastCheckpointUtc") if existing else None,
                "state": "Active",
            }
            _atomic_write_json(self.paths.lease_file(safe_mas), record)
            return {"acquired": True, "lease": record, "reason": None}

    def _emit_conflict(
        self,
        mas_id: str,
        local_node_id: str,
        local_token: int,
        remote_node_id: str,
        remote_user: str,
        remote_token: int,
        uncommitted_count: int = 1,
    ) -> Dict[str, Any]:
        conflict_id = f"conflict-{mas_id}-{uuid.uuid4().hex[:10]}"
        conflict = {
            "conflictId": conflict_id,
            "masId": mas_id,
            "localNodeId": local_node_id,
            "localFencingToken": int(local_token),
            "remoteHolderNodeId": remote_node_id,
            "remoteHolderUser": remote_user,
            "remoteFencingToken": int(remote_token),
            "uncommittedItemCount": int(uncommitted_count),
            "detectedAtUtc": _utc_iso(),
            "availableResolutions": list(CONFLICT_RESOLUTION_ACTIONS),
        }
        _atomic_write_json(self.paths.conflict_file(conflict_id), conflict)
        return conflict

    def renew_lease(
        self,
        mas_id: str,
        fencing_token: int,
        node_id: Optional[str] = None,
        marker: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[float] = None,
        activity_summary: Optional[str] = None,
        progress_pct: Optional[float] = None,
    ) -> Dict[str, Any]:
        safe_mas = validate_identifier(mas_id, "masId")
        caller_node = node_id or self.node_id
        token_int = int(fencing_token)
        ttl = float(ttl_seconds or self.default_ttl_seconds)

        with self._lease_mutex(safe_mas):
            existing = self._read_lease_unlocked(safe_mas)
            if existing is None:
                conflict = self._emit_conflict(
                    mas_id=safe_mas,
                    local_node_id=caller_node,
                    local_token=token_int,
                    remote_node_id="none",
                    remote_user="system",
                    remote_token=0,
                    uncommitted_count=1,
                )
                return {"extended": False, "conflict": conflict}

            active_token = int(existing.get("fencingToken") or 0)
            active_holder = str(existing.get("holderNodeId") or "")
            if token_int != active_token or existing.get("state") == "Released" or (
                node_id is not None and active_holder != caller_node
            ):
                conflict = self._emit_conflict(
                    mas_id=safe_mas,
                    local_node_id=caller_node,
                    local_token=token_int,
                    remote_node_id=active_holder,
                    remote_user=str(existing.get("holderUser") or ""),
                    remote_token=active_token,
                    uncommitted_count=1,
                )
                existing["state"] = "ConflictPending"
                _atomic_write_json(self.paths.lease_file(safe_mas), existing)
                return {"extended": False, "conflict": conflict}

            now = _utc_now()
            existing["expiresAt"] = (now + timedelta(seconds=ttl)).isoformat()
            existing["state"] = "Active"
            if marker is not None:
                existing["lastKnownMarker"] = marker
                existing["lastCheckpointUtc"] = now.isoformat()
            if activity_summary is not None:
                existing["activitySummary"] = activity_summary
            if progress_pct is not None:
                existing["progressPct"] = float(progress_pct)
            _atomic_write_json(self.paths.lease_file(safe_mas), existing)
            return {"extended": True, "conflict": None}

    def release_lease(
        self,
        mas_id: str,
        fencing_token: int,
        node_id: Optional[str] = None,
    ) -> bool:
        safe_mas = validate_identifier(mas_id, "masId")
        with self._lease_mutex(safe_mas):
            existing = self._read_lease_unlocked(safe_mas)
            if existing is None:
                return False
            if int(existing.get("fencingToken") or 0) != int(fencing_token):
                return False
            if node_id is not None and existing.get("holderNodeId") != node_id:
                return False
            existing["state"] = "Released"
            existing["expiresAt"] = _utc_iso()
            _atomic_write_json(self.paths.lease_file(safe_mas), existing)
            return True

    def get_last_marker(self, mas_id: str) -> Optional[Dict[str, Any]]:
        lease = self.get_lease(mas_id, active_only=False)
        if lease is None:
            return None
        return lease.get("lastKnownMarker")

    def set_volunteer_enabled(
        self,
        mas_id: str,
        enabled: bool,
        mode: str = "CellSingleton",
        user: Optional[str] = None,
        node_id: Optional[str] = None,
        host_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_mas = validate_identifier(mas_id, "masId")
        caller_node = node_id or self.node_id
        caller_user = user or self.user
        caller_host = host_name or self.host_name

        if mode == "LocalOnly":
            now = _utc_now()
            return {
                "masId": safe_mas,
                "mode": "LocalOnly",
                "holderNodeId": caller_node,
                "holderUser": caller_user,
                "holderHostName": caller_host,
                "fencingToken": 1,
                "acquiredAt": now.isoformat(),
                "expiresAt": (now + timedelta(seconds=self.default_ttl_seconds)).isoformat(),
                "lastKnownMarker": None,
                "pid": os.getpid(),
                "volunteerEnabled": False,
                "activitySummary": None,
                "progressPct": None,
                "lastCheckpointUtc": None,
                "state": "Standby",
            }

        if enabled:
            res = self.try_acquire_lease(
                mas_id=safe_mas,
                mode=mode,
                node_id=caller_node,
                user=caller_user,
                host_name=caller_host,
                volunteer_enabled=True,
            )
            lease = dict(res["lease"])
            if not res["acquired"]:
                lease["volunteerEnabled"] = True
            return lease

        with self._lease_mutex(safe_mas):
            existing = self._read_lease_unlocked(safe_mas)
            if existing is None:
                now = _utc_now()
                return {
                    "masId": safe_mas,
                    "mode": mode,
                    "holderNodeId": caller_node,
                    "holderUser": caller_user,
                    "holderHostName": caller_host,
                    "fencingToken": 1,
                    "acquiredAt": now.isoformat(),
                    "expiresAt": now.isoformat(),
                    "lastKnownMarker": None,
                    "pid": os.getpid(),
                    "volunteerEnabled": False,
                    "activitySummary": None,
                    "progressPct": None,
                    "lastCheckpointUtc": None,
                    "state": "Standby",
                }
            if existing.get("holderNodeId") == caller_node and existing.get("state") == "Active":
                existing["state"] = "Released"
                existing["expiresAt"] = _utc_iso()
            existing["volunteerEnabled"] = False
            _atomic_write_json(self.paths.lease_file(safe_mas), existing)
            return existing

    def is_valid_fencing_token(self, token: int) -> bool:
        if not self.paths.leases_dir.exists():
            return False
        token_int = int(token)
        now = _utc_now()
        for path in self.paths.leases_dir.glob("*.lease.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                rec.get("state") == "Active"
                and int(rec.get("fencingToken") or 0) == token_int
                and not self._is_lease_expired(rec, now=now)
            ):
                return True
        if self.paths.claims_dir.exists():
            for path in self.paths.claims_dir.rglob("*.claim.json"):
                try:
                    claim = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if claim.get("status") in ("Claimed", "Staged") and int(claim.get("fencingToken") or 0) == token_int:
                    return True
        return False

    def record_external_conflict(self, conflict: Dict[str, Any]) -> None:
        cid = validate_identifier(str(conflict["conflictId"]), "conflictId")
        _atomic_write_json(self.paths.conflict_file(cid), conflict)

    def list_conflicts(self, mas_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.paths.conflicts_dir.exists():
            return []
        conflicts: List[Dict[str, Any]] = []
        for path in sorted(self.paths.conflicts_dir.glob("*.json")):
            try:
                c = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if mas_id and c.get("masId") != mas_id:
                continue
            conflicts.append(c)
        return conflicts

    def resolve_conflict(
        self,
        conflict_id: str,
        action: str,
        node_id: Optional[str] = None,
        user: Optional[str] = None,
        host_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_cid = validate_identifier(conflict_id, "conflictId")
        if action not in CONFLICT_RESOLUTION_ACTIONS:
            raise ValueError(f"Unsupported conflict resolution action: {action!r}")

        conflict_path = self.paths.conflict_file(safe_cid)
        if not conflict_path.exists():
            raise KeyError(f"Conflict {safe_cid!r} not found")
        conflict = json.loads(conflict_path.read_text(encoding="utf-8"))
        mas_id = validate_identifier(str(conflict["masId"]), "masId")

        caller_node = node_id or conflict.get("localNodeId") or self.node_id
        caller_user = user or self.user
        caller_host = host_name or self.host_name
        local_token = int(conflict.get("localFencingToken") or 1)

        with self._lease_mutex(mas_id):
            existing = self._read_lease_unlocked(mas_id)
            local_stage = self.paths.fenced_staging_dir(mas_id, local_token)
            now = _utc_now()

            if action == "DiscardLocalAndSync":
                if local_stage.exists():
                    shutil.rmtree(local_stage, ignore_errors=True)
                conflict_path.unlink(missing_ok=True)
                if existing is not None:
                    if existing.get("state") == "ConflictPending":
                        existing["state"] = "Standby"
                        _atomic_write_json(self.paths.lease_file(mas_id), existing)
                    return existing
                return {
                    "masId": mas_id,
                    "mode": "CellSingleton",
                    "holderNodeId": str(conflict.get("remoteHolderNodeId") or caller_node),
                    "holderUser": str(conflict.get("remoteHolderUser") or caller_user),
                    "holderHostName": caller_host,
                    "fencingToken": max(1, int(conflict.get("remoteFencingToken") or 1)),
                    "acquiredAt": now.isoformat(),
                    "expiresAt": (now + timedelta(seconds=self.default_ttl_seconds)).isoformat(),
                    "lastKnownMarker": None,
                    "pid": os.getpid(),
                    "volunteerEnabled": True,
                    "state": "Standby",
                }

            if action == "KeepLocalAndStageMerge":
                review_dir = validate_within_root(
                    self.paths.staging_dir, Path(mas_id) / f"manual-merge-{safe_cid}"
                )
                review_dir.parent.mkdir(parents=True, exist_ok=True)
                if local_stage.exists():
                    if review_dir.exists():
                        shutil.rmtree(review_dir, ignore_errors=True)
                    shutil.move(str(local_stage), str(review_dir))
                else:
                    review_dir.mkdir(parents=True, exist_ok=True)
                _atomic_write_json(review_dir / "conflict.json", conflict)
                conflict_path.unlink(missing_ok=True)
                if existing is not None:
                    if existing.get("holderNodeId") == caller_node:
                        existing["state"] = "Released"
                    elif existing.get("state") == "ConflictPending":
                        existing["state"] = "Active"
                    _atomic_write_json(self.paths.lease_file(mas_id), existing)
                    return existing
                return {
                    "masId": mas_id,
                    "mode": "CellSingleton",
                    "holderNodeId": caller_node,
                    "holderUser": caller_user,
                    "holderHostName": caller_host,
                    "fencingToken": max(1, local_token),
                    "acquiredAt": now.isoformat(),
                    "expiresAt": now.isoformat(),
                    "lastKnownMarker": None,
                    "pid": os.getpid(),
                    "volunteerEnabled": False,
                    "state": "Released",
                }

            if action == "TakeOverStaleLock":
                floor = max(local_token, int(conflict.get("remoteFencingToken") or 0))
                new_token = self._next_fencing_token(mas_id, observed_floor=floor)
                taken = {
                    "masId": mas_id,
                    "mode": existing.get("mode", "CellSingleton") if existing else "CellSingleton",
                    "holderNodeId": caller_node,
                    "holderUser": caller_user,
                    "holderHostName": caller_host,
                    "fencingToken": new_token,
                    "acquiredAt": now.isoformat(),
                    "expiresAt": (now + timedelta(seconds=self.default_ttl_seconds)).isoformat(),
                    "lastKnownMarker": existing.get("lastKnownMarker") if existing else None,
                    "pid": os.getpid(),
                    "volunteerEnabled": True,
                    "activitySummary": "Recovered lock via TakeOverStaleLock",
                    "progressPct": existing.get("progressPct") if existing else 0.0,
                    "lastCheckpointUtc": now.isoformat(),
                    "state": "Active",
                }
                _atomic_write_json(self.paths.lease_file(mas_id), taken)
                conflict_path.unlink(missing_ok=True)
                return taken

            # YieldToPeer
            conflict_path.unlink(missing_ok=True)
            if existing is not None:
                existing["state"] = "Active" if not self._is_lease_expired(existing, now=now) else "Standby"
                _atomic_write_json(self.paths.lease_file(mas_id), existing)
                return existing
            return {
                "masId": mas_id,
                "mode": "CellSingleton",
                "holderNodeId": str(conflict.get("remoteHolderNodeId") or caller_node),
                "holderUser": str(conflict.get("remoteHolderUser") or caller_user),
                "holderHostName": caller_host,
                "fencingToken": max(1, int(conflict.get("remoteFencingToken") or 1)),
                "acquiredAt": now.isoformat(),
                "expiresAt": (now + timedelta(seconds=self.default_ttl_seconds)).isoformat(),
                "lastKnownMarker": None,
                "pid": os.getpid(),
                "volunteerEnabled": False,
                "state": "Standby",
            }

