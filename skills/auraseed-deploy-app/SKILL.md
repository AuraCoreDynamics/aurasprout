---
name: auraseed-deploy-app
description: >-
  Deploy, install, update, rollback, package, and manage applications using the AuraSprout
  application installer engine (`app.manifest.json` and legacy `app.json` migration).
  Covers zero-privilege user-space installs, offline wheelhouses, MVenv relinking, and launchers.
---

# AuraSprout Application Deployment & Lifecycle

AuraSprout installs and manages AuraCore applications strictly in user-writable storage (`%LOCALAPPDATA%\AuraSprout` or `~/.local/share/aurasprout`) without requiring administrator privileges.

## 1. Quick Start CLI

```bash
# Install from a local directory or .apkg archive (app.manifest.json or legacy app.json)
python -m aurasprout install /path/to/app_or_package --json

# Install from an attached catalog or channel
python -m aurasprout install my-app --from-catalog /path/to/catalog --channel stable --json

# Package into <appId>-<version>.apkg (auto-collects wheels/ or --wheelhouse)
python -m aurasprout package /path/to/app --out ./dist --json

# Rollback to previous version while preserving app_data/
python -m aurasprout rollback my-app --json

# Uninstall cleanly
python -m aurasprout uninstall my-app --json
```

## 2. Canonical Manifest (`app.manifest.json`) & Legacy Migration (`app.json`)

- Canonical `app.manifest.json` follows `auragrid-api/schemas/app.manifest.schema.json` using placement modes `LocalOnly`, `CellSingleton`, and `Distributed`.
- Legacy `app.json` manifests are automatically migrated on load, synthesizing a `LocalOnly` service entry if `entrypoint_command` is set.
- Cross-platform non-admin version activation uses atomic `active_version.json` pointers alongside `app_data/` preservation across updates and rollbacks.
- Lifecycle hooks (`pre_install`, `post_install`) run inside the app's sovereign `auraseed` MVenv with `shell=False` and validated paths.

## 3. Opt-In Volunteering & Conflict Resolution

New installations set `volunteerEnabled = false` by default and never touch shared `.sprout/` cell coordination directories for `LocalOnly` services or non-volunteered workloads.

```bash
python -m aurasprout volunteer status --json
python -m aurasprout volunteer enable my-service --mode CellSingleton --json
python -m aurasprout volunteer disable my-service --json
python -m aurasprout conflicts list --json
python -m aurasprout conflicts resolve <conflictId> TakeOverStaleLock --json
```

