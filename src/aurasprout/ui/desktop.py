"""Tkinter Desktop Hub for AuraSprout composing `auraseed.gui.MvenvManagerPanel`."""

from __future__ import annotations

import logging
import sys
import webbrowser
from typing import Any, Optional

try:
    import tkinter as tk
    from tkinter import messagebox, ttk

    HAS_TK = True
except ImportError:
    HAS_TK = False

from auraseed.gui import MvenvManagerPanel
from auraseed.mvenv.manager import MvenvManager

from aurasprout.contracts.paths import SproutPaths
from aurasprout.installer.engine import SproutInstallerEngine
from aurasprout.installer.registry import InstalledAppsRegistry
from aurasprout.ui.server import ManagementServerState, SproutManagementServer

_LOG = logging.getLogger(__name__)


class SproutDesktopHub:
    """Unified Desktop Hub composing AuraSprout app/volunteer management & AuraSeed MVenv panel."""

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
        self.installer = installer_engine or SproutInstallerEngine(
            paths=self.paths,
            mvenv_manager=self.mvenv_manager,
            registry=self.registry,
        )
        self.root = root or tk.Tk()
        self.root.title("AuraSprout - User-Space Cell & Application Hub")
        self.root.geometry("980x640")
        self.root.minsize(760, 480)
        self._web_server: Optional[SproutManagementServer] = None

        self._build_ui()
        self.refresh_apps()

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(12, 10))
        header.pack(fill=tk.X)
        ttk.Label(header, text="AuraSprout", font=("Segoe UI", 16, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text="  Zero-Privilege Cell & App Manager", font=("Segoe UI", 10)).pack(
            side=tk.LEFT, pady=(4, 0)
        )
        ttk.Button(header, text="Open Web Portal", command=self.open_web_portal).pack(side=tk.RIGHT)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        # Tab 1: Installed Apps
        apps_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(apps_tab, text="Installed Applications")

        top_apps = ttk.Frame(apps_tab)
        top_apps.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(top_apps, text="Refresh", command=self.refresh_apps).pack(side=tk.LEFT)

        cols = ("appId", "name", "version", "mvenv")
        self.apps_tree = ttk.Treeview(apps_tab, columns=cols, show="headings", selectmode="browse")
        self.apps_tree.heading("appId", text="App ID")
        self.apps_tree.heading("name", text="Name")
        self.apps_tree.heading("version", text="Version")
        self.apps_tree.heading("mvenv", text="Managed MVenv")
        self.apps_tree.pack(fill=tk.BOTH, expand=True)

        # Tab 2: Composed AuraSeed MVenv Manager Panel with relink callback
        mvenv_tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(mvenv_tab, text="Runtimes & MVenvs (AuraSeed)")
        self.mvenv_panel = MvenvManagerPanel(
            mvenv_tab,
            mvenv_manager=self.mvenv_manager,
            repoint_callback=self.installer.relink_app_mvenv,
            on_mvenv_changed=self.refresh_apps,
        )
        self.mvenv_panel.pack(fill=tk.BOTH, expand=True)

    def refresh_apps(self) -> None:
        self.apps_tree.delete(*self.apps_tree.get_children())
        for rec in self.registry.list_all():
            self.apps_tree.insert(
                "",
                tk.END,
                iid=rec.app_id,
                values=(rec.app_id, rec.name, rec.version, rec.mvenv_name or "-"),
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

