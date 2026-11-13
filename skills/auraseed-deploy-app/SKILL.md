---
name: auraseed-deploy-app
description: >-
  Deploy, install, update, rollback, package, and manage applications using the AuraSprout
  application installer engine (`app.manifest.json`).
  Covers zero-privilege user-space installs, standalone self-install/cleanup, MVenv relinking, and launchers.
---

# AuraSprout Application Deployment & Lifecycle

AuraSprout installs and manages AuraCore applications strictly in user-writable storage without requiring administrator privileges:
- **Override**: `$AURASPROUT_HOME`
- **Windows**: `%LOCALAPPDATA%\AuraSprout`
- **macOS**: `~/Library/Application Support/aurasprout`
- **Linux**: `$XDG_DATA_HOME/aurasprout` or `~/.local/share/aurasprout`

## 1. Quick Start CLI

Use `aurasprout` (installed console script / standalone launcher) or `python -m aurasprout`:

```bash
# Cleanroom standalone AuraSprout installation & full user/OS cleanup
python -m aurasprout self-install --add-to-path --shortcut --json
aurasprout self-uninstall --yes --json

# Install from a local directory or .apkg archive
aurasprout install /path/to/app_or_package --shortcut --json

# Install from an attached or remote HTTP catalog
aurasprout install my-app --from-catalog /path/to/catalog --channel stable --port 8100 --json

# Package into <appId>-<version>.apkg (auto-collects wheels/ or --wheelhouse; --format zip|tar.gz)
aurasprout package /path/to/app --out ./dist --json

# Rollback to previous version while preserving app_data/
aurasprout rollback my-app --json

# Uninstall cleanly (optionally delete the app's sovereign mvenv)
aurasprout uninstall my-app --remove-mvenv --json
```

## 2. Canonical Manifest (`app.manifest.json`)

- Canonical `app.manifest.json` follows `auragrid-api/schemas/app.manifest.schema.json` using placement modes `LocalOnly`, `CellSingleton`, and `Distributed`.
- Cross-platform non-admin version activation uses atomic `active_version.json` pointers alongside `app_data/` preservation across updates and rollbacks.
- Lifecycle hooks (`pre_install`, `post_install`) and `post_install_commands` run inside the app's sovereign `auraseed` MVenv with `shell=False` and validated paths.

## 3. Opt-In Volunteering & Conflict Resolution

New installations set `volunteerEnabled = false` by default and never touch shared `.sprout/` cell coordination directories for `LocalOnly` services or non-volunteered workloads.

```bash
aurasprout volunteer status --json
aurasprout volunteer enable my-service --mode CellSingleton --json
aurasprout volunteer disable my-service --json
aurasprout conflicts list --json
aurasprout conflicts resolve <conflictId> TakeOverStaleLock --json
```
