"""Opt-in Volunteer, Lease Status, and Conflict Resolution CLI subcommands for AuraSprout."""

from __future__ import annotations

import argparse
import json
from typing import Optional

from aurasprout.contracts.paths import SproutPaths
from aurasprout.coordination.leases import CONFLICT_RESOLUTION_ACTIONS, VolunteerLeaseManager
from aurasprout.installer.registry import InstalledAppsRegistry


def register_volunteer_commands(subparsers: argparse._SubParsersAction) -> None:
    p_vol = subparsers.add_parser("volunteer", help="Manage opt-in peer volunteering for cell services")
    vol_sub = p_vol.add_subparsers(dest="vol_cmd", required=True)

    p_vstatus = vol_sub.add_parser("status", help="Show volunteer status and active lease holders")
    p_vstatus.add_argument("mas_id", nargs="?", default=None, help="Optional MAS service ID filter")
    p_vstatus.add_argument("--json", action="store_true", help="Emit JSON output")

    p_venable = vol_sub.add_parser("enable", help="Opt in to collaborative execution for masId")
    p_venable.add_argument("mas_id", help="MAS service ID")
    p_venable.add_argument("--mode", default=None, choices=["CellSingleton", "Distributed"])
    p_venable.add_argument("--json", action="store_true", help="Emit JSON output")

    p_vdisable = vol_sub.add_parser("disable", help="Disable volunteering and release held lease for masId")
    p_vdisable.add_argument("mas_id", help="MAS service ID")
    p_vdisable.add_argument("--json", action="store_true", help="Emit JSON output")

    p_conf = subparsers.add_parser("conflicts", help="Inspect or resolve split-brain lease/batch conflicts")
    conf_sub = p_conf.add_subparsers(dest="conf_cmd", required=True)

    p_clist = conf_sub.add_parser("list", help="List pending lease/batch conflicts")
    p_clist.add_argument("--mas-id", default=None, help="Filter conflicts by MAS ID")
    p_clist.add_argument("--json", action="store_true", help="Emit JSON output")

    p_cres = conf_sub.add_parser("resolve", help="Resolve a pending conflict with an explicit policy")
    p_cres.add_argument("conflict_id", help="Conflict identifier")
    p_cres.add_argument("action", choices=list(CONFLICT_RESOLUTION_ACTIONS), help="Resolution action")
    p_cres.add_argument("--json", action="store_true", help="Emit JSON output")


def handle_volunteer_command(args: argparse.Namespace, paths: SproutPaths) -> Optional[int]:
    registry = InstalledAppsRegistry(paths=paths)
    lease_mgr = VolunteerLeaseManager(paths=paths)

    if args.command == "volunteer":
        if args.vol_cmd == "status":
            items = []
            for app in registry.list_all():
                for svc in app.services:
                    if args.mas_id and svc.mas_id != args.mas_id:
                        continue
                    lease = lease_mgr.get_lease(svc.mas_id, active_only=False)
                    items.append(
                        {
                            "masId": svc.mas_id,
                            "mode": svc.mode,
                            "volunteerEnabled": svc.volunteer_enabled,
                            "state": lease.get("state", svc.state) if lease else svc.state,
                            "holderNodeId": lease.get("holderNodeId") if lease else None,
                            "fencingToken": lease.get("fencingToken") if lease else None,
                        }
                    )
            if not items and args.mas_id:
                lease = lease_mgr.get_lease(args.mas_id, active_only=False)
                if lease:
                    items.append(lease)
            if args.json:
                print(json.dumps({"services": items}, indent=2))
            else:
                for it in items:
                    print(f"{it['masId']} mode={it['mode']} volunteer={it['volunteerEnabled']} state={it['state']}")
            return 0

        if args.vol_cmd in ("enable", "disable"):
            enabled = args.vol_cmd == "enable"
            requested_mode = getattr(args, "mode", None)
            mode = requested_mode or "CellSingleton"
            for app in registry.list_all():
                changed = False
                for svc in app.services:
                    if svc.mas_id == args.mas_id:
                        if requested_mode and svc.mode != "LocalOnly":
                            svc.mode = requested_mode
                        mode = svc.mode
                        svc.volunteer_enabled = enabled if svc.mode != "LocalOnly" else False
                        changed = True
                if changed:
                    registry.register(app)
            res = lease_mgr.set_volunteer_enabled(args.mas_id, enabled=enabled, mode=mode)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"{res['masId']}: volunteerEnabled={res['volunteerEnabled']} state={res['state']}")
            return 0

    if args.command == "conflicts":
        if args.conf_cmd == "list":
            conflicts = lease_mgr.list_conflicts(mas_id=args.mas_id)
            if args.json:
                print(json.dumps({"conflicts": conflicts}, indent=2))
            else:
                if not conflicts:
                    print("No pending conflicts.")
                for c in conflicts:
                    print(
                        f"{c['conflictId']} masId={c['masId']} remote={c['remoteHolderUser']}@{c['remoteHolderNodeId']}"
                    )
            return 0

        if args.conf_cmd == "resolve":
            resolved = lease_mgr.resolve_conflict(args.conflict_id, args.action)
            if args.json:
                print(json.dumps(resolved, indent=2))
            else:
                print(f"Resolved {args.conflict_id} -> {resolved['state']} (token={resolved['fencingToken']})")
            return 0

    return None

