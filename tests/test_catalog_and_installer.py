"""Unit & Integration tests for AuraSprout Catalog, Packager, Installer, Rollback & Hub."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
from auraseed.mvenv.manager import MvenvManager
from auraseed.mvenv.models import MvenvCreateRequest

from aurasprout.catalog.crypto import compute_sha256
from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.models import AppManifest
from aurasprout.catalog.packager import SproutPackager
from aurasprout.contracts.paths import SproutPaths
from aurasprout.installer.engine import SproutInstallerEngine


def _make_sample_app(root: Path, app_id: str = "geo-demo", version: str = "1.0.0", legacy: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    pkg_dir = root / "geo_demo"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (pkg_dir / "worker.py").write_text(
        "import os\nprint('WORKER_OK', os.environ.get('AURAGRID_MAS_ID', ''))\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n"
        f"[project]\nname = '{app_id}'\nversion = '{version}'\n"
        "[tool.setuptools]\npackages = ['geo_demo']\n",
        encoding="utf-8",
    )
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "post_install.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "install_dir = Path(os.environ['AURASPROUT_INSTALL_DIR'])\n"
        "(install_dir / 'hook_ran.txt').write_text(os.environ['AURASPROUT_VERSION'], encoding='utf-8')\n",
        encoding="utf-8",
    )
    if legacy:
        manifest = {
            "name": app_id,
            "display_name": "Legacy Geo Demo",
            "version": version,
            "description": "Legacy app.json migration fixture",
            "entrypoint_command": "geo_demo.worker",
            "default_port": 8081,
            "hooks": {"post_install": "hooks/post_install.py"},
        }
        (root / "app.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    else:
        manifest = {
            "appId": app_id,
            "name": "Geo Demo",
            "version": version,
            "description": "Canonical app.manifest.json fixture",
            "services": [
                {
                    "masId": f"{app_id}-local",
                    "mode": "LocalOnly",
                    "runtime": "Python",
                    "pythonConfig": {"scriptPath": "geo_demo/worker.py"},
                },
                {
                    "masId": f"{app_id}-singleton",
                    "mode": "CellSingleton",
                    "runtime": "Python",
                    "pythonConfig": {"scriptPath": "geo_demo/worker.py"},
                },
            ],
            "distribution": {
                "channels": ["stable", "preview"],
                "entrypointCommand": "-m geo_demo.worker",
                "defaultPort": 8081,
                "hooks": {"post_install": "hooks/post_install.py"},
                "tags": ["geospatial", "test"],
            },
        }
        (root / "app.manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root


def test_legacy_app_json_migration_and_canonical_manifest(tmp_path: Path) -> None:
    app_src = _make_sample_app(tmp_path / "legacy_src", app_id="legacy-app", version="0.2.0", legacy=True)
    loaded = AppManifest.from_file(app_src)
    assert loaded.app_id == "legacy-app"
    assert loaded.version == "0.2.0"
    assert len(loaded.services) == 1
    assert loaded.services[0].mode == "LocalOnly"
    assert loaded.distribution.hooks["post_install"] == "hooks/post_install.py"


def test_package_publish_verify_install_update_and_rollback(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    # Pre-seed sovereign-false fast test mvenv using current conda python
    mvenv_mgr.create_venv(
        MvenvCreateRequest(
            name="sprout-geo-demo",
            base_python=sys.executable,
            sovereign=False,
        )
    )
    catalog_mgr = SproutCatalogManager(catalog_dir=tmp_path / "public_catalog", paths=paths)
    engine = SproutInstallerEngine(
        paths=paths,
        mvenv_manager=mvenv_mgr,
        catalog_manager=catalog_mgr,
    )

    src_v1 = _make_sample_app(tmp_path / "src_v1", version="1.0.0")
    packager1 = SproutPackager(src_v1)
    apkg1, sha1, size1 = packager1.package(tmp_path / "dist")
    assert apkg1.exists() and size1 > 0
    assert compute_sha256(apkg1) == sha1

    ver1 = catalog_mgr.publish_package(apkg1, channel="stable")
    assert ver1.bundle_sha256 == sha1
    assert (catalog_mgr.catalog_dir / "index.html").exists()
    html = (catalog_mgr.catalog_dir / "index.html").read_text(encoding="utf-8")
    assert "cdn." not in html.lower() and "unpkg" not in html.lower() and "jsdelivr" not in html.lower()

    rec1 = engine.install_from_catalog(
        source=str(catalog_mgr.catalog_dir),
        app_id="geo-demo",
        version="1.0.0",
        channel="stable",
    )
    assert rec1.version == "1.0.0"
    assert all(s.volunteer_enabled is False for s in rec1.services)
    assert (Path(rec1.install_path) / "hook_ran.txt").read_text(encoding="utf-8") == "1.0.0"

    # Zero .sprout/ writes during non-volunteered install
    assert not paths.leases_dir.exists() or not any(paths.leases_dir.iterdir())
    assert not paths.nodes_dir.exists() or not any(paths.nodes_dir.iterdir())

    # User writes data into app_data/
    user_note = Path(rec1.install_path) / "app_data" / "user_state.txt"
    user_note.write_text("preserve-me-across-upgrade-and-rollback", encoding="utf-8")

    # Upgrade to v2.0.0
    src_v2 = _make_sample_app(tmp_path / "src_v2", version="2.0.0")
    apkg2, _, _ = SproutPackager(src_v2).package(tmp_path / "dist")
    catalog_mgr.publish_package(apkg2, channel="stable")
    rec2 = engine.install_from_catalog(source=str(catalog_mgr.catalog_dir), app_id="geo-demo", version="2.0.0")
    assert rec2.version == "2.0.0"
    assert "1.0.0" in rec2.previous_versions
    assert user_note.read_text(encoding="utf-8") == "preserve-me-across-upgrade-and-rollback"

    # Rollback to v1.0.0 -> app_data preserved & active_version.json updated
    rolled = engine.rollback_app("geo-demo", target_version="1.0.0")
    assert rolled.version == "1.0.0"
    assert user_note.read_text(encoding="utf-8") == "preserve-me-across-upgrade-and-rollback"
    ptr = engine.registry.read_active_version_pointer(Path(rolled.install_path))
    assert ptr is not None and ptr["activeVersion"] == "1.0.0"

    # Tampered package SHA-256 check fails cleanly
    bundle_in_cat = catalog_mgr.get_bundle_path("geo-demo", version="1.0.0")
    assert bundle_in_cat is not None
    with open(bundle_in_cat, "ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        catalog_mgr.download_verified_package(str(catalog_mgr.catalog_dir), "geo-demo", version="1.0.0")


def test_relink_apps_on_mvenv_merge(tmp_path: Path) -> None:
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    mvenv_mgr.create_venv(MvenvCreateRequest(name="sprout-app-a", base_python=sys.executable, sovereign=False))
    mvenv_mgr.create_venv(MvenvCreateRequest(name="sprout-app-b", base_python=sys.executable, sovereign=False))

    engine = SproutInstallerEngine(paths=paths, mvenv_manager=mvenv_mgr)
    src_a = _make_sample_app(tmp_path / "app_a", app_id="app-a", version="1.0.0")
    rec_a = engine.install_from_directory(src_a, mvenv_name="sprout-app-a")
    assert rec_a.mvenv_name == "sprout-app-a"

    merged = mvenv_mgr.merge_venvs(
        source_names=["sprout-app-a", "sprout-app-b"],
        target_name="sprout-shared",
        delete_sources=True,
        repoint_apps=True,
        repoint_callback=engine.relink_app_mvenv,
    )
    assert "app-a" in merged["repointed_apps"]
    updated_a = engine.registry.get("app-a")
    assert updated_a is not None
    assert updated_a.mvenv_name == "sprout-shared"


def _build_local_wheel(wheelhouse: Path, pkg_name: str = "offline_hello", version: str = "0.1.0") -> Path:
    """Create a minimal valid pure-Python .whl inside wheelhouse without network access."""
    import base64
    import hashlib

    wheelhouse.mkdir(parents=True, exist_ok=True)
    whl_path = wheelhouse / f"{pkg_name}-{version}-py3-none-any.whl"
    dist_info = f"{pkg_name}-{version}.dist-info"
    files = {
        f"{pkg_name}/__init__.py": b"HELLO_VALUE = 'OFFLINE_WHEEL_OK'\n",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {pkg_name}\nVersion: {version}\n"
        ).encode("utf-8"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: aurasprout-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode("utf-8"),
        f"{dist_info}/top_level.txt": f"{pkg_name}\n".encode("utf-8"),
    }
    records = []
    with zipfile.ZipFile(whl_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in files.items():
            zf.writestr(arc, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            records.append(f"{arc},sha256={digest},{len(data)}")
        records.append(f"{dist_info}/RECORD,,")
        zf.writestr(f"{dist_info}/RECORD", ("\n".join(records) + "\n").encode("utf-8"))
    return whl_path


def test_offline_wheelhouse_installation_and_cli_smoke(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from aurasprout.cli import main as cli_main

    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    mvenv_mgr.create_venv(MvenvCreateRequest(name="sprout-wheel-app", base_python=sys.executable, sovereign=False))

    wheelhouse = tmp_path / "wheelhouse"
    _build_local_wheel(wheelhouse)

    app_dir = tmp_path / "wheel_app"
    app_dir.mkdir(parents=True)
    (app_dir / "requirements.txt").write_text("offline_hello==0.1.0\n", encoding="utf-8")
    (app_dir / "app.manifest.json").write_text(
        json.dumps(
            {
                "appId": "wheel-app",
                "name": "Offline Wheel App",
                "version": "1.0.0",
                "services": [{"masId": "wheel-svc", "mode": "LocalOnly", "runtime": "Python"}],
                "distribution": {"channels": ["stable"]},
            }
        ),
        encoding="utf-8",
    )

    apkg, _, _ = SproutPackager(app_dir).package(tmp_path / "dist", wheelhouse_dir=wheelhouse)
    engine = SproutInstallerEngine(paths=paths, mvenv_manager=mvenv_mgr)
    rec = engine.install_from_package(apkg, mvenv_name="sprout-wheel-app", sovereign=False)
    assert rec.app_id == "wheel-app"

    import subprocess

    probe = subprocess.run(
        [rec.python_path, "-c", "import offline_hello; print(offline_hello.HELLO_VALUE)"],
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    assert probe.returncode == 0
    assert "OFFLINE_WHEEL_OK" in probe.stdout

    # CLI smoke checks across catalog, volunteer, conflicts
    assert cli_main(["--cell-root", str(paths.cell_root), "--user-root", str(paths.user_root), "catalog", "list", "--installed", "--json"]) == 0
    assert "wheel-app" in capsys.readouterr().out

    assert cli_main(["--cell-root", str(paths.cell_root), "--user-root", str(paths.user_root), "volunteer", "status", "--json"]) == 0
    assert "wheel-svc" in capsys.readouterr().out

    assert cli_main(["--cell-root", str(paths.cell_root), "--user-root", str(paths.user_root), "conflicts", "list", "--json"]) == 0
    assert "conflicts" in capsys.readouterr().out


def test_desktop_hub_composes_auraseed_mvenv_panel(tmp_path: Path) -> None:
    import tkinter as tk
    from aurasprout.ui.desktop import SproutDesktopHub

    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"No Tk display available: {exc}")
        return

    try:
        hub = SproutDesktopHub(root=root, paths=paths, mvenv_manager=mvenv_mgr)
        assert hub.mvenv_panel is not None
        assert hub.mvenv_panel.repoint_callback == hub.installer.relink_app_mvenv
    finally:
        root.destroy()

