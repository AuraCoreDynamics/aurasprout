"""Distributed Micro-Batch Coordinator with Atomic Claim, Staged Outputs, and Fencing Checks."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root
from aurasprout.coordination.leases import CONFLICT_RESOLUTION_ACTIONS, VolunteerLeaseManager
from aurasprout.storage.cell_state import _atomic_write_json
from aurasprout.storage.mutex import SproutMutex


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class CollaborativeBatchCoordinator:
    """Coordinates atomic `WorkBatchClaimDto` lifecycle over `.sprout/claims/`."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        lease_manager: Optional[VolunteerLeaseManager] = None,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.lease_manager = lease_manager or VolunteerLeaseManager(paths=self.paths)

    def _claims_mutex(self, mas_id: str) -> SproutMutex:
        return SproutMutex(self.paths.lock_dir(f"claims-{validate_identifier(mas_id, 'masId')}"))

    def _next_batch_token(self, mas_id: str) -> int:
        mas_dir = self.paths.claims_mas_dir(mas_id)
        mas_dir.mkdir(parents=True, exist_ok=True)
        seq_file = mas_dir / ".batch_token_seq"
        current = 0
        if seq_file.exists():
            try:
                current = int(seq_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                current = 0
        nxt = current + 1
        tmp = seq_file.with_name(f".seq.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(str(nxt), encoding="utf-8")
        os.replace(tmp, seq_file)
        return nxt

    def claim_batch(
        self,
        mas_id: str,
        item_ids: List[str],
        node_id: Optional[str] = None,
        user: Optional[str] = None,
        ttl_seconds: float = 60.0,
        fencing_token: Optional[int] = None,
    ) -> Dict[str, Any]:
        safe_mas = validate_identifier(mas_id, "masId")
        caller_node = node_id or self.lease_manager.node_id
        caller_user = user or self.lease_manager.user
        requested_items = [str(i) for i in item_ids]

        with self._claims_mutex(safe_mas):
            mas_dir = self.paths.claims_mas_dir(safe_mas)
            mas_dir.mkdir(parents=True, exist_ok=True)
            now = _utc_now()

            # Evict expired unfinished claims and verify none of requested_items are actively held
            active_items: set[str] = set()
            for claim_path in sorted(mas_dir.glob("*.claim.json")):
                try:
                    existing = json.loads(claim_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                status = str(existing.get("status") or "")
                if status in ("Committed", "Abandoned"):
                    if status == "Committed":
                        active_items.update(existing.get("itemIds") or [])
                    continue
                exp = _parse_iso(existing.get("expiresAtUtc"))
                if exp is not None and exp <= now:
                    existing["status"] = "Abandoned"
                    _atomic_write_json(claim_path, existing)
                    continue
                active_items.update(existing.get("itemIds") or [])

            overlap = [item for item in requested_items if item in active_items]
            if overlap:
                raise ValueError(f"Work items already claimed or committed for {safe_mas}: {overlap}")

            batch_id = f"batch-{uuid.uuid4().hex[:12]}"
            token_val = int(fencing_token) if fencing_token is not None else self._next_batch_token(safe_mas)
            record = {
                "batchId": batch_id,
                "masId": safe_mas,
                "claimedByNodeId": caller_node,
                "claimedByUser": caller_user,
                "claimedAtUtc": now.isoformat(),
                "expiresAtUtc": (now + timedelta(seconds=max(0.1, float(ttl_seconds)))).isoformat(),
                "itemIds": requested_items,
                "status": "Claimed",
                "fencingToken": token_val,
                "checkpointMetadata": None,
            }
            _atomic_write_json(self.paths.claim_file(safe_mas, batch_id), record)
            return record

    def _find_claim_file(self, batch_id: str) -> Optional[Path]:
        safe_batch = validate_identifier(batch_id, "batchId")
        if not self.paths.claims_dir.exists():
            return None
        matches = list(self.paths.claims_dir.rglob(f"{safe_batch}.claim.json"))
        return matches[0] if matches else None

    def stage_output(
        self,
        mas_id: str,
        batch_id: str,
        fencing_token: int,
        relative_path: str,
        content: bytes,
    ) -> Path:
        safe_mas = validate_identifier(mas_id, "masId")
        safe_batch = validate_identifier(batch_id, "batchId")
        token_int = int(fencing_token)
        with self._claims_mutex(safe_mas):
            claim_path = self.paths.claim_file(safe_mas, safe_batch)
            if not claim_path.exists():
                raise KeyError(f"Work batch claim not found: {safe_batch}")
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            if int(claim.get("fencingToken") or 0) != token_int or claim.get("status") == "Abandoned":
                raise PermissionError(f"Stale fencing token {token_int} for batch {safe_batch}")
            stage_root = validate_within_root(
                self.paths.fenced_staging_dir(safe_mas, token_int), safe_batch
            )
            out_file = validate_within_root(stage_root, relative_path)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_bytes(content)
            claim["status"] = "Staged"
            _atomic_write_json(claim_path, claim)
            return out_file

    def commit_batch(
        self,
        batch_id: str,
        fencing_token: int,
        node_id: Optional[str] = None,
        checkpoint_metadata: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        claim_path = self._find_claim_file(batch_id)
        if claim_path is None:
            raise KeyError(f"Work batch {batch_id!r} not found")
        mas_id = validate_identifier(claim_path.parent.name, "masId")
        caller_node = node_id or self.lease_manager.node_id
        token_int = int(fencing_token)

        with self._claims_mutex(mas_id):
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            expected_token = int(claim.get("fencingToken") or 0)
            exp = _parse_iso(claim.get("expiresAtUtc"))
            expired = exp is not None and exp <= _utc_now()

            # Also check if an active CellSingleton lease has superseded token
            active_lease = self.lease_manager.get_lease(mas_id, active_only=True)
            lease_superseded = (
                active_lease is not None
                and active_lease.get("mode") == "CellSingleton"
                and int(active_lease.get("fencingToken") or 0) > token_int
            )

            if (
                token_int != expected_token
                or claim.get("status") == "Abandoned"
                or (expired and claim.get("claimedByNodeId") != caller_node)
                or lease_superseded
            ):
                remote_node = (
                    active_lease.get("holderNodeId")
                    if active_lease
                    else claim.get("claimedByNodeId", "unknown")
                )
                remote_user = (
                    active_lease.get("holderUser")
                    if active_lease
                    else claim.get("claimedByUser", "unknown")
                )
                remote_token = (
                    int(active_lease.get("fencingToken") or expected_token)
                    if active_lease
                    else expected_token
                )
                conflict = {
                    "conflictId": f"conflict-{batch_id}-{uuid.uuid4().hex[:8]}",
                    "masId": mas_id,
                    "localNodeId": caller_node,
                    "localFencingToken": token_int,
                    "remoteHolderNodeId": str(remote_node),
                    "remoteHolderUser": str(remote_user),
                    "remoteFencingToken": int(remote_token),
                    "uncommittedItemCount": len(claim.get("itemIds") or []),
                    "detectedAtUtc": _utc_now().isoformat(),
                    "availableResolutions": list(CONFLICT_RESOLUTION_ACTIONS),
                }
                self.lease_manager.record_external_conflict(conflict)
                return False, conflict

            # Promote staged outputs atomically to .sprout/storage/ if present
            stage_root = validate_within_root(
                self.paths.fenced_staging_dir(mas_id, token_int), validate_identifier(batch_id, "batchId")
            )
            if stage_root.exists():
                for src_file in stage_root.rglob("*"):
                    if src_file.is_file():
                        rel = src_file.relative_to(stage_root)
                        dest = validate_within_root(self.paths.storage_dir, rel)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:6]}.tmp")
                        shutil.copy2(src_file, tmp)
                        os.replace(tmp, dest)
                shutil.rmtree(stage_root, ignore_errors=True)

            claim["status"] = "Committed"
            if checkpoint_metadata is not None:
                claim["checkpointMetadata"] = {str(k): str(v) for k, v in checkpoint_metadata.items()}
            _atomic_write_json(claim_path, claim)
            return True, claim

