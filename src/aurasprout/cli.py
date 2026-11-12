"""Main AuraSprout CLI entrypoint (`python -m aurasprout` / `aurasprout`)."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from typing import List, Optional

from aurasprout.cli_catalog import (
    handle_catalog_and_install_command,
    register_catalog_and_install_commands,
)
from aurasprout.cli_volunteer import handle_volunteer_command, register_volunteer_commands
from aurasprout.contracts.paths import SproutPaths
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
    p_ui.add_argument("--json", action="store_true", help="Print server URL JSON and wait")

    subparsers.add_parser("desktop", help="Launch AuraSprout Tkinter Desktop Hub (composes AuraSeed)")

    p_ipc = subparsers.add_parser("ipc", help="Start standalone Tier 2 loopback IPC bridge server")
    p_ipc.add_argument("--host", default="127.0.0.1", help="Loopback bind host")
    p_ipc.add_argument("--port", type=int, default=0, help="Loopback port")
    p_ipc.add_argument("--token", default=None, help="Explicit IPC secret token")

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
        state = ManagementServerState(paths=paths)
        with SproutManagementServer(host=args.host, port=args.port, state=state) as srv:
            if args.json:
                print(json.dumps({"url": srv.auth_url, "port": srv.port}), flush=True)
            else:
                print(f"AuraSprout Web UI listening on {srv.auth_url}", flush=True)
            return _wait_forever()

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

