"""Sovereign Application Installer Engine for AuraSprout.

Handles `.apkg`/source installation, offline wheelhouse installation, versioned
upgrades & rollbacks (via `active_version.json` preserving `app_data/`), lifecycle
hooks (`shell=False`), launcher/shortcut generation, uninstall, and MVenv relinking
when consolidating environments through `auraseed.mvenv.manager.MvenvManager`.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from auraseed.mvenv.manager import MvenvManager
from auraseed.mvenv.models import MvenvCreateRequest, MvenvRecord

from aurasprout.catalog.crypto import extract_archive_safely, verify_sha256
from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.models import AppManifest
from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root
from aurasprout.installer.autostart import (
    configure_user_autostart,
    create_user_desktop_shortcut,
    create_user_start_menu_shortcut,
    generate_launchers,
    resolve_entry_point_invocation,
    resolve_user_desktop_dir,
    windowless_creationflags,
)
from aurasprout.installer.registry import (
    InstalledAppRecord,
    InstalledAppsRegistry,
    InstalledServiceState,
)

_LOG = logging.getLogger(__name__)


class SproutInstallerEngine:
    """User-space, zero-privilege application installation and lifecycle engine."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        mvenv_manager: Optional[MvenvManager] = None,
        catalog_manager: Optional[SproutCatalogManager] = None,
        registry: Optional[InstalledAppsRegistry] = None,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.mvenv_manager = mvenv_manager or MvenvManager()
        self.catalog_manager = catalog_manager or SproutCatalogManager(paths=self.paths)
        self.registry = registry or InstalledAppsRegistry(paths=self.paths)

    @staticmethod
    def _has_python_project(root: Path) -> bool:
        return any(
            (root / marker).exists()
            for marker in ("pyproject.toml", "setup.py", "setup.cfg")
        )

    @staticmethod
    def _build_backend_modules(project_dir: Path) -> List[str]:
        pyproject = project_dir / "pyproject.toml"
        reqs: List[str] = []
        if pyproject.exists():
            raw_text = pyproject.read_text(encoding="utf-8", errors="ignore")
            try:
                import tomllib  # Python 3.11+

                data = tomllib.loads(raw_text)
                raw_reqs = data.get("build-system", {}).get("requires") or []
                reqs = [str(r) for r in raw_reqs]
            except Exception:
                import re

                m = re.search(r"requires\s*=\s*\[([^\]]*)\]", raw_text, re.DOTALL)
                if m:
                    reqs = re.findall(r"""["']([^"']+)["']""", m.group(1))
        if not reqs:
            reqs = ["setuptools"]

        import re

        modules: List[str] = []
        for req in reqs:
            dist = re.split(r"[<>=!~;\[\s]", req.strip(), maxsplit=1)[0].strip().lower()
            if not dist:
                continue
            mod = {
                "flit-core": "flit_core",
                "poetry-core": "poetry",
                "pdm-backend": "pdm",
            }.get(dist, dist.replace("-", "_"))
            if mod not in modules:
                modules.append(mod)
        return modules or ["setuptools"]

    @classmethod
    def _can_skip_build_isolation(cls, python_exe: Path, project_dir: Path) -> bool:
        mods = cls._build_backend_modules(project_dir)
        probe = (
            "import importlib.util, sys; "
            f"mods = {mods!r}; "
            "sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in mods) else 1)"
        )
        res = subprocess.run(
            [str(Path(python_exe).resolve()), "-c", probe],
            capture_output=True,
            text=True,
            shell=False,
            timeout=20,
        )
        return res.returncode == 0

    @staticmethod
    def _run_pip(
        python_exe: Path,
        args: List[str],
        cwd: Optional[Path] = None,
        timeout: int = 300,
    ) -> None:
        cmd = [str(Path(python_exe).resolve()), "-m", "pip", *args]
        res = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"pip command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr or res.stdout}"
            )

    @staticmethod
    def _run_hook(
        hook_name: str,
        hook_script: Path,
        python_exe: Path,
        install_dir: Path,
        app_root: Path,
        manifest: AppManifest,
        port: int,
        artifacts_dir: Optional[Path] = None,
    ) -> None:
        if not hook_script.exists():
            raise FileNotFoundError(f"Lifecycle hook '{hook_name}' not found: {hook_script}")
        env = os.environ.copy()
        resolved_artifacts = artifacts_dir
        if resolved_artifacts is None or not resolved_artifacts.exists():
            resolved_artifacts = app_root / "artifacts"
            if not resolved_artifacts.exists() and (app_root.parent / "artifacts").exists():
                resolved_artifacts = app_root.parent / "artifacts"

        hook_env = {
            "AURASPROUT_INSTALL_DIR": str(install_dir),
            "AURASPROUT_APP_ROOT": str(app_root),
            "AURASPROUT_ARTIFACTS_DIR": str(resolved_artifacts),
            "AURASPROUT_PYTHON": str(python_exe),
            "AURASPROUT_PORT": str(port),
            "AURASPROUT_APP_ID": manifest.app_id,
            "AURASPROUT_APP_NAME": manifest.name,
            "AURASPROUT_VERSION": manifest.version,
            "AURASPROUT_HOOK": hook_name,
            # Legacy compatibility variables for migrated auraseed hooks
            "AURASEED_INSTALL_DIR": str(install_dir),
            "AURASEED_APP_ROOT": str(app_root),
            "AURASEED_BUNDLE_DIR": str(app_root),
            "AURASEED_ARTIFACTS_DIR": str(resolved_artifacts),
            "AURASEED_PYTHON": str(python_exe),
            "AURASEED_PORT": str(port),
            "AURASEED_APP_NAME": manifest.app_id,
            "AURASEED_VERSION": manifest.version,
            "AURASEED_HOOK": hook_name,
        }
        env.update(hook_env)
        res = subprocess.run(
            [str(Path(python_exe).resolve()), str(Path(hook_script).resolve())],
            cwd=str(install_dir),
            env=env,
            capture_output=True,
            text=True,
            shell=False,
            timeout=180,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Lifecycle hook '{hook_name}' failed ({res.returncode}): {res.stderr or res.stdout}"
            )

    def _ensure_mvenv(
        self,
        manifest: AppManifest,
        mvenv_name: Optional[str],
        base_python: Optional[str],
        sovereign: bool,
    ) -> MvenvRecord:
        target_name = mvenv_name
        if not target_name:
            for svc in manifest.services:
                if svc.python_config and svc.python_config.managed_venv:
                    target_name = svc.python_config.managed_venv
                    break
        if not target_name:
            target_name = f"sprout-{manifest.app_id}"
        target_name = validate_identifier(target_name, "mvenv name")

        existing = self.mvenv_manager.get_venv(target_name)
        if existing and Path(existing.python_path).exists():
            return existing

        req = MvenvCreateRequest(
            name=target_name,
            base_python=base_python,
            sovereign=sovereign,
            force=existing is not None,
        )
        return self.mvenv_manager.create_venv(req)

    def _install_dependencies_and_source(
        self,
        manifest: AppManifest,
        python_exe: Path,
        payload_root: Path,
        src_root: Path,
        wheelhouse_dir: Optional[Path],
        editable: bool,
    ) -> None:
        resolved_wh = Path(wheelhouse_dir).resolve() if wheelhouse_dir else None
        if resolved_wh is None and manifest.distribution.wheelhouse_path:
            for base in (payload_root, src_root):
                try:
                    cand = validate_within_root(base, manifest.distribution.wheelhouse_path)
                    if cand.exists() and cand.is_dir():
                        resolved_wh = cand
                        break
                except ValueError:
                    continue
        if resolved_wh is None:
            for cand in (payload_root / "wheels", payload_root / "wheelhouse", src_root / "wheels", src_root / "wheelhouse"):
                if cand.exists() and cand.is_dir():
                    resolved_wh = cand
                    break

        if resolved_wh is not None and resolved_wh.exists():
            wh_files = sorted(
                p for p in resolved_wh.iterdir()
                if p.is_file() and p.suffix.lower() in (".whl", ".gz", ".zip")
            )
            req_txt = src_root / "requirements.txt"
            if req_txt.exists():
                self._run_pip(
                    python_exe,
                    ["install", "--no-index", "--find-links", str(resolved_wh), "-r", str(req_txt)],
                    cwd=src_root,
                )
            elif wh_files and not self._has_python_project(src_root):
                self._run_pip(
                    python_exe,
                    ["install", "--no-index", "--find-links", str(resolved_wh), *[str(w) for w in wh_files]],
                    cwd=src_root,
                )

        if self._has_python_project(src_root):
            pip_args = list(manifest.distribution.pip_install_args)
            if not pip_args:
                pip_args = ["install"]
                if resolved_wh is not None and resolved_wh.exists():
                    pip_args.extend(["--no-build-isolation", "--no-index", "--find-links", str(resolved_wh)])
                elif self._can_skip_build_isolation(python_exe, src_root):
                    pip_args.append("--no-build-isolation")
                if editable:
                    pip_args.extend(["-e", str(src_root)])
                else:
                    pip_args.append(str(src_root))
            elif pip_args[0] != "install":
                pip_args = ["install", *pip_args]
            self._run_pip(python_exe, pip_args, cwd=src_root)

        for rel_sub in manifest.distribution.subprojects:
            sub_dir = validate_within_root(src_root, rel_sub)
            if not sub_dir.exists():
                raise FileNotFoundError(f"Configured subproject not found: {sub_dir}")
            if resolved_wh is not None and resolved_wh.exists():
                sub_args = ["install", "--no-index", "--find-links", str(resolved_wh), "--no-build-isolation"]
            elif self._can_skip_build_isolation(python_exe, sub_dir):
                sub_args = ["install", "--no-build-isolation"]
            else:
                sub_args = ["install"]
            sub_args.extend(["-e", str(sub_dir)] if editable else [str(sub_dir)])
            self._run_pip(python_exe, sub_args, cwd=sub_dir)

    def _copy_app_artifacts(
        self,
        manifest: AppManifest,
        payload_root: Path,
        src_root: Path,
        version_dir: Path,
    ) -> None:
        if (payload_root / "artifacts").exists():
            shutil.copytree(payload_root / "artifacts", version_dir / "artifacts", dirs_exist_ok=True)
        for pattern in manifest.distribution.artifacts:
            for matched in src_root.glob(pattern):
                if matched.is_file():
                    rel = matched.relative_to(src_root)
                    dest = validate_within_root(version_dir / "artifacts", rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(matched, dest)

    def install_from_directory(
        self,
        source_dir: Path,
        dest_dir: Optional[Path] = None,
        mvenv_name: Optional[str] = None,
        base_python: Optional[str] = None,
        editable: bool = False,
        create_shortcut: bool = False,
        enable_autostart: bool = False,
        port: Optional[int] = None,
        channel: str = "stable",
        sovereign: bool = True,
        desktop_dir: Optional[Path] = None,
        skills_dir: Optional[Path] = None,
    ) -> InstalledAppRecord:
        payload_root = Path(source_dir).resolve()
        if not payload_root.exists():
            raise FileNotFoundError(f"Application source directory not found: {payload_root}")

        manifest = AppManifest.from_file(payload_root)
        app_id = validate_identifier(manifest.app_id, "appId")
        ver = validate_identifier(manifest.version, "version")

        is_packaged_archive = (
            (payload_root / "app.manifest.json").exists()
            and (payload_root / "src").is_dir()
            and not self._has_python_project(payload_root)
        )
        src_root = (
            payload_root / "src"
            if (
                is_packaged_archive
                or (payload_root / "src" / "app.manifest.json").exists()
                or ((payload_root / "src").is_dir() and self._has_python_project(payload_root / "src"))
            )
            else payload_root
        )

        install_root = (
            Path(dest_dir).resolve()
            if dest_dir is not None
            else validate_within_root(self.registry.apps_dir, app_id)
        )
        versions_root = install_root / "versions"
        version_dir = validate_within_root(versions_root, ver)
        app_data_dir = install_root / "app_data"
        install_root.mkdir(parents=True, exist_ok=True)
        versions_root.mkdir(parents=True, exist_ok=True)
        app_data_dir.mkdir(parents=True, exist_ok=True)

        # Preserve previous versions & app_data across upgrades
        existing_record = self.registry.get(app_id)
        prev_versions = list(existing_record.previous_versions) if existing_record else []
        if existing_record and existing_record.version != ver and existing_record.version not in prev_versions:
            prev_versions.append(existing_record.version)

        if version_dir.exists():
            shutil.rmtree(version_dir)
        version_dir.mkdir(parents=True, exist_ok=True)

        src_copy_target = version_dir / "src"
        shutil.copytree(
            src_root,
            src_copy_target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache", ".venv", "venv"),
        )
        manifest.to_file(version_dir / "app.manifest.json")
        self._copy_app_artifacts(manifest, payload_root, src_root, version_dir)

        mvenv_rec = self._ensure_mvenv(manifest, mvenv_name, base_python, sovereign=sovereign)
        python_exe = Path(mvenv_rec.python_path).resolve()
        assigned_port = int(port or manifest.distribution.default_port or 8000)

        artifacts_target = version_dir / "artifacts"
        pre_hook_rel = manifest.distribution.hooks.get("pre_install")
        if pre_hook_rel:
            hook_base = payload_root if (payload_root / pre_hook_rel).exists() else src_root
            hook_path = validate_within_root(hook_base, pre_hook_rel)
            self._run_hook(
                "pre_install",
                hook_path,
                python_exe,
                install_root,
                payload_root,
                manifest,
                assigned_port,
                artifacts_dir=artifacts_target,
            )

        install_target_src = src_root if editable else src_copy_target
        self._install_dependencies_and_source(
            manifest=manifest,
            python_exe=python_exe,
            payload_root=payload_root,
            src_root=install_target_src,
            wheelhouse_dir=None,
            editable=editable,
        )

        post_hook_rel = manifest.distribution.hooks.get("post_install")
        if post_hook_rel:
            hook_base = payload_root if (payload_root / post_hook_rel).exists() else src_root
            hook_path = validate_within_root(hook_base, post_hook_rel)
            self._run_hook(
                "post_install",
                hook_path,
                python_exe,
                install_root,
                payload_root,
                manifest,
                assigned_port,
                artifacts_dir=artifacts_target,
            )

        for raw_cmd in manifest.distribution.post_install_commands:
            cmd_str = (
                str(raw_cmd)
                .replace("{python}", str(python_exe))
                .replace("{port}", str(assigned_port))
                .replace("{install_dir}", str(install_root))
                .replace("{app_root}", str(payload_root))
                .replace("{artifacts_dir}", str(artifacts_target))
                .strip()
            )
            if not cmd_str:
                continue
            tokens = shlex.split(cmd_str, posix=(platform.system().lower() != "windows"))
            if not tokens:
                continue
            cmd_res = subprocess.run(
                tokens,
                cwd=str(install_root),
                capture_output=True,
                text=True,
                shell=False,
                timeout=120,
            )
            if cmd_res.returncode != 0:
                raise RuntimeError(
                    f"post_install_commands entry {raw_cmd!r} failed: {cmd_res.stderr or cmd_res.stdout}"
                )

        entrypoint_cmd = manifest.distribution.entrypoint_command or (
            manifest.gui_entry_points[0].entry_command if manifest.gui_entry_points else f"{app_id}"
        )
        launchers: Dict[str, str] = {}
        if manifest.distribution.create_launchers:
            launchers = generate_launchers(
                install_dir=install_root,
                app_id=app_id,
                python_exe=python_exe,
                entrypoint_command=entrypoint_cmd,
                port=assigned_port,
                mvenv_root=Path(mvenv_rec.path),
            )
            primary_launcher = (
                launchers.get("bat") or launchers.get("ps1") or launchers.get("sh")
            )
            sm_target = Path(launchers["pythonw"]) if (launchers.get("pythonw") and launchers.get("pyw")) else (
                Path(primary_launcher) if primary_launcher else None
            )
            sm_args = f'"{launchers["pyw"]}"' if (launchers.get("pythonw") and launchers.get("pyw")) else ""
            if sm_target is not None:
                resolved_icon: Optional[Path] = None
                raw_icon = (
                    manifest.gui_entry_points[0].icon_path
                    if manifest.gui_entry_points and manifest.gui_entry_points[0].icon_path
                    else None
                )
                if raw_icon:
                    for base_dir in (src_copy_target, version_dir, artifacts_target):
                        try:
                            cand_icon = validate_within_root(base_dir, raw_icon)
                        except ValueError:
                            continue
                        if cand_icon.exists() and cand_icon.is_file():
                            resolved_icon = cand_icon
                            break
                sm_shortcut = create_user_start_menu_shortcut(
                    app_id=app_id,
                    display_name=manifest.name,
                    launcher_path=sm_target,
                    arguments=sm_args,
                    working_dir=app_data_dir,
                    description=manifest.description or manifest.name,
                    icon_path=resolved_icon,
                    refresh_cache=True,
                )
                if sm_shortcut is not None:
                    launchers["start_menu_shortcut"] = str(sm_shortcut)
            if create_shortcut and manifest.distribution.create_desktop_shortcut and primary_launcher:
                desk_launcher = Path(launchers["pyw"]) if (launchers.get("pythonw") and launchers.get("pyw")) else Path(primary_launcher)
                shortcut = create_user_desktop_shortcut(
                    app_id=app_id,
                    display_name=manifest.name,
                    launcher_path=desk_launcher,
                    desktop_dir=desktop_dir,
                    pythonw_path=Path(launchers["pythonw"]) if launchers.get("pythonw") else None,
                )
                if shortcut is not None:
                    launchers["desktop_shortcut"] = str(shortcut)
            if enable_autostart and primary_launcher:
                autostart_path = configure_user_autostart(app_id, [primary_launcher], enabled=True)
                if autostart_path is not None:
                    launchers["autostart"] = str(autostart_path)

        InstalledAppsRegistry.write_active_version_pointer(
            install_dir=install_root,
            app_id=app_id,
            version=ver,
            previous_versions=prev_versions,
        )

        # Preserve prior volunteer opt-in preference when upgrading; default STRICTLY False for new installs.
        prev_vol: Dict[str, bool] = {}
        if existing_record:
            prev_vol = {s.mas_id: s.volunteer_enabled for s in existing_record.services}

        service_states = [
            InstalledServiceState(
                mas_id=svc.mas_id,
                mode=svc.mode,
                runtime=svc.runtime,
                display_name=svc.display_name,
                volunteer_enabled=bool(prev_vol.get(svc.mas_id, False)),
                state="Installed",
            )
            for svc in manifest.services
        ]

        registered_skills: List[str] = []
        if manifest.distribution.skills:
            skills_root = (
                Path(skills_dir).resolve()
                if skills_dir is not None
                else (Path.home() / ".agents" / "skills").resolve()
            )
            skills_root.mkdir(parents=True, exist_ok=True)
            for rel_skill in manifest.distribution.skills:
                resolved_skill: Optional[Path] = None
                for base_dir in (src_copy_target, version_dir):
                    try:
                        cand = validate_within_root(base_dir, rel_skill)
                    except ValueError:
                        continue
                    if cand.exists():
                        resolved_skill = cand
                        break
                if resolved_skill is None:
                    continue
                if resolved_skill.is_dir():
                    skill_name = validate_identifier(resolved_skill.name, "skillName")
                    cand_dest = skills_root / skill_name
                    if hasattr(cand_dest, "is_junction") and cand_dest.is_junction():
                        try:
                            os.rmdir(cand_dest)
                        except OSError:
                            pass
                    elif cand_dest.is_symlink():
                        try:
                            cand_dest.unlink()
                        except OSError:
                            pass
                    dest_skill = validate_within_root(skills_root, skill_name)
                    shutil.copytree(resolved_skill, dest_skill, dirs_exist_ok=True)
                    registered_skills.append(str(dest_skill))
                    try:
                        from aurasprout.installer.harness import link_skill_to_harnesses
                        for hl in link_skill_to_harnesses(dest_skill):
                            registered_skills.append(str(hl))
                    except (ImportError, OSError):
                        pass
                elif resolved_skill.is_file():
                    parent_name = resolved_skill.parent.name
                    skill_name = validate_identifier(
                        parent_name if parent_name not in ("", "skills") else app_id,
                        "skillName",
                    )
                    dest_skill = validate_within_root(skills_root, skill_name)
                    dest_skill.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(resolved_skill, dest_skill / resolved_skill.name)
                    registered_skills.append(str(dest_skill))
                    try:
                        from aurasprout.installer.harness import link_skill_to_harnesses
                        for hl in link_skill_to_harnesses(dest_skill):
                            registered_skills.append(str(hl))
                    except (ImportError, OSError):
                        pass

        record = InstalledAppRecord(
            app_id=app_id,
            name=manifest.name,
            version=ver,
            install_path=str(install_root),
            mvenv_name=mvenv_rec.name,
            mvenv_path=mvenv_rec.path,
            python_path=str(python_exe),
            channel=channel,
            port=assigned_port,
            services=service_states,
            launchers=launchers,
            registered_skills=registered_skills,
            previous_versions=prev_versions,
        )
        self.registry.register(record)
        return record

    def install_from_package(
        self,
        package_path: Path,
        dest_dir: Optional[Path] = None,
        expected_sha256: Optional[str] = None,
        mvenv_name: Optional[str] = None,
        base_python: Optional[str] = None,
        create_shortcut: bool = False,
        enable_autostart: bool = False,
        port: Optional[int] = None,
        channel: str = "stable",
        sovereign: bool = True,
        desktop_dir: Optional[Path] = None,
        skills_dir: Optional[Path] = None,
    ) -> InstalledAppRecord:
        pkg = Path(package_path).resolve()
        if not pkg.exists():
            raise FileNotFoundError(f"Package archive not found: {pkg}")
        if expected_sha256:
            verify_sha256(pkg, expected_sha256)

        staging = Path(tempfile.mkdtemp(prefix="aurasprout_install_"))
        try:
            extract_archive_safely(pkg, staging)
            return self.install_from_directory(
                source_dir=staging,
                dest_dir=dest_dir,
                mvenv_name=mvenv_name,
                base_python=base_python,
                editable=False,
                create_shortcut=create_shortcut,
                enable_autostart=enable_autostart,
                port=port,
                channel=channel,
                sovereign=sovereign,
                desktop_dir=desktop_dir,
                skills_dir=skills_dir,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def install_from_catalog(
        self,
        source: str,
        app_id: str,
        version: Optional[str] = None,
        channel: Optional[str] = "stable",
        dest_dir: Optional[Path] = None,
        mvenv_name: Optional[str] = None,
        base_python: Optional[str] = None,
        create_shortcut: bool = False,
        port: Optional[int] = None,
    ) -> InstalledAppRecord:
        dl_dir = Path(tempfile.mkdtemp(prefix="aurasprout_cat_install_"))
        try:
            bundle_path, ver_entry = self.catalog_manager.download_verified_package(
                source=source,
                app_id=app_id,
                version=version,
                channel=channel,
                dest_dir=dl_dir,
            )
            return self.install_from_package(
                package_path=bundle_path,
                dest_dir=dest_dir,
                expected_sha256=ver_entry.bundle_sha256 or None,
                mvenv_name=mvenv_name,
                base_python=base_python,
                create_shortcut=create_shortcut,
                port=port or ver_entry.default_port,
                channel=ver_entry.channel,
            )
        finally:
            shutil.rmtree(dl_dir, ignore_errors=True)

    def rollback_app(
        self,
        app_id: str,
        target_version: Optional[str] = None,
    ) -> InstalledAppRecord:
        safe_id = validate_identifier(app_id, "appId")
        record = self.registry.get(safe_id)
        if record is None:
            raise KeyError(f"Application {safe_id!r} is not installed")

        install_root = Path(record.install_path).resolve()
        candidate_version = target_version or (
            record.previous_versions[-1] if record.previous_versions else None
        )
        if not candidate_version:
            raise ValueError(f"No previous version available to rollback for {safe_id!r}")

        ver_safe = validate_identifier(candidate_version, "version")
        target_dir = validate_within_root(install_root / "versions", ver_safe)
        if not target_dir.exists():
            raise FileNotFoundError(f"Rollback version directory missing: {target_dir}")

        manifest = AppManifest.from_file(target_dir)
        updated_prev = [v for v in record.previous_versions if v != ver_safe]
        if record.version not in updated_prev:
            updated_prev.append(record.version)

        InstalledAppsRegistry.write_active_version_pointer(
            install_dir=install_root,
            app_id=safe_id,
            version=ver_safe,
            previous_versions=updated_prev,
        )
        record.version = ver_safe
        record.previous_versions = updated_prev
        if manifest.services:
            vol_map = {s.mas_id: s.volunteer_enabled for s in record.services}
            record.services = [
                InstalledServiceState(
                    mas_id=svc.mas_id,
                    mode=svc.mode,
                    runtime=svc.runtime,
                    display_name=svc.display_name,
                    volunteer_enabled=bool(vol_map.get(svc.mas_id, False)),
                    state="Installed",
                )
                for svc in manifest.services
            ]
        self.registry.register(record)
        return record

    def uninstall_app(self, app_id: str, remove_mvenv: bool = False) -> bool:
        safe_id = validate_identifier(app_id, "appId")
        record = self.registry.get(safe_id)
        if record is None:
            return False

        shortcut_str = record.launchers.get("desktop_shortcut")
        if shortcut_str:
            shortcut_path = Path(shortcut_str)
            if shortcut_path.exists():
                shortcut_path.unlink()
        else:
            d_dir = resolve_user_desktop_dir()
            for cand in (d_dir / f"{safe_id}.cmd", d_dir / f"aurasprout-{safe_id}.desktop"):
                if cand.exists():
                    cand.unlink()

        sm_str = record.launchers.get("start_menu_shortcut")
        if sm_str:
            sm_path = Path(sm_str)
            if sm_path.exists():
                sm_path.unlink()
                from auraseed.lifecycle import refresh_os_shell_cache

                refresh_os_shell_cache(sm_path.parent)

        configure_user_autostart(safe_id, [], enabled=False)

        for skill_path_str in record.registered_skills:
            skill_path = Path(skill_path_str)
            if skill_path.exists():
                if skill_path.is_dir():
                    shutil.rmtree(skill_path, ignore_errors=True)
                else:
                    skill_path.unlink(missing_ok=True)

        install_root = Path(record.install_path)
        if install_root.exists():
            shutil.rmtree(install_root, ignore_errors=True)

        if remove_mvenv and record.mvenv_name:
            self.mvenv_manager.delete_venv(record.mvenv_name)

        self.registry.unregister(safe_id)
        return True

    def relink_app_mvenv(self, source_mvenvs: List[str], target_mvenv: str) -> List[str]:
        """Repoint callback invoked by `MvenvManager.merge_venvs(..., repoint_callback=...)`."""
        target_rec = self.mvenv_manager.get_venv(target_mvenv)
        if target_rec is None:
            raise KeyError(f"Target merged mvenv {target_mvenv!r} not found")

        source_set = {s.strip() for s in source_mvenvs}
        repointed: List[str] = []
        target_py = Path(target_rec.python_path).resolve()
        target_venv_root = Path(target_rec.path).resolve()

        for app_rec in self.registry.list_all():
            if app_rec.mvenv_name not in source_set:
                continue
            install_root = Path(app_rec.install_path).resolve()
            manifest_path = install_root / "versions" / app_rec.version / "app.manifest.json"
            entrypoint_cmd = f"{app_rec.app_id}"
            if manifest_path.exists():
                manifest = AppManifest.from_file(manifest_path)
                entrypoint_cmd = manifest.distribution.entrypoint_command or entrypoint_cmd

            app_rec.mvenv_name = target_rec.name
            app_rec.mvenv_path = str(target_venv_root)
            app_rec.python_path = str(target_py)
            if app_rec.launchers:
                new_launchers = generate_launchers(
                    install_dir=install_root,
                    app_id=app_rec.app_id,
                    python_exe=target_py,
                    entrypoint_command=entrypoint_cmd,
                    port=app_rec.port,
                    mvenv_root=target_venv_root,
                )
                if "desktop_shortcut" in app_rec.launchers:
                    new_launchers["desktop_shortcut"] = app_rec.launchers["desktop_shortcut"]
                if "start_menu_shortcut" in app_rec.launchers:
                    new_launchers["start_menu_shortcut"] = app_rec.launchers["start_menu_shortcut"]
                app_rec.launchers = new_launchers
            self.registry.register(app_rec)
            repointed.append(app_rec.app_id)

        return repointed

    def get_app_manifest(self, app_id: str) -> Optional[AppManifest]:
        """Load the canonical `app.manifest.json` for an installed app's active version, if present."""
        safe_id = validate_identifier(app_id, "appId")
        record = self.registry.get(safe_id)
        if record is None:
            return None
        install_root = Path(record.install_path).resolve()
        manifest_path = install_root / "versions" / record.version / "app.manifest.json"
        if not manifest_path.exists():
            return None
        return AppManifest.from_file(manifest_path)

    def get_app_details(self, app_id: str) -> Dict[str, Any]:
        """Return full installed app metadata, manifest fields, services, and GUI entry points."""
        safe_id = validate_identifier(app_id, "appId")
        record = self.registry.get(safe_id)
        if record is None:
            raise KeyError(f"Application {safe_id!r} is not installed")
        manifest = self.get_app_manifest(safe_id)
        install_root = Path(record.install_path).resolve()
        app_data_dir = install_root / "app_data"

        entry_points: List[Dict[str, Any]] = []
        if manifest and manifest.gui_entry_points:
            for ep in manifest.gui_entry_points:
                entry_points.append(
                    {
                        "name": ep.name,
                        "context": ep.context,
                        "entryCommand": ep.entry_command,
                        "description": ep.description or "",
                        "iconPath": ep.icon_path or "",
                    }
                )
        else:
            default_cmd = (manifest.distribution.entrypoint_command if manifest else "") or safe_id
            entry_points.append(
                {
                    "name": "default",
                    "context": "Python",
                    "entryCommand": default_cmd,
                    "description": "Default application entrypoint",
                    "iconPath": "",
                }
            )

        return {
            "appId": record.app_id,
            "name": record.name,
            "version": record.version,
            "channel": record.channel,
            "description": manifest.description if manifest else "",
            "installPath": str(install_root),
            "appDataDir": str(app_data_dir if app_data_dir.exists() else install_root),
            "mvenvName": record.mvenv_name or "",
            "mvenvPath": record.mvenv_path or "",
            "pythonPath": record.python_path or "",
            "port": record.port,
            "previousVersions": list(record.previous_versions),
            "installedAt": record.installed_at,
            "updatedAt": record.updated_at,
            "tags": list(manifest.distribution.tags) if manifest else [],
            "services": [s.to_dict() for s in record.services],
            "guiEntryPoints": entry_points,
            "launchers": dict(record.launchers),
            "providesContracts": list(manifest.provides_contracts) if manifest else [],
            "dependencies": list(manifest.dependencies) if manifest else [],
        }

    def launch_entry_point(
        self,
        app_id: str,
        entry_point_name: Optional[str] = None,
        port: Optional[int] = None,
        extra_args: Optional[List[str]] = None,
    ) -> subprocess.Popen[Any]:
        """Manually launch a specific GUI entry point (or the primary launcher) for an installed app."""
        safe_id = validate_identifier(app_id, "appId")
        record = self.registry.get(safe_id)
        if record is None:
            raise KeyError(f"Application {safe_id!r} is not installed")
        manifest = self.get_app_manifest(safe_id)
        assigned_port = int(port or record.port or 8000)

        install_root = Path(record.install_path).resolve()
        app_data_dir = install_root / "app_data"
        work_dir = app_data_dir if app_data_dir.exists() else install_root

        context = "Python"
        entry_cmd = ""
        if manifest and manifest.gui_entry_points:
            chosen = None
            if entry_point_name:
                for ep in manifest.gui_entry_points:
                    if ep.name == entry_point_name:
                        chosen = ep
                        break
                if chosen is None:
                    raise KeyError(f"GUI entry point {entry_point_name!r} not found for {safe_id!r}")
            else:
                chosen = manifest.gui_entry_points[0]
            context = chosen.context
            entry_cmd = chosen.entry_command
        else:
            if entry_point_name and entry_point_name != "default":
                raise KeyError(f"GUI entry point {entry_point_name!r} not found for {safe_id!r}")
            entry_cmd = (manifest.distribution.entrypoint_command if manifest else "") or safe_id

        env = os.environ.copy()
        env.update(
            {
                "AURASPROUT_APP_ID": safe_id,
                "AURASPROUT_INSTALL_DIR": str(install_root),
                "AURASPROUT_PORT": str(assigned_port),
            }
        )

        if context == "Python":
            py_str = record.python_path
            if not py_str and record.mvenv_name:
                mv = self.mvenv_manager.get_venv(record.mvenv_name)
                py_str = mv.python_path if mv else None
            if not py_str:
                raise RuntimeError(f"No Python interpreter configured for installed app {safe_id!r}")
            argv, env_updates = resolve_entry_point_invocation(
                python_exe=Path(py_str),
                entry_command=entry_cmd,
                port=assigned_port,
                mvenv_root=Path(record.mvenv_path) if record.mvenv_path else None,
                extra_args=extra_args,
            )
            env.update(env_updates)
        elif context == "PowerShell":
            rendered = entry_cmd.replace("{port}", str(assigned_port))
            ps_exe = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
            if not ps_exe:
                raise RuntimeError("PowerShell executable not found on PATH")
            argv = [ps_exe, "-NoProfile", "-Command", rendered]
        else:
            rendered = entry_cmd.replace("{port}", str(assigned_port))
            argv = shlex.split(rendered, posix=(platform.system().lower() != "windows"))
            if extra_args:
                argv.extend(str(a) for a in extra_args)

        popen_kwargs: Dict[str, Any] = {
            "cwd": str(work_dir),
            "env": env,
            "shell": False,
        }
        cflags = windowless_creationflags()
        if cflags:
            popen_kwargs["creationflags"] = cflags

        return subprocess.Popen(argv, **popen_kwargs)
