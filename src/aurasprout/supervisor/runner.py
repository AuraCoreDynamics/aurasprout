"""MAS Lifecycle Supervisor for AuraSprout.

Spawns local & volunteered MAS processes inside isolated OS process groups,
injecting Tier 2 IPC connection variables (`AURAGRID_LOCAL_URL`,
`AURAGRID_IPC_PORT`, `AURAGRID_IPC_TOKEN`, `AURAGRID_FENCING_TOKEN`), strictly
enforcing `LocalOnly` zero-shared-state guarantees and opt-in volunteering
defaults (`volunteerEnabled=False`), and emitting schema-valid `ops.*`
DevSecOps runtime telemetry into `SqliteEventStore`.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import json
import logging
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Deque, Dict, List, Optional, Tuple

from auraseed.mvenv.manager import MvenvManager

from aurasprout.catalog.models import AppManifest, MasDefinition, ObservabilityConfig
from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root
from aurasprout.installer.autostart import resolve_entry_point_invocation
from aurasprout.installer.registry import InstalledAppRecord, InstalledAppsRegistry
from aurasprout.ipc.server import IpcBridgeServer

_LOG = logging.getLogger(__name__)

_VALID_PERF_METRICS = ("latency_ms", "rss_mb", "cpu_pct", "error_rate")
_METRIC_ALIASES = {
    "startup_ms": "latency_ms",
    "endpoint_latency_ms": "latency_ms",
}

_TB_HEADER_RE = re.compile(r"^Traceback \(most recent call last\):\s*$")
_TB_CHAIN_MESSAGES = (
    "During handling of the above exception, another exception occurred:",
    "The above exception was the direct cause of the following exception:",
)
_TB_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line (\d+), in (.+)$')
_PY_COLON_LOG_RE = re.compile(r"^(ERROR|CRITICAL):([A-Za-z0-9_.-]+):(.*)$")
_DASH_LOG_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ][0-9:,.\-+Z]+\s+-\s+([A-Za-z0-9_.-]+)\s+-\s+(ERROR|CRITICAL)\s+-\s+(.+)$"
)
_BRACKET_LOG_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[T ][0-9:,.\-+Z]+\s+)?"
    r"(?:\[(ERROR|CRITICAL)\]|(ERROR|CRITICAL))"
    r"(?:\s+([A-Za-z0-9_.-]+)\s*[:\-]\s*|\s*:\s*|\s+)(.+)$"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_fingerprint(*parts: str) -> str:
    joined = "|".join(str(p) for p in parts)
    return f"sha256:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


class _LogStreamParser:
    """Stateful single-stream parser for Python tracebacks and structured ERROR/CRITICAL logs."""

    def __init__(
        self,
        stream_name: str,
        ring: Deque[str],
        provenance: Dict[str, Any],
        emit_callback: Callable[[Dict[str, Any]], None],
    ) -> None:
        self.stream_name = stream_name
        self.ring = ring
        self.provenance = provenance
        self.emit_callback = emit_callback

        self._in_traceback = False
        self._tb_lines: List[str] = []
        self._tb_frames: List[Dict[str, Any]] = []
        self._last_logger: Optional[str] = None
        self._last_level: Optional[str] = None

    def feed(self, line: str) -> None:
        if _TB_HEADER_RE.match(line):
            if not self._in_traceback:
                self._in_traceback = True
                self._tb_lines = [line]
                self._tb_frames = []
            else:
                self._tb_lines.append(line)
            return

        if self._in_traceback:
            m_frame = _TB_FRAME_RE.match(line)
            if m_frame:
                file_path = m_frame.group(1).strip() or "<unknown>"
                line_no = max(0, int(m_frame.group(2)))
                func_name = m_frame.group(3).strip() or "<unknown>"
                self._tb_frames.append(
                    {
                        "file": file_path,
                        "line": line_no,
                        "function": func_name,
                    }
                )
                self._tb_lines.append(line)
                return

            stripped = line.strip()
            if line[:1] in (" ", "\t") or stripped == "" or stripped in _TB_CHAIN_MESSAGES:
                self._tb_lines.append(line)
                return

            # Non-indented terminal exception line `<ExcType>: <msg>` or `<ExcType>`
            self._tb_lines.append(line)
            self._emit_current_traceback(terminal_line=stripped)
            return

        self._check_structured_error_line(line)

    def flush(self) -> None:
        if self._in_traceback and self._tb_lines:
            last_non_empty = next(
                (ln.strip() for ln in reversed(self._tb_lines) if ln.strip()),
                "UnhandledException",
            )
            self._emit_current_traceback(terminal_line=last_non_empty)

    def _emit_current_traceback(self, terminal_line: str) -> None:
        if ":" in terminal_line:
            exc_part, msg_part = terminal_line.split(":", 1)
            exc_type = exc_part.strip() or "Exception"
            msg = msg_part.strip() or exc_type
        else:
            exc_type = terminal_line.strip() or "Exception"
            msg = exc_type

        stack_trace = "\n".join(self._tb_lines)
        frames = list(self._tb_frames)
        logger_name = self._last_logger or self.stream_name
        level_name = self._last_level or "ERROR"

        self._in_traceback = False
        self._tb_lines = []
        self._tb_frames = []
        self._last_logger = None
        self._last_level = None

        top_frame_sig = (
            f"{Path(frames[-1]['file']).name}:{frames[-1]['function']}:{frames[-1]['line']}"
            if frames
            else logger_name
        )
        fp = _compute_fingerprint(
            self.provenance.get("appId", "unknown"),
            self.provenance.get("masId", "unknown"),
            "ops.logs.error",
            exc_type,
            top_frame_sig,
        )
        payload: Dict[str, Any] = {
            "topic": "ops.logs.error",
            "timestamp": _utc_now_iso(),
            "provenance": dict(self.provenance),
            "logger": logger_name,
            "level": level_name,
            "message": msg,
            "exceptionType": exc_type,
            "stackTrace": stack_trace,
            "frames": frames,
            "logTail": list(self.ring),
            "fingerprint": fp,
        }
        self.emit_callback(payload)

    def _check_structured_error_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return

        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                raw_lvl = str(
                    parsed.get("level") or parsed.get("levelname") or parsed.get("severity") or ""
                ).upper()
                if raw_lvl in ("ERROR", "CRITICAL", "FATAL"):
                    lvl = "CRITICAL" if raw_lvl in ("CRITICAL", "FATAL") else "ERROR"
                    logger_name = (
                        str(parsed.get("logger") or parsed.get("name") or self.stream_name).strip()
                        or self.stream_name
                    )
                    msg = (
                        str(parsed.get("message") or parsed.get("msg") or stripped).strip()
                        or stripped
                    )
                    exc_type = (
                        str(parsed["exceptionType"]).strip()
                        if parsed.get("exceptionType")
                        else None
                    )
                    stack_trace = (
                        str(parsed["stackTrace"])
                        if parsed.get("stackTrace")
                        else None
                    )
                    self._last_logger = logger_name
                    self._last_level = lvl
                    self._emit_structured_error(
                        logger_name=logger_name,
                        level=lvl,
                        message=msg,
                        exc_type=exc_type,
                        stack_trace=stack_trace,
                    )
                return

        m_colon = _PY_COLON_LOG_RE.match(stripped)
        if m_colon:
            lvl = m_colon.group(1)
            logger_name = m_colon.group(2).strip() or self.stream_name
            msg = m_colon.group(3).strip() or stripped
            self._last_logger = logger_name
            self._last_level = lvl
            self._emit_structured_error(logger_name=logger_name, level=lvl, message=msg)
            return

        m_dash = _DASH_LOG_RE.match(stripped)
        if m_dash:
            logger_name = m_dash.group(1).strip() or self.stream_name
            lvl = m_dash.group(2)
            msg = m_dash.group(3).strip() or stripped
            self._last_logger = logger_name
            self._last_level = lvl
            self._emit_structured_error(logger_name=logger_name, level=lvl, message=msg)
            return

        m_bracket = _BRACKET_LOG_RE.match(stripped)
        if m_bracket:
            lvl = m_bracket.group(1) or m_bracket.group(2) or "ERROR"
            logger_name = (m_bracket.group(3) or "").strip() or self.stream_name
            msg = m_bracket.group(4).strip() or stripped
            self._last_logger = logger_name
            self._last_level = lvl
            self._emit_structured_error(logger_name=logger_name, level=lvl, message=msg)

    def _emit_structured_error(
        self,
        logger_name: str,
        level: str,
        message: str,
        exc_type: Optional[str] = None,
        stack_trace: Optional[str] = None,
    ) -> None:
        fp = _compute_fingerprint(
            self.provenance.get("appId", "unknown"),
            self.provenance.get("masId", "unknown"),
            "ops.logs.error",
            exc_type or level,
            logger_name,
            message,
        )
        payload: Dict[str, Any] = {
            "topic": "ops.logs.error",
            "timestamp": _utc_now_iso(),
            "provenance": dict(self.provenance),
            "logger": logger_name,
            "level": level,
            "message": message,
            "exceptionType": exc_type,
            "stackTrace": stack_trace,
            "frames": [],
            "logTail": list(self.ring),
            "fingerprint": fp,
        }
        self.emit_callback(payload)


@dataclass
class ManagedProcessState:
    mas_id: str
    app_id: str
    mode: str
    process: Optional[subprocess.Popen[bytes]] = None
    fencing_token: Optional[int] = None
    pid: Optional[int] = None
    state: str = "Stopped"
    restart_count: int = 0
    command: List[str] = field(default_factory=list)
    stdout_ring: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=200))
    stderr_ring: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=200))
    log_ring_lines: int = 200
    provenance: Dict[str, Any] = field(default_factory=dict)
    observability: Optional[ObservabilityConfig] = None
    started_monotonic: float = field(default_factory=time.monotonic)
    exit_code: Optional[int] = None
    _stopping: bool = False
    _exit_done: threading.Event = field(default_factory=threading.Event, repr=False)


class SproutSupervisor:
    """Supervises local and volunteered MAS services backed by an ``IpcBridgeServer``."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        registry: Optional[InstalledAppsRegistry] = None,
        ipc_server: Optional[IpcBridgeServer] = None,
        mvenv_manager: Optional[MvenvManager] = None,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.registry = registry or InstalledAppsRegistry(paths=self.paths)
        self._mvenv_manager = mvenv_manager
        self._owns_ipc = ipc_server is None
        self.ipc_server = ipc_server or IpcBridgeServer(
            paths=self.paths, mode_resolver=self._resolve_mas_mode
        )
        self._lock = threading.RLock()
        self._processes: Dict[str, ManagedProcessState] = {}

    def _get_mvenv_manager(self) -> MvenvManager:
        if self._mvenv_manager is None:
            self._mvenv_manager = MvenvManager()
        return self._mvenv_manager

    def _resolve_mas_mode(self, mas_id: str) -> str:
        with self._lock:
            st = self._processes.get(mas_id)
            if st is not None:
                return st.mode
        for app in self.registry.list_all():
            for svc in app.services:
                if svc.mas_id == mas_id:
                    return svc.mode
        return "CellSingleton"

    def ensure_bridge_started(self) -> IpcBridgeServer:
        self.ipc_server.start()
        return self.ipc_server

    def shutdown(self) -> None:
        with self._lock:
            states = list(self._processes.values())
        for st in states:
            self.stop_service(st.mas_id)
            st._exit_done.wait(timeout=3.0)
        if self._owns_ipc:
            self.ipc_server.stop()

    @staticmethod
    def _resolve_app_manifest_and_src_root(app: InstalledAppRecord) -> Tuple[AppManifest, Path]:
        """Resolve `AppManifest` and `src_root` supporting both live attached workspaces and extracted versions."""
        install_root = Path(app.install_path).resolve()
        ptr = InstalledAppsRegistry.read_active_version_pointer(install_root)
        version_dir = (
            Path(ptr["versionDir"]).resolve()
            if ptr and ptr.get("versionDir")
            else (install_root / "versions" / app.version)
        )
        is_workspace = (app.attached_mode == "workspace") or bool(
            ptr and ptr.get("attachedMode") == "workspace"
        )

        if is_workspace or not (version_dir / "app.manifest.json").exists():
            active_link = install_root / "active"
            if active_link.exists() and (active_link / "app.manifest.json").exists():
                active_dir = active_link.resolve()
                return AppManifest.from_file(active_dir), active_dir

            raw_source = app.source_path or (ptr.get("sourcePath") if ptr else None)
            if raw_source:
                src_candidate = Path(raw_source).resolve()
                if src_candidate.exists() and (src_candidate / "app.manifest.json").exists():
                    return AppManifest.from_file(src_candidate), src_candidate

        manifest = AppManifest.from_file(version_dir)
        src_root = version_dir / "src" if (version_dir / "src").exists() else version_dir
        return manifest, src_root

    def _resolve_mvenv_digest(
        self,
        mvenv_name: Optional[str],
        mvenv_path: Optional[str] = None,
    ) -> str:
        candidates = [c for c in (mvenv_name, mvenv_path) if c and str(c).strip()]
        if not candidates:
            return "sha256:unknown"
        try:
            mgr = self._get_mvenv_manager()
        except Exception:
            return "sha256:unknown"

        for cand in candidates:
            try:
                prov = mgr.get_mvenv_provenance(str(cand))
                digest = str(prov.get("provenance_digest") or "").strip()
                if digest:
                    return digest
            except Exception:
                continue
        return "sha256:unknown"

    @staticmethod
    def _resolve_commit_sha(src_root: Optional[Path]) -> str:
        if src_root is None:
            return "unknown"
        resolved = Path(src_root).resolve()
        if not (resolved / ".git").exists():
            return "unknown"
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(resolved),
                capture_output=True,
                text=True,
                shell=False,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return "unknown"

    def _build_provenance(
        self,
        app_id: str,
        mas_id: str,
        version: str = "0.0.0",
        commit_sha: str = "unknown",
        mvenv_digest: str = "sha256:unknown",
    ) -> Dict[str, Any]:
        safe_app = validate_identifier(app_id, "appId")
        safe_mas = validate_identifier(mas_id, "masId")
        node_id = (self.ipc_server.state.node_id or "sprout-node").strip() or "sprout-node"
        return {
            "appId": safe_app,
            "masId": safe_mas,
            "version": (version or "0.0.0").strip() or "0.0.0",
            "commitSha": (commit_sha or "unknown").strip() or "unknown",
            "mvenvDigest": (mvenv_digest or "sha256:unknown").strip() or "sha256:unknown",
            "nodeId": node_id,
            "runtimeTier": "aurasprout",
        }

    def _publish_ops_event(self, topic: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        payload_b64 = base64.b64encode(raw_bytes).decode("ascii")
        node_id = (self.ipc_server.state.node_id or "sprout-node").strip() or "sprout-node"
        return self.ipc_server.state.event_store.publish(
            topic,
            payload_b64,
            source_node_id=node_id,
            event_type=topic,
        )

    def _stream_reader(
        self,
        state: ManagedProcessState,
        pipe: IO[bytes],
        ring: Deque[str],
        stream_name: str,
    ) -> None:
        parser = _LogStreamParser(
            stream_name=stream_name,
            ring=ring,
            provenance=state.provenance,
            emit_callback=lambda evt: self._safe_publish_ops_event("ops.logs.error", evt),
        )
        try:
            for raw_line in iter(pipe.readline, b""):
                text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                ring.append(text)
                parser.feed(text)
            parser.flush()
        except Exception as exc:
            _LOG.debug("Stream reader (%s) error for %s: %s", stream_name, state.mas_id, exc)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _safe_publish_ops_event(self, topic: str, payload: Dict[str, Any]) -> None:
        try:
            self._publish_ops_event(topic, payload)
        except Exception as exc:
            _LOG.debug("Failed to publish %s telemetry event: %s", topic, exc)

    def _exit_watcher(
        self,
        state: ManagedProcessState,
        stdout_thread: Optional[threading.Thread],
        stderr_thread: Optional[threading.Thread],
    ) -> None:
        proc = state.process
        if proc is None:
            state._exit_done.set()
            return
        try:
            rc = proc.wait()
            if stdout_thread is not None:
                stdout_thread.join(timeout=2.0)
            if stderr_thread is not None:
                stderr_thread.join(timeout=2.0)

            state.exit_code = int(rc)
            if state._stopping or rc == 0:
                state.state = "Stopped"
                return

            state.state = "Crashed"
            uptime = round(max(0.0, time.monotonic() - state.started_monotonic), 6)
            sig_name: Optional[str] = None
            if rc < 0:
                sig_num = -rc
                try:
                    sig_name = signal.Signals(sig_num).name
                except ValueError:
                    sig_name = f"SIG{sig_num}"

            stderr_tail = list(state.stderr_ring)
            fp = _compute_fingerprint(
                state.app_id,
                state.mas_id,
                "ops.lifecycle.crash",
                f"exit={rc}",
                f"sig={sig_name or 'none'}",
            )
            crash_payload: Dict[str, Any] = {
                "topic": "ops.lifecycle.crash",
                "timestamp": _utc_now_iso(),
                "provenance": dict(state.provenance),
                "exitCode": int(rc),
                "signal": sig_name,
                "restartCount": int(state.restart_count),
                "uptimeSeconds": uptime,
                "stderrTail": stderr_tail,
                "fingerprint": fp,
            }
            self._safe_publish_ops_event("ops.lifecycle.crash", crash_payload)
        except Exception as exc:
            _LOG.debug("Exit watcher error for %s: %s", state.mas_id, exc)
        finally:
            state._exit_done.set()

    def _build_mas_command(
        self,
        svc: MasDefinition,
        app_root: Path,
        python_exe: Path,
    ) -> Tuple[List[str], Path]:
        cwd = app_root
        if svc.working_directory:
            cwd = validate_within_root(app_root, svc.working_directory)

        if svc.python_config is not None:
            py_cfg = svc.python_config
            target_py = str(python_exe)
            if py_cfg.python_executable and py_cfg.python_executable not in ("python", "python3"):
                cand = Path(py_cfg.python_executable)
                if cand.exists():
                    target_py = str(cand.resolve())
            args = shlex.split(py_cfg.arguments) if py_cfg.arguments else []
            if py_cfg.script_path:
                script = validate_within_root(cwd, py_cfg.script_path)
                return [target_py, str(script), *args], cwd
            return [target_py, *args], cwd

        raise ValueError(f"Unsupported runtime config for supervised service {svc.mas_id!r}")

    def start_installed_service(
        self,
        app_id: str,
        mas_id: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> ManagedProcessState:
        app = self.registry.get(validate_identifier(app_id, "appId"))
        if app is None:
            raise KeyError(f"Application {app_id!r} not found")

        manifest, src_root = self._resolve_app_manifest_and_src_root(app)
        target_svc: Optional[MasDefinition] = None
        for svc in manifest.services:
            if mas_id is None or svc.mas_id == mas_id:
                target_svc = svc
                break
        if target_svc is None:
            raise KeyError(f"Service {mas_id or '<default>'} not found in app {app_id!r}")

        svc_state = next((s for s in app.services if s.mas_id == target_svc.mas_id), None)
        vol_enabled = bool(svc_state.volunteer_enabled) if svc_state else False

        python_exe = Path(app.python_path).resolve() if app.python_path else Path(sys.executable).resolve()
        cmd, cwd = self._build_mas_command(target_svc, src_root, python_exe)

        provenance = self._build_provenance(
            app_id=app.app_id,
            mas_id=target_svc.mas_id,
            version=manifest.version or app.version,
            commit_sha=self._resolve_commit_sha(src_root),
            mvenv_digest=self._resolve_mvenv_digest(app.mvenv_name, app.mvenv_path),
        )

        return self.start_process(
            mas_id=target_svc.mas_id,
            app_id=app.app_id,
            mode=target_svc.mode,
            command=cmd,
            cwd=cwd,
            volunteer_enabled=vol_enabled,
            extra_env=extra_env,
            provenance=provenance,
            observability=manifest.observability,
        )

    def start_process(
        self,
        mas_id: str,
        app_id: str,
        mode: str,
        command: List[str],
        cwd: Optional[Path] = None,
        volunteer_enabled: bool = False,
        extra_env: Optional[Dict[str, str]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        observability: Optional[ObservabilityConfig] = None,
    ) -> ManagedProcessState:
        safe_mas = validate_identifier(mas_id, "masId")
        safe_app = validate_identifier(app_id, "appId")
        if mode in ("CellSingleton", "Distributed") and not volunteer_enabled:
            raise PermissionError(
                f"Cannot start cell service {safe_mas!r} ({mode}): volunteerEnabled is False (opt-in required)"
            )

        t_start = time.monotonic()
        self.ensure_bridge_started()

        fencing_token: Optional[int] = None
        if mode != "LocalOnly" and volunteer_enabled:
            res = self.ipc_server.state.lease_manager.try_acquire_lease(
                mas_id=safe_mas,
                mode=mode,
                volunteer_enabled=True,
                activity_summary=f"Starting {safe_mas} ({mode})",
            )
            if mode == "CellSingleton" and not res["acquired"]:
                raise RuntimeError(
                    f"CellSingleton lease for {safe_mas!r} is held by another node: {res.get('reason')}"
                )
            if res.get("lease"):
                fencing_token = int(res["lease"]["fencingToken"])
            self.ipc_server.state.presence_manager.heartbeat(
                self.ipc_server.state.node_id, registered_services=[safe_mas]
            )

        resolved_cwd = Path(cwd).resolve() if cwd else None
        if provenance is None:
            reg_app = self.registry.get(safe_app)
            provenance = self._build_provenance(
                app_id=safe_app,
                mas_id=safe_mas,
                version=reg_app.version if reg_app else "0.0.0",
                commit_sha=self._resolve_commit_sha(resolved_cwd),
                mvenv_digest=(
                    self._resolve_mvenv_digest(reg_app.mvenv_name, reg_app.mvenv_path)
                    if reg_app
                    else "sha256:unknown"
                ),
            )

        ring_lines = (
            max(10, min(10000, int(observability.log_ring_lines)))
            if observability is not None
            else 200
        )

        with self._lock:
            prev = self._processes.get(safe_mas)
            prior_restarts = (
                (prev.restart_count + (1 if prev.state == "Crashed" else 0))
                if prev is not None
                else 0
            )

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env["AURAGRID_LOCAL_URL"] = self.ipc_server.base_url
        env["AURAGRID_IPC_PORT"] = str(self.ipc_server.port)
        env["AURAGRID_IPC_TOKEN"] = self.ipc_server.ipc_token
        env["AURAGRID_MAS_ID"] = safe_mas
        env["AURAGRID_NODE_ID"] = self.ipc_server.state.node_id
        env["AURAGRID_MAS_MODE"] = mode
        if fencing_token is not None:
            env["AURAGRID_FENCING_TOKEN"] = str(fencing_token)
        else:
            env.pop("AURAGRID_FENCING_TOKEN", None)
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})

        kwargs: Dict[str, Any] = {
            "cwd": str(resolved_cwd) if resolved_cwd else None,
            "env": env,
            "shell": False,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if platform.system().lower() == "windows":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(command, **kwargs)
        spawned_monotonic = time.monotonic()
        state = ManagedProcessState(
            mas_id=safe_mas,
            app_id=safe_app,
            mode=mode,
            process=proc,
            fencing_token=fencing_token,
            pid=proc.pid,
            state="Running",
            restart_count=prior_restarts,
            command=list(command),
            stdout_ring=collections.deque(maxlen=ring_lines),
            stderr_ring=collections.deque(maxlen=ring_lines),
            log_ring_lines=ring_lines,
            provenance=dict(provenance),
            observability=observability,
            started_monotonic=spawned_monotonic,
        )
        with self._lock:
            self._processes[safe_mas] = state

        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None
        if proc.stdout is not None:
            stdout_thread = threading.Thread(
                target=self._stream_reader,
                args=(state, proc.stdout, state.stdout_ring, "stdout"),
                name=f"sprout-stdout-{safe_mas}",
                daemon=True,
            )
            stdout_thread.start()
        if proc.stderr is not None:
            stderr_thread = threading.Thread(
                target=self._stream_reader,
                args=(state, proc.stderr, state.stderr_ring, "stderr"),
                name=f"sprout-stderr-{safe_mas}",
                daemon=True,
            )
            stderr_thread.start()

        watcher = threading.Thread(
            target=self._exit_watcher,
            args=(state, stdout_thread, stderr_thread),
            name=f"sprout-exit-{safe_mas}",
            daemon=True,
        )
        watcher.start()

        if (
            observability is not None
            and observability.slo is not None
            and observability.slo.max_startup_ms is not None
        ):
            startup_ms = (spawned_monotonic - t_start) * 1000.0
            self.record_perf_metric(
                mas_id=safe_mas,
                metric="latency_ms",
                observed_value=startup_ms,
                slo_threshold=observability.slo.max_startup_ms,
                operation="startup",
            )

        return state

    def record_perf_metric(
        self,
        mas_id: str,
        metric: str,
        observed_value: float,
        slo_threshold: float,
        operation: str,
    ) -> Optional[Dict[str, Any]]:
        """Record a runtime performance observation and emit `ops.perf.anomaly` if breaching `slo_threshold`."""
        safe_mas = validate_identifier(mas_id, "masId")
        norm_metric = _METRIC_ALIASES.get(metric, metric)
        if norm_metric not in _VALID_PERF_METRICS:
            raise ValueError(
                f"Unsupported perf metric {metric!r}; expected one of {_VALID_PERF_METRICS}"
            )

        obs = float(observed_value)
        thresh = float(slo_threshold)
        if obs <= thresh:
            return None

        prov: Optional[Dict[str, Any]] = None
        with self._lock:
            st = self._processes.get(safe_mas)
            if st is not None and st.provenance:
                prov = dict(st.provenance)

        if prov is None:
            matched_app: Optional[InstalledAppRecord] = None
            for cand_app in self.registry.list_all():
                if any(s.mas_id == safe_mas for s in cand_app.services):
                    matched_app = cand_app
                    break
            if matched_app is not None:
                prov = self._build_provenance(
                    app_id=matched_app.app_id,
                    mas_id=safe_mas,
                    version=matched_app.version,
                    commit_sha=self._resolve_commit_sha(
                        Path(matched_app.source_path) if matched_app.source_path else None
                    ),
                    mvenv_digest=self._resolve_mvenv_digest(
                        matched_app.mvenv_name, matched_app.mvenv_path
                    ),
                )
            else:
                prov = self._build_provenance(app_id="unknown", mas_id=safe_mas)

        op_clean = str(operation).strip() if operation is not None and str(operation).strip() else None
        fp = _compute_fingerprint(
            prov["appId"],
            safe_mas,
            "ops.perf.anomaly",
            norm_metric,
            op_clean or "default",
        )
        anomaly_payload: Dict[str, Any] = {
            "topic": "ops.perf.anomaly",
            "timestamp": _utc_now_iso(),
            "provenance": prov,
            "metric": norm_metric,
            "observedValue": max(0.0, obs),
            "sloThreshold": max(0.0, thresh),
            "operation": op_clean,
            "fingerprint": fp,
        }
        self._publish_ops_event("ops.perf.anomaly", anomaly_payload)
        return anomaly_payload

    def stop_service(self, mas_id: str, timeout: float = 5.0) -> bool:
        safe_mas = validate_identifier(mas_id, "masId")
        with self._lock:
            st = self._processes.get(safe_mas)
        if st is None:
            return False

        st._stopping = True
        proc = st.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            deadline = time.monotonic() + timeout
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

        st._exit_done.wait(timeout=2.0)

        if st.mode != "LocalOnly" and st.fencing_token is not None:
            self.ipc_server.state.lease_manager.release_lease(safe_mas, st.fencing_token)
            self.ipc_server.state.registry_manager.unregister_node_service(
                safe_mas, self.ipc_server.state.node_id
            )

        st.state = "Stopped"
        return True

    def launch_app_entrypoint(
        self,
        app_id: str,
        entrypoint_name: Optional[str] = None,
    ) -> Optional[int]:
        app = self.registry.get(validate_identifier(app_id, "appId"))
        if app is None:
            raise KeyError(f"Application {app_id!r} not found")

        install_root = Path(app.install_path).resolve()
        manifest, src_root = self._resolve_app_manifest_and_src_root(app)

        python_exe = Path(app.python_path).resolve() if app.python_path else Path(sys.executable).resolve()
        app_data_dir = install_root / "app_data"
        launch_cwd = app_data_dir if app_data_dir.exists() else src_root

        if manifest.gui_entry_points:
            if entrypoint_name:
                matches = [ep for ep in manifest.gui_entry_points if ep.name == entrypoint_name]
                if not matches:
                    raise KeyError(f"GUI entry point {entrypoint_name!r} not found for {app.app_id!r}")
                chosen = matches[0]
            else:
                chosen = manifest.gui_entry_points[0]
            if chosen.context == "Python":
                cmd, env_updates = resolve_entry_point_invocation(
                    python_exe=python_exe,
                    entry_command=chosen.entry_command,
                    port=app.port,
                    mvenv_root=Path(app.mvenv_path) if app.mvenv_path else None,
                )
            else:
                rendered = chosen.entry_command.replace("{port}", str(app.port))
                cmd = shlex.split(rendered, posix=(platform.system().lower() != "windows"))
                env_updates = None
            mas_gui_id = f"{app.app_id}-gui-{chosen.name}"
            provenance = self._build_provenance(
                app_id=app.app_id,
                mas_id=mas_gui_id,
                version=manifest.version or app.version,
                commit_sha=self._resolve_commit_sha(src_root),
                mvenv_digest=self._resolve_mvenv_digest(app.mvenv_name, app.mvenv_path),
            )
            st = self.start_process(
                mas_id=mas_gui_id,
                app_id=app.app_id,
                mode="LocalOnly",
                command=cmd,
                cwd=launch_cwd,
                extra_env=env_updates,
                volunteer_enabled=False,
                provenance=provenance,
                observability=manifest.observability,
            )
            return st.pid

        for svc in manifest.services:
            if svc.mode == "LocalOnly":
                st = self.start_installed_service(app.app_id, mas_id=svc.mas_id)
                return st.pid

        return None

    def list_statuses(self) -> List[Dict[str, Any]]:
        with self._lock:
            items: List[Dict[str, Any]] = []
            for st in self._processes.values():
                running = st.process is not None and st.process.poll() is None
                state_str = st.state
                if not running and state_str == "Running":
                    rc = st.process.returncode if st.process else 0
                    if st._stopping or rc in (0, None):
                        state_str = "Stopped"
                    else:
                        state_str = "Crashed"
                items.append(
                    {
                        "masId": st.mas_id,
                        "appId": st.app_id,
                        "mode": st.mode,
                        "pid": st.pid,
                        "fencingToken": st.fencing_token,
                        "state": state_str,
                    }
                )
            return items
