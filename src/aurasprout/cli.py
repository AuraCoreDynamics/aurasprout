"""Main AuraSprout CLI entrypoint (`python -m aurasprout` / `aurasprout`)."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

from aurasprout.cli_catalog import (
    handle_catalog_and_install_command,
    register_catalog_and_install_commands,
)
from aurasprout.cli_volunteer import handle_volunteer_command, register_volunteer_commands
from aurasprout.contracts.paths import SproutPaths
from aurasprout.installer.engine import SproutInstallerEngine
from aurasprout.installer.self_install import install_standalone, uninstall_self
from aurasprout.ipc.server import IpcBridgeServer
from aurasprout.ui.desktop import launch_desktop_hub
from aurasprout.ui.server import ManagementServerState, SproutManagementServer


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aurasprout",
        description="AuraSprout user-space app catalog, installer, cell coordinator, and Web UI",
    )
    parser.add_argument(
        "--cell-root",
        default=None,
        help="Shared or local cell root containing .sprout/ (defaults to AURASPROUT_CELL_ROOT or user data dir)",
    )
    parser.add_argument(
        "--user-root",
        default=None,
        help="User-writable AuraSprout data root (defaults to AURASPROUT_HOME or user AppData)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_catalog_and_install_commands(subparsers)
    register_volunteer_commands(subparsers)

    p_ui = subparsers.add_parser("ui", help="Start Tier 3 loopback Management Web UI server")
    p_ui.add_argument("--host", default="127.0.0.1", help="Loopback bind host (default 127.0.0.1)")
    p_ui.add_argument("--port", type=int, default=0, help="Loopback port (0 = ephemeral)")
    p_ui.add_argument("--token", default=None, help="Optional explicit UI bootstrap token")
    p_ui.add_argument("--token-file", default=None, help="Write bootstrap token & URL metadata to JSON file on startup")
    p_ui.add_argument("--json", action="store_true", help="Print server URL JSON and wait")

    subparsers.add_parser("desktop", help="Launch AuraSprout Tkinter Desktop Hub (composes AuraSeed)")

    p_ipc = subparsers.add_parser("ipc", help="Start standalone Tier 2 loopback IPC bridge server")
    p_ipc.add_argument("--host", default="127.0.0.1", help="Loopback bind host")
    p_ipc.add_argument("--port", type=int, default=0, help="Loopback port")
    p_ipc.add_argument("--token", default=None, help="Explicit IPC secret token")

    p_si = subparsers.add_parser("self-install", help="Cleanroom standalone user-space install of AuraSprout")
    p_si.add_argument("--dest", default=None, help="Target standalone venv directory")
    p_si.add_argument("--base-python", default=None, help="Base Python executable to create standalone venv from")
    p_si.add_argument("--source", default=None, help="Local AuraSprout source directory")
    p_si.add_argument("--dep", action="append", default=None, help="Local dependency repo path (repeatable)")
    p_si.add_argument("--wheelhouse", default=None, help="Optional offline wheelhouse directory")
    p_si.add_argument("--editable", action="store_true", help="Install local sources in editable mode")
    p_si.add_argument("--no-register", action="store_true", help="Skip OS registry and HKCU env var registration")
    p_si.add_argument("--add-to-path", action="store_true", help="Add standalone bin directory to HKCU PATH")
    p_si.add_argument("--shortcut", action="store_true", help="Create user desktop shortcut")
    p_si.add_argument("--json", action="store_true", help="Emit JSON output")

    p_su = subparsers.add_parser("self-uninstall", help="Full cleanup/uninstall of AuraSprout user & OS state")
    p_su.add_argument("--yes", "-y", action="store_true", help="Confirm cleanup without prompt")
    p_su.add_argument("--dest", default=None, help="Explicit standalone directory to remove")
    p_su.add_argument("--keep-apps", action="store_true", help="Do not uninstall installed apps first")
    p_su.add_argument("--keep-mvenvs", action="store_true", help="Preserve app mvenvs")
    p_su.add_argument("--no-registry", action="store_true", help="Skip HKCU registry and env var cleanup")
    p_su.add_argument("--remove-package", action="store_true", help="Also pip-uninstall aurasprout from active interpreter")
    p_su.add_argument("--json", action="store_true", help="Emit JSON output")

    p_clean = subparsers.add_parser("cleanup", help="Clean up installed apps or AuraSprout itself")
    clean_sub = p_clean.add_subparsers(dest="cleanup_cmd", required=True)
    p_c_app = clean_sub.add_parser("app", help="Uninstall an app and its mvenv")
    p_c_app.add_argument("app_id", help="Installed application ID")
    p_c_app.add_argument("--keep-mvenv", action="store_true", help="Preserve the app's mvenv")
    p_c_app.add_argument("--json", action="store_true", help="Emit JSON output")

    p_c_self = clean_sub.add_parser("self", help="Full cleanup of AuraSprout user & OS state")
    p_c_self.add_argument("--yes", "-y", action="store_true", help="Confirm cleanup without prompt")
    p_c_self.add_argument("--dest", default=None, help="Explicit standalone directory to remove")
    p_c_self.add_argument("--keep-apps", action="store_true", help="Do not uninstall installed apps first")
    p_c_self.add_argument("--keep-mvenvs", action="store_true", help="Preserve app mvenvs")
    p_c_self.add_argument("--no-registry", action="store_true", help="Skip HKCU registry and env var cleanup")
    p_c_self.add_argument("--remove-package", action="store_true", help="Also pip-uninstall aurasprout from active interpreter")
    p_c_self.add_argument("--json", action="store_true", help="Emit JSON output")

    return parser


def _wait_forever() -> int:
    stop = False

    def _on_signal(signum: int, frame: object) -> None:
        del signum, frame
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)
    while not stop:
        time.sleep(0.25)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    paths = SproutPaths(cell_root=args.cell_root, user_root=args.user_root)

    res = handle_catalog_and_install_command(args, paths)
    if res is not None:
        return res

    res = handle_volunteer_command(args, paths)
    if res is not None:
        return res

    if args.command == "desktop":
        return launch_desktop_hub(paths=paths)

    if args.command == "ui":
        state = ManagementServerState(paths=paths, bootstrap_token=args.token)
        with SproutManagementServer(host=args.host, port=args.port, state=state) as srv:
            info = {
                "url": srv.auth_url,
                "baseUrl": srv.base_url,
                "catalogUrl": f"{srv.base_url}/catalog",
                "port": srv.port,
                "token": srv.bootstrap_token,
            }
            if args.token_file:
                tf = Path(args.token_file).resolve()
                tf.parent.mkdir(parents=True, exist_ok=True)
                tf.write_text(json.dumps(info, indent=2), encoding="utf-8")
            if args.json:
                print(json.dumps({"url": srv.auth_url, "baseUrl": srv.base_url, "catalogUrl": f"{srv.base_url}/catalog", "port": srv.port}), flush=True)
            else:
                print(f"AuraSprout Web UI listening on {srv.auth_url}", flush=True)
            return _wait_forever()

    if args.command == "self-install":
        report = install_standalone(
            dest_dir=Path(args.dest) if args.dest else None,
            user_root=Path(args.user_root) if args.user_root else None,
            cell_root=Path(args.cell_root) if args.cell_root else None,
            base_python=args.base_python,
            source_dir=Path(args.source) if args.source else None,
            dep_sources=[Path(d) for d in args.dep] if args.dep else None,
            wheelhouse_dir=Path(args.wheelhouse) if args.wheelhouse else None,
            editable=bool(args.editable),
            register_os=not args.no_register,
            add_to_path=bool(args.add_to_path),
            create_shortcut=bool(args.shortcut),
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Installed AuraSprout standalone -> {report['install_dir']}")
        return 0

    if args.command == "self-uninstall" or (args.command == "cleanup" and args.cleanup_cmd == "self"):
        if not args.yes and sys.stdin.isatty():
            reply = input("[AuraSprout] Remove all AuraSprout apps, user dirs, env vars, and registry keys? [y/N]: ").strip().lower()
            if reply not in ("y", "yes"):
                print("[AuraSprout] Cleanup cancelled.")
                return 1
        report = uninstall_self(
            remove_apps=not args.keep_apps,
            remove_mvenvs=not args.keep_mvenvs,
            remove_package=bool(args.remove_package),
            user_root=Path(args.user_root) if args.user_root else None,
            cell_root=Path(args.cell_root) if args.cell_root else None,
            dest_dir=Path(args.dest) if args.dest else None,
            clean_os_registry=not args.no_registry,
        )
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"AuraSprout cleanup complete: apps={len(report['removed_apps'])}, "
                f"paths={len(report['removed_paths'])}, registry={len(report['cleaned_registry'])}"
            )
        return 0

    if args.command == "cleanup" and args.cleanup_cmd == "app":
        engine = SproutInstallerEngine(paths=paths)
        removed = engine.uninstall_app(args.app_id, remove_mvenv=not args.keep_mvenv)
        if args.json:
            print(json.dumps({"appId": args.app_id, "uninstalled": removed}, indent=2))
        else:
            print(f"Cleaned app {args.app_id}: {removed}")
        return 0 if removed else 1

    if args.command == "ipc":
        with IpcBridgeServer(host=args.host, port=args.port, paths=paths, ipc_token=args.token) as srv:
            print(
                json.dumps({"url": srv.base_url, "port": srv.port, "token": srv.ipc_token}),
                flush=True,
            )
            return _wait_forever()

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

