"""Merged Application Catalog & Distribution Engine for AuraSprout."""

from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.models import (
    AppManifest,
    AppVersionEntry,
    CatalogAppEntry,
    CatalogManifest,
)
from aurasprout.catalog.packager import SproutPackager
from aurasprout.catalog.remote_sync import (
    SftpCatalogDeployer,
    SftpDeployResult,
    build_and_deploy_catalog,
)
from aurasprout.catalog.sources import CatalogSourcesRegistry

__all__ = [
    "AppManifest",
    "AppVersionEntry",
    "CatalogAppEntry",
    "CatalogManifest",
    "CatalogSourcesRegistry",
    "SftpCatalogDeployer",
    "SftpDeployResult",
    "SproutCatalogManager",
    "SproutPackager",
    "build_and_deploy_catalog",
]
