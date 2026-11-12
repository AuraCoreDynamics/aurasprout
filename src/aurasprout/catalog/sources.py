"""Attached Catalog Sources Registry for AuraSprout."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from aurasprout.contracts.paths import SproutPaths


class CatalogSourcesRegistry:
    """Manages client-attached HTTP/REST and local/shared-FS application catalogs."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        paths: Optional[SproutPaths] = None,
        default_source_url: Optional[str] = None,
    ) -> None:
        resolved_paths = paths or SproutPaths()
        self.config_path = Path(config_path).resolve() if config_path else resolved_paths.sources_file
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._default_source = (
            default_source_url
            or os.environ.get("AURASPROUT_DEFAULT_CATALOG")
            or str(resolved_paths.catalog_dir)
        )
        if not self.config_path.exists():
            self._save_sources(
                [
                    {
                        "url": self._normalize_source(self._default_source),
                        "label": "Local User Catalog",
                        "isDefault": True,
                    }
                ]
            )

    @staticmethod
    def _normalize_source(source: str) -> str:
        s = (source or "").strip()
        if s.startswith(("http://", "https://", "file://")):
            return s.rstrip("/")
        return str(Path(s).resolve())

    def _read_sources(self) -> List[Dict[str, Any]]:
        if not self.config_path.exists():
            return []
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [
            {
                "url": str(item.get("url", "")),
                "label": str(item.get("label", item.get("url", ""))),
                "isDefault": bool(item.get("isDefault", item.get("is_default", False))),
            }
            for item in raw
            if isinstance(item, dict) and item.get("url")
        ]

    def _save_sources(self, sources: List[Dict[str, Any]]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_name(f"{self.config_path.name}.tmp")
        tmp.write_text(json.dumps(sources, indent=2), encoding="utf-8")
        os.replace(tmp, self.config_path)

    def list_sources(self) -> List[Dict[str, Any]]:
        return self._read_sources()

    def attach_source(
        self,
        url: str,
        label: Optional[str] = None,
        set_default: bool = False,
    ) -> Dict[str, Any]:
        norm = self._normalize_source(url)
        sources = self._read_sources()
        for item in sources:
            if self._normalize_source(item["url"]) == norm:
                if label:
                    item["label"] = label
                if set_default:
                    for other in sources:
                        other["isDefault"] = False
                    item["isDefault"] = True
                self._save_sources(sources)
                return item

        if set_default:
            for other in sources:
                other["isDefault"] = False

        entry = {
            "url": norm,
            "label": label or norm,
            "isDefault": bool(set_default or len(sources) == 0),
        }
        sources.append(entry)
        self._save_sources(sources)
        return entry

    def remove_source(self, url: str) -> bool:
        norm = self._normalize_source(url)
        sources = self._read_sources()
        remaining = [s for s in sources if self._normalize_source(s["url"]) != norm]
        if len(remaining) == len(sources):
            return False
        if remaining and not any(s.get("isDefault") for s in remaining):
            remaining[0]["isDefault"] = True
        self._save_sources(remaining)
        return True

    def get_default_source(self) -> Optional[str]:
        sources = self._read_sources()
        for s in sources:
            if s.get("isDefault"):
                return str(s["url"])
        return sources[0]["url"] if sources else None

