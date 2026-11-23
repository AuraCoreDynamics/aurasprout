"""Local Installed Applications Registry & Active Version Pointer Manager."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class InstalledServiceState:
    mas_id: str
    mode: str = "LocalOnly"
    runtime: str = "Python"
    display_name: Optional[str] = None
    volunteer_enabled: bool = False
    state: str = "Installed"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InstalledServiceState":
        return cls(
            mas_id=str(data.get("masId") or data.get("mas_id") or ""),
            mode=str(data.get("mode") or "LocalOnly"),
            runtime=str(data.get("runtime") or "Python"),
            display_name=data.get("displayName") or data.get("display_name"),
            volunteer_enabled=bool(data.get("volunteerEnabled", data.get("volunteer_enabled", False))),
            state=str(data.get("state") or "Installed"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "masId": self.mas_id,
            "displayName": self.display_name,
            "mode": self.mode,
            "runtime": self.runtime,
            "volunteerEnabled": self.volunteer_enabled,
            "state": self.state,
        }


@dataclass
class InstalledAppRecord:
    app_id: str
    name: str
    version: str
    install_path: str
    mvenv_name: Optional[str] = None
    mvenv_path: Optional[str] = None
    python_path: Optional[str] = None
    channel: str = "stable"
    port: int = 8000
    services: List[InstalledServiceState] = field(default_factory=list)
    launchers: Dict[str, str] = field(default_factory=dict)
    registered_skills: List[str] = field(default_factory=list)
    previous_versions: List[str] = field(default_factory=list)
    installed_at: str = field(default_factory=_now_utc_iso)
    updated_at: str = field(default_factory=_now_utc_iso)
    repository_url: Optional[str] = None
    attached_mode: str = "extracted"
    source_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InstalledAppRecord":
        return cls(
            app_id=str(data.get("appId") or data.get("app_id") or data.get("name") or ""),
            name=str(data.get("name") or data.get("display_name") or data.get("appId") or ""),
            version=str(data.get("version") or "1.0.0"),
            install_path=str(data.get("installPath") or data.get("install_dir") or ""),
            mvenv_name=data.get("mvenvName") or data.get("mvenv_name"),
            mvenv_path=data.get("mvenvPath") or data.get("mvenv_path"),
            python_path=data.get("pythonPath") or data.get("python_path"),
            channel=str(data.get("channel") or "stable"),
            port=int(data.get("port") or 8000),
            services=[InstalledServiceState.from_dict(s) for s in (data.get("services") or [])],
            launchers=dict(data.get("launchers") or {}),
            registered_skills=list(data.get("registeredSkills") or data.get("registered_skills") or []),
            previous_versions=list(data.get("previousVersions") or []),
            installed_at=str(data.get("installedAt") or data.get("installed_at") or _now_utc_iso()),
            updated_at=str(data.get("updatedAt") or data.get("updated_at") or _now_utc_iso()),
            repository_url=data.get("repositoryUrl") or data.get("repository_url"),
            attached_mode=str(data.get("attachedMode") or data.get("attached_mode") or "extracted"),
            source_path=data.get("sourcePath") or data.get("source_path"),
        )

    def to_summary_dict(self) -> Dict[str, Any]:
        """Serialize strictly matching `aurasprout-management.openapi.json#/components/schemas/InstalledAppSummary`."""
        d = {
            "appId": self.app_id,
            "name": self.name,
            "version": self.version,
            "installPath": self.install_path,
            "mvenvName": self.mvenv_name,
            "channel": self.channel,
            "services": [s.to_dict() for s in self.services],
        }
        if self.repository_url:
            d["repositoryUrl"] = self.repository_url
        if self.attached_mode != "extracted":
            d["attachedMode"] = self.attached_mode
        if self.source_path:
            d["sourcePath"] = self.source_path
        return d

    def to_dict(self) -> Dict[str, Any]:
        d = {
            **self.to_summary_dict(),
            "mvenvPath": self.mvenv_path,
            "pythonPath": self.python_path,
            "port": self.port,
            "launchers": dict(self.launchers),
            "registeredSkills": list(self.registered_skills),
            "previousVersions": list(self.previous_versions),
            "installedAt": self.installed_at,
            "updatedAt": self.updated_at,
        }
        if self.repository_url:
            d["repositoryUrl"] = self.repository_url
        d["attachedMode"] = self.attached_mode
        if self.source_path:
            d["sourcePath"] = self.source_path
        return d


class InstalledAppsRegistry:
    """User-space registry of locally installed AuraSprout applications."""

    def __init__(
        self,
        apps_dir: Optional[Path] = None,
        registry_file: Optional[Path] = None,
        paths: Optional[SproutPaths] = None,
    ) -> None:
        resolved = paths or SproutPaths()
        self.apps_dir = Path(apps_dir).resolve() if apps_dir else resolved.apps_dir
        self.registry_file = (
            Path(registry_file).resolve() if registry_file else resolved.installed_registry_file
        )
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        if not self.registry_file.exists():
            return {"apps": {}}
        data = json.loads(self.registry_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
            return {"apps": {}}
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_file.with_name(f"{self.registry_file.name}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.registry_file)

    def register(self, record: InstalledAppRecord) -> None:
        safe_id = validate_identifier(record.app_id, "appId")
        data = self._read()
        record.updated_at = _now_utc_iso()
        data["apps"][safe_id] = record.to_dict()
        self._write(data)

    def unregister(self, app_id: str) -> bool:
        safe_id = validate_identifier(app_id, "appId")
        data = self._read()
        if safe_id not in data["apps"]:
            return False
        del data["apps"][safe_id]
        self._write(data)
        return True

    def get(self, app_id: str) -> Optional[InstalledAppRecord]:
        safe_id = validate_identifier(app_id, "appId")
        raw = self._read()["apps"].get(safe_id)
        if not isinstance(raw, dict):
            return None
        return InstalledAppRecord.from_dict(raw)

    def list_all(self) -> List[InstalledAppRecord]:
        raw_apps = self._read()["apps"]
        records: List[InstalledAppRecord] = []
        for app_id, raw in sorted(raw_apps.items()):
            rec = InstalledAppRecord.from_dict(raw)
            if Path(rec.install_path).exists():
                records.append(rec)
        return records

    @staticmethod
    def write_active_version_pointer(
        install_dir: Path,
        app_id: str,
        version: str,
        previous_versions: Optional[List[str]] = None,
        attached_mode: Optional[str] = None,
        source_path: Optional[str] = None,
        repository_url: Optional[str] = None,
    ) -> Path:
        """Write `active_version.json` pointer (non-admin Windows & POSIX safe)."""
        root = Path(install_dir).resolve()
        ptr_file = root / "active_version.json"
        existing_data: Dict[str, Any] = {}
        if ptr_file.exists():
            try:
                existing_data = json.loads(ptr_file.read_text(encoding="utf-8"))
            except Exception:
                existing_data = {}

        mode = attached_mode or existing_data.get("attachedMode") or "extracted"
        src = source_path or existing_data.get("sourcePath")
        repo = repository_url or existing_data.get("repositoryUrl")

        payload = {
            "appId": app_id,
            "activeVersion": version,
            "versionDir": str(validate_within_root(root, Path("versions") / validate_identifier(version, "version"))),
            "appDataDir": str(validate_within_root(root, "app_data")),
            "activeDir": str(root / "active"),
            "previousVersions": list(previous_versions or []),
            "attachedMode": mode,
            "updatedAt": _now_utc_iso(),
        }
        if src:
            payload["sourcePath"] = str(src)
        if repo:
            payload["repositoryUrl"] = str(repo)

        tmp = ptr_file.with_name(f"{ptr_file.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, ptr_file)
        return ptr_file

    @staticmethod
    def read_active_version_pointer(install_dir: Path) -> Optional[Dict[str, Any]]:
        ptr = Path(install_dir).resolve() / "active_version.json"
        if not ptr.exists():
            return None
        return json.loads(ptr.read_text(encoding="utf-8"))

