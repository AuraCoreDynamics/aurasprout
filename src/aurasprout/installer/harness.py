"""Agent client harness discovery and companion skill junction linking for AuraSprout.

Detects installed agent harnesses (Claude Code, Antigravity/Gemini, OpenCode, Cursor)
and creates directory junctions / symlinks from harness skill roots to canonical
AuraSprout installed application companion skills.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import List, Tuple


def get_detected_harness_roots() -> List[Tuple[str, Path]]:
    """Detect installed agent client harness paths on the host system.

    Returns list of (harness_name, skills_directory_path).
    """
    home = Path.home()
    candidates = [
        ("agents", home / ".agents" / "skills"),
        ("claude", home / ".claude" / "skills"),
        ("gemini_antigravity", home / ".gemini" / "antigravity" / "skills"),
        ("opencode", home / ".opencode" / "skills"),
        ("cursor", home / ".cursor" / "skills"),
    ]
    detected: List[Tuple[str, Path]] = []
    for name, skills_dir in candidates:
        parent = skills_dir.parent
        # Detected if parent harness configuration dir exists, skills dir exists, or base agents
        if parent.exists() or skills_dir.exists() or name == "agents":
            detected.append((name, skills_dir))
    return detected


def link_skill_to_harnesses(skill_source_dir: Path) -> List[Path]:
    """Link a source skill directory to all detected client harness skill roots.

    Creates directory junctions on Windows or symlinks on Unix, falling back to copytree.
    Returns list of destination skill paths.
    """
    if not skill_source_dir.is_dir():
        return []

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
            continue

        linked = False
        if os.name == "nt":
            # Create NTFS directory junction (zero admin privileges required)
            try:
                res = subprocess.run(
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
