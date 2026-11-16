"""Builder for OS-native self-contained AuraSprout setup bundles.

Produces ready-to-run zero-privilege installer archives for Windows (and Unix):
- Contains native bootstrappers (setup.bat, setup.ps1, setup.sh)
- Bundles full source trees for `aurasprout` and `auraseed`
- Uses AuraSeed discovery to find or provision a Python runtime
- Installs AuraSprout into `%LOCALAPPDATA%\\AuraSprout\\standalone`
- Generates desktop shortcuts, start menu entries, and desktop hub launcher
- Safely handles atomic self-updating to avoid file-locking on active processes
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

from aurasprout.catalog.crypto import compute_sha256

_LOG = logging.getLogger(__name__)

SETUP_BAT_TEMPLATE = """@echo off
setlocal enabledelayedexpansion
title AuraSprout Zero-Privilege Installer

echo =======================================================
echo   AuraSprout OS-Native User-Space Installer
echo   Zero-Privilege Python Application Hub
echo =======================================================
echo.

set "SCRIPT_DIR=%~dp0"
set "FOUND_PYTHON="

:: 1. Search PATH for valid Python
for %%X in (python.exe) do (
    if exist "%%~$PATH:X" (
        for %%F in ("%%~$PATH:X") do (
            if %%~zF gtr 0 (
                "%%~$PATH:X" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
                if !errorlevel! equ 0 (
                    set "FOUND_PYTHON=%%~$PATH:X"
                    goto :RunInstaller
                )
            )
        )
    )
)

:: 2. Search common user & system install paths
set "CANDIDATE_PATHS=%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe %LOCALAPPDATA%\\Programs\\Python\\Python311\\python.exe %LOCALAPPDATA%\\Programs\\Python\\Python310\\python.exe %PROGRAMFILES%\\Python312\\python.exe %PROGRAMFILES%\\Python311\\python.exe %PROGRAMFILES%\\Python310\\python.exe"

for %%P in (%CANDIDATE_PATHS%) do (
    if exist "%%P" (
        "%%P" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
        if !errorlevel! equ 0 (
            set "FOUND_PYTHON=%%P"
            goto :RunInstaller
        )
    )
)

:: 3. Check existing AuraSeed bootstrapped runtimes
set "USER_BOOTSTRAP=%LOCALAPPDATA%\\AuraSeed\\base_python\\python.exe"
if exist "%USER_BOOTSTRAP%" (
    set "FOUND_PYTHON=%USER_BOOTSTRAP%"
    goto :RunInstaller
)

:: 4. If no Python >= 3.10 is found, delegate to PowerShell installer with automated bootstrap
if exist "%SCRIPT_DIR%setup.ps1" (
    echo [AuraSprout] No Python 3.10+ detected on PATH. Delegating to PowerShell bootstrapper...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup.ps1" %*
    exit /b !errorlevel!
)

echo [AuraSprout Error] No Python 3.10+ runtime found. Please install Python or PowerShell.
pause
exit /b 1

:RunInstaller
echo [AuraSprout] Using Python interpreter: !FOUND_PYTHON!
"%FOUND_PYTHON%" "%SCRIPT_DIR%setup.py" %*
if !errorlevel! equ 0 (
    echo.
    echo =======================================================
    echo   AuraSprout installation completed successfully!
    echo =======================================================
) else (
    echo.
    echo [AuraSprout Error] Installation failed with exit code !errorlevel!
)
pause
exit /b !errorlevel!
"""

SETUP_PS1_TEMPLATE = """# AuraSprout OS-Native PowerShell Installer
[CmdletBinding()]
param(
    [string]$Dest = "$env:LOCALAPPDATA\\AuraSprout\\standalone",
    [switch]$NoShortcut,
    [switch]$AddToPath
)

$ErrorActionPreference = 'Stop'
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  AuraSprout Zero-Privilege Installer (PowerShell)" -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. Discover Python using PATH and standard locations
$PythonCandidates = @(
    "python.exe",
    "$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe",
    "$env:LOCALAPPDATA\\Programs\\Python\\Python311\\python.exe",
    "$env:LOCALAPPDATA\\Programs\\Python\\Python310\\python.exe",
    "$env:ProgramFiles\\Python312\\python.exe",
    "$env:ProgramFiles\\Python311\\python.exe",
    "$env:ProgramFiles\\Python310\\python.exe",
    "$env:LOCALAPPDATA\\AuraSeed\\base_python\\python.exe"
)

$ValidPython = $null
foreach ($cand in $PythonCandidates) {
    try {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        $p = if ($cmd) { $cmd.Source } else { $cand }
        if (Test-Path $p) {
            $test = & $p -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $ValidPython = $p
                break
            }
        }
    } catch {}
}

# 2. Automated bootstrap fallback if Python is missing
if (-not $ValidPython) {
    Write-Host "[AuraSprout] No Python 3.10+ found. Bootstrapping user-space Python runtime..." -ForegroundColor Yellow
    $BootstrapTarget = "$env:LOCALAPPDATA\\AuraSeed\\base_python"
    New-Item -ItemType Directory -Force -Path $BootstrapTarget | Out-Null
    
    # Standalone embeddable python download
    $PyVer = "3.12.8"
    $ZipUrl = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip"
    $ZipDest = "$BootstrapTarget\\python-embed.zip"
    Write-Host "[AuraSprout] Downloading $ZipUrl..." -ForegroundColor Gray
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipDest
    Expand-Archive -Path $ZipDest -DestinationPath $BootstrapTarget -Force
    Remove-Item -Path $ZipDest -Force -ErrorAction SilentlyContinue
    
    # Enable site-packages in embeddable zip
    $PthFile = Get-ChildItem -Path $BootstrapTarget -Filter "*._pth" | Select-Object -First 1
    if ($PthFile) {
        Add-Content -Path $PthFile.FullName -Value "`nimport site"
    }
    
    $ValidPython = "$BootstrapTarget\\python.exe"
}

Write-Host "[AuraSprout] Executing installer with $ValidPython..." -ForegroundColor Green
$SetupPy = Join-Path $ScriptDir "setup.py"

$ArgsList = @($SetupPy, "--dest", $Dest)
if (-not $NoShortcut) { $ArgsList += "--shortcut" }
if ($AddToPath) { $ArgsList += "--add-to-path" }

& $ValidPython @ArgsList
if ($LASTEXITCODE -eq 0) {
    Write-Host "`n[AuraSprout] Installation completed successfully!" -ForegroundColor Green
} else {
    Write-Host "`n[AuraSprout Error] Installation failed with exit code $LASTEXITCODE" -ForegroundColor Red
}
"""

SETUP_SH_TEMPLATE = """#!/usr/bin/env bash
set -e

echo "======================================================="
echo "  AuraSprout OS-Native User-Space Installer (Unix)"
echo "======================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FOUND_PYTHON=""

for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
            FOUND_PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$FOUND_PYTHON" ]; then
    echo "[AuraSprout Error] Python 3.10+ is required but not found in PATH."
    exit 1
fi

echo "[AuraSprout] Using Python interpreter: $($FOUND_PYTHON -c 'import sys; print(sys.executable)')"
"$FOUND_PYTHON" "$SCRIPT_DIR/setup.py" "$@"
"""

SETUP_PY_TEMPLATE = """\"\"\"AuraSprout Standalone Setup Script (Runs within the unpacked installer).\"\"\"
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Add bundled packages to PYTHONPATH
SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLED_DIR = SCRIPT_DIR / "packages"
if (BUNDLED_DIR / "aurasprout" / "src").is_dir():
    sys.path.insert(0, str(BUNDLED_DIR / "aurasprout" / "src"))
if (BUNDLED_DIR / "auraseed").is_dir():
    sys.path.insert(0, str(BUNDLED_DIR / "auraseed"))

from aurasprout.installer.self_install import install_standalone

def main() -> int:
    parser = argparse.ArgumentParser(description="AuraSprout Standalone Installer")
    parser.add_argument("--dest", default=None, help="Target standalone directory")
    parser.add_argument("--shortcut", action="store_true", default=True, help="Create desktop shortcut")
    parser.add_argument("--no-shortcut", dest="shortcut", action="store_false")
    parser.add_argument("--add-to-path", action="store_true", default=True, help="Add to user PATH")
    parser.add_argument("--no-add-to-path", dest="add_to_path", action="store_false")
    args = parser.parse_args()

    sprout_src = BUNDLED_DIR / "aurasprout"
    seed_src = BUNDLED_DIR / "auraseed"

    print("[AuraSprout] Installing standalone environment...")
    result = install_standalone(
        dest_dir=Path(args.dest) if args.dest else None,
        source_dir=sprout_src,
        dep_sources=[seed_src] if seed_src.is_dir() else None,
        add_to_path=args.add_to_path,
        create_shortcut=args.shortcut,
        register_os=True,
    )

    print(f"[AuraSprout] Installed to: {result.get('install_dir')}")
    print(f"[AuraSprout] Desktop Shortcut: {result.get('desktop_shortcut')}")
    print("[AuraSprout] Setup complete! You can now launch AuraSprout from your desktop or start menu.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""


def build_native_installer_zip(
    out_dir: Path,
    aurasprout_dir: Path,
    auraseed_dir: Path,
    version: str = "0.1.1",
) -> Path:
    """Build a zero-privilege, ready-to-run `aurasprout-<version>-installer.zip` bundle."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"aurasprout-{version}-installer.zip"

    stage_dir = Path(tempfile.mkdtemp(prefix="sprout_bundle_"))
    try:
        # Write setup scripts
        (stage_dir / "setup.bat").write_text(SETUP_BAT_TEMPLATE, encoding="utf-8")
        (stage_dir / "setup.ps1").write_text(SETUP_PS1_TEMPLATE, encoding="utf-8")
        (stage_dir / "setup.sh").write_text(SETUP_SH_TEMPLATE, encoding="utf-8")
        (stage_dir / "setup.py").write_text(SETUP_PY_TEMPLATE, encoding="utf-8")

        # Copy bundled packages
        pkg_dir = stage_dir / "packages"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # aurasprout
        dest_sprout = pkg_dir / "aurasprout"
        shutil.copytree(
            aurasprout_dir,
            dest_sprout,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git", ".pytest_cache", ".venv", "venv", "dist", "build"
            ),
        )

        # auraseed
        dest_seed = pkg_dir / "auraseed"
        shutil.copytree(
            auraseed_dir,
            dest_seed,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git", ".pytest_cache", ".venv", "venv", "dist", "build"
            ),
        )

        # Create zip archive
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(stage_dir.rglob("*")):
                if item.is_file():
                    arcname = item.relative_to(stage_dir).as_posix()
                    zf.write(item, arcname)

        sha256 = compute_sha256(zip_path)
        sha_file = Path(str(zip_path) + ".sha256")
        sha_file.write_text(f"{sha256} *{zip_path.name}\n", encoding="utf-8")

        _LOG.info("Built native installer archive: %s (SHA-256: %s)", zip_path, sha256)
        return zip_path
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

