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
from typing import Dict, List

from aurasprout.contracts.paths import validate_identifier

_LOG = logging.getLogger(__name__)


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
    py_str = str(Path(python_exe).resolve())
    venv_root_str = str(Path(mvenv_root).resolve()) if mvenv_root else str(target_dir)

    cmd_rendered = (entrypoint_command or f"{safe_id}").replace("{port}", str(port))
    launchers: Dict[str, str] = {}

    if platform.system().lower() == "windows":
        bat_path = target_dir / f"run-{safe_id}.bat"
        bat_content = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'set "VIRTUAL_ENV={venv_root_str}"\r\n'
            f'set "PATH={Path(py_str).parent};%PATH%"\r\n'
            f'"{py_str}" -m {cmd_rendered} %*\r\n'
        )
        bat_path.write_text(bat_content, encoding="utf-8")
        launchers["bat"] = str(bat_path)

        ps1_path = target_dir / f"run-{safe_id}.ps1"
        ps1_content = (
            f'$env:VIRTUAL_ENV = "{venv_root_str}"\n'
            f'$env:PATH = "{Path(py_str).parent};$env:PATH"\n'
            f'& "{py_str}" -m {cmd_rendered} @args\n'
        )
        ps1_path.write_text(ps1_content, encoding="utf-8")
        launchers["ps1"] = str(ps1_path)
    else:
        sh_path = target_dir / f"run-{safe_id}.sh"
        sh_tokens = " ".join(shlex.quote(tok) for tok in cmd_rendered.split())
        sh_content = (
            "#!/bin/sh\n"
            f'export VIRTUAL_ENV={shlex.quote(venv_root_str)}\n'
            f'export PATH={shlex.quote(str(Path(py_str).parent))}:"$PATH"\n'
            f'exec {shlex.quote(py_str)} -m {sh_tokens} "$@"\n'
        )
        sh_path.write_text(sh_content, encoding="utf-8")
        try:
            os.chmod(sh_path, 0o755)
        except OSError as exc:
            _LOG.debug("Could not chmod launcher %s: %s", sh_path, exc)
        launchers["sh"] = str(sh_path)

    return launchers


def create_user_desktop_shortcut(
    app_id: str,
    display_name: str,
    launcher_path: Path,
    desktop_dir: Path | None = None,
) -> Path | None:
    """Create a user-space desktop shortcut without COM/`_winapi`/`ctypes`."""
    safe_id = validate_identifier(app_id, "appId")
    desktop = Path(desktop_dir).resolve() if desktop_dir else (Path.home() / "Desktop")
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

