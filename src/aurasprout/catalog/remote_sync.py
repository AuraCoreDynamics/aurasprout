"""Generic remote catalog synchronization and deployment adapters for AuraSprout.
Supports SFTP deployment (e.g. DreamHost, generic Linux hosting) and static catalog publishing.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.models import AppVersionEntry
from aurasprout.contracts.paths import SproutPaths

_LOG = logging.getLogger(__name__)


def parse_credentials_file(creds_path: Union[str, Path]) -> Dict[str, str]:
    """Parse key: value credentials file (e.g. username: foo, password: bar, host: example.com)."""
    p = Path(creds_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Credentials file not found: {p}")
    result: Dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip().lower()] = v.strip()
        elif "=" in line:
            k, v = line.split("=", 1)
            result[k.strip().lower()] = v.strip()
    return result


@dataclass
class SftpDeployResult:
    """Result of an SFTP catalog sync operation."""
    host: str
    remote_dir: str
    uploaded_files: List[str]
    total_bytes_transferred: int
    catalog_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "remoteDir": self.remote_dir,
            "uploadedFiles": self.uploaded_files,
            "fileCount": len(self.uploaded_files),
            "totalBytesTransferred": self.total_bytes_transferred,
            "catalogUrl": self.catalog_url,
        }


class SftpCatalogDeployer:
    """Generic SFTP adapter for synchronizing local or generated AuraSprout catalogs to remote hosts.
    Works seamlessly with DreamHost, generic cPanel/Linux servers, and cloud SFTP storage.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: Optional[str] = None,
        key_filename: Optional[Union[str, Path]] = None,
        port: int = 22,
        timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.key_filename = str(key_filename) if key_filename else None
        self.port = int(port)
        self.timeout = float(timeout)

    @classmethod
    def from_credentials_file(
        cls,
        creds_file: Union[str, Path],
        default_host: Optional[str] = None,
        default_port: int = 22,
    ) -> "SftpCatalogDeployer":
        """Instantiate deployer from a credentials file."""
        creds = parse_credentials_file(creds_file)
        username = creds.get("username") or creds.get("user")
        if not username:
            raise ValueError(f"Credentials file {creds_file} missing 'username'")
        password = creds.get("password") or creds.get("pass")
        host = creds.get("host") or creds.get("hostname") or default_host
        if not host:
            raise ValueError("No host specified in credentials file or default_host")
        port = int(creds.get("port") or default_port)
        key_filename = creds.get("key") or creds.get("key_file")
        return cls(
            host=host,
            username=username,
            password=password,
            key_filename=key_filename,
            port=port,
        )

    def _ensure_remote_dir(self, sftp: Any, remote_dir: str) -> None:
        """Recursively ensure remote directory path exists."""
        clean = remote_dir.strip().replace("\\", "/").rstrip("/")
        if not clean:
            return
        parts = clean.split("/")
        current = "/" if clean.startswith("/") else ""
        for part in parts:
            if not part:
                continue
            current = f"{current}/{part}" if current and current != "/" else f"{current}{part}"
            try:
                sftp.stat(current)
            except Exception:
                try:
                    sftp.mkdir(current)
                except Exception as exc:
                    _LOG.debug("mkdir %s error (may already exist): %s", current, exc)

    def sync_catalog(
        self,
        local_catalog_dir: Union[str, Path],
        remote_catalog_dir: str,
        catalog_base_url: Optional[str] = None,
        skip_identical_size: bool = True,
    ) -> SftpDeployResult:
        """Synchronize an entire local catalog directory tree to the remote SFTP host."""
        try:
            import paramiko
        except ImportError as err:
            raise RuntimeError(
                "Paramiko is required for SFTP catalog deployment. Install with: pip install paramiko"
            ) from err

        local_root = Path(local_catalog_dir).resolve()
        if not local_root.is_dir():
            raise FileNotFoundError(f"Local catalog directory not found: {local_root}")

        transport = paramiko.Transport((self.host, self.port))
        transport.banner_timeout = self.timeout
        transport.auth_timeout = self.timeout

        try:
            if self.key_filename:
                pkey = paramiko.RSAKey.from_private_key_file(self.key_filename)
                transport.connect(username=self.username, pkey=pkey)
            else:
                transport.connect(username=self.username, password=self.password)

            sftp = paramiko.SFTPClient.from_transport(transport)
            try:
                clean_remote_root = remote_catalog_dir.strip().replace("\\", "/").rstrip("/")
                self._ensure_remote_dir(sftp, clean_remote_root)

                uploaded: List[str] = []
                total_bytes = 0

                # Walk local catalog directory
                for item in sorted(local_root.rglob("*")):
                    if item.is_dir():
                        continue

                    rel_path = item.relative_to(local_root).as_posix()
                    remote_file_path = f"{clean_remote_root}/{rel_path}"
                    remote_parent = remote_file_path.rsplit("/", 1)[0]
                    self._ensure_remote_dir(sftp, remote_parent)

                    file_size = item.stat().st_size
                    should_upload = True

                    if skip_identical_size:
                        try:
                            rem_stat = sftp.stat(remote_file_path)
                            if rem_stat.st_size == file_size:
                                # For json and html manifests, always re-upload to guarantee freshness
                                if not item.name.endswith((".json", ".html")):
                                    should_upload = False
                        except Exception:
                            should_upload = True

                    if should_upload:
                        _LOG.info("Uploading %s -> %s (%d bytes)", rel_path, remote_file_path, file_size)
                        sftp.put(str(item), remote_file_path)
                        uploaded.append(rel_path)
                        total_bytes += file_size

                return SftpDeployResult(
                    host=self.host,
                    remote_dir=clean_remote_root,
                    uploaded_files=uploaded,
                    total_bytes_transferred=total_bytes,
                    catalog_url=catalog_base_url,
                )
            finally:
                sftp.close()
        finally:
            transport.close()


def build_and_deploy_catalog(
    package_sources: Sequence[Union[str, Path]],
    remote_host: str,
    remote_dir: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    creds_file: Optional[Union[str, Path]] = None,
    catalog_title: str = "AuraCore Application Catalog",
    base_url: str = "https://auracoredynamics.com/aurasprout/catalog",
    local_staging_dir: Optional[Union[str, Path]] = None,
    channel: str = "stable",
    paths: Optional[SproutPaths] = None,
) -> SftpDeployResult:
    """Build a static catalog from a list of packages/directories, generate static HTML & JSON,
    and deploy it via SFTP to the target remote directory.
    """
    tmp_dir: Optional[Path] = None
    if local_staging_dir:
        stage_dir = Path(local_staging_dir).resolve()
        stage_dir.mkdir(parents=True, exist_ok=True)
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="aurasprout_cat_deploy_"))
        stage_dir = tmp_dir

    try:
        cat_mgr = SproutCatalogManager(
            catalog_dir=stage_dir,
            base_url=base_url,
            paths=paths,
        )
        cat_mgr.manifest.catalog_name = catalog_title

        for src in package_sources:
            src_path = Path(src).resolve()
            if not src_path.exists():
                raise FileNotFoundError(f"Package source does not exist: {src_path}")
            cat_mgr.publish_package(src_path, channel=channel)

        cat_mgr.save(build_html=True)

        if creds_file:
            deployer = SftpCatalogDeployer.from_credentials_file(
                creds_file=creds_file,
                default_host=remote_host,
            )
        else:
            if not username:
                raise ValueError("Must provide username or creds_file for SFTP deployment")
            deployer = SftpCatalogDeployer(
                host=remote_host,
                username=username,
                password=password,
            )

        return deployer.sync_catalog(
            local_catalog_dir=stage_dir,
            remote_catalog_dir=remote_dir,
            catalog_base_url=base_url,
        )
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

