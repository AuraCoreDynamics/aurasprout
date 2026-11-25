"""Comprehensive tests for SproutSupervisor Live Workspace Supervision & `ops.*` Telemetry."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest
from auraseed.mvenv.manager import MvenvManager
from auraseed.mvenv.models import MvenvCreateRequest

from aurasprout.catalog.models import AppManifest, ObservabilityConfig, SloConfig
from aurasprout.contracts.paths import SproutPaths
from aurasprout.installer.engine import SproutInstallerEngine
from aurasprout.supervisor.runner import SproutSupervisor

OPS_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "auragrid-api" / "schemas" / "ops-telemetry.schema.json"
)
APP_MANIFEST_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "auragrid-api" / "schemas" / "app.manifest.schema.json"
)


@pytest.fixture(scope="module")
def ops_telemetry_schema() -> Dict[str, Any]:
    assert OPS_SCHEMA_PATH.exists(), f"Missing schema file: {OPS_SCHEMA_PATH}"
    return json.loads(OPS_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def app_manifest_schema() -> Dict[str, Any]:
    assert APP_MANIFEST_SCHEMA_PATH.exists(), f"Missing schema file: {APP_MANIFEST_SCHEMA_PATH}"
    return json.loads(APP_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_ops_payload(payload: Dict[str, Any], schema: Dict[str, Any]) -> None:
    validator = jsonschema.Draft7Validator(
        schema,
        format_checker=jsonschema.Draft7Validator.FORMAT_CHECKER,
    )
    validator.validate(payload)


def _decode_consumed_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    decoded: List[Dict[str, Any]] = []
    for item in events:
        raw = base64.b64decode(item["payload"].encode("ascii"))
        decoded.append(json.loads(raw.decode("utf-8")))
    return decoded


def test_app_manifest_observability_and_slo_roundtrip_without_breaking_defaults(
    app_manifest_schema: Dict[str, Any],
) -> None:
    # Default manifest without observability does NOT inject "observability" on to_dict()
    base_raw = {
        "appId": "plain-app",
        "name": "Plain App",
        "version": "1.0.0",
        "services": [{"masId": "plain-svc", "mode": "LocalOnly", "runtime": "Python"}],
    }
    plain = AppManifest.from_dict(base_raw)
    assert plain.observability is None
    plain_serialized = plain.to_dict()
    assert "observability" not in plain_serialized
    jsonschema.Draft7Validator(app_manifest_schema).validate(plain_serialized)

    # Manifest with observability parses, clamps logRingLines, and round-trips cleanly
    obs_raw = {
        "appId": "obs-app",
        "name": "Observable App",
        "version": "2.1.0",
        "services": [{"masId": "obs-svc", "mode": "LocalOnly", "runtime": "Python"}],
        "observability": {
            "slo": {
                "maxStartupMs": 1500.0,
                "maxEndpointLatencyMs": 250.0,
                "maxRssMb": 512.0,
                "maxRestartCount": 3,
            },
            "triagePolicy": "BOUNDED_AUTONOMOUS",
            "logRingLines": 5,  # clamped up to 10
        },
    }
    obs_manifest = AppManifest.from_dict(obs_raw)
    assert isinstance(obs_manifest.observability, ObservabilityConfig)
    assert isinstance(obs_manifest.observability.slo, SloConfig)
    assert obs_manifest.observability.triage_policy == "BOUNDED_AUTONOMOUS"
    assert obs_manifest.observability.log_ring_lines == 10
    assert obs_manifest.observability.slo.max_startup_ms == 1500.0
    assert obs_manifest.observability.slo.max_endpoint_latency_ms == 250.0
    assert obs_manifest.observability.slo.max_rss_mb == 512.0
    assert obs_manifest.observability.slo.max_restart_count == 3

    roundtripped = obs_manifest.to_dict()
    assert "observability" in roundtripped
    assert roundtripped["observability"]["logRingLines"] == 10
    assert roundtripped["observability"]["triagePolicy"] == "BOUNDED_AUTONOMOUS"
    jsonschema.Draft7Validator(app_manifest_schema).validate(roundtripped)

    # Upper clamp check (10..10000) and invalid triagePolicy rejection
    high_ring = ObservabilityConfig(log_ring_lines=999999)
    assert high_ring.log_ring_lines == 10000
    with pytest.raises(ValueError, match="Invalid triagePolicy"):
        ObservabilityConfig(triage_policy="UNBOUNDED_YOLO")


def test_supervise_live_attached_workspace_without_versions_folder(
    tmp_path: Path,
    ops_telemetry_schema: Dict[str, Any],
) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    mvenv_mgr.create_venv(
        MvenvCreateRequest(
            name="sprout-live-ws",
            base_python=sys.executable,
            sovereign=False,
        )
    )

    ws_dir = tmp_path / "live_ws_repo"
    ws_dir.mkdir(parents=True)
    out_marker = tmp_path / "live_ws_ran.json"
    (ws_dir / "service_main.py").write_text(
        "import json, logging, os\n"
        "from pathlib import Path\n"
        "logging.basicConfig(level=logging.INFO)\n"
        "logging.getLogger('live.ws').error('structured workspace error ping')\n"
        f"Path({repr(str(out_marker))}).write_text(\n"
        "    json.dumps({'cwd': str(Path.cwd()), 'masId': os.environ['AURAGRID_MAS_ID']}),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "print('LIVE_WORKSPACE_STDOUT_OK')\n",
        encoding="utf-8",
    )
    manifest_data = {
        "appId": "live-ws",
        "name": "Live Workspace App",
        "version": "0.4.0",
        "services": [
            {
                "masId": "live-ws-worker",
                "mode": "LocalOnly",
                "runtime": "Python",
                "pythonConfig": {"scriptPath": "service_main.py"},
            }
        ],
        "distribution": {"createLaunchers": False, "createDesktopShortcut": False},
        "observability": {
            "triagePolicy": "SUPERVISED",
            "logRingLines": 50,
        },
    }
    (ws_dir / "app.manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    # Initialize a real git commit so commitSha provenance resolves deterministically
    subprocess.run(["git", "init"], cwd=str(ws_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "dev@auracore.local"], cwd=str(ws_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Aura Dev"], cwd=str(ws_dir), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(ws_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial live workspace"], cwd=str(ws_dir), capture_output=True, check=True)
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(ws_dir), capture_output=True, text=True, check=True
    ).stdout.strip()

    installer = SproutInstallerEngine(paths=paths, mvenv_manager=mvenv_mgr)
    rec = installer.attach_workspace(ws_dir, mvenv_name="sprout-live-ws")
    install_root = Path(rec.install_path)
    assert rec.attached_mode == "workspace"
    assert not (install_root / "versions").exists()

    supervisor = SproutSupervisor(paths=paths, mvenv_manager=mvenv_mgr)
    try:
        st = supervisor.start_installed_service("live-ws", mas_id="live-ws-worker")
        assert st._exit_done.wait(timeout=15.0)
        assert st.exit_code == 0
        assert st.state == "Stopped"
        assert out_marker.exists()
        marker_data = json.loads(out_marker.read_text(encoding="utf-8"))
        assert marker_data["masId"] == "live-ws-worker"
        assert Path(marker_data["cwd"]).resolve() == ws_dir.resolve()
        assert st.log_ring_lines == 50
        assert any("LIVE_WORKSPACE_STDOUT_OK" in line for line in st.stdout_ring)

        assert st.provenance["appId"] == "live-ws"
        assert st.provenance["masId"] == "live-ws-worker"
        assert st.provenance["version"] == "0.4.0"
        assert st.provenance["commitSha"] == expected_sha
        assert st.provenance["mvenvDigest"].startswith("sha256:")
        assert st.provenance["mvenvDigest"] != "sha256:unknown"
        assert st.provenance["runtimeTier"] == "aurasprout"

        # Structured ERROR log emitted ops.logs.error while clean exit 0 emitted NO crash event
        err_events = _decode_consumed_events(
            supervisor.ipc_server.state.event_store.consume("ops.logs.error")
        )
        assert len(err_events) == 1
        _validate_ops_payload(err_events[0], ops_telemetry_schema)
        assert err_events[0]["logger"] == "live.ws"
        assert err_events[0]["level"] == "ERROR"
        assert "structured workspace error ping" in err_events[0]["message"]

        crash_events = supervisor.ipc_server.state.event_store.consume("ops.lifecycle.crash")
        assert crash_events == []
    finally:
        supervisor.shutdown()


def test_unhandled_exception_emits_schema_valid_log_error_and_crash_events(
    tmp_path: Path,
    ops_telemetry_schema: Dict[str, Any],
) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    mvenv_mgr.create_venv(
        MvenvCreateRequest(
            name="sprout-crash-demo",
            base_python=sys.executable,
            sovereign=False,
        )
    )

    ws_dir = tmp_path / "crash_ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "boom_service.py").write_text(
        "import sys\n"
        "def compute_tile(scale: int) -> float:\n"
        "    if scale == 0:\n"
        "        raise ValueError('invalid zero tile scale')\n"
        "    return 256.0 / scale\n"
        "\n"
        "def main() -> None:\n"
        "    print('starting tile worker', flush=True)\n"
        "    compute_tile(0)\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    (ws_dir / "app.manifest.json").write_text(
        json.dumps(
            {
                "appId": "crash-demo",
                "name": "Crash Demo",
                "version": "1.2.3",
                "services": [
                    {
                        "masId": "crash-worker",
                        "mode": "LocalOnly",
                        "runtime": "Python",
                        "pythonConfig": {"scriptPath": "boom_service.py"},
                    }
                ],
                "distribution": {"createLaunchers": False, "createDesktopShortcut": False},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    installer = SproutInstallerEngine(paths=paths, mvenv_manager=mvenv_mgr)
    installer.attach_workspace(ws_dir, mvenv_name="sprout-crash-demo")

    supervisor = SproutSupervisor(paths=paths, mvenv_manager=mvenv_mgr)
    try:
        st1 = supervisor.start_installed_service("crash-demo", mas_id="crash-worker")
        assert st1._exit_done.wait(timeout=15.0)
        assert st1.state == "Crashed"
        assert st1.exit_code not in (0, None)

        statuses = supervisor.list_statuses()
        assert any(s["masId"] == "crash-worker" and s["state"] == "Crashed" for s in statuses)

        store = supervisor.ipc_server.state.event_store
        log_errors = _decode_consumed_events(store.consume("ops.logs.error"))
        crashes = _decode_consumed_events(store.consume("ops.lifecycle.crash"))

        assert len(log_errors) == 1
        assert len(crashes) == 1

        err_payload = log_errors[0]
        crash_payload = crashes[0]

        _validate_ops_payload(err_payload, ops_telemetry_schema)
        _validate_ops_payload(crash_payload, ops_telemetry_schema)

        assert err_payload["topic"] == "ops.logs.error"
        assert err_payload["exceptionType"] == "ValueError"
        assert err_payload["message"] == "invalid zero tile scale"
        assert "Traceback (most recent call last):" in err_payload["stackTrace"]
        assert len(err_payload["frames"]) >= 2
        assert err_payload["frames"][-1]["function"] == "compute_tile"
        assert len(err_payload["logTail"]) >= 3

        assert crash_payload["topic"] == "ops.lifecycle.crash"
        assert crash_payload["exitCode"] == st1.exit_code
        assert crash_payload["restartCount"] == 0
        assert crash_payload["uptimeSeconds"] >= 0
        assert any("ValueError: invalid zero tile scale" in ln for ln in crash_payload["stderrTail"])

        # Re-running the identical crash produces deterministic fingerprints and increments restartCount
        st2 = supervisor.start_installed_service("crash-demo", mas_id="crash-worker")
        assert st2._exit_done.wait(timeout=15.0)
        log_errors_2 = _decode_consumed_events(store.consume("ops.logs.error", after_sequence=1))
        crashes_2 = _decode_consumed_events(store.consume("ops.lifecycle.crash", after_sequence=1))
        assert len(log_errors_2) == 1
        assert len(crashes_2) == 1
        _validate_ops_payload(log_errors_2[0], ops_telemetry_schema)
        _validate_ops_payload(crashes_2[0], ops_telemetry_schema)
        assert log_errors_2[0]["fingerprint"] == err_payload["fingerprint"]
        assert crashes_2[0]["fingerprint"] == crash_payload["fingerprint"]
        assert crashes_2[0]["restartCount"] == 1
    finally:
        supervisor.shutdown()


def test_stop_service_intentional_termination_emits_no_false_crash(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    ready_file = tmp_path / "sleeper_ready.txt"
    sleeper_script = tmp_path / "sleeper_service.py"
    sleeper_script.write_text(
        "import time\n"
        "from pathlib import Path\n"
        f"Path({repr(str(ready_file))}).write_text('ready', encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )

    supervisor = SproutSupervisor(paths=paths)
    try:
        st = supervisor.start_process(
            mas_id="long-sleeper",
            app_id="sleeper-app",
            mode="LocalOnly",
            command=[sys.executable, str(sleeper_script)],
        )
        deadline = time.monotonic() + 10.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists()

        stopped = supervisor.stop_service("long-sleeper")
        assert stopped is True
        assert st._stopping is True
        assert st.state == "Stopped"

        statuses = supervisor.list_statuses()
        assert any(s["masId"] == "long-sleeper" and s["state"] == "Stopped" for s in statuses)

        crash_events = supervisor.ipc_server.state.event_store.consume("ops.lifecycle.crash")
        assert crash_events == []
    finally:
        supervisor.shutdown()


def test_record_perf_metric_breach_emits_schema_valid_ops_perf_anomaly(
    tmp_path: Path,
    ops_telemetry_schema: Dict[str, Any],
) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    noop_script = tmp_path / "noop.py"
    noop_script.write_text("import time\ntime.sleep(0.05)\n", encoding="utf-8")

    supervisor = SproutSupervisor(paths=paths)
    try:
        st = supervisor.start_process(
            mas_id="perf-svc",
            app_id="perf-app",
            mode="LocalOnly",
            command=[sys.executable, str(noop_script)],
        )
        assert st._exit_done.wait(timeout=10.0)

        # Within SLO -> None, no event published
        ok_res = supervisor.record_perf_metric(
            mas_id="perf-svc",
            metric="latency_ms",
            observed_value=85.0,
            slo_threshold=200.0,
            operation="GET /tiles/0/0/0",
        )
        assert ok_res is None
        assert supervisor.ipc_server.state.event_store.consume("ops.perf.anomaly") == []

        # Breaching SLO -> emits schema-valid ops.perf.anomaly
        breach = supervisor.record_perf_metric(
            mas_id="perf-svc",
            metric="latency_ms",
            observed_value=420.5,
            slo_threshold=200.0,
            operation="GET /tiles/0/0/0",
        )
        assert breach is not None
        _validate_ops_payload(breach, ops_telemetry_schema)

        persisted = _decode_consumed_events(
            supervisor.ipc_server.state.event_store.consume("ops.perf.anomaly")
        )
        assert len(persisted) == 1
        _validate_ops_payload(persisted[0], ops_telemetry_schema)
        assert persisted[0]["topic"] == "ops.perf.anomaly"
        assert persisted[0]["metric"] == "latency_ms"
        assert persisted[0]["observedValue"] == 420.5
        assert persisted[0]["sloThreshold"] == 200.0
        assert persisted[0]["operation"] == "GET /tiles/0/0/0"
        assert persisted[0]["provenance"]["appId"] == "perf-app"
        assert persisted[0]["provenance"]["masId"] == "perf-svc"
        assert persisted[0]["provenance"]["runtimeTier"] == "aurasprout"
    finally:
        supervisor.shutdown()
