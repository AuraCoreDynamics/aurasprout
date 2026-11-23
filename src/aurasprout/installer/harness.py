"""Agent client harness discovery and companion skill junction linking for AuraSprout.

Detects installed agent harnesses (Claude Code, Antigravity/Gemini, OpenCode, Cursor,
and workspace-level .agents/skills) and creates directory junctions / symlinks from
harness skill roots to canonical AuraSprout installed application companion skills.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import List, Tuple


def get_detected_harness_roots() -> List[Tuple[str, Path]]:
    """Detect installed agent client harness paths on the host system.

    Returns list of (harness_name, skills_directory_path).
    Installs into all discovered harnesses (not just the first one).
    """
    home = Path.home()
    candidates = [
        ("claude", home / ".claude" / "skills"),
        ("gemini_config", home / ".gemini" / "config" / "skills"),
        ("gemini_antigravity", home / ".gemini" / "antigravity" / "skills"),
        ("opencode", home / ".opencode" / "skills"),
        ("cursor", home / ".cursor" / "skills"),
    ]
    detected: List[Tuple[str, Path]] = []
    seen_paths = set()

    for name, skills_dir in candidates:
        parent = skills_dir.parent
        # Detected if parent harness configuration dir exists or skills dir exists
        if parent.exists() or skills_dir.exists():
            resolved = str(skills_dir.resolve())
            if resolved not in seen_paths:
                detected.append((name, skills_dir))
                seen_paths.add(resolved)

    # Detect workspace .agents/skills roots (hierarchical traversal from CWD and known project roots)
    workspace_candidates = [Path.cwd().resolve()]
    if os.name == "nt":
        workspace_candidates.extend([Path("C:/projects"), home / "projects"])
    else:
        workspace_candidates.extend([Path("/projects"), home / "projects"])

    for base in workspace_candidates:
        cur = base
        visited = set()
        while cur and cur != cur.parent and cur not in visited:
            visited.add(cur)
            ws_agents_skills = cur / ".agents" / "skills"
            if ws_agents_skills.exists() or (cur / ".agents").exists():
                resolved = str(ws_agents_skills.resolve())
                if resolved not in seen_paths:
                    detected.append(("antigravity_workspace", ws_agents_skills))
                    seen_paths.add(resolved)
                break
            cur = cur.parent

    return detected


def sync_antigravity_global_config(skills_source_root: Path) -> None:
    """Ensure Antigravity global configuration (~/.gemini/config/skills.json)

    includes the global agent skills directory.
    """
    home = Path.home()
    config_dir = home / ".gemini" / "config"
    if not config_dir.exists():
        return

    skills_json_path = config_dir / "skills.json"
    data = {"entries": []}
    if skills_json_path.exists():
        try:
            with open(skills_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {"entries": []}

    entries = data.get("entries", [])
    path_str = str(skills_source_root)
    # Check if entry already exists
    already_present = any(
        isinstance(e, dict)
        and e.get("path") in (path_str, "~/.agents/skills", str(skills_source_root.resolve()))
        for e in entries
    )
    if not already_present:
        entries.append({"path": str(skills_source_root)})
        data["entries"] = entries
        try:
            with open(skills_json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass


def link_skill_to_harnesses(skill_source_dir: Path) -> List[Path]:
    """Link a source skill directory to all detected client harness skill roots.

    Creates directory junctions on Windows or symlinks on Unix, falling back to copytree.
    Installs into all discovered harnesses (not just the first one).
    Returns list of destination skill paths.
    """
    if not skill_source_dir.is_dir():
        return []

    # Sync global Antigravity config if ~/.gemini/config exists
    if skill_source_dir.parent:
        sync_antigravity_global_config(skill_source_dir.parent)

    skill_name = skill_source_dir.name
    linked_paths: List[Path] = []
    harnesses = get_detected_harness_roots()

    for harness_name, harness_root in harnesses:
        try:
            if skill_source_dir.resolve().is_relative_to(harness_root.resolve()):
                continue
        except (ValueError, RuntimeError):
            pass

        target_dir = harness_root / skill_name
        harness_root.mkdir(parents=True, exist_ok=True)

        if target_dir.exists():
            if hasattr(target_dir, "is_junction") and target_dir.is_junction():
                try:
                    if os.readlink(target_dir) == str(skill_source_dir.resolve()):
                        linked_paths.append(target_dir)
                        continue
                    os.rmdir(target_dir)
                except OSError:
                    pass
            elif target_dir.is_symlink():
                try:
                    target_dir.unlink()
                except OSError:
                    pass
            else:
                continue

        linked = False
        if os.name == "nt":
            # Create NTFS directory junction (zero admin privileges required)
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(target_dir), str(skill_source_dir.resolve())],
                    capture_output=True,
                    check=False,
                )
                if target_dir.exists():
                    linked = True
            except OSError:
                linked = False

        if not linked:
            # Fallback to symlink
            try:
                target_dir.symlink_to(skill_source_dir.resolve(), target_is_directory=True)
                linked = True
            except (OSError, NotImplementedError):
                linked = False

        if not linked:
            # Fallback to direct copy
            try:
                shutil.copytree(skill_source_dir, target_dir, dirs_exist_ok=True)
                linked = True
            except OSError:
                linked = False

        if linked:
            linked_paths.append(target_dir)

    return linked_paths
