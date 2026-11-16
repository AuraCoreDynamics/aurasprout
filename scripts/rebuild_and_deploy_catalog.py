"""Rebuild and deploy AuraSprout catalog with all applications and website/git URLs."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from jsonschema import validate

from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.remote_sync import SftpCatalogDeployer, parse_credentials_file
from aurasprout.installer.bundle import build_native_installer_zip


def main():
    root_dir = Path("c:/projects/auracore/aurasprout")
    dist_dir = root_dir / "dist" / "test_catalog"
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)

    catalog_url = "https://auracoredynamics.com/aurasprout/catalog"
    print(f"[*] Initializing catalog at {dist_dir} with base URL: {catalog_url}")
    manager = SproutCatalogManager(
        catalog_dir=dist_dir,
        base_url=catalog_url,
    )
    manager.manifest.catalog_name = "AuraCore Application Catalog"

    apps_to_publish = [
        ("aurasprout", Path("c:/projects/auracore/aurasprout")),
        ("auraseed", Path("c:/projects/auracore/auraseed")),
        ("aurarouter", Path("c:/projects/auracore/aurarouter")),
        ("benchwarmer", Path("c:/projects/auracore/benchwarmer")),
        ("bijou", Path("c:/projects/bijou")),
        ("old-rag", Path("c:/projects/old_rag")),
    ]

    for app_id, app_dir in apps_to_publish:
        print(f"[*] Publishing {app_id} from {app_dir}...")
        if not (app_dir / "app.manifest.json").exists():
            raise FileNotFoundError(f"Missing app.manifest.json in {app_dir}")
        v_entry = manager.publish_source_directory(app_dir, channel="stable")
        print(f"    -> {app_id} v{v_entry.version} published (SHA-256: {v_entry.bundle_sha256[:12]}...)")

    print("[*] Building native installer zip bundle...")
    installers_dir = dist_dir / "installers"
    zip_path = build_native_installer_zip(
        out_dir=installers_dir,
        aurasprout_dir=Path("c:/projects/auracore/aurasprout"),
        auraseed_dir=Path("c:/projects/auracore/auraseed"),
        version="0.1.1",
    )
    print(f"    -> Built installer: {zip_path.name} ({zip_path.stat().st_size} bytes)")

    print("[*] Generating catalog HTML5 portal...")
    html_path = manager.build_static_site(title="AuraCore Application Catalog")
    print(f"    -> Generated {html_path.name}")

    print("[*] Validating catalog.json against JSON schema...")
    schema_path = Path("c:/projects/auracore/auragrid-api/schemas/catalog.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    catalog_data = json.loads((dist_dir / "catalog.json").read_text(encoding="utf-8"))
    validate(instance=catalog_data, schema=schema)
    print("    -> catalog.json schema validation PASSED!")

    # Verify URLs in catalog
    print("[*] Verifying URLs in catalog...")
    for app_id, app_info in catalog_data.get("apps", {}).items():
        w_url = app_info.get("websiteUrl")
        r_url = app_info.get("repositoryUrl")
        print(f"    - {app_id}: website={w_url} | git={r_url}")
        assert w_url, f"Expected websiteUrl for {app_id}"
        if app_id != "old-rag":
            assert r_url, f"Expected repositoryUrl for {app_id}"

    # Verify links rendered in index.html
    html_content = html_path.read_text(encoding="utf-8")
    for app_id, app_info in catalog_data.get("apps", {}).items():
        if app_info.get("websiteUrl"):
            assert app_info["websiteUrl"] in html_content, f"websiteUrl for {app_id} not found in index.html"
        if app_info.get("repositoryUrl"):
            assert app_info["repositoryUrl"] in html_content, f"repositoryUrl for {app_id} not found in index.html"
    print("    -> All website and git links confirmed present in index.html!")

    # Deploy to DreamHost
    creds_file = Path("C:/Users/smsie/private/dreamhost.txt")
    if not creds_file.exists():
        print(f"[!] Credentials file not found at {creds_file}; skipping SFTP deployment.")
        return 0

    print(f"[*] Deploying to DreamHost via SFTP using credentials from {creds_file}...")
    deployer = SftpCatalogDeployer.from_credentials_file(
        creds_file=creds_file,
        default_host="auracoredynamics.com",
    )
    res = deployer.sync_catalog(
        local_catalog_dir=dist_dir,
        remote_catalog_dir="auracoredynamics.com/aurasprout/catalog",
        catalog_base_url=catalog_url,
    )
    print(f"    -> SFTP Upload Complete: {len(res.uploaded_files)} files uploaded, {res.total_bytes_transferred} bytes transferred to {res.host}:{res.remote_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
