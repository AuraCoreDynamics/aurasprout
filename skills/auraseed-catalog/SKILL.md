---
name: auraseed-catalog
description: >-
  Manage, publish, push, browse, and install applications from AuraSprout Application Catalogs.
  Covers static web portal deployments (GitLab Pages, GitHub Pages, Confluence), HTTP catalog
  push/hosting, release channels (stable, preview, canary, nightly), and SHA-256 verification.
---

# AuraSprout Application Catalog & Static Web Deployments

AuraSprout consolidates application catalog management, release channels, SHA-256 verification, HTTP catalog push/hosting, and zero-CDN static HTML5 portals for all AuraCore Python and MAS applications (using `auraseed` underneath strictly for Python interpreter discovery and sovereign MVenv isolation).

## 1. Catalog Layout (`catalog.schema.json`)

```text
<catalog_root>/
├── index.html                           # Zero-CDN self-contained static web portal
├── catalog.json                         # Machine-readable unified CatalogManifest
├── channels/
│   └── <channel>/<appId>.manifest.json  # Channel-scoped manifests (stable, preview, canary, nightly)
└── apps/
    └── <appId>/
        ├── <appId>-<version>.manifest.json
        └── <appId>-<version>.apkg       # Verified zip/tar.gz package archive
```

## 2. Browsing, Sources & Querying Catalogs

Use `aurasprout` (installed console script / standalone launcher) or `python -m aurasprout`:

```bash
# Query local catalog store (optional filters: --q, --channel)
aurasprout catalog list --json

# Query remote HTTP or shared-filesystem catalog
aurasprout catalog list --remote "http://127.0.0.1:8765/catalog" --json

# Query locally installed applications
aurasprout catalog list --installed --json

# Attach, list, or detach catalog sources
aurasprout catalog add-source "http://127.0.0.1:8765/catalog" --label local-ui --default
aurasprout catalog sources --json
aurasprout catalog remove-source "http://127.0.0.1:8765/catalog" --json
```

## 3. Packaging, Publishing & HTTP Pushing

```bash
# Package & publish to a static directory portal
aurasprout package /path/to/app \
  --out ./dist \
  --publish /path/to/catalog-repo \
  --channel stable \
  --base-url "https://example.internal/catalog/" \
  --json

# Push an .apkg or app directory to a running AuraSprout HTTP UI server
aurasprout catalog push ./dist/my-app-1.0.0.apkg \
  --server "http://127.0.0.1:8765" \
  --token-file /path/to/ui_daemon.json \
  --channel stable \
  --json

# Rebuild static HTML portal or delete an app from the local catalog
aurasprout catalog build /path/to/catalog-repo --title "AuraCore App Store"
aurasprout catalog delete my-app --json
```

## 4. Installing Directly from a Catalog

```bash
aurasprout install my-app --from-catalog "http://127.0.0.1:8765/catalog" --channel stable --shortcut --json
```

AuraSprout verifies SHA-256 digests (`bundleSha256`) before extraction, rejects path traversal/symlink archives, creates an isolated sovereign `auraseed` MVenv, and leaves all distributed `volunteerEnabled` flags strictly `false` until explicitly opted in.
