"""Deterministic user-space and shared cell storage paths with path-traversal validation."""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Optional

_VALID_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


def validate_identifier(value: str, label: str = "identifier") -> str:
    """Validate that a logical identifier contains only safe filename characters."""
    cleaned = (value or "").strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or ".." in cleaned
        or not _VALID_SEGMENT_RE.match(cleaned)
    ):
        raise ValueError(f"Unsafe or invalid {label}: {value!r}")
    return cleaned


def validate_within_root(root: Path, target: str | Path) -> Path:
    """Resolve ``target`` relative to ``root`` and ensure it stays within ``root``."""
    base = Path(root).resolve()
    raw = str(target).strip()
    if not raw or "\x00" in raw:
        raise ValueError(f"Invalid empty or NUL path: {target!r}")

    candidate_input = Path(raw)
    if candidate_input.is_absolute():
        candidate = candidate_input.resolve()
    else:
        candidate = (base / candidate_input).resolve()

    if candidate != base and base not in candidate.parents:
        raise ValueError(f"Path traversal rejected: {target!r} escapes root {base}")

    # Reject symlinked path segments pointing outside root.
    if not candidate_input.is_absolute():
        cur = base
        for part in candidate_input.parts:
            if part in ("", "."):
                continue
            cur = cur / part
            if cur.is_symlink():
                link_target = cur.resolve()
                if link_target != base and base not in link_target.parents:
                    raise ValueError(f"Symlink escape rejected at {cur}")

    return candidate


def default_user_data_root() -> Path:
    """Return user-writable AuraSprout root directory (zero admin privileges required)."""
    env_override = os.environ.get("AURASPROUT_HOME")
    if env_override and env_override.strip():
        return Path(env_override.strip()).resolve()

    sys_name = platform.system().lower()
    if sys_name == "windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data and local_app_data.strip():
            return (Path(local_app_data.strip()) / "AuraSprout").resolve()
        return (Path.home() / ".aurasprout").resolve()
    if sys_name == "darwin":
        return (Path.home() / "Library" / "Application Support" / "aurasprout").resolve()

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data and xdg_data.strip():
        return (Path(xdg_data.strip()) / "aurasprout").resolve()
    return (Path.home() / ".local" / "share" / "aurasprout").resolve()


class SproutPaths:
    """Canonical directory topology for local user state and shared cell coordination."""

    def __init__(
        self,
        cell_root: Optional[str | Path] = None,
        user_root: Optional[str | Path] = None,
    ) -> None:
        if cell_root is not None:
            self.cell_root = Path(cell_root).resolve()
        else:
            env_cell = os.environ.get("AURASPROUT_CELL_ROOT") or os.environ.get("AURAGRID_CELL_ROOT")
            self.cell_root = Path(env_cell).resolve() if env_cell and env_cell.strip() else default_user_data_root()

        self.user_root = Path(user_root).resolve() if user_root is not None else default_user_data_root()
        self.sprout_dir = (self.cell_root / ".sprout").resolve()

    # ------------------------------------------------------------------
    # Shared .sprout/ topology (matches package-and-catalog-spec.md §2)
    # ------------------------------------------------------------------
    @property
    def events_dir(self) -> Path:
        return self.sprout_dir / "events"

    @property
    def events_db(self) -> Path:
        return self.events_dir / "events.sqlite"

    @property
    def leases_dir(self) -> Path:
        return self.sprout_dir / "leases"

    def lease_file(self, mas_id: str) -> Path:
        safe = validate_identifier(mas_id, "mas_id")
        return validate_within_root(self.leases_dir, f"{safe}.lease.json")

    @property
    def locks_dir(self) -> Path:
        return self.sprout_dir / "locks"

    def lock_dir(self, name: str) -> Path:
        safe = validate_identifier(name, "lock name")
        return validate_within_root(self.locks_dir, f"{safe}.lock")

    @property
    def claims_dir(self) -> Path:
        return self.sprout_dir / "claims"

    def claims_mas_dir(self, mas_id: str) -> Path:
        safe = validate_identifier(mas_id, "mas_id")
        return validate_within_root(self.claims_dir, safe)

    def claim_file(self, mas_id: str, batch_id: str) -> Path:
        safe_mas = validate_identifier(mas_id, "mas_id")
        safe_batch = validate_identifier(batch_id, "batch_id")
        return validate_within_root(self.claims_dir, Path(safe_mas) / f"{safe_batch}.claim.json")

    @property
    def nodes_dir(self) -> Path:
        return self.sprout_dir / "nodes"

    def node_file(self, node_id: str) -> Path:
        safe = validate_identifier(node_id, "node_id")
        return validate_within_root(self.nodes_dir, f"{safe}.json")

    @property
    def config_dir(self) -> Path:
        return self.sprout_dir / "config"

    @property
    def cell_config_file(self) -> Path:
        return self.config_dir / "cell_config.json"

    @property
    def registry_dir(self) -> Path:
        return self.sprout_dir / "registry"

    @property
    def services_file(self) -> Path:
        return self.registry_dir / "services.json"

    @property
    def conflicts_dir(self) -> Path:
        return self.sprout_dir / "conflicts"

    def conflict_file(self, conflict_id: str) -> Path:
        safe = validate_identifier(conflict_id, "conflict_id")
        return validate_within_root(self.conflicts_dir, f"{safe}.json")

    @property
    def staging_dir(self) -> Path:
        return self.sprout_dir / "staging"

    def fenced_staging_dir(self, mas_id: str, token: int) -> Path:
        safe_mas = validate_identifier(mas_id, "mas_id")
        if int(token) < 0:
            raise ValueError(f"Invalid negative fencing token: {token}")
        return validate_within_root(self.staging_dir, Path(safe_mas) / str(int(token)))

    @property
    def storage_dir(self) -> Path:
        return self.sprout_dir / "storage"

    def storage_file(self, relative_path: str) -> Path:
        return validate_within_root(self.storage_dir, relative_path)

    # ------------------------------------------------------------------
    # User-local app & catalog paths
    # ------------------------------------------------------------------
    @property
    def apps_dir(self) -> Path:
        return self.user_root / "apps"

    @property
    def catalog_dir(self) -> Path:
        return self.user_root / "catalog"

    @property
    def sources_file(self) -> Path:
        return self.user_root / "catalogs.json"

    @property
    def installed_registry_file(self) -> Path:
        return self.user_root / "installed_apps.json"

    @property
    def audit_log_file(self) -> Path:
        return self.sprout_dir / "governance_audit.json"

    def ensure_cell_layout(self) -> None:
        for path in (
            self.events_dir,
            self.leases_dir,
            self.locks_dir,
            self.claims_dir,
            self.nodes_dir,
            self.config_dir,
            self.registry_dir,
            self.conflicts_dir,
            self.staging_dir,
            self.storage_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

