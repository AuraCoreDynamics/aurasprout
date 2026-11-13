"""MAS Lifecycle Supervisor for AuraSprout.

Spawns local & volunteered MAS processes inside isolated OS process groups,
injecting Tier 2 IPC connection variables (`AURAGRID_LOCAL_URL`,
`AURAGRID_IPC_PORT`, `AURAGRID_IPC_TOKEN`, `AURAGRID_FENCING_TOKEN`), and
strictly enforcing `LocalOnly` zero-shared-state guarantees and opt-in
volunteering defaults (`volunteerEnabled=False`).
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aurasprout.catalog.models import AppManifest, MasDefinition
from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root
from aurasprout.installer.autostart import resolve_entry_point_invocation
from aurasprout.installer.registry import InstalledAppsRegistry
from aurasprout.ipc.server import IpcBridgeServer

_LOG = logging.getLogger(__name__)


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


class SproutSupervisor:
    """Supervises local and volunteered MAS services backed by an ``IpcBridgeServer``."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        registry: Optional[InstalledAppsRegistry] = None,
        ipc_server: Optional[IpcBridgeServer] = None,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.registry = registry or InstalledAppsRegistry(paths=self.paths)
        self._owns_ipc = ipc_server is None
        self.ipc_server = ipc_server or IpcBridgeServer(
            paths=self.paths, mode_resolver=self._resolve_mas_mode
        )
        self._lock = threading.RLock()
        self._processes: Dict[str, ManagedProcessState] = {}

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
            mas_ids = list(self._processes.keys())
        for mid in mas_ids:
            self.stop_service(mid)
        if self._owns_ipc:
            self.ipc_server.stop()

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

        install_root = Path(app.install_path).resolve()
        ptr = InstalledAppsRegistry.read_active_version_pointer(install_root)
        version_dir = (
            Path(ptr["versionDir"]).resolve()
            if ptr and ptr.get("versionDir")
            else (install_root / "versions" / app.version)
        )
        manifest = AppManifest.from_file(version_dir)
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
        src_root = version_dir / "src" if (version_dir / "src").exists() else version_dir
        cmd, cwd = self._build_mas_command(target_svc, src_root, python_exe)

        return self.start_process(
            mas_id=target_svc.mas_id,
            app_id=app.app_id,
            mode=target_svc.mode,
            command=cmd,
            cwd=cwd,
            volunteer_enabled=vol_enabled,
            extra_env=extra_env,
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
    ) -> ManagedProcessState:
        safe_mas = validate_identifier(mas_id, "masId")
        if mode in ("CellSingleton", "Distributed") and not volunteer_enabled:
            raise PermissionError(
                f"Cannot start cell service {safe_mas!r} ({mode}): volunteerEnabled is False (opt-in required)"
            )

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

        env = os.environ.copy()
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
            "cwd": str(Path(cwd).resolve()) if cwd else None,
            "env": env,
            "shell": False,
        }
        if platform.system().lower() == "windows":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(command, **kwargs)
        state = ManagedProcessState(
            mas_id=safe_mas,
            app_id=app_id,
            mode=mode,
            process=proc,
            fencing_token=fencing_token,
            pid=proc.pid,
            state="Running",
            command=list(command),
        )
        with self._lock:
            self._processes[safe_mas] = state
        return state

    def stop_service(self, mas_id: str, timeout: float = 5.0) -> bool:
        safe_mas = validate_identifier(mas_id, "masId")
        with self._lock:
            st = self._processes.get(safe_mas)
        if st is None:
            return False

        proc = st.process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            deadline = time.monotonic() + timeout
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

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
        ptr = InstalledAppsRegistry.read_active_version_pointer(install_root)
        version_dir = (
            Path(ptr["versionDir"]).resolve()
            if ptr and ptr.get("versionDir")
            else (install_root / "versions" / app.version)
        )
        manifest = AppManifest.from_file(version_dir)

        python_exe = Path(app.python_path).resolve() if app.python_path else Path(sys.executable).resolve()
        src_root = version_dir / "src" if (version_dir / "src").exists() else version_dir
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
            st = self.start_process(
                mas_id=f"{app.app_id}-gui-{chosen.name}",
                app_id=app.app_id,
                mode="LocalOnly",
                command=cmd,
                cwd=launch_cwd,
                extra_env=env_updates,
                volunteer_enabled=False,
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
                if not running and st.state == "Running":
                    st.state = f"Exited({st.process.returncode})" if st.process else "Stopped"
                items.append(
                    {
                        "masId": st.mas_id,
                        "appId": st.app_id,
                        "mode": st.mode,
                        "pid": st.pid,
                        "fencingToken": st.fencing_token,
                        "state": st.state,
                    }
                )
            return items
