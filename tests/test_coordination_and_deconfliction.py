"""Tests for SproutMutex, VolunteerLeaseManager, CollaborativeBatchCoordinator & Conflicts."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aurasprout.contracts.paths import SproutPaths
from aurasprout.coordination.batches import CollaborativeBatchCoordinator
from aurasprout.coordination.leases import VolunteerLeaseManager
from aurasprout.storage.mutex import SproutMutex


def test_sprout_mutex_mutual_exclusion_and_stale_reclaim_race(tmp_path: Path) -> None:
    lock_dir = tmp_path / ".sprout" / "locks" / "shared-res.lock"
    shared_counter = {"value": 0, "active": 0, "max_active": 0}

    def worker(idx: int) -> None:
        del idx
        with SproutMutex(lock_dir, timeout_seconds=5.0):
            shared_counter["active"] += 1
            shared_counter["max_active"] = max(shared_counter["max_active"], shared_counter["active"])
            cur = shared_counter["value"]
            time.sleep(0.01)
            shared_counter["value"] = cur + 1
            shared_counter["active"] -= 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(12)))

    assert shared_counter["max_active"] == 1
    assert shared_counter["value"] == 12

    # Inject a stale abandoned lock owned by a non-existent host/expired heartbeat
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "owner.json").write_text(
        json.dumps(
            {
                "ownerId": "dead-peer",
                "pid": 999999,
                "hostName": "dead-host",
                "heartbeatEpoch": time.time() - 30.0,
                "monotonicSeq": 1,
            }
        ),
        encoding="utf-8",
    )

    winners: list[str] = []
    barrier = threading.Barrier(4)

    def reclaim_contender(name: str) -> None:
        barrier.wait()
        with SproutMutex(lock_dir, stale_seconds=1.0, timeout_seconds=5.0, owner_id=name):
            winners.append(name)
            time.sleep(0.01)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(reclaim_contender, ["a", "b", "c", "d"]))

    assert len(winners) == 4


def test_lease_monotonicity_localonly_isolation_and_all_resolutions(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mgr_a = VolunteerLeaseManager(paths=paths, node_id="node-a", user="alice", default_ttl_seconds=30.0)
    mgr_b = VolunteerLeaseManager(paths=paths, node_id="node-b", user="bob", default_ttl_seconds=30.0)

    # LocalOnly NEVER writes .sprout/leases or .sprout/nodes
    local_res = mgr_a.try_acquire_lease("svc-local", mode="LocalOnly")
    assert local_res["acquired"] is False
    assert not paths.leases_dir.exists() or not any(paths.leases_dir.iterdir())
    assert not paths.nodes_dir.exists() or not any(paths.nodes_dir.iterdir())

    # CellSingleton exclusive acquisition & monotonic token
    acq1 = mgr_a.try_acquire_lease("svc-single", mode="CellSingleton")
    assert acq1["acquired"] is True
    tok1 = acq1["lease"]["fencingToken"]

    blocked = mgr_b.try_acquire_lease("svc-single", mode="CellSingleton")
    assert blocked["acquired"] is False
    assert blocked["lease"]["holderNodeId"] == "node-a"

    assert mgr_a.release_lease("svc-single", tok1) is True
    assert mgr_a.get_lease("svc-single", active_only=True) is None

    acq2 = mgr_b.try_acquire_lease("svc-single", mode="CellSingleton")
    assert acq2["acquired"] is True
    tok2 = acq2["lease"]["fencingToken"]
    assert tok2 > tok1

    # Stale renewal by node-a with tok1 triggers LeaseConflictDto & ConflictPending
    renew_stale = mgr_a.renew_lease("svc-single", tok1)
    assert renew_stale["extended"] is False
    conflict = renew_stale["conflict"]
    assert conflict is not None
    assert conflict["masId"] == "svc-single"
    assert conflict["remoteHolderNodeId"] == "node-b"

    # Test TakeOverStaleLock -> new incremented token & Active
    taken = mgr_a.resolve_conflict(conflict["conflictId"], "TakeOverStaleLock", node_id="node-a")
    assert taken["state"] == "Active"
    assert taken["holderNodeId"] == "node-a"
    assert taken["fencingToken"] > tok2

    # Test KeepLocalAndStageMerge moves local stage folder & releases
    stage_file = paths.fenced_staging_dir("svc-single", tok1) / "work.bin"
    stage_file.parent.mkdir(parents=True, exist_ok=True)
    stage_file.write_bytes(b"local-work")
    c2 = mgr_b.renew_lease("svc-single", tok1)["conflict"]
    assert c2 is not None
    kept = mgr_b.resolve_conflict(c2["conflictId"], "KeepLocalAndStageMerge")
    assert kept["state"] in ("Released", "Active")
    assert (paths.staging_dir / "svc-single" / f"manual-merge-{c2['conflictId']}" / "work.bin").exists()

    # Test DiscardLocalAndSync clears stage dir
    stage_file2 = paths.fenced_staging_dir("svc-single", 99) / "temp.bin"
    stage_file2.parent.mkdir(parents=True, exist_ok=True)
    stage_file2.write_bytes(b"discard-me")
    c3 = mgr_b.renew_lease("svc-single", 99)["conflict"]
    assert c3 is not None
    mgr_b.resolve_conflict(c3["conflictId"], "DiscardLocalAndSync")
    assert not stage_file2.exists()

    # Test YieldToPeer
    c4 = mgr_b.renew_lease("svc-single", 1)["conflict"]
    assert c4 is not None
    mgr_b.resolve_conflict(c4["conflictId"], "YieldToPeer")
    assert mgr_b.list_conflicts("svc-single") == []


def test_collaborative_batches_and_fenced_staging(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mgr = VolunteerLeaseManager(paths=paths, node_id="worker-1", user="alice")
    coord = CollaborativeBatchCoordinator(paths=paths, lease_manager=mgr)

    batch1 = coord.claim_batch("dist-svc", ["tile-1", "tile-2"], ttl_seconds=30.0)
    with pytest.raises(ValueError, match="already claimed"):
        coord.claim_batch("dist-svc", ["tile-2", "tile-3"], node_id="worker-2")

    tok = int(batch1["fencingToken"])
    coord.stage_output("dist-svc", batch1["batchId"], tok, "results/tile-1.txt", b"done-tile-1")

    # Wrong token fails commit with LeaseConflictDto
    ok_bad, conflict = coord.commit_batch(batch1["batchId"], fencing_token=tok + 10)
    assert ok_bad is False
    assert conflict["masId"] == "dist-svc"

    # Matching token promotes staged output to .sprout/storage/
    ok_good, committed = coord.commit_batch(
        batch1["batchId"], fencing_token=tok, checkpoint_metadata={"tiles": "2"}
    )
    assert ok_good is True
    assert committed["status"] == "Committed"
    assert (paths.storage_dir / "results" / "tile-1.txt").read_bytes() == b"done-tile-1"

    # Expired claim can be reclaimed by another peer
    short_batch = coord.claim_batch("dist-svc", ["tile-9"], ttl_seconds=0.1)
    assert short_batch["masId"] == "dist-svc"
    time.sleep(0.25)
    reclaimed = coord.claim_batch("dist-svc", ["tile-9"], node_id="worker-2", ttl_seconds=30.0)
    assert reclaimed["claimedByNodeId"] == "worker-2"

