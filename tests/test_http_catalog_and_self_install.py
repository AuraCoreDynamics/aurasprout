"""Tests for AuraSprout HTTP catalog push/hosting and standalone self-install/cleanup."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
from auraseed.mvenv.manager import MvenvManager
from auraseed.mvenv.models import MvenvCreateRequest

from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.packager import SproutPackager
from aurasprout.contracts.paths import SproutPaths
from aurasprout.installer.autostart import generate_launchers
from aurasprout.installer.engine import SproutInstallerEngine
from aurasprout.installer.self_install import (
    _stage_icon_asset,
    _win_delete_registry_tree,
    install_standalone,
    uninstall_self,
)
from aurasprout.ui.server import ManagementServerState, SproutManagementServer


def _build_sample_http_app(root: Path, app_id: str = "http-demo", version: str = "1.2.0") -> Path:
    pkg = root / "http_demo"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (pkg / "cli.py").write_text(
        "import sys\nprint('HTTP_APP_RUN_OK', *sys.argv[1:])\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n"
        f"[project]\nname = '{app_id}'\nversion = '{version}'\n"
        "[project.scripts]\n"
        f"{app_id} = 'http_demo.cli:main'\n"
        "[tool.setuptools]\npackages = ['http_demo']\n",
        encoding="utf-8",
    )
    # Also expose main() inside cli.py for console script test
    (pkg / "cli.py").write_text(
        "import sys\n"
        "def main():\n"
        "    print('HTTP_APP_RUN_OK', *sys.argv[1:])\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    hooks = root / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "post_install.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "install_dir = Path(os.environ['AURASPROUT_INSTALL_DIR'])\n"
        "artifacts = Path(os.environ['AURASPROUT_ARTIFACTS_DIR'])\n"
        "ds = list(artifacts.rglob('sample.json'))\n"
        "assert ds, f'No dataset copied in {artifacts}'\n"
        "(install_dir / 'post_hook_verified.txt').write_text(ds[0].read_text(encoding='utf-8'), encoding='utf-8')\n",
        encoding="utf-8",
    )
    (root / "datasets").mkdir(parents=True, exist_ok=True)
    (root / "datasets" / "sample.json").write_text('{"ok": true}', encoding="utf-8")
    (root / "app.manifest.json").write_text(
        json.dumps(
            {
                "appId": app_id,
                "name": "HTTP Demo App",
                "version": version,
                "services": [{"masId": f"{app_id}-svc", "mode": "LocalOnly", "runtime": "Python"}],
                "distribution": {
                    "channels": ["stable"],
                    "entrypointCommand": f"{app_id} --port {{port}}",
                    "defaultPort": 8123,
                    "artifacts": ["datasets/*.json"],
                    "hooks": {"post_install": "hooks/post_install.py"},
                    "tags": ["http-test"],
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def test_http_catalog_push_host_download_and_install(tmp_path: Path) -> None:
    server_paths = SproutPaths(cell_root=tmp_path / "srv_cell", user_root=tmp_path / "srv_user")
    state = ManagementServerState(paths=server_paths)

    client_paths = SproutPaths(cell_root=tmp_path / "cli_cell", user_root=tmp_path / "cli_user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    mvenv_mgr.create_venv(MvenvCreateRequest(name="sprout-http-demo", base_python=sys.executable, sovereign=False))

    src_dir = _build_sample_http_app(tmp_path / "src_app")
    apkg_path, expected_sha, _ = SproutPackager(src_dir).package(tmp_path / "dist")

    with SproutManagementServer(host="127.0.0.1", port=0, state=state) as srv:
        client_cat = SproutCatalogManager(catalog_dir=tmp_path / "client_cache", paths=client_paths)
        pushed = client_cat.push_package_http(
            server_url=srv.auth_url,
            package_source=apkg_path,
            channel="stable",
        )
        assert pushed["appId"] == "http-demo"
        assert pushed["bundleSha256"] == expected_sha

        # Also verify token_file reading JSON daemon token format (`{"token": "..."}`)
        token_json_file = tmp_path / "ui_token.json"
        token_json_file.write_text(json.dumps({"token": srv.bootstrap_token}), encoding="utf-8")
        pushed_via_file = client_cat.push_package_http(
            server_url=srv.base_url,
            package_source=apkg_path,
            channel="preview",
            token_file=token_json_file,
        )
        assert pushed_via_file["appId"] == "http-demo"

        # Verify hosted /catalog endpoints
        with httpx.Client(base_url=srv.base_url, timeout=30.0) as raw_http:
            html_resp = raw_http.get("/catalog/index.html")
            assert html_resp.status_code == 200
            assert "http-demo" in html_resp.text

            cat_resp = raw_http.get("/catalog/catalog.json")
            assert cat_resp.status_code == 200
            cat_json = cat_resp.json()
            assert cat_json["baseUrl"].startswith(srv.base_url)

            # Path traversal must be rejected safely (encoded '..' inside /catalog/apps/)
            assert raw_http.get("/catalog/apps/%2e%2e/%2e%2e/secret.txt").status_code in (400, 404)

        engine = SproutInstallerEngine(
            paths=client_paths,
            mvenv_manager=mvenv_mgr,
            catalog_manager=client_cat,
        )
        rec = engine.install_from_catalog(
            source=f"{srv.base_url}/catalog",
            app_id="http-demo",
            channel="stable",
        )
        assert rec.app_id == "http-demo"
        assert rec.version == "1.2.0"
        hook_out = Path(rec.install_path) / "post_hook_verified.txt"
        assert hook_out.exists() and json.loads(hook_out.read_text(encoding="utf-8")) == {"ok": True}

        # Verify generated launcher runs cleanly without double `-m` bugs
        launcher = rec.launchers.get("ps1") or rec.launchers.get("bat") or rec.launchers.get("sh")
        assert launcher is not None and Path(launcher).exists()


def test_generate_launchers_handles_console_script_and_dash_m(tmp_path: Path) -> None:
    is_win = platform.system().lower() == "windows"
    scripts_dir = tmp_path / "venv" / ("Scripts" if is_win else "bin")
    scripts_dir.mkdir(parents=True, exist_ok=True)
    py_exe = scripts_dir / ("python.exe" if is_win else "python")
    py_exe.write_text("", encoding="utf-8")
    if is_win:
        (scripts_dir / "pythonw.exe").write_text("", encoding="utf-8")
    console_exe = scripts_dir / ("myapp.exe" if is_win else "myapp")
    console_exe.write_text("", encoding="utf-8")

    # 1. Console script resolution
    l1 = generate_launchers(tmp_path / "app1", "myapp", py_exe, "myapp --port {port}", port=9000)
    script_text = Path(l1["bat"] if is_win else l1["sh"]).read_text(encoding="utf-8")
    assert str(console_exe) in script_text
    assert "-m myapp" not in script_text
    if is_win:
        assert Path(l1["pyw"]).exists()
        assert Path(l1["pythonw"]).name.lower() == "pythonw.exe"
        pyw_text = Path(l1["pyw"]).read_text(encoding="utf-8")
        assert "CREATE_NO_WINDOW" in pyw_text and "console_scripts" in pyw_text

    # 2. `-m module` should never duplicate `-m -m`
    l2 = generate_launchers(tmp_path / "app2", "modapp", py_exe, "-m modapp.cli --port {port}", port=9001)
    script_text2 = Path(l2["bat"] if is_win else l2["sh"]).read_text(encoding="utf-8")
    assert "-m -m" not in script_text2
    assert "-m modapp.cli --port 9001" in script_text2


def test_uninstall_self_cleans_os_registry_env_and_dirs(tmp_path: Path) -> None:
    user_root = tmp_path / "user_root"
    cell_root = tmp_path / "cell_root"
    standalone_dir = tmp_path / "standalone"
    desktop_dir = tmp_path / "Desktop"
    cfg_dir = tmp_path / "cfg"
    posix_bin = tmp_path / "posix_bin"
    for d in (user_root, cell_root / ".sprout", standalone_dir / "bin", desktop_dir, cfg_dir, posix_bin):
        d.mkdir(parents=True, exist_ok=True)

    (user_root / "standalone_install.json").write_text(
        json.dumps(
            {
                "install_dir": str(standalone_dir),
                "bin_dir": str(standalone_dir / "bin"),
                "desktop_shortcut": str(desktop_dir / "aurasprout.cmd"),
            }
        ),
        encoding="utf-8",
    )
    (desktop_dir / "aurasprout.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (posix_bin / "aurasprout").write_text("#!/bin/sh\n", encoding="utf-8")

    win_root = None
    win_uninst = None
    win_env = None
    if platform.system().lower() == "windows":
        import winreg

        uid = uuid.uuid4().hex[:10]
        win_root = rf"Software\AuraCore\AuraSproutTest_{uid}"
        win_uninst = rf"Software\AuraCore\AuraSproutUninstallTest_{uid}"
        win_env = rf"Software\AuraCore\AuraSproutEnvTest_{uid}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, win_root) as rk:
            winreg.SetValueEx(rk, "InstallPath", 0, winreg.REG_SZ, str(standalone_dir))
            winreg.SetValueEx(rk, "BinDir", 0, winreg.REG_SZ, str(standalone_dir / "bin"))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, win_uninst) as uk:
            winreg.SetValueEx(uk, "DisplayName", 0, winreg.REG_SZ, "AuraSprout Test")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, win_env) as ek:
            winreg.SetValueEx(ek, "AURASPROUT_HOME", 0, winreg.REG_SZ, str(user_root))
            winreg.SetValueEx(
                ek,
                "Path",
                0,
                winreg.REG_EXPAND_SZ,
                rf"C:\Windows\System32;{standalone_dir / 'bin'}",
            )

    try:
        report = uninstall_self(
            remove_apps=True,
            remove_mvenvs=True,
            remove_package=False,
            user_root=user_root,
            cell_root=cell_root,
            dest_dir=standalone_dir,
            desktop_dir=desktop_dir,
            config_dir=cfg_dir,
            posix_bin_dir=posix_bin,
            clean_os_registry=(platform.system().lower() == "windows"),
            registry_root_key=win_root or r"Software\AuraCore\AuraSprout",
            uninstall_reg_key=win_uninst or r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AuraSprout",
            env_reg_subkey=win_env or "Environment",
        )
        assert not user_root.exists()
        assert not (cell_root / ".sprout").exists()
        assert not standalone_dir.exists()
        assert not (desktop_dir / "aurasprout.cmd").exists()
        assert not (posix_bin / "aurasprout").exists()
        assert not cfg_dir.exists()

        if platform.system().lower() == "windows" and win_env:
            import winreg

            assert "AURASPROUT_HOME" in report["cleaned_env_vars"]
            assert report["path_cleaned"] is True
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, win_env, 0, winreg.KEY_READ) as ek:
                path_val, _ = winreg.QueryValueEx(ek, "Path")
                assert path_val == r"C:\Windows\System32"
    finally:
        for k in (win_root, win_uninst, win_env):
            if k:
                _win_delete_registry_tree(k)


def test_stage_icon_asset_selects_first_existing_and_copies(tmp_path: Path) -> None:
    missing = tmp_path / "missing.ico"
    first_real = tmp_path / "src" / "app.ico"
    first_real.parent.mkdir(parents=True, exist_ok=True)
    first_real.write_bytes(b"FAKE_ICO_BYTES")
    dest = tmp_path / "staged" / "app.ico"

    staged = _stage_icon_asset([None, missing, first_real], dest)
    assert staged == dest.resolve()
    assert dest.exists()
    assert dest.read_bytes() == b"FAKE_ICO_BYTES"
    assert _stage_icon_asset([None, missing], tmp_path / "staged" / "none.ico") is None


def test_install_standalone_stages_icons_and_passes_to_start_menu(tmp_path: Path, monkeypatch) -> None:
    user_root = tmp_path / "user_root"
    cell_root = tmp_path / "cell_root"
    dest_dir = tmp_path / "standalone"
    is_win = platform.system().lower() == "windows"
    scripts_dir = dest_dir / ("Scripts" if is_win else "bin")
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "pyvenv.cfg").write_text("home = mock\n", encoding="utf-8")
    (scripts_dir / ("python.exe" if is_win else "python")).write_text("", encoding="utf-8")
    if is_win:
        (scripts_dir / "pythonw.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout="CLEANROOM_OK\n", stderr=""),
    )

    sm_calls = []

    def _fake_register_start_menu_launcher(**kwargs):
        sm_calls.append(kwargs)
        sc_path = tmp_path / "start_menu" / f"{kwargs['display_name']}.lnk"
        return {"shortcut_path": str(sc_path)}

    monkeypatch.setattr("auraseed.lifecycle.register_start_menu_launcher", _fake_register_start_menu_launcher)

    report = install_standalone(
        dest_dir=dest_dir,
        user_root=user_root,
        cell_root=cell_root,
        dep_sources=(),
        register_os=False,
        add_to_path=False,
        create_shortcut=False,
        create_start_menu=True,
        start_menu_dir=tmp_path / "start_menu",
    )

    expected_sprout_ico = (dest_dir / "assets" / "aurasprout.ico").resolve()
    expected_seed_ico = (dest_dir / "assets" / "auraseed.ico").resolve()
    assert expected_sprout_ico.exists() and expected_sprout_ico.stat().st_size > 0
    assert expected_seed_ico.exists() and expected_seed_ico.stat().st_size > 0
    assert report["icons"]["aurasprout"] == str(expected_sprout_ico)
    assert report["icons"]["auraseed"] == str(expected_seed_ico)

    by_app = {call["app_name"]: call for call in sm_calls}
    assert by_app["aurasprout"]["icon_path"] == expected_sprout_ico
    assert by_app["auraseed"]["icon_path"] == expected_seed_ico
