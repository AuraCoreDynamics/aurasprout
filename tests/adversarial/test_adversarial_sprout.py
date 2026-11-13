"""Adversarial RED-TEAM tests covering all 7 scoring.json axes for AuraSprout."""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest
from auragrid import CollaborationClient, ConflictError, IpcClient, IpcOptions, LeaseClient, StorageClient

from aurasprout.catalog.crypto import extract_archive_safely
from aurasprout.catalog.models import AppManifest
from aurasprout.contracts.paths import SproutPaths, validate_within_root
from aurasprout.ipc.server import IpcBridgeServer
from aurasprout.ui.server import ManagementServerState, SproutManagementServer


def test_adversarial_zip_slip_tar_symlink_and_path_traversal_blocked(tmp_path: Path) -> None:
    outside = tmp_path / "escaped.txt"
    extract_dir = tmp_path / "extract"

    # Malicious Zip-Slip archive
    malicious_zip = tmp_path / "slip.apkg"
    with zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("../../escaped.txt", "pwned")
    with pytest.raises(ValueError, match="Path traversal|escapes"):
        extract_archive_safely(malicious_zip, extract_dir)
    assert not outside.exists()

    # Malicious Tar symlink escape
    malicious_tar = tmp_path / "symlink.apkg"
    with tarfile.open(malicious_tar, "w:gz") as tf:
        info = tarfile.TarInfo("evil_link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(ValueError, match="Unsafe tar member"):
        extract_archive_safely(malicious_tar, extract_dir)

    # Path traversal in SproutPaths & manifest identifiers
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    with pytest.raises(ValueError):
        paths.storage_file("../../etc/passwd")
    with pytest.raises(ValueError):
        validate_within_root(paths.apps_dir, "../escape")
    with pytest.raises(ValueError):
        AppManifest.from_dict({"appId": "../escape", "name": "Bad", "services": []})


def test_adversarial_tier2_ipc_and_tier3_ui_security_boundaries(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")

    with IpcBridgeServer(paths=paths) as bridge:
        with httpx.Client(base_url=bridge.base_url) as client:
            # Missing token -> 401
            assert client.get("/cell/config").status_code == 401
            # Wrong token -> 401
            assert client.get("/cell/config", headers={"X-AuraGrid-IPC-Token": "bad"}).status_code == 401
            # Browser Origin rejected on Tier 2 IPC -> 403
            resp_origin = client.get(
                "/cell/config",
                headers={
                    "X-AuraGrid-IPC-Token": bridge.ipc_token,
                    "Origin": "https://attacker.example",
                },
            )
            assert resp_origin.status_code == 403
            # Wildcard CORS strictly absent
            opt_resp = client.options("/cell/config")
            assert "Access-Control-Allow-Origin" not in opt_resp.headers
            # Path traversal via storage query -> 400
            trav = client.get(
                "/cell/storage/read",
                params={"path": "../../secret.txt"},
                headers={"X-AuraGrid-IPC-Token": bridge.ipc_token},
            )
            assert trav.status_code == 400

    with SproutManagementServer(state=ManagementServerState(paths=paths)) as ui:
        with httpx.Client(base_url=ui.base_url) as client:
            # Unauthenticated API call blocked
            assert client.post("/api/v1/volunteer/svc", json={"enabled": True}).status_code == 401
            # Exchange token, then try CSRF omit -> 403
            auth = client.post("/api/v1/auth/session", json={"token": ui.bootstrap_token})
            assert auth.status_code == 200
            assert client.post("/api/v1/volunteer/svc", json={"enabled": True}).status_code == 403


def test_adversarial_split_brain_fencing_and_smb_wal_absence(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")

    with IpcBridgeServer(paths=paths, node_id="node-primary") as bridge:
        opts = IpcOptions(port=bridge.port, token=bridge.ipc_token, timeout=5.0, max_retries=0)
        with IpcClient(opts) as ipc:
            leases = LeaseClient(ipc)
            storage = StorageClient(ipc)
            collab = CollaborationClient(ipc)

            acq1 = leases.try_acquire_lease("critical-job", {"nodeId": "node-primary"})
            assert acq1.acquired and acq1.lease is not None
            tok1 = acq1.lease.fencing_token
            storage.write("state.json", b'{"owner":"node-primary"}', fencing_token=tok1)

            # Release and reacquire on secondary -> token monotonically increases; tok1 becomes stale
            leases.release_lease("critical-job", tok1)
            acq2 = leases.try_acquire_lease("critical-job", {"nodeId": "node-secondary"})
            assert acq2.acquired and acq2.lease is not None
            assert acq2.lease.fencing_token > tok1

            # Stale storage mutation with tok1 rejected (403)
            with pytest.raises(Exception):
                storage.write("state.json", b'{"owner":"stale"}', fencing_token=tok1)
            assert storage.read("state.json") == b'{"owner":"node-primary"}'

            # Distributed batch claim superseded or wrong token raises ConflictError (409)
            batch = collab.claim_work_batch("critical-dist", ["a", "b"])
            with pytest.raises(ConflictError):
                collab.commit_work_batch(batch.batch_id, fencing_token=0)

            # Event store uses TRUNCATE, zero WAL sidecars created
            pragmas = bridge.state.event_store.get_pragmas()
            assert pragmas["journal_mode"] == "TRUNCATE"
            assert not list(paths.events_dir.glob("*-wal"))
            assert not list(paths.events_dir.glob("*-shm"))

