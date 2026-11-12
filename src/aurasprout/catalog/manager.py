"""Unified Application Catalog Manager merging local/static HTML catalogs, channels,
delta upgrades, and HTTP/shared-FS distribution for AuraSprout.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import urllib.parse
import zipfile
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from aurasprout.catalog.crypto import compute_sha256, verify_sha256
from aurasprout.catalog.html_generator import generate_catalog_html
from aurasprout.catalog.models import (
    RELEASE_CHANNELS,
    AppManifest,
    AppVersionEntry,
    CatalogAppEntry,
    CatalogManifest,
    now_utc_iso,
)
from aurasprout.catalog.packager import SproutPackager
from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root

_LOG = logging.getLogger(__name__)


def _extract_manifest_from_archive(archive_path: Path) -> AppManifest:
    target = Path(archive_path).resolve()
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target, "r") as zf:
            names = set(zf.namelist())
            for cand in ("app.manifest.json", "app.json"):
                if cand in names:
                    data = json.loads(zf.read(cand).decode("utf-8"))
                    return AppManifest.from_dict(data)
    elif tarfile.is_tarfile(target):
        with tarfile.open(target, "r:*") as tf:
            for cand in ("app.manifest.json", "app.json"):
                try:
                    member = tf.getmember(cand)
                    fobj = tf.extractfile(member)
                    if fobj is not None:
                        data = json.loads(fobj.read().decode("utf-8"))
                        return AppManifest.from_dict(data)
                except KeyError:
                    continue
    raise ValueError(f"Package archive {target} missing app.manifest.json / app.json")


class SproutCatalogManager:
    """Manages local, static-web, shared-filesystem, and HTTP/REST application catalogs."""

    def __init__(
        self,
        catalog_dir: Optional[Path] = None,
        base_url: Optional[str] = None,
        paths: Optional[SproutPaths] = None,
        timeout: float = 30.0,
    ) -> None:
        resolved_paths = paths or SproutPaths()
        self.catalog_dir = (
            Path(catalog_dir).resolve()
            if catalog_dir is not None
            else resolved_paths.catalog_dir
        )
        self.catalog_dir.mkdir(parents=True, exist_ok=True)
        self.apps_dir = self.catalog_dir / "apps"
        self.channels_dir = self.catalog_dir / "channels"
        self.catalog_file = self.catalog_dir / "catalog.json"
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url
        self.timeout = float(timeout)
        self.manifest = self._load_or_create()

    def _load_or_create(self) -> CatalogManifest:
        if self.catalog_file.exists():
            try:
                loaded = CatalogManifest.from_file(self.catalog_file)
                if self.base_url and not loaded.base_url:
                    loaded.base_url = self.base_url
                return loaded
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                _LOG.warning("Initializing fresh catalog after unreadable %s: %s", self.catalog_file, exc)
        return CatalogManifest(base_url=self.base_url)

    def save(self, build_html: bool = True) -> None:
        self.manifest.to_file(self.catalog_file)
        if build_html:
            self.build_static_site()

    def build_static_site(
        self,
        output_dir: Optional[Path] = None,
        title: Optional[str] = None,
    ) -> Path:
        out_dir = Path(output_dir).resolve() if output_dir else self.catalog_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        if title:
            self.manifest.catalog_name = title
        html_content = generate_catalog_html(self.manifest, catalog_url=self.base_url)
        index_file = out_dir / "index.html"
        index_file.write_text(html_content, encoding="utf-8")
        return index_file

    def publish_source_directory(
        self,
        app_dir: Path,
        channel: str = "stable",
        delta_from_version: Optional[str] = None,
        changelog: str = "",
        wheelhouse_dir: Optional[Path] = None,
        is_latest: bool = True,
    ) -> AppVersionEntry:
        packager = SproutPackager(app_dir)
        tmp_out = Path(tempfile.mkdtemp(prefix="aurasprout_pub_"))
        try:
            apkg_path, _, _ = packager.package(tmp_out, wheelhouse_dir=wheelhouse_dir)
            return self.publish_package(
                package_source=apkg_path,
                channel=channel,
                delta_from_version=delta_from_version,
                changelog=changelog,
                is_latest=is_latest,
            )
        finally:
            shutil.rmtree(tmp_out, ignore_errors=True)

    def publish_package(
        self,
        package_source: Union[Path, bytes],
        channel: str = "stable",
        filename: Optional[str] = None,
        delta_from_version: Optional[str] = None,
        changelog: str = "",
        tags: Optional[List[str]] = None,
        is_latest: bool = True,
    ) -> AppVersionEntry:
        if channel not in RELEASE_CHANNELS:
            raise ValueError(f"Unsupported channel {channel!r}; expected one of {RELEASE_CHANNELS}")

        temp_archive: Optional[Path] = None
        if isinstance(package_source, bytes):
            if not package_source:
                raise ValueError("Empty package bytes payload")
            temp_archive = Path(tempfile.mkdtemp(prefix="aurasprout_upload_")) / (filename or "package.apkg")
            temp_archive.write_bytes(package_source)
            src_path = temp_archive
        else:
            src_path = Path(package_source).resolve()
            if not src_path.exists():
                raise FileNotFoundError(f"Package not found: {src_path}")
            if src_path.is_dir():
                return self.publish_source_directory(
                    src_path,
                    channel=channel,
                    delta_from_version=delta_from_version,
                    changelog=changelog,
                    is_latest=is_latest,
                )

        try:
            manifest = _extract_manifest_from_archive(src_path)
            app_id = validate_identifier(manifest.app_id, "appId")
            ver = validate_identifier(manifest.version, "version")

            dest_app_dir = validate_within_root(self.apps_dir, app_id)
            dest_app_dir.mkdir(parents=True, exist_ok=True)
            bundle_filename = f"{app_id}-{ver}.apkg"
            dest_bundle = validate_within_root(dest_app_dir, bundle_filename)
            shutil.copy2(src_path, dest_bundle)

            sha256 = compute_sha256(dest_bundle)
            size_bytes = dest_bundle.stat().st_size

            # Write channel manifest copy for CatalogChannelReconciler compatibility
            ch_dir = validate_within_root(self.channels_dir, channel)
            ch_dir.mkdir(parents=True, exist_ok=True)
            manifest.to_file(validate_within_root(ch_dir, f"{app_id}.manifest.json"))
            manifest.to_file(validate_within_root(dest_app_dir, f"{app_id}-{ver}.manifest.json"))

            manifest_sha256 = compute_sha256(validate_within_root(dest_app_dir, f"{app_id}-{ver}.manifest.json"))
            combined_tags = list(dict.fromkeys([*(tags or []), *manifest.distribution.tags]))

            rel_bundle_url = f"apps/{app_id}/{bundle_filename}"
            if self.base_url:
                rel_bundle_url = f"{self.base_url.rstrip('/')}/{rel_bundle_url}"

            version_entry = AppVersionEntry(
                version=ver,
                channel=channel,
                release_date=now_utc_iso(),
                bundle_filename=bundle_filename,
                bundle_url=rel_bundle_url,
                bundle_sha256=sha256,
                bundle_size_bytes=size_bytes,
                manifest_sha256=manifest_sha256,
                manifest_uri=f"channels/{channel}/{app_id}.manifest.json",
                entrypoint_command=manifest.distribution.entrypoint_command,
                default_port=manifest.distribution.default_port,
                wheelhouse_path=manifest.distribution.wheelhouse_path,
                delta_from_version=delta_from_version,
                subprojects=list(manifest.distribution.subprojects),
                artifacts=list(manifest.distribution.artifacts),
                changelog=changelog,
            )
            self.manifest.register_app_version(
                app_id=app_id,
                display_name=manifest.name,
                version_entry=version_entry,
                description=manifest.description,
                tags=combined_tags,
                is_latest=is_latest,
                name=manifest.name,
            )
            self.save()
            return version_entry
        finally:
            if temp_archive is not None:
                shutil.rmtree(temp_archive.parent, ignore_errors=True)

    def list_apps(
        self,
        query: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> List[CatalogAppEntry]:
        q = (query or "").strip().lower()
        results: List[CatalogAppEntry] = []
        for app in sorted(self.manifest.apps.values(), key=lambda a: a.app_id):
            if channel and not any(v.channel == channel for v in app.versions.values()):
                continue
            if q:
                haystack = " ".join([app.app_id, app.name, app.display_name, app.description, *app.tags]).lower()
                if q not in haystack:
                    continue
            results.append(app)
        return results

    def get_app(self, app_id: str) -> Optional[CatalogAppEntry]:
        return self.manifest.get_app(app_id)

    def get_bundle_path(
        self,
        app_id: str,
        version: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Optional[Path]:
        safe_id = validate_identifier(app_id, "appId")
        app = self.get_app(safe_id)
        if not app:
            return None
        ver_entry = app.get_version(version, channel=channel)
        if not ver_entry:
            return None
        candidate = validate_within_root(self.apps_dir, Path(safe_id) / ver_entry.bundle_filename)
        return candidate if candidate.exists() else None

    def delete_app(self, app_id: str) -> bool:
        safe_id = validate_identifier(app_id, "appId")
        if safe_id not in self.manifest.apps:
            return False
        del self.manifest.apps[safe_id]
        app_dir = validate_within_root(self.apps_dir, safe_id)
        if app_dir.exists():
            shutil.rmtree(app_dir, ignore_errors=True)
        for ch in RELEASE_CHANNELS:
            ch_manifest = validate_within_root(self.channels_dir, Path(ch) / f"{safe_id}.manifest.json")
            if ch_manifest.exists():
                ch_manifest.unlink()
        self.save()
        return True

    def fetch_remote_manifest(self, source: str) -> CatalogManifest:
        raw = (source or "").strip()
        if raw.startswith(("http://", "https://")):
            url = raw if raw.endswith(".json") else f"{raw.rstrip('/')}/catalog.json"
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
                resp.raise_for_status()
                manifest = CatalogManifest.from_dict(resp.json())
                if not manifest.base_url:
                    manifest.base_url = url.rsplit("/", 1)[0]
                return manifest

        local = Path(raw).resolve()
        if local.is_dir():
            local = local / "catalog.json"
        if not local.exists():
            raise FileNotFoundError(f"Catalog manifest not found at {local}")
        manifest = CatalogManifest.from_file(local)
        if not manifest.base_url:
            manifest.base_url = str(local.parent)
        return manifest

    def download_verified_package(
        self,
        source: str,
        app_id: str,
        version: Optional[str] = None,
        channel: Optional[str] = None,
        dest_dir: Optional[Path] = None,
    ) -> Tuple[Path, AppVersionEntry]:
        manifest = self.fetch_remote_manifest(source)
        app = manifest.get_app(app_id)
        if not app:
            raise KeyError(f"Application {app_id!r} not found in catalog source {source!r}")
        v_entry = app.get_version(version, channel=channel)
        if not v_entry:
            raise KeyError(f"No matching version/channel for {app_id!r} in catalog")

        target_dir = Path(dest_dir).resolve() if dest_dir else Path(tempfile.mkdtemp(prefix="aurasprout_dl_"))
        target_dir.mkdir(parents=True, exist_ok=True)
        out_file = validate_within_root(target_dir, Path(v_entry.bundle_filename).name)

        bundle_loc = v_entry.bundle_url or f"apps/{app_id}/{v_entry.bundle_filename}"
        if bundle_loc.startswith(("http://", "https://")) or source.startswith(("http://", "https://")):
            dl_url = (
                bundle_loc
                if bundle_loc.startswith(("http://", "https://"))
                else urllib.parse.urljoin((manifest.base_url or source).rstrip("/") + "/", bundle_loc)
            )
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("GET", dl_url) as resp:
                    resp.raise_for_status()
                    with open(out_file, "wb") as handle:
                        for chunk in resp.iter_bytes(65536):
                            handle.write(chunk)
        else:
            src_root = Path(source).resolve()
            if src_root.is_file():
                src_root = src_root.parent
            src_bundle = validate_within_root(src_root, bundle_loc)
            if not src_bundle.exists():
                src_bundle = validate_within_root(src_root, Path("apps") / app_id / v_entry.bundle_filename)
            if not src_bundle.exists():
                raise FileNotFoundError(f"Bundle not found in catalog: {src_bundle}")
            shutil.copy2(src_bundle, out_file)

        if v_entry.bundle_sha256:
            verify_sha256(out_file, v_entry.bundle_sha256)
        return out_file, v_entry

    def push_package_http(
        self,
        server_url: str,
        package_source: Path,
        bootstrap_token: Optional[str] = None,
        channel: str = "stable",
        token_file: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Push an `.apkg` archive (or package an app directory first) over HTTP to AuraSprout UI."""
        if channel not in RELEASE_CHANNELS:
            raise ValueError(f"Unsupported channel {channel!r}; expected one of {RELEASE_CHANNELS}")

        parsed = urllib.parse.urlsplit((server_url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Invalid AuraSprout server URL: {server_url!r}")
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        resolved_token = (bootstrap_token or "").strip()
        if not resolved_token and parsed.fragment:
            resolved_token = parsed.fragment.strip()
        if not resolved_token and token_file is not None:
            raw_token_text = Path(token_file).read_text(encoding="utf-8").strip()
            if raw_token_text.startswith("{"):
                try:
                    token_payload = json.loads(raw_token_text)
                    if isinstance(token_payload, dict) and token_payload.get("token"):
                        raw_token_text = str(token_payload["token"]).strip()
                except json.JSONDecodeError:
                    pass
            resolved_token = raw_token_text
        if not resolved_token:
            resolved_token = (os.environ.get("AURASPROUT_UI_TOKEN") or "").strip()
        if not resolved_token:
            raise ValueError(
                "Bootstrap token required to push to AuraSprout HTTP API "
                "(pass --token, include #<token> in URL, or set AURASPROUT_UI_TOKEN)"
            )

        src = Path(package_source).resolve()
        if not src.exists():
            raise FileNotFoundError(f"Package or application directory not found: {src}")

        temp_dir: Optional[Path] = None
        if src.is_dir():
            temp_dir = Path(tempfile.mkdtemp(prefix="aurasprout_push_"))
            apkg_path, _, _ = SproutPackager(src).package(temp_dir)
        else:
            apkg_path = src

        try:
            payload_bytes = apkg_path.read_bytes()
            qs = urllib.parse.urlencode({"channel": channel, "filename": apkg_path.name})
            with httpx.Client(base_url=base_url, timeout=max(self.timeout, 60.0)) as client:
                auth_resp = client.post("/api/v1/auth/session", json={"token": resolved_token})
                auth_resp.raise_for_status()
                csrf_token = str(auth_resp.json().get("csrfToken") or "")
                pub_resp = client.post(
                    f"/api/v1/catalog/publish?{qs}",
                    content=payload_bytes,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-CSRF-Token": csrf_token,
                        "X-Filename": apkg_path.name,
                    },
                )
                pub_resp.raise_for_status()
                data = pub_resp.json()
                if isinstance(data, dict):
                    data.setdefault("catalogUrl", f"{base_url}/catalog")
                return data
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

