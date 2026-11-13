"""Zero-privilege user-space launchers, desktop shortcuts, and autostart registration.

Uses strictly user-writable locations (`%APPDATA%`, `HKCU`, `~/.local/share`,
`~/.config/autostart`, `~/Library/LaunchAgents`) without `_winapi` or `ctypes.windll`.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from aurasprout.contracts.paths import validate_identifier

_LOG = logging.getLogger(__name__)


def _resolve_launcher_parts(python_exe: Path, cmd_rendered: str) -> tuple[str, str]:
    """Return `(executable_or_python, arguments)` handling `-m/-c`, `.py`, `python ...`, and console scripts."""
    stripped = (cmd_rendered or "").strip()
    if not stripped:
        return str(python_exe), ""
    if stripped.startswith(("-m ", "-c ")):
        return str(python_exe), stripped

    try:
        tokens = shlex.split(stripped, posix=(platform.system().lower() != "windows"))
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return str(python_exe), ""

    first = tokens[0]
    first_unquoted = first[1:-1] if len(first) >= 2 and first[0] == first[-1] and first[0] in ('"', "'") else first
    rest = " ".join(tokens[1:]) if platform.system().lower() == "windows" else " ".join(shlex.quote(t) for t in tokens[1:])
    if Path(first_unquoted).name.lower() in ("python", "python3", "python.exe"):
        return str(python_exe), rest
    if first_unquoted.lower().endswith(".py"):
        return str(python_exe), stripped

    scripts_dir = Path(python_exe).resolve().parent
    is_win = platform.system().lower() == "windows"
    script_candidates = (
        [
            scripts_dir / f"{first_unquoted}.exe",
            scripts_dir / f"{first_unquoted}.cmd",
            scripts_dir / f"{first_unquoted}.bat",
            scripts_dir / first_unquoted,
        ]
        if is_win
        else [scripts_dir / first_unquoted]
    )
    for cand in script_candidates:
        if cand.exists() and cand.is_file():
            return str(cand), rest

    return str(python_exe), f"-m {stripped}"


def resolve_entry_point_invocation(
    python_exe: Path,
    entry_command: str,
    port: int = 8000,
    mvenv_root: Optional[Path] = None,
    extra_args: Optional[List[str]] = None,
) -> tuple[List[str], Dict[str, str]]:
    """Build argv list (`shell=False`) and MVenv environment variables for launching an entry point."""
    py_path = Path(python_exe).resolve()
    cmd_rendered = (entry_command or "").replace("{port}", str(port)).strip()
    exec_bin, exec_args = _resolve_launcher_parts(py_path, cmd_rendered)
    posix_split = platform.system().lower() != "windows"
    arg_tokens: List[str] = []
    if exec_args:
        try:
            arg_tokens = shlex.split(exec_args, posix=posix_split)
        except ValueError:
            arg_tokens = exec_args.split()
        # Strip literal outer quotes preserved by Windows shlex.split(posix=False)
        if not posix_split:
            arg_tokens = [
                t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'") else t
                for t in arg_tokens
            ]
    if extra_args:
        arg_tokens.extend(str(a) for a in extra_args)

    venv_dir = str(Path(mvenv_root).resolve()) if mvenv_root else str(py_path.parent.parent)
    scripts_dir = str(py_path.parent)
    existing_path = os.environ.get("PATH", "")
    env_updates = {
        "VIRTUAL_ENV": venv_dir,
        "PATH": f"{scripts_dir}{os.pathsep}{existing_path}" if existing_path else scripts_dir,
        "AURASPROUT_PORT": str(port),
    }
    return [exec_bin, *arg_tokens], env_updates


def windowless_creationflags() -> int:
    """Return Windows `CREATE_NO_WINDOW` creationflag (or `0` on non-Windows)."""
    if platform.system().lower() == "windows":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def resolve_windowless_python(python_exe: Path) -> Optional[Path]:
    """Return sibling `pythonw.exe` next to `python_exe` on Windows when present."""
    if platform.system().lower() != "windows":
        return None
    py = Path(python_exe).resolve()
    cand = py.with_name("pythonw.exe")
    if cand.exists() and cand.is_file():
        return cand
    return None


def write_windowless_pyw_launcher(
    pyw_path: Path,
    python_exe: Path,
    entry_command: str,
    port: int = 8000,
    mvenv_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Path:
    """Generate a `.pyw` windowless Python launcher that suppresses console windows cleanly."""
    py_path = Path(python_exe).resolve()
    cmd_rendered = (entry_command or "").replace("{port}", str(port)).strip()
    exec_bin, exec_args = _resolve_launcher_parts(py_path, cmd_rendered)
    argv, env_updates = resolve_entry_point_invocation(
        python_exe=py_path,
        entry_command=entry_command,
        port=port,
        mvenv_root=mvenv_root,
    )
    venv_dir = env_updates["VIRTUAL_ENV"]
    scripts_dir = str(py_path.parent)
    resolved_log = str(Path(log_path).resolve()) if log_path else ""
    extra_defaults = {str(k): str(v) for k, v in (extra_env or {}).items()}
    is_python_host = Path(exec_bin).resolve() == py_path
    script_stem = Path(exec_bin).stem
    arg_list = list(argv[1:])

    pyw_target = Path(pyw_path).resolve()
    pyw_target.parent.mkdir(parents=True, exist_ok=True)
    pyw_code = f'''"""Auto-generated windowless GUI launcher by AuraSprout."""
from __future__ import annotations

import importlib.metadata
import os
import runpy
import subprocess
import sys
from pathlib import Path

os.environ["VIRTUAL_ENV"] = {venv_dir!r}
_scripts_dir = {scripts_dir!r}
_cur_path = os.environ.get("PATH", "")
os.environ["PATH"] = _scripts_dir + os.pathsep + _cur_path if _cur_path else _scripts_dir
os.environ["AURASPROUT_PORT"] = {str(port)!r}
for _k, _v in {extra_defaults!r}.items():
    os.environ.setdefault(_k, _v)

if sys.stdout is None or sys.stderr is None:
    _log_target = {resolved_log!r}
    try:
        if _log_target:
            Path(_log_target).parent.mkdir(parents=True, exist_ok=True)
            _stream = open(_log_target, "a", encoding="utf-8", buffering=1)
        else:
            _stream = open(os.devnull, "a", encoding="utf-8")
    except OSError:
        _stream = open(os.devnull, "a", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _stream
    if sys.stderr is None:
        sys.stderr = _stream

_args = {arg_list!r} + list(sys.argv[1:])
if {is_python_host!r}:
    if _args and _args[0] == "-m" and len(_args) >= 2:
        _mod = _args[1]
        sys.argv = [_mod] + _args[2:]
        runpy.run_module(_mod, run_name="__main__", alter_sys=True)
    elif _args and _args[0] == "-c" and len(_args) >= 2:
        sys.argv = ["-c"] + _args[2:]
        exec(compile(_args[1], "<aurasprout-pyw>", "exec"), {{"__name__": "__main__"}})
    elif _args:
        sys.argv = list(_args)
        runpy.run_path(_args[0], run_name="__main__")
else:
    _eps = list(importlib.metadata.entry_points(group="console_scripts", name={script_stem!r}))
    if not _eps:
        _eps = list(importlib.metadata.entry_points(group="gui_scripts", name={script_stem!r}))
    if _eps:
        sys.argv = [{exec_bin!r}] + _args
        _ret = _eps[0].load()()
        if isinstance(_ret, int) and _ret != 0:
            raise SystemExit(_ret)
    else:
        _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        raise SystemExit(subprocess.call([{exec_bin!r}] + _args, creationflags=_flags))
'''
    pyw_target.write_text(pyw_code, encoding="utf-8")
    return pyw_target


def generate_launchers(
    install_dir: Path,
    app_id: str,
    python_exe: Path,
    entrypoint_command: str,
    port: int = 8000,
    mvenv_root: Path | None = None,
) -> Dict[str, str]:
    """Generate native user-space launcher scripts (`bat`/`ps1`/`pyw` on Windows, `sh` on POSIX)."""
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

        app_data_dir = target_dir / "app_data"
        log_file = (app_data_dir if app_data_dir.exists() else target_dir) / f"{safe_id}-launch.log"
        pyw_path = write_windowless_pyw_launcher(
            pyw_path=target_dir / f"launch-{safe_id}.pyw",
            python_exe=py_path,
            entry_command=entrypoint_command or safe_id,
            port=port,
            mvenv_root=mvenv_root,
            log_path=log_file,
        )
        launchers["pyw"] = str(pyw_path)
        pythonw_exe = resolve_windowless_python(py_path)
        if pythonw_exe is not None:
            launchers["pythonw"] = str(pythonw_exe)
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
    icon_path: Optional[Path] = None,
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
        icon_path=icon_path,
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
    pythonw_path: Optional[Path] = None,
) -> Optional[Path]:
    """Create a user-space desktop shortcut without COM/`_winapi`/`ctypes`."""
    safe_id = validate_identifier(app_id, "appId")
    desktop = resolve_user_desktop_dir(desktop_dir)
    if not desktop.exists():
        return None

    sys_name = platform.system().lower()
    if sys_name == "windows":
        cmd_shortcut = desktop / f"{safe_id}.cmd"
        resolved_launcher = Path(launcher_path).resolve()
        if resolved_launcher.suffix.lower() == ".pyw" and pythonw_path is not None:
            cmd_shortcut.write_text(
                f'@echo off\r\nstart "" /B "{Path(pythonw_path).resolve()}" "{resolved_launcher}" %*\r\n',
                encoding="utf-8",
            )
        else:
            cmd_shortcut.write_text(
                f'@echo off\r\ncall "{resolved_launcher}" %*\r\n',
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

