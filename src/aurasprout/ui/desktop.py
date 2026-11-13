"""Tkinter Desktop Hub for AuraSprout composing `auraseed.gui.MvenvManagerPanel`."""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    HAS_TK = True
except ImportError:
    HAS_TK = False

from auraseed.gui import MvenvManagerPanel
from auraseed.mvenv.manager import MvenvManager

from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.models import CatalogManifest
from aurasprout.catalog.sources import CatalogSourcesRegistry
from aurasprout.contracts.paths import SproutPaths
from aurasprout.installer.engine import SproutInstallerEngine
from aurasprout.installer.registry import InstalledAppsRegistry
from aurasprout.ui.server import ManagementServerState, SproutManagementServer

_LOG = logging.getLogger(__name__)


def _open_folder_in_explorer(folder: Path) -> None:
    target = str(Path(folder).resolve())
    sys_name = platform.system().lower()
    if sys_name == "windows" and hasattr(os, "startfile"):
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys_name == "darwin":
        subprocess.Popen(["open", target], shell=False)
    else:
        subprocess.Popen(["xdg-open", target], shell=False)


class SproutDesktopHub:
    """Unified Desktop Hub for managing catalogs, inspecting app details, launching entry points, and MVenvs."""

    def __init__(
        self,
        root: Optional[Any] = None,
        paths: Optional[SproutPaths] = None,
        mvenv_manager: Optional[MvenvManager] = None,
        installer_engine: Optional[SproutInstallerEngine] = None,
    ) -> None:
        if not HAS_TK:
            raise RuntimeError("Tkinter is not available in the current Python environment.")

        self.paths = paths or SproutPaths()
        self.mvenv_manager = mvenv_manager or MvenvManager()
        self.registry = InstalledAppsRegistry(paths=self.paths)
        self.sources_registry = CatalogSourcesRegistry(paths=self.paths)
        self.catalog_manager = SproutCatalogManager(paths=self.paths)
        self.installer = installer_engine or SproutInstallerEngine(
            paths=self.paths,
            mvenv_manager=self.mvenv_manager,
            registry=self.registry,
        )
        self.root = root or tk.Tk()
        self.root.title("AuraSprout - User-Space Cell & Application Hub")
        self.root.geometry("1120x760")
        self.root.minsize(880, 580)
        try:
            ico_path = Path(__file__).resolve().parent / "static" / "aurasprout.ico"
            if ico_path.exists():
                self.root.iconbitmap(str(ico_path))
        except Exception as exc:
            _LOG.debug("Window iconbitmap skipped: %s", exc)
        self._web_server: Optional[SproutManagementServer] = None
        self._loaded_catalog: Optional[CatalogManifest] = None
        self._source_choices: List[Dict[str, Any]] = []
        self._current_app_details: Optional[Dict[str, Any]] = None
        self._launched_procs: List[Dict[str, Any]] = []
        self._busy = False

        self._build_ui()
        self.refresh_apps()
        self.refresh_sources()

    def close(self) -> None:
        if self._web_server is not None:
            try:
                self._web_server.stop()
            finally:
                self._web_server = None
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception as exc:
                _LOG.debug("Tk root destroy skipped: %s", exc)
            finally:
                self.root = None

    def _set_status(self, text: str) -> None:
        if hasattr(self, "status_var"):
            self.status_var.set(text)

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(12, 10))
        header.pack(fill=tk.X)
        ttk.Label(header, text="AuraSprout", font=("Segoe UI", 16, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="  Zero-Privilege Cell & App Manager", font=("Segoe UI", 10)).pack(
            side=tk.LEFT, pady=(4, 0)
        )
        ttk.Button(header, text="Open Web Portal", command=self.open_web_portal).pack(side=tk.RIGHT)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

        self._build_installed_apps_tab()
        self._build_catalog_tab()

        # Tab 3: Composed AuraSeed MVenv Manager Panel with relink callback
        mvenv_tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(mvenv_tab, text="Runtimes & MVenvs (AuraSeed)")
        self.mvenv_panel = MvenvManagerPanel(
            mvenv_tab,
            mvenv_manager=self.mvenv_manager,
            repoint_callback=self.installer.relink_app_mvenv,
            on_mvenv_changed=self.refresh_apps,
        )
        self.mvenv_panel.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor=tk.W, padding=(12, 4))
        status_bar.pack(fill=tk.X)

    def _build_installed_apps_tab(self) -> None:
        apps_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(apps_tab, text="Installed Applications")

        paned = ttk.PanedWindow(apps_tab, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # Top pane: list + primary actions
        top_pane = ttk.Frame(paned)
        paned.add(top_pane, weight=2)

        top_apps = ttk.Frame(top_pane)
        top_apps.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(top_apps, text="Refresh", command=self.refresh_apps).pack(side=tk.LEFT)
        ttk.Button(top_apps, text="Launch Default", command=self.launch_selected_app).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top_apps, text="Rollback", command=self.rollback_selected_app).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top_apps, text="Uninstall", command=self.uninstall_selected_app).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top_apps, text="Install Local...", command=self.install_local_package).pack(side=tk.RIGHT)

        cols = ("appId", "name", "version", "channel", "port", "mvenv")
        self.apps_tree = ttk.Treeview(top_pane, columns=cols, show="headings", selectmode="browse", height=7)
        self.apps_tree.heading("appId", text="App ID")
        self.apps_tree.heading("name", text="Name")
        self.apps_tree.heading("version", text="Version")
        self.apps_tree.heading("channel", text="Channel")
        self.apps_tree.heading("port", text="Port")
        self.apps_tree.heading("mvenv", text="Managed MVenv")
        self.apps_tree.column("appId", width=130)
        self.apps_tree.column("name", width=320)
        self.apps_tree.column("version", width=90)
        self.apps_tree.column("channel", width=90)
        self.apps_tree.column("port", width=70)
        self.apps_tree.column("mvenv", width=160)
        self.apps_tree.pack(fill=tk.BOTH, expand=True)
        self.apps_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_app_selected())

        # Bottom pane: App Details & Entry Points
        detail_pane = ttk.LabelFrame(paned, text="Application Details & Entry Points", padding=6)
        paned.add(detail_pane, weight=3)

        self.detail_notebook = ttk.Notebook(detail_pane)
        self.detail_notebook.pack(fill=tk.BOTH, expand=True)

        # Detail sub-tab 1: Entry Points & Summary
        ep_tab = ttk.Frame(self.detail_notebook, padding=6)
        self.detail_notebook.add(ep_tab, text="GUI Entry Points & Overview")

        meta_grid = ttk.Frame(ep_tab)
        meta_grid.pack(fill=tk.X, pady=(0, 6))

        self.detail_title_var = tk.StringVar(value="Select an installed application to view details.")
        ttk.Label(meta_grid, textvariable=self.detail_title_var, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=tk.W
        )

        self.detail_meta_var = tk.StringVar(value="")
        ttk.Label(meta_grid, textvariable=self.detail_meta_var, justify=tk.LEFT, wraplength=820).grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=(2, 4)
        )

        folder_btns = ttk.Frame(meta_grid)
        folder_btns.grid(row=1, column=2, sticky=tk.NE, padx=(8, 0))
        ttk.Button(folder_btns, text="Open Install Dir", command=self.open_selected_install_dir).pack(
            fill=tk.X, pady=(0, 3)
        )
        ttk.Button(folder_btns, text="Open App Data", command=self.open_selected_app_data_dir).pack(fill=tk.X)
        meta_grid.columnconfigure(1, weight=1)

        ep_ctrl = ttk.Frame(ep_tab)
        ep_ctrl.pack(fill=tk.X, pady=(2, 4))
        ttk.Label(ep_ctrl, text="GUI Entry Points:", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
        ttk.Label(ep_ctrl, text="Port:").pack(side=tk.LEFT, padx=(14, 2))
        self.launch_port_var = tk.StringVar()
        ttk.Entry(ep_ctrl, textvariable=self.launch_port_var, width=8).pack(side=tk.LEFT)
        ttk.Label(ep_ctrl, text="Extra Args:").pack(side=tk.LEFT, padx=(10, 2))
        self.launch_args_var = tk.StringVar()
        ttk.Entry(ep_ctrl, textvariable=self.launch_args_var, width=28).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(ep_ctrl, text="Launch Entry Point", command=self.launch_selected_entry_point).pack(side=tk.RIGHT)

        ep_cols = ("name", "context", "entryCommand", "description", "iconPath")
        self.entry_points_tree = ttk.Treeview(ep_tab, columns=ep_cols, show="headings", selectmode="browse", height=5)
        self.entry_points_tree.heading("name", text="Entry Point")
        self.entry_points_tree.heading("context", text="Runtime")
        self.entry_points_tree.heading("entryCommand", text="Command")
        self.entry_points_tree.heading("description", text="Description")
        self.entry_points_tree.heading("iconPath", text="Icon")
        self.entry_points_tree.column("name", width=130)
        self.entry_points_tree.column("context", width=85)
        self.entry_points_tree.column("entryCommand", width=270)
        self.entry_points_tree.column("description", width=260)
        self.entry_points_tree.column("iconPath", width=140)
        self.entry_points_tree.pack(fill=tk.BOTH, expand=True)
        self.entry_points_tree.bind("<Double-1>", lambda _e: self.launch_selected_entry_point())

        # Detail sub-tab 2: Services, Launchers & Running Processes
        svc_tab = ttk.Frame(self.detail_notebook, padding=6)
        self.detail_notebook.add(svc_tab, text="Services, Launchers & Processes")

        svc_cols = ("masId", "displayName", "mode", "runtime", "volunteer", "state")
        self.services_tree = ttk.Treeview(svc_tab, columns=svc_cols, show="headings", selectmode="browse", height=4)
        for col, title, width in (
            ("masId", "MAS ID", 150),
            ("displayName", "Display Name", 220),
            ("mode", "Mode", 110),
            ("runtime", "Runtime", 90),
            ("volunteer", "Volunteer", 85),
            ("state", "State", 100),
        ):
            self.services_tree.heading(col, text=title)
            self.services_tree.column(col, width=width)
        self.services_tree.pack(fill=tk.X, pady=(0, 6))

        proc_hdr = ttk.Frame(svc_tab)
        proc_hdr.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(proc_hdr, text="Launched Processes (This Session) & Launchers:", font=("Segoe UI", 9, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Button(proc_hdr, text="Stop Selected Process", command=self.stop_selected_process).pack(side=tk.RIGHT)
        ttk.Button(proc_hdr, text="Refresh Processes", command=self._refresh_process_tree).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        self.processes_tree = ttk.Treeview(
            svc_tab,
            columns=("appId", "entryPoint", "pid", "status", "detail"),
            show="headings",
            selectmode="browse",
            height=4,
        )
        for col, title, width in (
            ("appId", "App / Launcher", 140),
            ("entryPoint", "Entry Point", 130),
            ("pid", "PID", 80),
            ("status", "Status", 95),
            ("detail", "Command / Path", 420),
        ):
            self.processes_tree.heading(col, text=title)
            self.processes_tree.column(col, width=width)
        self.processes_tree.pack(fill=tk.BOTH, expand=True)

    def _build_catalog_tab(self) -> None:
        catalog_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(catalog_tab, text="Catalogs & App Store")

        src_frame = ttk.LabelFrame(catalog_tab, text="Connected Catalogs", padding=6)
        src_frame.pack(fill=tk.X, pady=(0, 6))

        self.source_var = tk.StringVar()
        self.source_combo = ttk.Combobox(src_frame, textvariable=self.source_var, state="readonly", width=64)
        self.source_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.source_combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_catalog())

        ttk.Button(src_frame, text="Refresh", command=self.refresh_catalog).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(src_frame, text="Attach...", command=self.attach_catalog_source).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(src_frame, text="Set Default", command=self.set_default_catalog_source).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(src_frame, text="Remove", command=self.remove_catalog_source).pack(side=tk.LEFT, padx=(6, 0))

        filter_bar = ttk.Frame(catalog_tab)
        filter_bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(filter_bar, text="Filter:").pack(side=tk.LEFT)
        self.catalog_query_var = tk.StringVar()
        query_entry = ttk.Entry(filter_bar, textvariable=self.catalog_query_var, width=28)
        query_entry.pack(side=tk.LEFT, padx=(4, 10))
        query_entry.bind("<KeyRelease>", lambda _e: self._render_catalog_rows())

        ttk.Label(filter_bar, text="Channel:").pack(side=tk.LEFT)
        self.channel_var = tk.StringVar(value="all")
        ch_combo = ttk.Combobox(
            filter_bar,
            textvariable=self.channel_var,
            state="readonly",
            values=("all", "stable", "preview", "canary", "nightly"),
            width=12,
        )
        ch_combo.pack(side=tk.LEFT, padx=(4, 10))
        ch_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_catalog_rows())

        self.install_btn = ttk.Button(
            filter_bar, text="Install / Update Latest", command=self.install_selected_catalog_app
        )
        self.install_btn.pack(side=tk.RIGHT)

        cat_paned = ttk.PanedWindow(catalog_tab, orient=tk.VERTICAL)
        cat_paned.pack(fill=tk.BOTH, expand=True)

        cat_top = ttk.Frame(cat_paned)
        cat_paned.add(cat_top, weight=2)

        cat_cols = ("appId", "name", "latestVersion", "channels", "installed")
        self.catalog_tree = ttk.Treeview(cat_top, columns=cat_cols, show="headings", selectmode="browse", height=7)
        self.catalog_tree.heading("appId", text="App ID")
        self.catalog_tree.heading("name", text="Name")
        self.catalog_tree.heading("latestVersion", text="Latest")
        self.catalog_tree.heading("channels", text="Channels")
        self.catalog_tree.heading("installed", text="Installed")
        self.catalog_tree.column("appId", width=150)
        self.catalog_tree.column("name", width=340)
        self.catalog_tree.column("latestVersion", width=100)
        self.catalog_tree.column("channels", width=150)
        self.catalog_tree.column("installed", width=110)
        self.catalog_tree.pack(fill=tk.BOTH, expand=True)
        self.catalog_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_catalog_app_selected())

        cat_detail = ttk.LabelFrame(cat_paned, text="Catalog App Details & Versions", padding=6)
        cat_paned.add(cat_detail, weight=2)

        self.cat_detail_var = tk.StringVar(value="Select an application from the catalog to view versions and details.")
        hdr_row = ttk.Frame(cat_detail)
        hdr_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(hdr_row, textvariable=self.cat_detail_var, justify=tk.LEFT, wraplength=780).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        ttk.Button(hdr_row, text="Install Selected Version", command=self.install_selected_catalog_version).pack(
            side=tk.RIGHT, padx=(8, 0)
        )

        ver_cols = ("version", "channel", "port", "size", "sha256", "changelog")
        self.catalog_versions_tree = ttk.Treeview(
            cat_detail, columns=ver_cols, show="headings", selectmode="browse", height=5
        )
        for col, title, width in (
            ("version", "Version", 95),
            ("channel", "Channel", 90),
            ("port", "Default Port", 90),
            ("size", "Bundle Bytes", 100),
            ("sha256", "SHA-256", 150),
            ("changelog", "Changelog", 320),
        ):
            self.catalog_versions_tree.heading(col, text=title)
            self.catalog_versions_tree.column(col, width=width)
        self.catalog_versions_tree.pack(fill=tk.BOTH, expand=True)

    def refresh_apps(self) -> None:
        prev_selected = self._selected_app_id()
        self.apps_tree.delete(*self.apps_tree.get_children())
        records = self.registry.list_all()
        for rec in records:
            self.apps_tree.insert(
                "",
                tk.END,
                iid=rec.app_id,
                values=(rec.app_id, rec.name, rec.version, rec.channel, rec.port, rec.mvenv_name or "-"),
            )
        target_sel = prev_selected if (prev_selected and self.apps_tree.exists(prev_selected)) else None
        if not target_sel and records:
            target_sel = records[0].app_id
        if target_sel:
            self.apps_tree.selection_set(target_sel)
            self.apps_tree.focus(target_sel)
        self._on_app_selected()
        self._render_catalog_rows()

    def _selected_app_id(self) -> Optional[str]:
        if not hasattr(self, "apps_tree"):
            return None
        sel = self.apps_tree.selection()
        return str(sel[0]) if sel else None

    def _on_app_selected(self) -> None:
        app_id = self._selected_app_id()
        if not hasattr(self, "entry_points_tree"):
            return
        self.entry_points_tree.delete(*self.entry_points_tree.get_children())
        self.services_tree.delete(*self.services_tree.get_children())
        if not app_id:
            self._current_app_details = None
            self.detail_title_var.set("Select an installed application to view details.")
            self.detail_meta_var.set("")
            self.launch_port_var.set("")
            self._refresh_process_tree()
            return

        try:
            details = self.installer.get_app_details(app_id)
        except Exception as exc:
            self._current_app_details = None
            self.detail_title_var.set(f"{app_id}: unable to load details")
            self.detail_meta_var.set(str(exc))
            return

        self._current_app_details = details
        self.launch_port_var.set(str(details.get("port") or 8000))
        prev_vers = ", ".join(details.get("previousVersions") or []) or "none"
        tags_str = ", ".join(details.get("tags") or []) or "none"
        self.detail_title_var.set(f"{details['name']} ({details['appId']} v{details['version']})")
        meta_lines = [
            f"Channel: {details['channel']}   |   Default Port: {details['port']}   |   MVenv: {details['mvenvName'] or '-'}   |   Rollback versions: {prev_vers}",
            f"Tags: {tags_str}   |   Install Path: {details['installPath']}",
            f"Description: {details.get('description') or 'No description provided.'}",
        ]
        self.detail_meta_var.set("\n".join(meta_lines))

        eps = details.get("guiEntryPoints") or []
        for idx, ep in enumerate(eps):
            ep_name = ep.get("name") or f"ep-{idx}"
            self.entry_points_tree.insert(
                "",
                tk.END,
                iid=ep_name,
                values=(
                    ep_name,
                    ep.get("context", "Python"),
                    ep.get("entryCommand", ""),
                    ep.get("description", ""),
                    ep.get("iconPath", ""),
                ),
            )
        if eps:
            first_ep = eps[0].get("name") or "ep-0"
            self.entry_points_tree.selection_set(first_ep)
            self.entry_points_tree.focus(first_ep)

        for svc in details.get("services") or []:
            self.services_tree.insert(
                "",
                tk.END,
                values=(
                    svc.get("masId", ""),
                    svc.get("displayName") or "",
                    svc.get("mode", ""),
                    svc.get("runtime", ""),
                    "Yes" if svc.get("volunteerEnabled") else "No",
                    svc.get("state", ""),
                ),
            )
        self._refresh_process_tree()

    def _refresh_process_tree(self) -> None:
        if not hasattr(self, "processes_tree"):
            return
        self.processes_tree.delete(*self.processes_tree.get_children())
        for idx, item in enumerate(self._launched_procs):
            proc: subprocess.Popen[Any] = item["proc"]
            rc = proc.poll()
            status = "Running" if rc is None else f"Exited ({rc})"
            self.processes_tree.insert(
                "",
                tk.END,
                iid=f"proc-{idx}",
                values=(item["appId"], item["entryPoint"], proc.pid, status, item["command"]),
            )
        if self._current_app_details:
            for k, v in (self._current_app_details.get("launchers") or {}).items():
                self.processes_tree.insert(
                    "",
                    tk.END,
                    iid=f"launcher-{k}",
                    values=(self._current_app_details["appId"], f"shortcut:{k}", "-", "Ready", str(v)),
                )

    def _selected_source_entry(self) -> Optional[Dict[str, Any]]:
        idx = self.source_combo.current()
        if 0 <= idx < len(self._source_choices):
            return self._source_choices[idx]
        return None

    def refresh_sources(self, select_url: Optional[str] = None) -> None:
        self._source_choices = self.sources_registry.list_sources()
        display_vals = [
            f"{s.get('label') or s['url']} — {s['url']}" + (" [default]" if s.get("isDefault") else "")
            for s in self._source_choices
        ]
        self.source_combo["values"] = display_vals
        if not self._source_choices:
            self.source_var.set("")
            self._loaded_catalog = None
            self._render_catalog_rows()
            return
        target_idx = 0
        if select_url:
            for i, s in enumerate(self._source_choices):
                if s["url"] == select_url:
                    target_idx = i
                    break
        else:
            for i, s in enumerate(self._source_choices):
                if s.get("isDefault"):
                    target_idx = i
                    break
        self.source_combo.current(target_idx)

    def refresh_catalog(self) -> None:
        entry = self._selected_source_entry()
        if entry is None:
            self._loaded_catalog = None
            self._render_catalog_rows()
            return
        source_url = str(entry["url"])
        self._set_status(f"Loading catalog: {source_url} ...")

        def _worker() -> None:
            try:
                manifest = self.catalog_manager.fetch_remote_manifest(source_url)
                if self.root is not None:
                    self.root.after(0, lambda: self._on_catalog_loaded(manifest, source_url))
            except Exception as exc:
                err_msg = str(exc)
                if self.root is not None:
                    self.root.after(0, lambda: self._on_catalog_error(source_url, err_msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_catalog_loaded(self, manifest: CatalogManifest, source_url: str) -> None:
        if self.root is None:
            return
        self._loaded_catalog = manifest
        self._render_catalog_rows()
        self._set_status(f"Loaded {len(manifest.apps)} app(s) from {source_url}")

    def _on_catalog_error(self, source_url: str, err_msg: str) -> None:
        if self.root is None:
            return
        self._loaded_catalog = None
        self._render_catalog_rows()
        self._set_status(f"Failed to load catalog {source_url}: {err_msg}")

    def _render_catalog_rows(self) -> None:
        if not hasattr(self, "catalog_tree"):
            return
        self.catalog_tree.delete(*self.catalog_tree.get_children())
        if self._loaded_catalog is None:
            self._on_catalog_app_selected()
            return
        query = (self.catalog_query_var.get() or "").strip().lower()
        channel = self.channel_var.get()
        installed_map = {rec.app_id: rec.version for rec in self.registry.list_all()}
        for app in sorted(self._loaded_catalog.apps.values(), key=lambda a: a.app_id):
            versions = app.versions.values()
            if channel != "all" and not any(v.channel == channel for v in versions):
                continue
            if query:
                haystack = " ".join([app.app_id, app.name, app.display_name, app.description, *app.tags]).lower()
                if query not in haystack:
                    continue
            channels = sorted({v.channel for v in versions}) or ["stable"]
            latest = app.latest_version or (max(app.versions.keys()) if app.versions else "-")
            inst_ver = installed_map.get(app.app_id, "-")
            self.catalog_tree.insert(
                "",
                tk.END,
                iid=app.app_id,
                values=(app.app_id, app.display_name or app.name, latest, ", ".join(channels), inst_ver),
            )
        self._on_catalog_app_selected()

    def _on_catalog_app_selected(self) -> None:
        if not hasattr(self, "catalog_versions_tree"):
            return
        self.catalog_versions_tree.delete(*self.catalog_versions_tree.get_children())
        sel = self.catalog_tree.selection()
        if not sel or self._loaded_catalog is None:
            self.cat_detail_var.set("Select an application from the catalog to view versions and details.")
            return
        app_id = str(sel[0])
        app = self._loaded_catalog.apps.get(app_id)
        if app is None:
            return
        tags = ", ".join(app.tags) or "none"
        self.cat_detail_var.set(
            f"{app.display_name or app.name} ({app.app_id})  |  Tags: {tags}\n"
            f"{app.description or 'No description provided.'}"
        )
        for ver_key, ve in sorted(app.versions.items(), reverse=True):
            self.catalog_versions_tree.insert(
                "",
                tk.END,
                iid=ver_key,
                values=(
                    ve.version,
                    ve.channel,
                    ve.default_port or "-",
                    ve.bundle_size_bytes or "-",
                    (ve.bundle_sha256[:12] + "...") if ve.bundle_sha256 else "-",
                    ve.changelog or "",
                ),
            )
        children = self.catalog_versions_tree.get_children()
        if children:
            self.catalog_versions_tree.selection_set(children[0])

    def attach_catalog_source(self) -> None:
        url = simpledialog.askstring(
            "Attach Catalog",
            "Enter catalog URL (http://...) or local/shared folder path:",
            parent=self.root,
        )
        if not url or not url.strip():
            return
        label = simpledialog.askstring(
            "Catalog Label",
            "Optional display label for this catalog:",
            parent=self.root,
        )
        entry = self.sources_registry.attach_source(url.strip(), label=(label.strip() if label else None))
        self.refresh_sources(select_url=entry["url"])
        self.refresh_catalog()

    def remove_catalog_source(self) -> None:
        entry = self._selected_source_entry()
        if entry is None:
            return
        if not messagebox.askyesno("Remove Catalog", f"Disconnect catalog '{entry['url']}'?", parent=self.root):
            return
        self.sources_registry.remove_source(entry["url"])
        self.refresh_sources()
        self.refresh_catalog()

    def set_default_catalog_source(self) -> None:
        entry = self._selected_source_entry()
        if entry is None:
            return
        self.sources_registry.attach_source(entry["url"], set_default=True)
        self.refresh_sources(select_url=entry["url"])

    def _run_async_op(self, label: str, fn: Any, on_done: Any) -> None:
        if self._busy:
            messagebox.showinfo("Busy", "Another operation is already in progress.", parent=self.root)
            return
        self._busy = True
        self._set_status(label)

        def _worker() -> None:
            try:
                result = fn()
                if self.root is not None:
                    self.root.after(0, lambda: self._finish_async_op(result, None, on_done))
            except Exception as exc:
                err = str(exc)
                if self.root is not None:
                    self.root.after(0, lambda: self._finish_async_op(None, err, on_done))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_async_op(self, result: Any, error: Optional[str], on_done: Any) -> None:
        self._busy = False
        if error:
            self._set_status(f"Error: {error}")
            messagebox.showerror("AuraSprout", error, parent=self.root)
            return
        self.refresh_apps()
        if hasattr(self, "mvenv_panel"):
            self.mvenv_panel.refresh()
        on_done(result)

    def install_selected_catalog_app(self) -> None:
        entry = self._selected_source_entry()
        sel = self.catalog_tree.selection()
        if entry is None or not sel:
            messagebox.showinfo("Install App", "Select a catalog and an application first.", parent=self.root)
            return
        app_id = str(sel[0])
        ch = self.channel_var.get()
        channel = None if ch == "all" else ch
        source_url = str(entry["url"])
        self._run_async_op(
            f"Installing {app_id} from {source_url} ...",
            lambda: self.installer.install_from_catalog(source=source_url, app_id=app_id, channel=channel),
            lambda rec: self._set_status(f"Installed {rec.app_id} v{rec.version}"),
        )

    def install_selected_catalog_version(self) -> None:
        entry = self._selected_source_entry()
        app_sel = self.catalog_tree.selection()
        ver_sel = self.catalog_versions_tree.selection()
        if entry is None or not app_sel or not ver_sel:
            messagebox.showinfo("Install Version", "Select a catalog application and version first.", parent=self.root)
            return
        app_id = str(app_sel[0])
        ver_key = str(ver_sel[0])
        source_url = str(entry["url"])
        app_entry = self._loaded_catalog.apps.get(app_id) if self._loaded_catalog else None
        ver_entry = app_entry.versions.get(ver_key) if app_entry else None
        channel = ver_entry.channel if ver_entry else None
        version = ver_entry.version if ver_entry else ver_key
        self._run_async_op(
            f"Installing {app_id} v{version} from {source_url} ...",
            lambda: self.installer.install_from_catalog(
                source=source_url, app_id=app_id, version=version, channel=channel
            ),
            lambda rec: self._set_status(f"Installed {rec.app_id} v{rec.version}"),
        )

    def install_local_package(self) -> None:
        apkg = filedialog.askopenfilename(
            title="Select .apkg package (Cancel to choose a directory)",
            filetypes=[("AuraSprout Packages", "*.apkg"), ("All Files", "*.*")],
            parent=self.root,
        )
        target_str = apkg or filedialog.askdirectory(
            title="Select application directory containing app.manifest.json",
            parent=self.root,
        )
        if not target_str:
            return
        target = Path(target_str)
        fn = (
            (lambda: self.installer.install_from_directory(target))
            if target.is_dir()
            else (lambda: self.installer.install_from_package(target))
        )
        self._run_async_op(
            f"Installing from {target.name} ...",
            fn,
            lambda rec: self._set_status(f"Installed {rec.app_id} v{rec.version}"),
        )

    def launch_selected_app(self) -> None:
        app_id = self._selected_app_id()
        if not app_id:
            return
        self._launch_entry_point_for(app_id, entry_point_name=None)

    def launch_selected_entry_point(self) -> None:
        app_id = self._selected_app_id()
        if not app_id:
            return
        sel = self.entry_points_tree.selection()
        ep_name = str(sel[0]) if sel else None
        self._launch_entry_point_for(app_id, entry_point_name=ep_name)

    def _launch_entry_point_for(self, app_id: str, entry_point_name: Optional[str]) -> None:
        port_override: Optional[int] = None
        raw_port = self.launch_port_var.get().strip() if hasattr(self, "launch_port_var") else ""
        if raw_port:
            try:
                port_override = int(raw_port)
            except ValueError:
                messagebox.showerror("Invalid Port", f"Port must be an integer, got {raw_port!r}", parent=self.root)
                return

        extra_args: Optional[List[str]] = None
        raw_args = self.launch_args_var.get().strip() if hasattr(self, "launch_args_var") else ""
        if raw_args:
            extra_args = shlex.split(raw_args, posix=(platform.system().lower() != "windows"))

        try:
            proc = self.installer.launch_entry_point(
                app_id=app_id,
                entry_point_name=entry_point_name,
                port=port_override,
                extra_args=extra_args,
            )
        except Exception as exc:
            messagebox.showerror("Launch Failed", str(exc), parent=self.root)
            self._set_status(f"Launch failed for {app_id}: {exc}")
            return

        ep_label = entry_point_name or "default"
        cmd_preview = " ".join(str(x) for x in (proc.args if isinstance(proc.args, (list, tuple)) else [proc.args]))
        self._launched_procs.append(
            {"appId": app_id, "entryPoint": ep_label, "proc": proc, "command": cmd_preview}
        )
        self._refresh_process_tree()
        self._set_status(f"Launched {app_id} ({ep_label}) as PID {proc.pid}")

    def stop_selected_process(self) -> None:
        sel = self.processes_tree.selection()
        if not sel or not str(sel[0]).startswith("proc-"):
            return
        idx = int(str(sel[0]).split("-", 1)[1])
        if 0 <= idx < len(self._launched_procs):
            proc: subprocess.Popen[Any] = self._launched_procs[idx]["proc"]
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._refresh_process_tree()
            self._set_status(f"Stopped PID {proc.pid}")

    def open_selected_install_dir(self) -> None:
        if self._current_app_details and self._current_app_details.get("installPath"):
            _open_folder_in_explorer(Path(self._current_app_details["installPath"]))

    def open_selected_app_data_dir(self) -> None:
        if self._current_app_details and self._current_app_details.get("appDataDir"):
            _open_folder_in_explorer(Path(self._current_app_details["appDataDir"]))

    def rollback_selected_app(self) -> None:
        app_id = self._selected_app_id()
        if not app_id:
            return
        if not messagebox.askyesno("Rollback App", f"Roll back '{app_id}' to its previous version?", parent=self.root):
            return
        self._run_async_op(
            f"Rolling back {app_id} ...",
            lambda: self.installer.rollback_app(app_id),
            lambda rec: self._set_status(f"Rolled back {rec.app_id} to v{rec.version}"),
        )

    def uninstall_selected_app(self) -> None:
        app_id = self._selected_app_id()
        if not app_id:
            return
        if not messagebox.askyesno("Uninstall App", f"Uninstall '{app_id}'?", parent=self.root):
            return
        remove_mvenv = messagebox.askyesno(
            "Remove MVenv",
            f"Also delete the managed virtual environment for '{app_id}'?",
            parent=self.root,
        )
        self._run_async_op(
            f"Uninstalling {app_id} ...",
            lambda: self.installer.uninstall_app(app_id, remove_mvenv=remove_mvenv),
            lambda ok: self._set_status(f"Uninstalled {app_id}" if ok else f"{app_id} was not installed"),
        )

    def open_web_portal(self) -> str:
        if self._web_server is None:
            st = ManagementServerState(
                paths=self.paths,
                mvenv_manager=self.mvenv_manager,
                registry=self.registry,
                installer=self.installer,
            )
            self._web_server = SproutManagementServer(state=st).start()
        url = self._web_server.auth_url
        try:
            webbrowser.open(url)
        except Exception as exc:
            _LOG.debug("Could not open browser for %s: %s", url, exc)
            messagebox.showinfo("AuraSprout Web Portal", f"Portal URL:\n{url}", parent=self.root)
        return url


def launch_desktop_hub(paths: Optional[SproutPaths] = None) -> int:
    if not HAS_TK:
        print("[AuraSprout GUI] Tkinter is not available in this Python environment.", file=sys.stderr)
        return 1
    root = tk.Tk()
    SproutDesktopHub(root=root, paths=paths)
    root.mainloop()
    return 0
