"""Zero-privilege standalone installation and full OS/user cleanup for AuraSprout."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import venv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from aurasprout.contracts.paths import SproutPaths, default_user_data_root
from aurasprout.installer.autostart import (
    create_user_desktop_shortcut,
    resolve_user_desktop_dir,
    resolve_windowless_python,
    write_windowless_pyw_launcher,
)
from aurasprout.installer.engine import SproutInstallerEngine


def _norm_path_str(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p.resolve())))


def _win_delete_registry_tree(subkey: str) -> List[str]:
    if platform.system().lower() != "windows":
        return []
    import winreg

    deleted: List[str] = []

    def _recurse(path: str) -> None:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
        except OSError:
            return
        subkeys: List[str] = []
        try:
            idx = 0
            while True:
                try:
                    subkeys.append(winreg.EnumKey(key, idx))
                    idx += 1
                except OSError:
                    break
        finally:
            winreg.CloseKey(key)

        for child in subkeys:
            _recurse(f"{path}\\{child}")

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            deleted.append(f"HKCU\\{path}")
        except OSError:
            pass

    _recurse(subkey)
    return deleted


def _win_read_registry_value(subkey: str, value_name: str) -> Optional[str]:
    if platform.system().lower() != "windows":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, value_name)
            return str(val) if val is not None else None
    except OSError:
        return None


def _win_cleanup_hkcu_env(
    prefixes: Tuple[str, ...] = ("AURASPROUT_",),
    path_dirs_to_remove: Iterable[Path] = (),
    env_subkey: str = "Environment",
) -> Tuple[List[str], bool]:
    if platform.system().lower() != "windows":
        return [], False
    import winreg

    removed_vars: List[str] = []
    path_modified = False
    remove_norms = {_norm_path_str(Path(p)) for p in path_dirs_to_remove}

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            env_subkey,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        )
    except OSError:
        return removed_vars, path_modified

    try:
        values: List[Tuple[str, Any, int]] = []
        idx = 0
        while True:
            try:
                name, val, vtype = winreg.EnumValue(key, idx)
                values.append((name, val, vtype))
                idx += 1
            except OSError:
                break

        for name, val, vtype in values:
            upper_name = name.upper()
            if any(upper_name.startswith(p) for p in prefixes):
                try:
                    winreg.DeleteValue(key, name)
                    removed_vars.append(name)
                except OSError:
                    pass
                continue

            if upper_name == "PATH" and remove_norms and isinstance(val, str):
                parts = [seg for seg in val.split(";") if seg.strip()]
                kept: List[str] = []
                for seg in parts:
                    try:
                        norm_seg = os.path.normcase(os.path.normpath(seg.strip()))
                    except OSError:
                        norm_seg = seg.strip().lower()
                    if norm_seg in remove_norms:
                        path_modified = True
                    else:
                        kept.append(seg)
                if path_modified:
                    new_path = ";".join(kept)
                    if new_path:
                        winreg.SetValueEx(key, name, 0, vtype, new_path)
                    else:
                        winreg.DeleteValue(key, name)
    finally:
        winreg.CloseKey(key)

    return removed_vars, path_modified


def _win_add_path_dir(bin_dir: Path, env_subkey: str = "Environment") -> bool:
    if platform.system().lower() != "windows":
        return False
    import winreg

    resolved_str = str(bin_dir.resolve())
    norm_target = _norm_path_str(bin_dir)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            env_subkey,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                cur_val, cur_type = winreg.QueryValueEx(key, "Path")
            except OSError:
                cur_val, cur_type = "", winreg.REG_EXPAND_SZ
            parts = [s for s in str(cur_val).split(";") if s.strip()]
            if any(os.path.normcase(os.path.normpath(s.strip())) == norm_target for s in parts):
                return False
            parts.append(resolved_str)
            reg_type = cur_type if cur_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else winreg.REG_EXPAND_SZ
            winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(parts))
            return True
    except OSError:
        return False


def _detect_workspace_sources(explicit_source: Optional[Path] = None) -> Tuple[Optional[Path], List[Path]]:
    """Locate local AuraSprout source directory and sibling AuraCore dependencies if present."""
    src_root = Path(explicit_source).resolve() if explicit_source else None
    if src_root is None:
        # Try parent of `src/aurasprout/installer/self_install.py`
        candidate = Path(__file__).resolve().parents[3]
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "aurasprout").is_dir():
            src_root = candidate

    sibling_deps: List[Path] = []
    if src_root is not None:
        parent_ws = src_root.parent
        for dep_name in ("auragrid-api", "auraseed", "auragrid-py"):
            dep_dir = parent_ws / dep_name
            if dep_dir.is_dir() and (dep_dir / "pyproject.toml").exists():
                sibling_deps.append(dep_dir)
    return src_root, sibling_deps


def _stage_icon_asset(candidates: Sequence[Optional[Path]], dest: Path) -> Optional[Path]:
    """Locate the first existing icon file from candidates and stage it at `dest`."""
    for cand in candidates:
        if cand is not None and cand.exists() and cand.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if cand.resolve() != dest.resolve():
                shutil.copy2(cand, dest)
            return dest.resolve()
    return None


def install_standalone(
    *,
    dest_dir: Optional[Path] = None,
    user_root: Optional[Path] = None,
    cell_root: Optional[Path] = None,
    base_python: Optional[str] = None,
    source_dir: Optional[Path] = None,
    dep_sources: Optional[Sequence[Path]] = None,
    wheelhouse_dir: Optional[Path] = None,
    editable: bool = False,
    register_os: bool = True,
    add_to_path: bool = False,
    create_shortcut: bool = False,
    desktop_dir: Optional[Path] = None,
    create_start_menu: bool = True,
    start_menu_dir: Optional[Path] = None,
    registry_root_key: str = r"Software\AuraCore\AuraSprout",
    uninstall_reg_key: str = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AuraSprout",
    env_reg_subkey: str = "Environment",
) -> Dict[str, Any]:
    """Perform a cleanroom standalone installation of AuraSprout into an isolated venv."""
    is_win = platform.system().lower() == "windows"
    resolved_user_root = Path(user_root).resolve() if user_root else default_user_data_root()
    resolved_cell_root = Path(cell_root).resolve() if cell_root else resolved_user_root
    target_dir = Path(dest_dir).resolve() if dest_dir else (resolved_user_root / "standalone").resolve()

    if target_dir.exists() and any(target_dir.iterdir()):
        # Ensure cleanroom venv without stale site-packages
        if not (target_dir / "pyvenv.cfg").exists():
            target_dir.mkdir(parents=True, exist_ok=True)
    else:
        target_dir.mkdir(parents=True, exist_ok=True)

    py_base = str(Path(base_python).resolve()) if base_python else sys.executable
    if not (target_dir / "pyvenv.cfg").exists():
        if Path(py_base).resolve() == Path(sys.executable).resolve():
            venv.EnvBuilder(with_pip=True, system_site_packages=False).create(target_dir)
        else:
            subprocess.run(
                [py_base, "-m", "venv", str(target_dir)],
                check=True,
                capture_output=True,
                text=True,
                shell=False,
                timeout=120,
            )

    venv_py = (target_dir / "Scripts" / "python.exe") if is_win else (target_dir / "bin" / "python")
    if not venv_py.exists() and not is_win:
        venv_py = target_dir / "bin" / "python3"
    if not venv_py.exists():
        raise FileNotFoundError(f"Standalone venv Python executable not found in {target_dir}")

    resolved_src, auto_deps = _detect_workspace_sources(source_dir)
    install_deps = [Path(d).resolve() for d in dep_sources] if dep_sources is not None else auto_deps

    if wheelhouse_dir is not None:
        wh = Path(wheelhouse_dir).resolve()
        cmd = [str(venv_py), "-m", "pip", "install", "--no-index", "--find-links", str(wh), "--no-build-isolation"]
        if resolved_src and resolved_src.exists():
            cmd.extend([str(d) for d in install_deps])
            cmd.append("-e" if editable else "")
            if not editable:
                cmd.pop()
            cmd.append(str(resolved_src))
        else:
            cmd.append("aurasprout")
        res = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=300)
        if res.returncode != 0:
            raise RuntimeError(f"Offline standalone pip install failed: {res.stderr or res.stdout}")
    else:
        for dep in install_deps:
            dep_cmd = [str(venv_py), "-m", "pip", "install"]
            if editable:
                dep_cmd.extend(["-e", str(dep)])
            else:
                dep_cmd.append(str(dep))
            res = subprocess.run(dep_cmd, capture_output=True, text=True, shell=False, timeout=300)
            if res.returncode != 0:
                raise RuntimeError(f"Failed installing dependency {dep}: {res.stderr or res.stdout}")

        app_cmd = [str(venv_py), "-m", "pip", "install"]
        if resolved_src and resolved_src.exists():
            if editable:
                app_cmd.extend(["-e", str(resolved_src)])
            else:
                app_cmd.append(str(resolved_src))
        else:
            app_cmd.append("aurasprout")
        res = subprocess.run(app_cmd, capture_output=True, text=True, shell=False, timeout=300)
        if res.returncode != 0:
            raise RuntimeError(f"Failed installing aurasprout standalone: {res.stderr or res.stdout}")

    bin_dir = target_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    resolved_user_root.mkdir(parents=True, exist_ok=True)

    launchers: Dict[str, str] = {}
    if is_win:
        bat_path = bin_dir / "aurasprout.bat"
        bat_path.write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "AURASPROUT_HOME={resolved_user_root}"\r\n'
            f'set "AURASPROUT_CELL_ROOT={resolved_cell_root}"\r\n'
            f'"{venv_py}" -m aurasprout %*\r\n',
            encoding="utf-8",
        )
        launchers["bat"] = str(bat_path)

        ps1_path = bin_dir / "aurasprout.ps1"
        ps1_path.write_text(
            f'$env:AURASPROUT_HOME = "{resolved_user_root}"\n'
            f'$env:AURASPROUT_CELL_ROOT = "{resolved_cell_root}"\n'
            f'& "{venv_py}" -m aurasprout @args\n',
            encoding="utf-8",
        )
        launchers["ps1"] = str(ps1_path)
    else:
        sh_path = bin_dir / "aurasprout"
        sh_path.write_text(
            "#!/bin/sh\n"
            f'export AURASPROUT_HOME="{resolved_user_root}"\n'
            f'export AURASPROUT_CELL_ROOT="{resolved_cell_root}"\n'
            f'exec "{venv_py}" -m aurasprout "$@"\n',
            encoding="utf-8",
        )
        os.chmod(sh_path, 0o755)
        launchers["sh"] = str(sh_path)

    primary_launcher = launchers.get("bat") or launchers.get("ps1") or launchers.get("sh")
    pythonw_exe = resolve_windowless_python(venv_py) if is_win else None
    sprout_pyw: Optional[Path] = None
    seed_pyw: Optional[Path] = None
    if is_win:
        env_defaults = {
            "AURASPROUT_HOME": str(resolved_user_root),
            "AURASPROUT_STANDALONE_DIR": str(target_dir),
        }
        sprout_pyw = write_windowless_pyw_launcher(
            pyw_path=bin_dir / "launch-aurasprout-desktop.pyw",
            python_exe=venv_py,
            entry_command="-m aurasprout desktop",
            mvenv_root=target_dir,
            log_path=resolved_user_root / "aurasprout-desktop.log",
            extra_env=env_defaults,
        )
        seed_pyw = write_windowless_pyw_launcher(
            pyw_path=bin_dir / "launch-auraseed-gui.pyw",
            python_exe=venv_py,
            entry_command="-m auraseed gui",
            mvenv_root=target_dir,
            log_path=resolved_user_root / "auraseed-gui.log",
            extra_env=env_defaults,
        )
        launchers["pyw"] = str(sprout_pyw)

    shortcut_path: Optional[Path] = None
    if create_shortcut and primary_launcher:
        shortcut_path = create_user_desktop_shortcut(
            app_id="aurasprout",
            display_name="AuraSprout",
            launcher_path=sprout_pyw if (pythonw_exe and sprout_pyw) else Path(primary_launcher),
            desktop_dir=desktop_dir,
            pythonw_path=pythonw_exe,
        )

    assets_dir = target_dir / "assets"
    sprout_ico_candidates: List[Optional[Path]] = [
        target_dir / "Lib" / "site-packages" / "aurasprout" / "ui" / "static" / "aurasprout.ico",
        Path(__file__).resolve().parents[1] / "ui" / "static" / "aurasprout.ico",
        (resolved_src / "assets" / "aurasprout.ico") if resolved_src else None,
    ]
    sprout_icon = _stage_icon_asset(sprout_ico_candidates, assets_dir / "aurasprout.ico")

    import auraseed as _auraseed_pkg

    seed_ico_candidates: List[Optional[Path]] = [
        target_dir / "Lib" / "site-packages" / "auraseed" / "assets" / "auraseed.ico",
        Path(_auraseed_pkg.__file__).resolve().parent / "assets" / "auraseed.ico",
    ]
    for dep in install_deps:
        if dep.name.lower() == "auraseed":
            seed_ico_candidates.extend(
                [
                    dep / "auraseed" / "assets" / "auraseed.ico",
                    dep / "assets" / "auraseed.ico",
                ]
            )
    seed_icon = _stage_icon_asset(seed_ico_candidates, assets_dir / "auraseed.ico")

    start_menu_shortcuts: List[str] = []
    if create_start_menu and primary_launcher:
        from auraseed.lifecycle import register_start_menu_launcher

        sprout_target = pythonw_exe if (pythonw_exe and sprout_pyw) else Path(primary_launcher)
        sprout_args = f'"{sprout_pyw}"' if (pythonw_exe and sprout_pyw) else "desktop"
        sprout_sm = register_start_menu_launcher(
            app_name="aurasprout",
            target_path=sprout_target,
            display_name="AuraSprout",
            arguments=sprout_args,
            working_dir=resolved_user_root,
            description="AuraSprout Application Catalog & Desktop Hub",
            icon_path=sprout_icon,
            subfolder="AuraSeed",
            start_menu_dir=start_menu_dir,
            refresh_cache=False,
        )
        if sprout_sm.get("shortcut_path"):
            start_menu_shortcuts.append(str(sprout_sm["shortcut_path"]))

        seed_target = pythonw_exe if (pythonw_exe and seed_pyw) else venv_py
        seed_args = f'"{seed_pyw}"' if (pythonw_exe and seed_pyw) else "-m auraseed gui"
        seed_sm = register_start_menu_launcher(
            app_name="auraseed",
            target_path=seed_target,
            display_name="AuraSeed",
            arguments=seed_args,
            working_dir=resolved_user_root,
            description="AuraSeed MVenv Manager & Runtime Discovery",
            icon_path=seed_icon,
            subfolder="AuraSeed",
            start_menu_dir=start_menu_dir,
            refresh_cache=True,
        )
        if seed_sm.get("shortcut_path"):
            start_menu_shortcuts.append(str(seed_sm["shortcut_path"]))

    registered_keys: List[str] = []
    registered_env_vars: List[str] = []
    path_updated = False
    if register_os:
        if is_win:
            import winreg
            from aurasprout import __version__ as _aurasprout_version

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_root_key) as rk:
                winreg.SetValueEx(rk, "InstallPath", 0, winreg.REG_SZ, str(target_dir))
                winreg.SetValueEx(rk, "UserRoot", 0, winreg.REG_SZ, str(resolved_user_root))
                winreg.SetValueEx(rk, "PythonExecutable", 0, winreg.REG_SZ, str(venv_py))
                winreg.SetValueEx(rk, "BinDir", 0, winreg.REG_SZ, str(bin_dir))
                winreg.SetValueEx(rk, "Version", 0, winreg.REG_SZ, _aurasprout_version)
            registered_keys.append(f"HKCU\\{registry_root_key}")

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, uninstall_reg_key) as uk:
                winreg.SetValueEx(uk, "DisplayName", 0, winreg.REG_SZ, "AuraSprout (User)")
                winreg.SetValueEx(uk, "DisplayVersion", 0, winreg.REG_SZ, _aurasprout_version)
                winreg.SetValueEx(uk, "Publisher", 0, winreg.REG_SZ, "AuraCore Dynamics")
                winreg.SetValueEx(uk, "InstallLocation", 0, winreg.REG_SZ, str(target_dir))
                winreg.SetValueEx(
                    uk,
                    "UninstallString",
                    0,
                    winreg.REG_SZ,
                    f'"{venv_py}" -m aurasprout self-uninstall --yes',
                )
            registered_keys.append(f"HKCU\\{uninstall_reg_key}")

            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    env_reg_subkey,
                    0,
                    winreg.KEY_READ | winreg.KEY_SET_VALUE,
                ) as ek:
                    winreg.SetValueEx(ek, "AURASPROUT_HOME", 0, winreg.REG_SZ, str(resolved_user_root))
                    winreg.SetValueEx(ek, "AURASPROUT_STANDALONE_DIR", 0, winreg.REG_SZ, str(target_dir))
                    registered_env_vars.extend(["AURASPROUT_HOME", "AURASPROUT_STANDALONE_DIR"])
            except OSError:
                pass

            if add_to_path:
                path_updated = _win_add_path_dir(bin_dir, env_subkey=env_reg_subkey)
        else:
            cfg_dir = Path.home() / ".config" / "aurasprout"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "installation.json").write_text(
                json.dumps(
                    {
                        "install_dir": str(target_dir),
                        "user_root": str(resolved_user_root),
                        "python_path": str(venv_py),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            registered_keys.append(str(cfg_dir / "installation.json"))

    receipt_path = resolved_user_root / "standalone_install.json"
    receipt = {
        "install_dir": str(target_dir),
        "bin_dir": str(bin_dir),
        "scripts_dir": str(venv_py.parent),
        "python_path": str(venv_py),
        "user_root": str(resolved_user_root),
        "cell_root": str(resolved_cell_root),
        "base_python": str(py_base),
        "launchers": launchers,
        "desktop_shortcut": str(shortcut_path) if shortcut_path else None,
        "start_menu_shortcuts": start_menu_shortcuts,
        "icons": {
            "aurasprout": str(sprout_icon) if sprout_icon else None,
            "auraseed": str(seed_icon) if seed_icon else None,
        },
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    # Smoke verification inside newly provisioned cleanroom interpreter
    verify = subprocess.run(
        [
            str(venv_py),
            "-c",
            "import aurasprout, auraseed; \ntry:\n import auragrid\nexcept ImportError:\n pass\nprint('CLEANROOM_OK')",
        ],
        capture_output=True,
        text=True,
        shell=False,
        timeout=60,
    )
    if verify.returncode != 0 or "CLEANROOM_OK" not in verify.stdout:
        raise RuntimeError(
            f"Standalone cleanroom verification failed: {verify.stderr or verify.stdout}"
        )

    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "registered_keys": registered_keys,
        "registered_env_vars": registered_env_vars,
        "path_updated": path_updated,
        "verified": True,
    }


def uninstall_self(
    *,
    remove_apps: bool = True,
    remove_mvenvs: bool = True,
    remove_package: bool = False,
    user_root: Optional[Path] = None,
    cell_root: Optional[Path] = None,
    dest_dir: Optional[Path] = None,
    desktop_dir: Optional[Path] = None,
    config_dir: Optional[Path] = None,
    posix_bin_dir: Optional[Path] = None,
    clean_os_registry: bool = True,
    registry_root_key: str = r"Software\AuraCore\AuraSprout",
    uninstall_reg_key: str = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AuraSprout",
    env_reg_subkey: str = "Environment",
    python_executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Full user/OS cleanup of AuraSprout (apps, mvenvs, registry, HKCU env, PATH, standalone venv, data)."""
    is_win = platform.system().lower() == "windows"
    resolved_user_root = Path(user_root).resolve() if user_root else default_user_data_root()
    resolved_cell_root = Path(cell_root).resolve() if cell_root else resolved_user_root
    paths = SproutPaths(cell_root=resolved_cell_root, user_root=resolved_user_root)

    removed_apps: List[str] = []
    removed_paths: List[str] = []
    cleaned_registry: List[str] = []
    cleaned_env_vars: List[str] = []
    path_cleaned = False
    path_dirs_to_strip: List[Path] = []
    install_dirs: List[Path] = []
    base_py_for_deferred: Optional[str] = None

    if dest_dir is not None:
        d = Path(dest_dir).resolve()
        install_dirs.append(d)
        path_dirs_to_strip.extend([d / "bin", d / "Scripts"])

    receipt_file = resolved_user_root / "standalone_install.json"
    if receipt_file.exists():
        try:
            rec = json.loads(receipt_file.read_text(encoding="utf-8"))
            if rec.get("install_dir"):
                install_dirs.append(Path(rec["install_dir"]).resolve())
            if rec.get("bin_dir"):
                path_dirs_to_strip.append(Path(rec["bin_dir"]).resolve())
            if rec.get("scripts_dir"):
                path_dirs_to_strip.append(Path(rec["scripts_dir"]).resolve())
            if rec.get("desktop_shortcut"):
                sc = Path(rec["desktop_shortcut"])
                if sc.exists():
                    sc.unlink()
                    removed_paths.append(str(sc))
            for sm_item in rec.get("start_menu_shortcuts", []) or []:
                sm_p = Path(str(sm_item))
                if sm_p.exists():
                    sm_p.unlink()
                    removed_paths.append(str(sm_p))
            if rec.get("base_python"):
                base_py_for_deferred = str(rec["base_python"])
        except (OSError, json.JSONDecodeError):
            pass

    if clean_os_registry and is_win:
        reg_inst = _win_read_registry_value(registry_root_key, "InstallPath")
        if reg_inst:
            reg_p = Path(reg_inst).resolve()
            install_dirs.append(reg_p)
            path_dirs_to_strip.extend([reg_p / "bin", reg_p / "Scripts"])
        reg_bin = _win_read_registry_value(registry_root_key, "BinDir")
        if reg_bin:
            path_dirs_to_strip.append(Path(reg_bin).resolve())

    # 1. Uninstall every registered application cleanly via SproutInstallerEngine before purging roots
    if remove_apps and paths.installed_registry_file.exists():
        engine = SproutInstallerEngine(paths=paths)
        for app_rec in list(engine.registry.list_all()):
            if engine.uninstall_app(app_rec.app_id, remove_mvenv=remove_mvenvs):
                removed_apps.append(app_rec.app_id)

    # 2. Clean Desktop & POSIX shims/config
    desktop_candidates = (
        [Path(desktop_dir).resolve()]
        if desktop_dir is not None
        else [resolve_user_desktop_dir(), Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"]
    )
    for d_dir in desktop_candidates:
        for cand in (d_dir / "aurasprout.cmd", d_dir / "aurasprout-aurasprout.desktop"):
            try:
                if cand.exists():
                    cand.unlink()
                    if str(cand) not in removed_paths:
                        removed_paths.append(str(cand))
            except OSError:
                pass

    try:
        from auraseed.lifecycle import refresh_os_shell_cache, resolve_user_start_menu_dir

        sm_dir = resolve_user_start_menu_dir()
        for sub in (sm_dir / "AuraSeed", sm_dir):
            if not sub.exists():
                continue
            for name in ("AuraSprout", "aurasprout", "AuraSeed", "auraseed"):
                for ext in (".lnk", ".cmd", ".desktop", ".command"):
                    cand = sub / f"{name}{ext}"
                    if cand.exists():
                        cand.unlink()
                        if str(cand) not in removed_paths:
                            removed_paths.append(str(cand))
        seed_folder = sm_dir / "AuraSeed"
        if seed_folder.exists() and seed_folder.is_dir() and not any(seed_folder.iterdir()):
            seed_folder.rmdir()
        refresh_os_shell_cache(sm_dir)
    except Exception:
        pass

    cfg_dir = Path(config_dir).resolve() if config_dir else (Path.home() / ".config" / "aurasprout")
    if cfg_dir.exists():
        shutil.rmtree(cfg_dir, ignore_errors=True)
        removed_paths.append(str(cfg_dir))

    bin_posix = Path(posix_bin_dir).resolve() if posix_bin_dir else (Path.home() / ".local" / "bin")
    if (bin_posix / "aurasprout").exists():
        try:
            (bin_posix / "aurasprout").unlink()
            removed_paths.append(str(bin_posix / "aurasprout"))
        except OSError:
            pass

    # 3. Clean OS registry & HKCU Environment
    if clean_os_registry and is_win:
        cleaned_registry.extend(_win_delete_registry_tree(registry_root_key))
        cleaned_registry.extend(_win_delete_registry_tree(uninstall_reg_key))
        vars_del, path_cleaned = _win_cleanup_hkcu_env(
            prefixes=("AURASPROUT_",),
            path_dirs_to_remove=path_dirs_to_strip,
            env_subkey=env_reg_subkey,
        )
        cleaned_env_vars.extend(vars_del)

    # 4. Remove standalone directories & data roots
    cur_py_norm = _norm_path_str(Path(sys.executable))
    targets: List[Path] = []
    seen_norms = set()
    for p in [*install_dirs, resolved_user_root, resolved_cell_root / ".sprout"]:
        rp = Path(p).resolve()
        n = _norm_path_str(rp)
        if n not in seen_norms:
            seen_norms.add(n)
            targets.append(rp)

    deferred_dirs: List[Path] = []
    for target in targets:
        if not target.exists():
            continue
        target_norm = _norm_path_str(target)
        # On Windows a running python.exe locks its own directory tree while executing
        if is_win and cur_py_norm.startswith(target_norm + os.sep):
            shutil.rmtree(target, ignore_errors=True)
            if target.exists():
                deferred_dirs.append(target)
        else:
            shutil.rmtree(target, ignore_errors=True)
            removed_paths.append(str(target))

    if deferred_dirs and base_py_for_deferred and Path(base_py_for_deferred).exists():
        dirs_literal = repr([str(d) for d in deferred_dirs])
        script = (
            "import shutil, time; "
            f"dirs = {dirs_literal}; "
            "time.sleep(0.5); "
            "[shutil.rmtree(d, ignore_errors=True) for d in dirs]"
        )
        kwargs: Dict[str, Any] = {"shell": False}
        if is_win:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([base_py_for_deferred, "-c", script], **kwargs)
        removed_paths.extend(str(d) for d in deferred_dirs)

    package_removed = False
    if remove_package:
        py = python_executable or sys.executable
        res = subprocess.run(
            [str(py), "-m", "pip", "uninstall", "-y", "aurasprout"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=120,
        )
        package_removed = res.returncode == 0

    return {
        "removed_apps": sorted(set(removed_apps)),
        "removed_paths": removed_paths,
        "cleaned_registry": cleaned_registry,
        "cleaned_env_vars": cleaned_env_vars,
        "path_cleaned": path_cleaned,
        "deferred_cleanup": [str(d) for d in deferred_dirs],
        "package_removed": package_removed,
    }
