---
name: aurasprout
description: >-
  Zero-privilege user-space application distribution, catalog hosting, local supervision,
  and clean install/uninstall lifecycle for AuraCore applications using AuraSprout.
---

# AuraSprout: User-Space Application Hub & Installer

**AuraSprout** is the zero-privilege application packager (`.apkg`), catalog builder, non-admin installer, and local cell coordinator for AuraCore applications (such as `aurarouter`, `auracode`, `bijou`, and `auraseed`).

---

## 1. Zero-Configuration Bootstrappers (No `PYTHONPATH` Required)

From a source checkout, invoke the root Level-0 bootstrapper for your OS:

```powershell
# Windows PowerShell
& ".\aurasprout.ps1" self-install --add-to-path --shortcut --json
```

```bash
# Linux / macOS
./aurasprout.sh self-install --add-to-path --shortcut --json
```

Once installed, invoke `aurasprout` directly or via its standalone launcher (`%LOCALAPPDATA%\AuraSprout\standalone\bin\aurasprout.ps1` on Windows, `bin/aurasprout` on POSIX).

---

## 2. Clean User & OS Reset / Uninstall

Always pass `--yes --json` for unattended agent execution:

```powershell
# Full cleanup of AuraSprout, installed apps, mvenvs, shortcuts, registry, and PATH
aurasprout self-uninstall --yes --json
# Or via root bootstrapper:
& ".\aurasprout.ps1" self-uninstall --yes --json

# Clean up a single installed app and its sovereign mvenv
aurasprout cleanup app <appId> --json
```

---

## 3. Common Agent Workflows (`--json` Recommended)

| Task | Command |
| :--- | :--- |
| **Standalone self-install** | `aurasprout self-install --add-to-path --shortcut --json` |
| **Standalone self-uninstall** | `aurasprout self-uninstall --yes --json` |
| **Install from local repo or `.apkg`** | `aurasprout install <path_or_apkg> --shortcut --json` |
| **Attach live workspace** | `aurasprout attach <workspace_dir> --json` |
| **Install from catalog** | `aurasprout install <appId> --from-catalog <url_or_dir> --channel stable --json` |
| **Package app into `.apkg`** | `aurasprout package <app_dir> --out ./dist --json` |
| **List installed apps** | `aurasprout catalog list --installed --json` |
| **Rollback app version** | `aurasprout rollback <appId> --json` |
| **Uninstall app + mvenv** | `aurasprout uninstall <appId> --remove-mvenv --json` |
| **Cleanup single app (`cleanup` / `app`)** | `aurasprout cleanup app <appId> --json` |
| **Cleanup AuraSprout (`cleanup` / `self`)** | `aurasprout cleanup self --yes --json` |
| **Start Management Web UI (`ui`)** | `aurasprout ui --port 8765 --json` |
| **Launch Tkinter Desktop Hub (`desktop`)** | `aurasprout desktop` |
| **Start Tier 2 IPC Bridge (`ipc`)** | `aurasprout ipc --port 0` |

### CLI Subcommand Reference

- `self-install`: Cleanroom standalone user-space installation of AuraSprout.
- `self-uninstall`: Full cleanup/uninstall of AuraSprout user & OS state.
- `app`: Target an installed application under cleanup (`aurasprout app` / `aurasprout cleanup app <appId> --json`).
- `cleanup` (`app`, `self`): Clean up an installed `app` (`aurasprout cleanup app <appId>`) or AuraSprout `self` (`aurasprout cleanup self --yes`).
- `ui`: Start Tier 3 loopback Management Web UI server.
- `desktop`: Launch AuraSprout Tkinter Desktop Hub (composing AuraSeed).
- `ipc`: Start standalone Tier 2 loopback IPC bridge server and SQLite event store (`ops.logs.error`, `ops.lifecycle.crash`, `ops.perf.anomaly`).

---

## 4. Storage Locations by OS

- **Override**: `$AURASPROUT_HOME`
- **Windows**: `%LOCALAPPDATA%\AuraSprout` (+ `HKCU\Software\AuraCore\AuraSprout`, `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\AuraSprout`, Start Menu `AuraSeed\`)
- **macOS**: `~/Library/Application Support/aurasprout`
- **Linux**: `$XDG_DATA_HOME/aurasprout` or `~/.local/share/aurasprout`

