"""Catalog, Packaging, Install, Rollback, and Uninstall CLI subcommands for AuraSprout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.packager import SproutPackager
from aurasprout.catalog.sources import CatalogSourcesRegistry
from aurasprout.contracts.paths import SproutPaths
from aurasprout.installer.engine import SproutInstallerEngine


def register_catalog_and_install_commands(subparsers: argparse._SubParsersAction) -> None:
    # package
    p_pkg = subparsers.add_parser("package", help="Package an app directory into <appId>-<version>.apkg")
    p_pkg.add_argument("app_dir", help="Path to application root containing app.manifest.json")
    p_pkg.add_argument("--out", default="./dist", help="Output directory for .apkg")
    p_pkg.add_argument("--wheelhouse", default=None, help="Optional offline wheelhouse directory")
    p_pkg.add_argument("--format", choices=["zip", "tar.gz"], default="zip", help="Archive format")
    p_pkg.add_argument("--publish", default=None, help="Optional catalog directory to publish into")
    p_pkg.add_argument("--channel", default="stable", help="Release channel when publishing")
    p_pkg.add_argument("--base-url", default=None, help="Optional catalog baseUrl for static HTML portal")
    p_pkg.add_argument("--json", action="store_true", help="Emit JSON output")

    # install
    p_inst = subparsers.add_parser("install", help="Install an app from directory, .apkg, or catalog")
    p_inst.add_argument("target", help="App directory, .apkg path, or appId (when using --from-catalog)")
    p_inst.add_argument("--from-catalog", default=None, help="Catalog directory path or URL")
    p_inst.add_argument("--version", default=None, help="Specific catalog version")
    p_inst.add_argument("--channel", default="stable", help="Catalog release channel")
    p_inst.add_argument("--dest", default=None, help="Custom installation directory")
    p_inst.add_argument("--mvenv", default=None, help="Explicit AuraSeed managed venv name")
    p_inst.add_argument("--editable", action="store_true", help="Editable install from local source")
    p_inst.add_argument("--shortcut", action="store_true", help="Create user desktop shortcut")
    p_inst.add_argument("--port", type=int, default=None, help="Override default service port")
    p_inst.add_argument("--json", action="store_true", help="Emit JSON output")

    # uninstall
    p_uninst = subparsers.add_parser("uninstall", help="Uninstall a locally installed application")
    p_uninst.add_argument("app_id", help="Installed application ID")
    p_uninst.add_argument("--remove-mvenv", action="store_true", help="Also delete the app's mvenv")
    p_uninst.add_argument("--json", action="store_true", help="Emit JSON output")

    # rollback
    p_rb = subparsers.add_parser("rollback", help="Roll back an installed application to a previous version")
    p_rb.add_argument("app_id", help="Installed application ID")
    p_rb.add_argument("--to-version", default=None, help="Specific previous version tag")
    p_rb.add_argument("--json", action="store_true", help="Emit JSON output")

    # catalog
    p_cat = subparsers.add_parser("catalog", help="Manage local, remote, and static HTML catalogs")
    cat_sub = p_cat.add_subparsers(dest="catalog_cmd", required=True)

    p_clist = cat_sub.add_parser("list", help="List catalog apps or locally installed apps")
    p_clist.add_argument("--remote", default=None, help="Query remote catalog path or URL")
    p_clist.add_argument("--installed", action="store_true", help="List locally installed applications")
    p_clist.add_argument("--q", default=None, help="Search filter")
    p_clist.add_argument("--channel", default=None, help="Channel filter")
    p_clist.add_argument("--json", action="store_true", help="Emit JSON output")

    p_cpub = cat_sub.add_parser("publish", help="Publish an .apkg or app source directory into the catalog or push to HTTP server")
    p_cpub.add_argument("source", help="Path to .apkg file or application directory")
    p_cpub.add_argument("--catalog-dir", default=None, help="Target catalog root directory")
    p_cpub.add_argument("--server", default=None, help="Optional remote AuraSprout HTTP UI server URL to push to")
    p_cpub.add_argument("--token", default=None, help="Bootstrap token for AuraSprout HTTP UI server")
    p_cpub.add_argument("--token-file", default=None, help="File containing AuraSprout HTTP UI bootstrap token")
    p_cpub.add_argument("--channel", default="stable", help="Release channel")
    p_cpub.add_argument("--base-url", default=None, help="Optional baseUrl")
    p_cpub.add_argument("--json", action="store_true", help="Emit JSON output")

    p_cpush = cat_sub.add_parser("push", help="Push an .apkg or app source directory to a running AuraSprout HTTP server")
    p_cpush.add_argument("source", help="Path to .apkg file or application directory")
    p_cpush.add_argument("--server", required=True, help="AuraSprout HTTP server URL (e.g. http://<host>:<port>)")
    p_cpush.add_argument("--token", default=None, help="Bootstrap token (or include #<token> in --server)")
    p_cpush.add_argument("--token-file", default=None, help="Path to file containing bootstrap token")
    p_cpush.add_argument("--channel", default="stable", help="Release channel")
    p_cpush.add_argument("--json", action="store_true", help="Emit JSON output")

    p_cbuild = cat_sub.add_parser("build", help="Rebuild static zero-CDN index.html portal for a catalog")
    p_cbuild.add_argument("catalog_dir", help="Catalog root directory")
    p_cbuild.add_argument("--title", default=None, help="Catalog HTML portal title")
    p_cbuild.add_argument("--base-url", default=None, help="Base URL embedded in links")

    p_csrc_add = cat_sub.add_parser("add-source", help="Attach a remote or shared-FS catalog source")
    p_csrc_add.add_argument("url", help="Catalog URL or shared path")
    p_csrc_add.add_argument("--label", default=None, help="Friendly label")
    p_csrc_add.add_argument("--default", action="store_true", help="Mark as default source")

    p_csrc_list = cat_sub.add_parser("sources", help="List attached catalog sources")
    p_csrc_list.add_argument("--json", action="store_true", help="Emit JSON output")

    p_csrc_rm = cat_sub.add_parser("remove-source", help="Detach a catalog source by URL or path")
    p_csrc_rm.add_argument("url", help="Catalog URL or shared path")
    p_csrc_rm.add_argument("--json", action="store_true", help="Emit JSON output")

    p_cdel = cat_sub.add_parser("delete", help="Delete an application from the local catalog")
    p_cdel.add_argument("app_id", help="Application ID to remove")
    p_cdel.add_argument("--catalog-dir", default=None, help="Target catalog root directory")
    p_cdel.add_argument("--json", action="store_true", help="Emit JSON output")


def handle_catalog_and_install_command(args: argparse.Namespace, paths: SproutPaths) -> Optional[int]:
    cmd = args.command

    if cmd == "package":
        packager = SproutPackager(Path(args.app_dir))
        wheelhouse = Path(args.wheelhouse) if args.wheelhouse else None
        apkg_path, sha256_hex, size_bytes = packager.package(
            Path(args.out), wheelhouse_dir=wheelhouse, archive_format=args.format
        )
        payload = {
            "appId": packager.manifest.app_id,
            "version": packager.manifest.version,
            "packagePath": str(apkg_path),
            "sha256": sha256_hex,
            "sizeBytes": size_bytes,
        }
        if args.publish:
            cat = SproutCatalogManager(
                catalog_dir=Path(args.publish),
                base_url=args.base_url,
                paths=paths,
            )
            ver = cat.publish_package(apkg_path, channel=args.channel)
            payload["publishedChannel"] = ver.channel
            payload["catalogIndex"] = str(cat.catalog_dir / "index.html")
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Packaged {payload['appId']}@{payload['version']} -> {apkg_path} ({sha256_hex})")
        return 0

    if cmd == "install":
        engine = SproutInstallerEngine(paths=paths)
        dest = Path(args.dest) if args.dest else None
        if args.from_catalog:
            rec = engine.install_from_catalog(
                source=args.from_catalog,
                app_id=args.target,
                version=args.version,
                channel=args.channel,
                dest_dir=dest,
                mvenv_name=args.mvenv,
                create_shortcut=args.shortcut,
                port=args.port,
            )
        else:
            target_path = Path(args.target)
            if target_path.is_dir():
                rec = engine.install_from_directory(
                    source_dir=target_path,
                    dest_dir=dest,
                    mvenv_name=args.mvenv,
                    editable=args.editable,
                    create_shortcut=args.shortcut,
                    port=args.port,
                    channel=args.channel,
                )
            else:
                rec = engine.install_from_package(
                    package_path=target_path,
                    dest_dir=dest,
                    mvenv_name=args.mvenv,
                    create_shortcut=args.shortcut,
                    port=args.port,
                    channel=args.channel,
                )
        if args.json:
            print(json.dumps(rec.to_dict(), indent=2))
        else:
            print(f"Installed {rec.app_id}@{rec.version} -> {rec.install_path}")
        return 0

    if cmd == "uninstall":
        engine = SproutInstallerEngine(paths=paths)
        removed = engine.uninstall_app(args.app_id, remove_mvenv=args.remove_mvenv)
        if args.json:
            print(json.dumps({"appId": args.app_id, "uninstalled": removed}, indent=2))
        else:
            print(f"Uninstalled {args.app_id}: {removed}")
        return 0 if removed else 1

    if cmd == "rollback":
        engine = SproutInstallerEngine(paths=paths)
        rec = engine.rollback_app(args.app_id, target_version=args.to_version)
        if args.json:
            print(json.dumps(rec.to_dict(), indent=2))
        else:
            print(f"Rolled back {rec.app_id} to version {rec.version}")
        return 0

    if cmd == "catalog":
        if args.catalog_cmd == "list":
            engine = SproutInstallerEngine(paths=paths)
            if args.installed:
                items = [r.to_summary_dict() for r in engine.registry.list_all()]
                if args.json:
                    print(json.dumps({"apps": items}, indent=2))
                else:
                    for it in items:
                        print(f"{it['appId']}@{it['version']} ({it['installPath']})")
                return 0

            cat = SproutCatalogManager(paths=paths)
            if args.remote:
                manifest = cat.fetch_remote_manifest(args.remote)
                apps = list(manifest.apps.values())
            else:
                apps = cat.list_apps(query=args.q, channel=args.channel)
            out = [a.to_dict() for a in apps]
            if args.json:
                print(json.dumps({"apps": out}, indent=2))
            else:
                for a in out:
                    print(f"{a['appId']} latest={a['latestVersion']} channel={a['channel']}")
            return 0

        if args.catalog_cmd == "publish":
            if getattr(args, "server", None):
                cat = SproutCatalogManager(paths=paths)
                resp = cat.push_package_http(
                    server_url=args.server,
                    package_source=Path(args.source),
                    bootstrap_token=args.token,
                    channel=args.channel,
                    token_file=Path(args.token_file) if getattr(args, "token_file", None) else None,
                )
                if args.json:
                    print(json.dumps(resp, indent=2))
                else:
                    print(f"Pushed {resp.get('appId')}@{resp.get('latestVersion')} -> {args.server}")
                return 0
            cat_dir = Path(args.catalog_dir) if args.catalog_dir else None
            cat = SproutCatalogManager(catalog_dir=cat_dir, base_url=args.base_url, paths=paths)
            ver = cat.publish_package(Path(args.source), channel=args.channel)
            if args.json:
                print(json.dumps(ver.to_dict(), indent=2))
            else:
                print(f"Published version {ver.version} ({ver.bundle_sha256})")
            return 0

        if args.catalog_cmd == "push":
            cat = SproutCatalogManager(paths=paths)
            resp = cat.push_package_http(
                server_url=args.server,
                package_source=Path(args.source),
                bootstrap_token=args.token,
                channel=args.channel,
                token_file=Path(args.token_file) if args.token_file else None,
            )
            if args.json:
                print(json.dumps(resp, indent=2))
            else:
                print(
                    f"Pushed {resp.get('appId')}@{resp.get('latestVersion')} ({resp.get('bundleSha256')}) -> {resp.get('catalogUrl', args.server)}"
                )
            return 0

        if args.catalog_cmd == "build":
            cat = SproutCatalogManager(catalog_dir=Path(args.catalog_dir), base_url=args.base_url, paths=paths)
            idx = cat.build_static_site(title=args.title)
            print(str(idx))
            return 0

        if args.catalog_cmd == "add-source":
            reg = CatalogSourcesRegistry(paths=paths)
            saved = reg.attach_source(args.url, label=args.label, set_default=args.default)
            print(json.dumps(saved, indent=2))
            return 0

        if args.catalog_cmd == "sources":
            reg = CatalogSourcesRegistry(paths=paths)
            sources = reg.list_sources()
            if getattr(args, "json", False):
                print(json.dumps({"sources": sources}, indent=2))
            else:
                for src in sources:
                    marker = " (default)" if src.get("isDefault") else ""
                    print(f"{src.get('url')}{marker}")
            return 0

        if args.catalog_cmd == "remove-source":
            reg = CatalogSourcesRegistry(paths=paths)
            removed = reg.remove_source(args.url)
            if getattr(args, "json", False):
                print(json.dumps({"url": args.url, "removed": removed}, indent=2))
            else:
                print(f"Removed {args.url}: {removed}")
            return 0 if removed else 1

        if args.catalog_cmd == "delete":
            cat_dir = Path(args.catalog_dir) if args.catalog_dir else None
            cat = SproutCatalogManager(catalog_dir=cat_dir, paths=paths)
            deleted = cat.delete_app(args.app_id)
            if getattr(args, "json", False):
                print(json.dumps({"appId": args.app_id, "deleted": deleted}, indent=2))
            else:
                print(f"Deleted {args.app_id} from catalog: {deleted}")
            return 0 if deleted else 1

    return None
