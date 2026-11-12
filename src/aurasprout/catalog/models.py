"""Canonical Application Manifest & Unified Catalog Models for AuraSprout.

Implements `auragrid-api/schemas/app.manifest.schema.json` and
`auragrid-api/schemas/catalog.schema.json` with automatic migration support
for legacy AuraSeed `app.json` manifests.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aurasprout.contracts.paths import validate_identifier

RELEASE_CHANNELS = ("stable", "preview", "canary", "nightly")
MAS_MODES = ("LocalOnly", "CellSingleton", "Distributed")
EXECUTION_RUNTIMES = ("Python", "Container", "CSharp", "PowerShell", "Wasm")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _drop_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


@dataclass
class ExecutableDefinition:
    context: str = "Python"
    entry_command: str = ""
    arguments: Optional[str] = None
    working_directory: Optional[str] = None
    timeout_seconds: Optional[int] = None
    managed_venv: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutableDefinition":
        ctx = str(data.get("context", "Python"))
        if ctx not in EXECUTION_RUNTIMES:
            raise ValueError(f"Unsupported execution runtime: {ctx!r}")
        cmd = str(data.get("entryCommand") or data.get("entry_command") or "").strip()
        if not cmd:
            raise ValueError("ExecutableDefinition requires non-empty entryCommand")
        return cls(
            context=ctx,
            entry_command=cmd,
            arguments=data.get("arguments"),
            working_directory=data.get("workingDirectory") or data.get("working_directory"),
            timeout_seconds=data.get("timeoutSeconds") or data.get("timeout_seconds"),
            managed_venv=data.get("managedVenv") or data.get("managed_venv"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "context": self.context,
            "entryCommand": self.entry_command,
        }
        if self.arguments is not None:
            payload["arguments"] = self.arguments
        if self.working_directory is not None:
            payload["workingDirectory"] = self.working_directory
        if self.timeout_seconds is not None:
            payload["timeoutSeconds"] = int(self.timeout_seconds)
        if self.managed_venv is not None:
            payload["managedVenv"] = self.managed_venv
        return payload


@dataclass
class GuiEntryPoint:
    name: str
    context: str = "Python"
    entry_command: str = ""
    description: Optional[str] = None
    icon_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GuiEntryPoint":
        ctx = str(data.get("context", "Python"))
        if ctx not in EXECUTION_RUNTIMES:
            raise ValueError(f"Unsupported GUI execution runtime: {ctx!r}")
        return cls(
            name=str(data.get("name", "default")),
            context=ctx,
            entry_command=str(data.get("entryCommand") or data.get("entry_command") or ""),
            description=data.get("description"),
            icon_path=data.get("iconPath") or data.get("icon_path"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "context": self.context,
            "entryCommand": self.entry_command,
        }
        if self.description is not None:
            payload["description"] = self.description
        if self.icon_path is not None:
            payload["iconPath"] = self.icon_path
        return payload


@dataclass
class PythonMasConfig:
    script_path: Optional[str] = None
    arguments: Optional[str] = None
    python_executable: str = "python"
    managed_venv: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PythonMasConfig":
        return cls(
            script_path=data.get("scriptPath") or data.get("script_path"),
            arguments=data.get("arguments"),
            python_executable=str(data.get("pythonExecutable") or data.get("python_executable") or "python"),
            managed_venv=data.get("managedVenv") or data.get("managed_venv"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"pythonExecutable": self.python_executable}
        if self.script_path is not None:
            payload["scriptPath"] = self.script_path
        if self.arguments is not None:
            payload["arguments"] = self.arguments
        if self.managed_venv is not None:
            payload["managedVenv"] = self.managed_venv
        return payload


@dataclass
class MasDefinition:
    mas_id: str
    mode: str = "LocalOnly"
    runtime: str = "Python"
    display_name: Optional[str] = None
    restart_policy: str = "OnFailure"
    max_restart_attempts: int = 3
    working_directory: Optional[str] = None
    manifest_directory: Optional[str] = None
    preferred_instruction_sets: List[str] = field(default_factory=list)
    metadata_requirements: Dict[str, str] = field(default_factory=dict)
    appraisal_threshold: Optional[float] = None
    remoting: Optional[Dict[str, Any]] = None
    python_config: Optional[PythonMasConfig] = None
    extra_configs: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MasDefinition":
        mas_id = validate_identifier(str(data.get("masId") or data.get("mas_id") or ""), "masId")
        mode = str(data.get("mode", "LocalOnly"))
        if mode not in MAS_MODES:
            raise ValueError(f"Invalid MasMode {mode!r}; expected one of {MAS_MODES}")
        runtime = str(data.get("runtime", "Python"))
        if runtime not in EXECUTION_RUNTIMES:
            raise ValueError(f"Invalid ExecutionRuntime {runtime!r}")
        py_raw = data.get("pythonConfig") or data.get("python_config")
        py_cfg = PythonMasConfig.from_dict(py_raw) if isinstance(py_raw, dict) else None
        extra: Dict[str, Any] = {}
        for k in ("containerConfig", "cSharpConfig", "csharpConfig", "powerShellConfig"):
            if k in data and isinstance(data[k], dict):
                extra[k] = data[k]
        return cls(
            mas_id=mas_id,
            mode=mode,
            runtime=runtime,
            display_name=data.get("displayName") or data.get("display_name"),
            restart_policy=str(data.get("restartPolicy") or "OnFailure"),
            max_restart_attempts=int(data.get("maxRestartAttempts", 3)),
            working_directory=data.get("workingDirectory"),
            manifest_directory=data.get("manifestDirectory"),
            preferred_instruction_sets=list(data.get("preferredInstructionSets") or []),
            metadata_requirements={str(k): str(v) for k, v in (data.get("metadataRequirements") or {}).items()},
            appraisal_threshold=data.get("appraisalThreshold"),
            remoting=data.get("remoting"),
            python_config=py_cfg,
            extra_configs=extra,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "masId": self.mas_id,
            "mode": self.mode,
            "runtime": self.runtime,
            "restartPolicy": self.restart_policy,
            "maxRestartAttempts": self.max_restart_attempts,
        }
        if self.display_name is not None:
            payload["displayName"] = self.display_name
        if self.working_directory is not None:
            payload["workingDirectory"] = self.working_directory
        if self.manifest_directory is not None:
            payload["manifestDirectory"] = self.manifest_directory
        if self.preferred_instruction_sets:
            payload["preferredInstructionSets"] = list(self.preferred_instruction_sets)
        if self.metadata_requirements:
            payload["metadataRequirements"] = dict(self.metadata_requirements)
        if self.appraisal_threshold is not None:
            payload["appraisalThreshold"] = float(self.appraisal_threshold)
        if self.remoting is not None:
            payload["remoting"] = dict(self.remoting)
        if self.python_config is not None:
            payload["pythonConfig"] = self.python_config.to_dict()
        payload.update(self.extra_configs)
        return payload


@dataclass
class DistributionConfig:
    channels: List[str] = field(default_factory=lambda: ["stable"])
    wheelhouse_path: Optional[str] = None
    subprojects: List[str] = field(default_factory=list)
    pip_install_args: List[str] = field(default_factory=list)
    post_install_commands: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    hooks: Dict[str, str] = field(default_factory=dict)
    entrypoint_command: str = ""
    default_port: Optional[int] = None
    default_dest: Optional[str] = None
    create_desktop_shortcut: bool = True
    create_launchers: bool = True
    tags: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DistributionConfig":
        channels = [c for c in (data.get("channels") or ["stable"]) if c in RELEASE_CHANNELS]
        if not channels:
            channels = ["stable"]
        return cls(
            channels=channels,
            wheelhouse_path=data.get("wheelhousePath") or data.get("wheelhouse_path"),
            subprojects=list(data.get("subprojects") or []),
            pip_install_args=list(data.get("pipInstallArgs") or data.get("pip_install_args") or []),
            post_install_commands=list(data.get("postInstallCommands") or data.get("post_install_commands") or []),
            artifacts=list(data.get("artifacts") or []),
            hooks={str(k): str(v) for k, v in (data.get("hooks") or {}).items()},
            entrypoint_command=str(data.get("entrypointCommand") or data.get("entrypoint_command") or ""),
            default_port=data.get("defaultPort") if data.get("defaultPort") is not None else data.get("default_port"),
            default_dest=data.get("defaultDest") or data.get("default_dest"),
            create_desktop_shortcut=bool(
                data.get("createDesktopShortcut", data.get("create_desktop_shortcut", True))
            ),
            create_launchers=bool(data.get("createLaunchers", data.get("create_launchers", True))),
            tags=list(data.get("tags") or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "channels": list(self.channels),
            "createDesktopShortcut": self.create_desktop_shortcut,
            "createLaunchers": self.create_launchers,
        }
        if self.wheelhouse_path is not None:
            payload["wheelhousePath"] = self.wheelhouse_path
        if self.subprojects:
            payload["subprojects"] = list(self.subprojects)
        if self.pip_install_args:
            payload["pipInstallArgs"] = list(self.pip_install_args)
        if self.post_install_commands:
            payload["postInstallCommands"] = list(self.post_install_commands)
        if self.artifacts:
            payload["artifacts"] = list(self.artifacts)
        if self.hooks:
            payload["hooks"] = dict(self.hooks)
        if self.entrypoint_command:
            payload["entrypointCommand"] = self.entrypoint_command
        if self.default_port is not None:
            payload["defaultPort"] = int(self.default_port)
        if self.default_dest is not None:
            payload["defaultDest"] = self.default_dest
        if self.tags:
            payload["tags"] = list(self.tags)
        return payload


@dataclass
class AppManifest:
    """Canonical AuraGrid & AuraSprout application manifest (`app.manifest.json`)."""

    app_id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    services: List[MasDefinition] = field(default_factory=list)
    gui_entry_points: List[GuiEntryPoint] = field(default_factory=list)
    installer: Optional[ExecutableDefinition] = None
    startup: Optional[ExecutableDefinition] = None
    shutdown: Optional[ExecutableDefinition] = None
    uninstall: Optional[ExecutableDefinition] = None
    provides_contracts: List[Dict[str, str]] = field(default_factory=list)
    dependencies: List[Dict[str, str]] = field(default_factory=list)
    distribution: DistributionConfig = field(default_factory=DistributionConfig)
    schema_uri: Optional[str] = None
    manifest_dir: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppManifest":
        # Detect legacy app.json format vs canonical app.manifest.json
        if "appId" in data or "services" in data:
            app_id = validate_identifier(str(data.get("appId", "")), "appId")
            display_name = str(data.get("name") or app_id)
            services_raw = data.get("services")
            if not isinstance(services_raw, list):
                raise ValueError("app.manifest.json requires 'services' array")
            services = [MasDefinition.from_dict(s) for s in services_raw]
            gui_eps = [GuiEntryPoint.from_dict(g) for g in (data.get("guiEntryPoints") or [])]
            dist_raw = data.get("distribution")
            dist = DistributionConfig.from_dict(dist_raw) if isinstance(dist_raw, dict) else DistributionConfig()
            return cls(
                app_id=app_id,
                name=display_name,
                version=str(data.get("version") or "1.0.0"),
                description=str(data.get("description") or ""),
                services=services,
                gui_entry_points=gui_eps,
                installer=ExecutableDefinition.from_dict(data["installer"]) if isinstance(data.get("installer"), dict) else None,
                startup=ExecutableDefinition.from_dict(data["startup"]) if isinstance(data.get("startup"), dict) else None,
                shutdown=ExecutableDefinition.from_dict(data["shutdown"]) if isinstance(data.get("shutdown"), dict) else None,
                uninstall=ExecutableDefinition.from_dict(data["uninstall"]) if isinstance(data.get("uninstall"), dict) else None,
                provides_contracts=list(data.get("providesContracts") or []),
                dependencies=list(data.get("dependencies") or []),
                distribution=dist,
                schema_uri=data.get("$schema"),
            )

        # Legacy AuraSeed app.json migration path
        legacy_name = validate_identifier(str(data.get("name", "app")), "app name")
        display_name = str(data.get("display_name") or legacy_name)
        dist = DistributionConfig.from_dict(data)
        entrypoint = str(data.get("entrypoint_command") or "").strip()
        synthesized_services: List[MasDefinition] = []
        if entrypoint:
            synthesized_services.append(
                MasDefinition(
                    mas_id=legacy_name,
                    mode="LocalOnly",
                    runtime="Python",
                    display_name=display_name,
                    python_config=PythonMasConfig(arguments=entrypoint),
                )
            )
        return cls(
            app_id=legacy_name,
            name=display_name,
            version=str(data.get("version") or "1.0.0"),
            description=str(data.get("description") or ""),
            services=synthesized_services,
            distribution=dist,
        )

    @classmethod
    def from_file(cls, path: Path) -> "AppManifest":
        target = Path(path).resolve()
        if target.is_dir():
            for candidate in ("app.manifest.json", "app.json", "auraseed-app.json", "manifest.json"):
                cand_path = target / candidate
                if cand_path.exists():
                    target = cand_path
                    break
            else:
                raise FileNotFoundError(
                    f"No application manifest (app.manifest.json or app.json) found in {target}"
                )

        data = json.loads(target.read_text(encoding="utf-8"))
        manifest = cls.from_dict(data)
        manifest.manifest_dir = target.parent
        return manifest

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "appId": self.app_id,
            "name": self.name,
            "version": self.version,
            "services": [s.to_dict() for s in self.services],
        }
        if self.schema_uri:
            payload["$schema"] = self.schema_uri
        if self.description:
            payload["description"] = self.description
        if self.installer is not None:
            payload["installer"] = self.installer.to_dict()
        if self.startup is not None:
            payload["startup"] = self.startup.to_dict()
        if self.shutdown is not None:
            payload["shutdown"] = self.shutdown.to_dict()
        if self.uninstall is not None:
            payload["uninstall"] = self.uninstall.to_dict()
        if self.gui_entry_points:
            payload["guiEntryPoints"] = [g.to_dict() for g in self.gui_entry_points]
        if self.provides_contracts:
            payload["providesContracts"] = list(self.provides_contracts)
        if self.dependencies:
            payload["dependencies"] = list(self.dependencies)
        payload["distribution"] = self.distribution.to_dict()
        return payload

    def to_file(self, path: Path) -> None:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


@dataclass
class AppVersionEntry:
    """Catalog version entry matching `catalog.schema.json#/definitions/appVersionEntry`."""

    version: str
    bundle_filename: str
    bundle_sha256: str
    bundle_size_bytes: int
    channel: str = "stable"
    release_date: str = field(default_factory=now_utc_iso)
    bundle_url: str = ""
    manifest_sha256: str = ""
    manifest_uri: str = ""
    entrypoint_command: str = ""
    default_port: Optional[int] = None
    wheelhouse_path: Optional[str] = None
    delta_from_version: Optional[str] = None
    subprojects: List[str] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)
    changelog: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppVersionEntry":
        ch = str(data.get("channel") or "stable")
        if ch not in RELEASE_CHANNELS:
            ch = "stable"
        return cls(
            version=str(data.get("version", "1.0.0")),
            channel=ch,
            release_date=str(data.get("releaseDate") or data.get("release_date") or now_utc_iso()),
            bundle_filename=str(data.get("bundleFilename") or data.get("bundle_filename") or ""),
            bundle_url=str(data.get("bundleUrl") or data.get("bundle_url") or ""),
            bundle_sha256=str(data.get("bundleSha256") or data.get("bundle_sha256") or ""),
            bundle_size_bytes=int(data.get("bundleSizeBytes") if data.get("bundleSizeBytes") is not None else data.get("bundle_size_bytes", 0)),
            manifest_sha256=str(data.get("manifestSha256") or data.get("manifest_sha256") or ""),
            manifest_uri=str(data.get("manifestUri") or data.get("manifest_uri") or ""),
            entrypoint_command=str(data.get("entrypointCommand") or data.get("entrypoint_command") or ""),
            default_port=data.get("defaultPort") if data.get("defaultPort") is not None else data.get("default_port"),
            wheelhouse_path=data.get("wheelhousePath") or data.get("wheelhouse_path"),
            delta_from_version=data.get("deltaFromVersion") or data.get("delta_from_version"),
            subprojects=list(data.get("subprojects") or []),
            artifacts=list(data.get("artifacts") or []),
            changelog=str(data.get("changelog") or ""),
            metadata={str(k): str(v) for k, v in (data.get("metadata") or {}).items()},
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "version": self.version,
            "channel": self.channel,
            "releaseDate": self.release_date,
            "bundleFilename": self.bundle_filename,
            "bundleSha256": self.bundle_sha256,
            "bundleSizeBytes": int(self.bundle_size_bytes),
        }
        if self.bundle_url:
            payload["bundleUrl"] = self.bundle_url
        if self.manifest_sha256:
            payload["manifestSha256"] = self.manifest_sha256
        if self.manifest_uri:
            payload["manifestUri"] = self.manifest_uri
        if self.entrypoint_command:
            payload["entrypointCommand"] = self.entrypoint_command
        if self.default_port is not None:
            payload["defaultPort"] = int(self.default_port)
        if self.wheelhouse_path is not None:
            payload["wheelhousePath"] = self.wheelhouse_path
        if self.delta_from_version is not None:
            payload["deltaFromVersion"] = self.delta_from_version
        if self.subprojects:
            payload["subprojects"] = list(self.subprojects)
        if self.artifacts:
            payload["artifacts"] = list(self.artifacts)
        if self.changelog:
            payload["changelog"] = self.changelog
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass
class CatalogAppEntry:
    """Catalog application entry matching `catalog.schema.json#/definitions/catalogAppEntry`."""

    app_id: str
    name: str
    display_name: str
    description: str = ""
    latest_version: str = "1.0.0"
    channel: str = "stable"
    manifest_uri: Optional[str] = None
    is_default: bool = False
    tags: List[str] = field(default_factory=list)
    versions: Dict[str, AppVersionEntry] = field(default_factory=dict)
    created_at: str = field(default_factory=now_utc_iso)
    updated_at: str = field(default_factory=now_utc_iso)
    metadata: Dict[str, str] = field(default_factory=dict)

    def get_version(self, ver: Optional[str] = None, channel: Optional[str] = None) -> Optional[AppVersionEntry]:
        if not self.versions:
            return None
        if ver and ver != "latest":
            return self.versions.get(ver)
        if channel:
            matching = [v for v in self.versions.values() if v.channel == channel]
            if matching:
                matching.sort(key=lambda v: v.release_date, reverse=True)
                return matching[0]
        return self.versions.get(self.latest_version)

    def add_version(self, version_entry: AppVersionEntry, is_latest: bool = True) -> None:
        self.versions[version_entry.version] = version_entry
        self.updated_at = now_utc_iso()
        if is_latest or not self.latest_version:
            self.latest_version = version_entry.version
            self.channel = version_entry.channel

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CatalogAppEntry":
        app_id = str(data.get("appId") or data.get("name") or "app")
        name = str(data.get("name") or app_id)
        display_name = str(data.get("displayName") or data.get("display_name") or name)
        versions_raw = data.get("versions") or {}
        versions = {str(k): AppVersionEntry.from_dict(v) for k, v in versions_raw.items()}
        ch = str(data.get("channel") or "stable")
        if ch not in RELEASE_CHANNELS:
            ch = "stable"
        return cls(
            app_id=app_id,
            name=name,
            display_name=display_name,
            description=str(data.get("description") or ""),
            latest_version=str(data.get("latestVersion") or data.get("latest_version") or "1.0.0"),
            channel=ch,
            manifest_uri=data.get("manifestUri") or data.get("manifest_uri"),
            is_default=bool(data.get("isDefault", False)),
            tags=list(data.get("tags") or []),
            versions=versions,
            created_at=str(data.get("createdAt") or data.get("created_at") or now_utc_iso()),
            updated_at=str(data.get("updatedAt") or data.get("updated_at") or now_utc_iso()),
            metadata={str(k): str(v) for k, v in (data.get("metadata") or {}).items()},
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "appId": self.app_id,
            "name": self.name,
            "displayName": self.display_name,
            "latestVersion": self.latest_version,
            "channel": self.channel,
            "isDefault": self.is_default,
            "versions": {k: v.to_dict() for k, v in self.versions.items()},
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.description:
            payload["description"] = self.description
        if self.manifest_uri is not None:
            payload["manifestUri"] = self.manifest_uri
        if self.tags:
            payload["tags"] = list(self.tags)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass
class CatalogManifest:
    """Canonical unified catalog matching `auragrid-api/schemas/catalog.schema.json`."""

    catalog_name: str = "AuraSprout Application Catalog"
    description: str = "Unified zero-privilege AuraCore application catalog"
    version: str = "1.0.0"
    base_url: Optional[str] = None
    updated_at: str = field(default_factory=now_utc_iso)
    apps: Dict[str, CatalogAppEntry] = field(default_factory=dict)
    attached_sources: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def get_app(self, app_id: str) -> Optional[CatalogAppEntry]:
        return self.apps.get(app_id)

    def register_app_version(
        self,
        app_id: str,
        display_name: str,
        version_entry: AppVersionEntry,
        description: str = "",
        tags: Optional[List[str]] = None,
        is_latest: bool = True,
        name: Optional[str] = None,
    ) -> CatalogAppEntry:
        self.updated_at = now_utc_iso()
        app = self.apps.get(app_id)
        if app is None:
            app = CatalogAppEntry(
                app_id=app_id,
                name=name or app_id,
                display_name=display_name or app_id,
                description=description,
                latest_version=version_entry.version,
                channel=version_entry.channel,
                tags=list(tags or []),
            )
            self.apps[app_id] = app
        else:
            if display_name:
                app.display_name = display_name
            if description:
                app.description = description
            if tags:
                app.tags = list(dict.fromkeys([*app.tags, *tags]))

        app.add_version(version_entry, is_latest=is_latest)
        return app

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CatalogManifest":
        apps_raw = data.get("apps") or {}
        apps = {str(k): CatalogAppEntry.from_dict(v) for k, v in apps_raw.items()}
        return cls(
            catalog_name=str(data.get("catalogName") or data.get("catalog_name") or "AuraSprout Application Catalog"),
            description=str(data.get("description") or ""),
            version=str(data.get("version") or "1.0.0"),
            base_url=data.get("baseUrl") or data.get("base_url"),
            updated_at=str(data.get("updatedAt") or data.get("updated_at") or now_utc_iso()),
            apps=apps,
            attached_sources=list(data.get("attachedSources") or []),
            metadata={str(k): str(v) for k, v in (data.get("metadata") or {}).items()},
        )

    @classmethod
    def from_json(cls, raw_json: str) -> "CatalogManifest":
        return cls.from_dict(json.loads(raw_json))

    @classmethod
    def from_file(cls, path: Path) -> "CatalogManifest":
        return cls.from_json(Path(path).resolve().read_text(encoding="utf-8"))

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "catalogName": self.catalog_name,
            "description": self.description,
            "version": self.version,
            "baseUrl": self.base_url,
            "updatedAt": self.updated_at,
            "apps": {k: v.to_dict() for k, v in sorted(self.apps.items())},
        }
        if self.attached_sources:
            payload["attachedSources"] = list(self.attached_sources)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return _drop_none(payload) if self.base_url is not None else payload

    def to_file(self, path: Path) -> None:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, target)

