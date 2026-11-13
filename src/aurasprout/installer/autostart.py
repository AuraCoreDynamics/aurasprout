"""Zero-privilege user-space launchers, desktop shortcuts, and autostart registration.

Uses strictly user-writable locations (`%APPDATA%`, `HKCU`, `~/.local/share`,
`~/.config/autostart`, `~/Library/LaunchAgents`) without `_winapi` or `ctypes.windll`.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
from pathlib import Path
from typing import Dict, List, Optional

from aurasprout.contracts.paths import validate_identifier

_LOG = logging.getLogger(__name__)


def _resolve_launcher_parts(python_exe: Path, cmd_rendered: str) -> tuple[str, str]:
    """Return `(executable_or_python, arguments)` handling `-m ...`, `python ...`, and console scripts."""
    stripped = (cmd_rendered or "").strip()
    if not stripped:
        return str(python_exe), ""
    if stripped.startswith("-m "):
        return str(python_exe), stripped

    try:
        tokens = shlex.split(stripped, posix=(platform.system().lower() != "windows"))
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return str(python_exe), ""

    first = tokens[0]
    rest = " ".join(tokens[1:]) if platform.system().lower() == "windows" else " ".join(shlex.quote(t) for t in tokens[1:])
    if Path(first).name.lower() in ("python", "python3", "python.exe"):
        return str(python_exe), rest

    scripts_dir = Path(python_exe).resolve().parent
    is_win = platform.system().lower() == "windows"
    script_candidates = (
        [scripts_dir / f"{first}.exe", scripts_dir / f"{first}.cmd", scripts_dir / f"{first}.bat", scripts_dir / first]
        if is_win
        else [scripts_dir / first]
    )
    for cand in script_candidates:
        if cand.exists() and cand.is_file():
            return str(cand), rest

    return str(python_exe), f"-m {stripped}"


def generate_launchers(
    install_dir: Path,
    app_id: str,
    python_exe: Path,
    entrypoint_command: str,
    port: int = 8000,
    mvenv_root: Path | None = None,
) -> Dict[str, str]:
    """Generate native user-space launcher scripts (`bat`/`ps1` on Windows, `sh` on POSIX)."""
    safe_id = validate_identifier(app_id, "appId")
    target_dir = Path(install_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    py_path = Path(python_exe).resolve()
    py_str = str(py_path)
    venv_root_str = str(Path(mvenv_root).resolve()) if mvenv_root else str(target_dir)

    cmd_rendered = (entrypoint_command or f"{safe_id}").replace("{port}", str(port))
    exec_bin, exec_args = _resolve_launcher_parts(py_path, cmd_rendered)
    args_suffix = f" {exec_args}" if exec_args else ""
    launchers: Dict[str, str] = {}

    if platform.system().lower() == "windows":
        bat_path = target_dir / f"run-{safe_id}.bat"
        bat_content = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "VIRTUAL_ENV={venv_root_str}"\r\n'
            f'set "PATH={Path(py_str).parent};%PATH%"\r\n'
            f'"{exec_bin}"{args_suffix} %*\r\n'
        )
        bat_path.write_text(bat_content, encoding="utf-8")
        launchers["bat"] = str(bat_path)

        ps1_path = target_dir / f"run-{safe_id}.ps1"
        ps1_content = (
            f'$env:VIRTUAL_ENV = "{venv_root_str}"\n'
            f'$env:PATH = "{Path(py_str).parent};$env:PATH"\n'
            f'& "{exec_bin}"{args_suffix} @args\n'
        )
        ps1_path.write_text(ps1_content, encoding="utf-8")
        launchers["ps1"] = str(ps1_path)
    else:
        sh_path = target_dir / f"run-{safe_id}.sh"
        sh_tokens = " ".join(shlex.quote(tok) for tok in cmd_rendered.split()) if exec_args == f"-m {cmd_rendered.strip()}" else exec_args
        sh_suffix = f" {sh_tokens}" if sh_tokens else ""
        sh_content = (
            "#!/bin/sh\n"
            f'export VIRTUAL_ENV={shlex.quote(venv_root_str)}\n'
            f'export PATH={shlex.quote(str(Path(py_str).parent))}:"$PATH"\n'
            f'exec {shlex.quote(exec_bin)}{sh_suffix} "$@"\n'
        )
        sh_path.write_text(sh_content, encoding="utf-8")
        try:
            os.chmod(sh_path, 0o755)
        except OSError as exc:
            _LOG.debug("Could not chmod launcher %s: %s", sh_path, exc)
        launchers["sh"] = str(sh_path)

    return launchers


def resolve_user_desktop_dir(desktop_dir: Optional[Path] = None) -> Path:
    """Resolve user's Desktop directory, respecting Windows folder redirection (e.g. OneDrive)."""
    if desktop_dir is not None:
        return Path(desktop_dir).resolve()

    home = Path.home()
    if platform.system().lower() == "windows":
        import winreg

        for subkey in (
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ) as rk:
                    raw_val, _ = winreg.QueryValueEx(rk, "Desktop")
                    expanded = os.path.expandvars(str(raw_val or "").strip())
                    if expanded:
                        cand = Path(expanded)
                        if cand.exists():
                            return cand.resolve()
            except OSError:
                continue

        onedrive_desktop = home / "OneDrive" / "Desktop"
        if (home / "Desktop").exists():
            return (home / "Desktop").resolve()
        if onedrive_desktop.exists():
            return onedrive_desktop.resolve()

    return (home / "Desktop").resolve()


def create_user_start_menu_shortcut(
    app_id: str,
    display_name: str,
    launcher_path: Path,
    working_dir: Optional[Path] = None,
    description: str = "",
    arguments: str = "",
    subfolder: str = "AuraSeed",
    start_menu_dir: Optional[Path] = None,
    refresh_cache: bool = True,
) -> Optional[Path]:
    """Register a user-space Start Menu shortcut via AuraSeed and refresh OS shell cache."""
    from auraseed.lifecycle import register_start_menu_launcher

    safe_id = validate_identifier(app_id, "appId")
    res = register_start_menu_launcher(
        app_name=safe_id,
        target_path=Path(launcher_path).resolve(),
        display_name=display_name or safe_id,
        arguments=arguments,
        working_dir=working_dir,
        description=description or display_name or safe_id,
        subfolder=subfolder,
        start_menu_dir=start_menu_dir,
        refresh_cache=refresh_cache,
    )
    sc = res.get("shortcut_path")
    return Path(sc) if sc else None


def create_user_desktop_shortcut(
    app_id: str,
    display_name: str,
    launcher_path: Path,
    desktop_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Create a user-space desktop shortcut without COM/`_winapi`/`ctypes`."""
    safe_id = validate_identifier(app_id, "appId")
    desktop = resolve_user_desktop_dir(desktop_dir)
    if not desktop.exists():
        return None

    sys_name = platform.system().lower()
    if sys_name == "windows":
        cmd_shortcut = desktop / f"{safe_id}.cmd"
        cmd_shortcut.write_text(
            f'@echo off\r\ncall "{Path(launcher_path).resolve()}" %*\r\n',
            encoding="utf-8",
        )
        return cmd_shortcut

    desktop_entry = desktop / f"aurasprout-{safe_id}.desktop"
    desktop_entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={display_name or safe_id}\n"
        f"Exec={shlex.quote(str(Path(launcher_path).resolve()))}\n"
        "Terminal=false\n",
        encoding="utf-8",
    )
    try:
        os.chmod(desktop_entry, 0o755)
    except OSError as exc:
        _LOG.debug("Could not chmod desktop file %s: %s", desktop_entry, exc)
    return desktop_entry


def configure_user_autostart(
    app_id: str,
    command: List[str],
    enabled: bool = True,
    autostart_dir: Path | None = None,
) -> Optional[Path]:
    """Enable or disable opt-in user-space login autostart for an app."""
    safe_id = validate_identifier(app_id, "appId")
    sys_name = platform.system().lower()
    joined = " ".join(shlex.quote(c) for c in command)

    if sys_name == "windows":
        if autostart_dir is None:
            appdata = os.environ.get("APPDATA")
            target_dir = (
                Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                if appdata
                else (Path.home() / ".config" / "aurasprout" / "autostart")
            )
        else:
            target_dir = Path(autostart_dir).resolve()
        startup_script = target_dir / f"aurasprout-{safe_id}.cmd"
        if not enabled:
            if startup_script.exists():
                startup_script.unlink()
            return None
        target_dir.mkdir(parents=True, exist_ok=True)
        startup_script.write_text(f"@echo off\r\nstart \"\" /B {joined}\r\n", encoding="utf-8")
        return startup_script

    if sys_name == "darwin":
        agents_dir = (
            Path(autostart_dir).resolve()
            if autostart_dir
            else (Path.home() / "Library" / "LaunchAgents")
        )
        plist_path = agents_dir / f"io.aurasprout.{safe_id}.plist"
        if not enabled:
            if plist_path.exists():
                plist_path.unlink()
            return None
        agents_dir.mkdir(parents=True, exist_ok=True)
        args_xml = "\n    ".join(f"<string>{c}</string>" for c in command)
        plist_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            "<plist version=\"1.0\"><dict>\n"
            f"  <key>Label</key><string>io.aurasprout.{safe_id}</string>\n"
            f"  <key>ProgramArguments</key><array>\n    {args_xml}\n  </array>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "</dict></plist>\n",
            encoding="utf-8",
        )
        return plist_path

    xdg_autostart = (
        Path(autostart_dir).resolve()
        if autostart_dir
        else (Path.home() / ".config" / "autostart")
    )
    desktop_path = xdg_autostart / f"aurasprout-{safe_id}.desktop"
    if not enabled:
        if desktop_path.exists():
            desktop_path.unlink()
        return None
    xdg_autostart.mkdir(parents=True, exist_ok=True)
    desktop_path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name=AuraSprout {safe_id}\n"
        f"Exec={joined}\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return desktop_path

