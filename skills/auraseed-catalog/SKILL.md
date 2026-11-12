---
name: auraseed-catalog
description: >-
  Manage, publish, browse, and install applications from AuraSprout Application Catalogs.
  Covers static web portal deployments (GitLab Pages, GitHub Pages, Confluence), release channels
  (stable, preview, canary, nightly), cryptographic SHA-256 integrity verification, and Management UI APIs.
---

# AuraSprout Application Catalog & Static Web Deployments

AuraSprout consolidates application catalog management, release channels, SHA-256 verification, and zero-CDN static HTML5 portals for all AuraCore Python and MAS applications (using `auraseed` underneath strictly for Python interpreter discovery and sovereign MVenv isolation).

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

## 2. Browsing & Querying Catalogs

```bash
# Query local catalog store
python -m aurasprout catalog list --json

# Query remote HTTP or shared-filesystem catalog
python -m aurasprout catalog list --remote "https://example.internal/catalog/" --json

# Query locally installed applications
python -m aurasprout catalog list --installed --json
```

## 3. Packaging & Publishing Applications

```bash
python -m aurasprout package /path/to/app \
  --out ./dist \
  --publish /path/to/catalog-repo \
  --channel stable \
  --base-url "https://example.internal/catalog/" \
  --json
```

Rebuilding static HTML portal directly:
```bash
python -m aurasprout catalog build /path/to/catalog-repo --title "AuraCore App Store"
```

## 4. Installing Directly from a Catalog

```bash
python -m aurasprout install my-app --from-catalog /path/to/catalog-repo --channel stable --json
```

AuraSprout verifies SHA-256 digests (`bundleSha256`) before extraction, rejects path traversal/symlink archives, creates an isolated sovereign `auraseed` MVenv, and leaves all distributed `volunteerEnabled` flags strictly `false` until explicitly opted in.

