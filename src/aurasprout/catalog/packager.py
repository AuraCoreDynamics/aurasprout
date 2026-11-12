"""Application Package Builder (`.apkg` with offline wheelhouse & SHA-256) for AuraSprout."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, Optional, Tuple

from aurasprout.catalog.crypto import compute_sha256
from aurasprout.catalog.models import AppManifest
from aurasprout.contracts.paths import validate_identifier, validate_within_root


class SproutPackager:
    """Packages an application directory into a canonical `.apkg` archive."""

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = Path(app_dir).resolve()
        if not self.app_dir.exists() or not self.app_dir.is_dir():
            raise FileNotFoundError(f"Application source directory not found: {self.app_dir}")
        self.manifest = AppManifest.from_file(self.app_dir)

    @staticmethod
    def _copy_tree_clean(src: Path, dst: Path) -> None:
        ignore = shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            ".git",
            ".github",
            ".venv",
            "venv",
            ".bootstrapped_python",
            "*.egg-info",
            "dist",
            "build",
            ".pytest_cache",
            ".sprout",
        )
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)

    def package(
        self,
        output_dir: Path,
        wheelhouse_dir: Optional[Path] = None,
        archive_format: str = "zip",
    ) -> Tuple[Path, str, int]:
        """Build `<appId>-<version>.apkg` and return `(apkg_path, sha256_hex, size_bytes)`."""
        app_id = validate_identifier(self.manifest.app_id, "appId")
        ver_safe = validate_identifier(self.manifest.version, "version")
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        if archive_format not in ("zip", "tar.gz"):
            raise ValueError(f"Unsupported archive format: {archive_format!r}")

        staging = Path(tempfile.mkdtemp(prefix=f"aurasprout_pkg_{app_id}_"))
        try:
            src_dst = staging / "src"
            src_dst.mkdir(parents=True, exist_ok=True)
            self._copy_tree_clean(self.app_dir, src_dst)

            # Collect offline wheelhouse if present or explicitly provided
            wh_src = Path(wheelhouse_dir).resolve() if wheelhouse_dir else None
            if wh_src is None and self.manifest.distribution.wheelhouse_path:
                cand_wh = validate_within_root(self.app_dir, self.manifest.distribution.wheelhouse_path)
                if cand_wh.exists() and cand_wh.is_dir():
                    wh_src = cand_wh
            if wh_src is None:
                for cand_name in ("wheels", "wheelhouse"):
                    cand_dir = self.app_dir / cand_name
                    if cand_dir.exists() and cand_dir.is_dir():
                        wh_src = cand_dir
                        break

            wheels_dst = staging / "wheels"
            if wh_src is not None and wh_src.exists() and wh_src.is_dir():
                wheels_dst.mkdir(parents=True, exist_ok=True)
                for whl in wh_src.glob("*"):
                    if whl.is_file():
                        shutil.copy2(whl, wheels_dst / whl.name)

            # Copy hooks/ if present
            hooks_src = self.app_dir / "hooks"
            if hooks_src.exists() and hooks_src.is_dir():
                hooks_dst = staging / "hooks"
                hooks_dst.mkdir(parents=True, exist_ok=True)
                self._copy_tree_clean(hooks_src, hooks_dst)

            # Emit canonical app.manifest.json at archive root
            manifest_copy = AppManifest.from_dict(self.manifest.to_dict())
            if (wheels_dst.exists() and any(wheels_dst.iterdir())) and not manifest_copy.distribution.wheelhouse_path:
                manifest_copy.distribution.wheelhouse_path = "wheels"
            manifest_copy.to_file(staging / "app.manifest.json")

            apkg_name = f"{app_id}-{ver_safe}.apkg"
            apkg_path = out_dir / apkg_name
            if apkg_path.exists():
                apkg_path.unlink()

            if archive_format == "zip":
                with zipfile.ZipFile(apkg_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(staging):
                        for fname in sorted(files):
                            full_path = Path(root) / fname
                            rel_path = full_path.relative_to(staging)
                            zf.write(full_path, rel_path.as_posix())
            else:
                with tarfile.open(apkg_path, "w:gz") as tf:
                    for root, _, files in os.walk(staging):
                        for fname in sorted(files):
                            full_path = Path(root) / fname
                            rel_path = full_path.relative_to(staging)
                            tf.add(full_path, arcname=rel_path.as_posix())

            sha256 = compute_sha256(apkg_path)
            size_bytes = apkg_path.stat().st_size
            return apkg_path, sha256, size_bytes
        finally:
            shutil.rmtree(staging, ignore_errors=True)

