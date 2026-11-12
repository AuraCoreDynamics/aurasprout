# AuraSprout

Zero-privilege, user-space application distribution, local supervision, and shared-filesystem coordination layer for **AuraCore**.

## Architecture Boundaries

- **`auragrid-api`**: Canonical OpenAPI (`ipc-bridge.openapi.json`, `aurasprout-management.openapi.json`) and JSON Schemas (`app.manifest.schema.json`, `catalog.schema.json`, `lease-collaboration.schema.json`).
- **`auragrid-py`**: Typed Python SDK consumed by `LocalOnly`, `CellSingleton`, and `Distributed` MAS workloads.
- **`auraseed`**: Strictly Python discovery & sovereign virtual environment (`mvenv`) management (`aurasprout` composes `auraseed.gui.MvenvManagerPanel`).
- **`aurasprout`**: Application cataloging & packaging (`.apkg`), user-space installation/updates/rollback (`active_version.json` + preserved `app_data/`), SMB-safe SQLite event bus (`PRAGMA journal_mode = TRUNCATE`), atomic directory mutex & fenced leases/batches (`SproutMutex`), Tier 2 loopback IPC bridge (`X-AuraGrid-IPC-Token`), Tier 3 loopback Management Web UI (`aurasprout_session` + `X-CSRF-Token`), and Tkinter Desktop Hub.

## Quick Start

```powershell
# Package & publish an app to a static zero-CDN catalog
python -m aurasprout package .\my_app --out .\dist --publish .\catalog --channel stable --json

# Install locally (user-space only, zero admin privileges)
python -m aurasprout install .\dist\my-app-1.0.0.apkg --json

# Inspect or opt in to cell volunteering (opt-in default: false)
python -m aurasprout volunteer status --json
python -m aurasprout volunteer enable my-service --json

# Start Tier 3 loopback Management Web UI
python -m aurasprout ui

# Start Tkinter Desktop Hub (composing AuraSeed MVenvManagerPanel)
python -m aurasprout desktop
```

