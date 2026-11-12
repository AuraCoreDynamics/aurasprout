# AuraSprout

Zero-privilege, user-space application distribution, local supervision, and shared-filesystem coordination layer for **AuraCore**.

## Architecture Boundaries

- **`auragrid-api`**: Canonical OpenAPI (`ipc-bridge.openapi.json`, `aurasprout-management.openapi.json`) and JSON Schemas (`app.manifest.schema.json`, `catalog.schema.json`, `lease-collaboration.schema.json`).
- **`auragrid-py`**: Typed Python SDK consumed by `LocalOnly`, `CellSingleton`, and `Distributed` MAS workloads.
- **`auraseed`**: Strictly Python discovery & sovereign virtual environment (`mvenv`) management (`aurasprout` composes `auraseed.gui.MvenvManagerPanel`).
- **`aurasprout`**: Application cataloging & packaging (`.apkg`), HTTP catalog hosting/push, user-space installation/updates/rollback (`active_version.json` + preserved `app_data/`), cleanroom standalone self-install/cleanup, SMB-safe SQLite event bus (`PRAGMA journal_mode = TRUNCATE`), atomic directory mutex & fenced leases/batches (`SproutMutex`), Tier 2 loopback IPC bridge (`X-AuraGrid-IPC-Token`), Tier 3 loopback Management Web UI (`aurasprout_session` + `X-CSRF-Token`), and Tkinter Desktop Hub.

## Quick Start

Both `aurasprout` (installed console script / standalone launcher) and `python -m aurasprout` expose the full CLI (supporting global `--cell-root` and `--user-root` flags):

```powershell
# Install AuraSprout standalone into %LOCALAPPDATA%\AuraSprout\standalone
python -m aurasprout self-install --add-to-path --shortcut --json

# Package & publish an app to a static zero-CDN catalog (supports --wheelhouse, --format zip|tar.gz)
aurasprout package .\my_app --out .\dist --publish .\catalog --channel stable --json

# Push an .apkg or source directory to a running AuraSprout HTTP server
aurasprout catalog push .\dist\my-app-1.0.0.apkg --server http://127.0.0.1:8765 --token-file ui_daemon.json --json

# Install locally from directory, .apkg, or HTTP/FS catalog (user-space only, zero admin privileges)
aurasprout install my-app --from-catalog http://127.0.0.1:8765/catalog --channel stable --shortcut --json

# Manage catalog entries & attached sources
aurasprout catalog list --installed --json
aurasprout catalog sources --json
aurasprout catalog add-source https://example.internal/catalog --default
aurasprout catalog remove-source https://example.internal/catalog
aurasprout catalog delete my-app --json

# Rollback, uninstall, or clean up user state completely
aurasprout rollback my-app --json
aurasprout uninstall my-app --remove-mvenv --json
aurasprout self-uninstall --yes --json

# Inspect or opt in to cell volunteering (opt-in default: false) & resolve conflicts
aurasprout volunteer status --json
aurasprout volunteer enable my-service --mode CellSingleton --json
aurasprout volunteer disable my-service --json
aurasprout conflicts list --json
aurasprout conflicts resolve <conflictId> TakeOverStaleLock --json

# Start Tier 3 loopback Management Web UI (plus /catalog hosting), Tier 2 IPC bridge, or Desktop Hub
aurasprout ui --port 8765 --token-file ui_daemon.json --json
aurasprout ipc --port 8766
aurasprout desktop
```

## HTTP Endpoints & Storage

- **Tier 3 Management UI & Hosted Catalog (`aurasprout ui`)**:
  - Public: `GET /`, `GET /api/health`, `GET /api/v1/health`, `GET /catalog/`, `GET /catalog/index.html`, `GET /catalog/catalog.json`, `GET /catalog/apps/<path>`, `GET /catalog/channels/<path>`.
  - Auth & CSRF (`POST /api/v1/auth/session`): `/api/v1/apps`, `/api/v1/apps/install`, `/api/v1/apps/{appId}`, `/api/v1/apps/{appId}/launch`, `/api/v1/catalog`, `/api/v1/catalog/sources`, `POST /api/v1/catalog/publish` (binary/multipart/base64/path), `/api/v1/catalog/apps/{appId}/download`, `/api/v1/volunteer*`, `/api/v1/conflicts*`, `/api/v1/runtimes`.
- **Tier 2 IPC Bridge (`aurasprout ipc`)**: Loopback `X-AuraGrid-IPC-Token` endpoints under `/health`, `/ready`, `/cell/config*`, `/cell/members*`, `/cell/governance*`, `/cell/events*`, `/cell/lease*`, `/cell/collaboration*`, `/cell/registry*`, `/cell/storage*`, `/cell/tattle`, and `/cell/chaos*`.
- **User Data Roots**: Resolved via `AURASPROUT_HOME` -> `%LOCALAPPDATA%\AuraSprout` (Windows), `~/Library/Application Support/aurasprout` (macOS), or `$XDG_DATA_HOME/aurasprout` / `~/.local/share/aurasprout` (Linux).
- **Environment Variables**: `AURASPROUT_HOME`, `AURASPROUT_CELL_ROOT`, `AURAGRID_CELL_ROOT`, `AURASPROUT_STANDALONE_DIR`, `AURASPROUT_UI_HOST`, `AURASPROUT_UI_PORT`, `AURASPROUT_UI_TOKEN`, `AURASPROUT_DEFAULT_CATALOG`, `AURAGRID_IPC_TOKEN`, `AURAGRID_NODE_ID`.
