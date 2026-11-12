"""Integration tests for Tier 2 IpcBridgeServer, SqliteEventStore, auragrid-py SDK & SproutSupervisor."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from auragrid import (
    CollaborationClient,
    ConflictError,
    ConflictResolutionAction,
    EventClient,
    GovernanceClient,
    IpcClient,
    IpcOptions,
    LeaseClient,
    RegistryClient,
    StorageClient,
)

from aurasprout.contracts.paths import SproutPaths
from aurasprout.ipc.server import IpcBridgeServer
from aurasprout.supervisor.runner import SproutSupervisor


def test_ipc_bridge_full_sdk_interop_and_sqlite_truncate(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    with IpcBridgeServer(paths=paths, node_id="node-alpha") as bridge:
        opts = IpcOptions(port=bridge.port, token=bridge.ipc_token, timeout=5.0, max_retries=0)

        # Public health probes require no token
        with httpx.Client(base_url=bridge.base_url) as anon:
            assert anon.get("/health").status_code == 200
            assert anon.get("/api/health").status_code == 200
            assert anon.get("/ready").status_code == 200
            assert anon.get("/cell/config").status_code == 401

        with IpcClient(opts) as ipc:
            # Config
            assert ipc.put("/cell/config/tileSize", json={"value": 512}).status_code == 204
            assert ipc.get("/cell/config/tileSize").json()["value"] == 512
            assert ipc.get("/cell/config").json()["config"]["tileSize"] == 512
            assert ipc.get("/cell/config/_metadata").json()["keyCount"] == 1
            assert ipc.delete("/cell/config/tileSize").json()["removed"] is True

            # Governance
            gov = GovernanceClient(ipc)
            assert gov.can_deploy("geo-app", "alice").is_permitted is True
            assert len(gov.get_audit_trail(limit=5)) >= 1

            # Events via EventClient (SQLite TRUNCATE, zero WAL files)
            ev = EventClient(ipc)
            published = ev.publish("jobs", b"hello-sprout", event_type="JobCreated")
            assert published.sequence_number == 1
            assert ev.topic_exists("jobs") is True
            assert "jobs" in ev.list_topics()
            consumed = ev.consume("jobs", after=0, count=10)
            assert len(consumed) == 1 and base64.b64decode(consumed[0].payload) == b"hello-sprout"
            assert ev.get_marker("jobs", "worker-a") is None
            ev.set_marker("jobs", 1, "worker-a")
            marker = ev.get_marker("jobs", "worker-a")
            assert marker is not None and marker.sequence_number == 1

            pragmas = bridge.state.event_store.get_pragmas()
            assert pragmas["journal_mode"] == "TRUNCATE"
            assert pragmas["synchronous"] == 2
            assert not (paths.events_dir / "events.sqlite-wal").exists()
            assert not (paths.events_dir / "events.sqlite-shm").exists()

            # Lease + Fenced Storage + Registry + Collaboration
            leases = LeaseClient(ipc)
            acq = leases.try_acquire_lease("svc-1", {"nodeId": "node-alpha", "score": 1.0})
            assert acq.acquired is True and acq.lease is not None
            token = acq.lease.fencing_token

            storage = StorageClient(ipc)
            storage.write("outputs/result.bin", b"0123456789", fencing_token=token)
            assert storage.read("outputs/result.bin") == b"0123456789"
            assert storage.read_range("outputs/result.bin", 2, 4) == b"2345"
            assert storage.exists("outputs/result.bin") is True

            assert ipc.post("/cell/service-port", json={"serviceName": "svc-1", "port": 9100}).status_code == 200
            reg = RegistryClient(ipc)
            assert any(e.service_name == "svc-1" for e in reg.discover("svc-1"))

            collab = CollaborationClient(ipc)
            batch = collab.claim_work_batch("dist-job", ["item-1", "item-2"])
            assert batch.fencing_token is not None
            with pytest.raises(ConflictError) as exc_info:
                collab.commit_work_batch(batch.batch_id, fencing_token=batch.fencing_token + 999)
            assert exc_info.value.conflict is not None

            resolved = collab.resolve_conflict(
                exc_info.value.conflict.conflict_id, ConflictResolutionAction.YIELD_TO_PEER
            )
            assert resolved.mas_id == "dist-job"

            committed = collab.commit_work_batch(batch.batch_id, fencing_token=batch.fencing_token)
            assert committed.status == "Committed"


def test_supervisor_injects_ipc_env_and_enforces_volunteer_opt_in(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    out_file = tmp_path / "child_env.json"
    child_script = tmp_path / "child_mas.py"
    child_script.write_text(
        "import json, os, time\n"
        "from pathlib import Path\n"
        "from auragrid import IpcClient, IpcOptions, EventClient\n"
        "with IpcClient(IpcOptions()) as ipc:\n"
        "    ev = EventClient(ipc)\n"
        "    evt = ev.publish('child-topic', b'ping', event_type='ChildReady')\n"
        f"    Path({repr(str(out_file))}).write_text(json.dumps({{\n"
        "        'masId': os.environ['AURAGRID_MAS_ID'],\n"
        "        'url': os.environ['AURAGRID_LOCAL_URL'],\n"
        "        'seq': evt.sequence_number,\n"
        "    }), encoding='utf-8')\n"
        "time.sleep(0.2)\n",
        encoding="utf-8",
    )

    supervisor = SproutSupervisor(paths=paths)
    try:
        # Opt-in safeguard: CellSingleton blocked if volunteer_enabled=False
        with pytest.raises(PermissionError, match="volunteerEnabled is False"):
            supervisor.start_process(
                mas_id="singleton-blocked",
                app_id="demo",
                mode="CellSingleton",
                command=[sys.executable, str(child_script)],
                volunteer_enabled=False,
            )

        # LocalOnly starts cleanly and zero shared .sprout/leases or .sprout/nodes files are created
        st = supervisor.start_process(
            mas_id="local-child",
            app_id="demo",
            mode="LocalOnly",
            command=[sys.executable, str(child_script)],
            volunteer_enabled=False,
        )
        assert st.process is not None
        st.process.wait(timeout=15.0)
        assert out_file.exists()
        payload = json.loads(out_file.read_text(encoding="utf-8"))
        assert payload["masId"] == "local-child"
        assert payload["seq"] == 1
        assert not paths.leases_dir.exists() or not any(paths.leases_dir.iterdir())
        assert not paths.nodes_dir.exists() or not any(paths.nodes_dir.iterdir())
    finally:
        supervisor.shutdown()

