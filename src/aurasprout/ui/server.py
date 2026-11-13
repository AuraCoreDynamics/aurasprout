"""Tier 3 Loopback Management HTTP Server matching `aurasprout-management.openapi.json`."""

from __future__ import annotations

import base64
import hmac
import http.cookies
import json
import logging
import os
import secrets
import threading
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from auraseed.mvenv.manager import MvenvManager

from aurasprout.catalog.html_generator import generate_catalog_html
from aurasprout.catalog.manager import SproutCatalogManager
from aurasprout.catalog.sources import CatalogSourcesRegistry
from aurasprout.contracts.paths import SproutPaths, validate_identifier, validate_within_root
from aurasprout.coordination.leases import VolunteerLeaseManager
from aurasprout.installer.engine import SproutInstallerEngine
from aurasprout.installer.registry import InstalledAppsRegistry
from aurasprout.supervisor.runner import SproutSupervisor

_LOG = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _is_loopback_host(host: str) -> bool:
    raw = (host or "").strip().lower()
    if raw.startswith("[::1]"):
        return True
    host_only = raw.split(":", 1)[0]
    return host_only in ("127.0.0.1", "localhost", "::1")


class ManagementServerState:
    """Shared backend state for Tier 3 loopback Web UI."""

    def __init__(
        self,
        paths: Optional[SproutPaths] = None,
        mvenv_manager: Optional[MvenvManager] = None,
        catalog_manager: Optional[SproutCatalogManager] = None,
        sources_registry: Optional[CatalogSourcesRegistry] = None,
        registry: Optional[InstalledAppsRegistry] = None,
        installer: Optional[SproutInstallerEngine] = None,
        supervisor: Optional[SproutSupervisor] = None,
        bootstrap_token: Optional[str] = None,
    ) -> None:
        self.paths = paths or SproutPaths()
        self.mvenv_manager = mvenv_manager or MvenvManager()
        self.catalog_manager = catalog_manager or SproutCatalogManager(paths=self.paths)
        self.sources_registry = sources_registry or CatalogSourcesRegistry(paths=self.paths)
        self.registry = registry or InstalledAppsRegistry(paths=self.paths)
        self.installer = installer or SproutInstallerEngine(
            paths=self.paths,
            mvenv_manager=self.mvenv_manager,
            catalog_manager=self.catalog_manager,
            registry=self.registry,
        )
        self.supervisor = supervisor or SproutSupervisor(
            paths=self.paths,
            registry=self.registry,
        )
        self.lease_manager: VolunteerLeaseManager = self.supervisor.ipc_server.state.lease_manager
        self.node_id = self.supervisor.ipc_server.state.node_id
        self.bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._sessions: Dict[str, str] = {}

    def issue_session(self, candidate_token: str) -> Optional[tuple[str, str]]:
        if not candidate_token or not hmac.compare_digest(candidate_token, self.bootstrap_token):
            return None
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[sid] = csrf
        return sid, csrf

    def get_session_csrf(self, sid: Optional[str]) -> Optional[str]:
        if not sid:
            return None
        with self._lock:
            return self._sessions.get(sid)


class _ManagementRequestHandler(BaseHTTPRequestHandler):
    server_version = "AuraSproutUI/4.3.0"
    protocol_version = "HTTP/1.1"

    @property
    def ui_state(self) -> ManagementServerState:
        return self.server.ui_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        _LOG.debug("UI: " + format, *args)

    def _send_json(
        self,
        status: int,
        payload: Any,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        if status >= 400 and not getattr(self, "_body_consumed", False):
            try:
                content_len = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                content_len = 0
            if 0 < content_len <= 1024 * 1024:
                try:
                    self.rfile.read(content_len)
                    self._body_consumed = True
                except OSError:
                    pass
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, file_path: Path, content_type: str) -> None:
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        self._body_consumed = True
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Expected JSON object")
        return parsed

    def _read_raw_body(self, max_bytes: int = 256 * 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            self._body_consumed = True
            return b""
        if length > max_bytes:
            raise ValueError(f"Payload exceeds maximum allowed upload size ({max_bytes} bytes)")
        self._body_consumed = True
        return self.rfile.read(length)

    def _catalog_http_base_url(self) -> str:
        host_hdr = (self.headers.get("Host") or "").strip()
        if host_hdr:
            return f"http://{host_hdr}/catalog"
        addr, port = self.server.server_address  # type: ignore[attr-defined]
        return f"http://{addr}:{port}/catalog"

    def _send_bytes(
        self,
        status: int,
        data: bytes,
        content_type: str,
        filename: Optional[str] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if filename:
            safe_disp = filename.replace('"', "")
            self.send_header("Content-Disposition", f'attachment; filename="{safe_disp}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_hosted_catalog(self, path: str) -> bool:
        """Serve read-only static HTML portal, catalog.json, and `.apkg` bundles under `/catalog`."""
        st = self.ui_state
        catalog_base = self._catalog_http_base_url()

        if path in ("/catalog", "/catalog/", "/catalog/index.html"):
            html_doc = generate_catalog_html(st.catalog_manager.manifest, catalog_url=catalog_base)
            data = html_doc.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return True

        if path == "/catalog/catalog.json":
            manifest_dict = st.catalog_manager.manifest.to_dict()
            manifest_dict["baseUrl"] = catalog_base
            apps_block = manifest_dict.get("apps")
            if isinstance(apps_block, dict):
                for app_id, app_entry in apps_block.items():
                    if not isinstance(app_entry, dict):
                        continue
                    versions = app_entry.get("versions")
                    if isinstance(versions, dict):
                        for v_entry in versions.values():
                            if isinstance(v_entry, dict) and v_entry.get("bundleFilename"):
                                b_url = str(v_entry.get("bundleUrl") or "")
                                if not b_url.startswith(("http://", "https://")):
                                    v_entry["bundleUrl"] = f"{catalog_base}/apps/{app_id}/{v_entry['bundleFilename']}"
            self._send_json(200, manifest_dict)
            return True

        for prefix, root_dir in (
            ("/catalog/apps/", st.catalog_manager.apps_dir),
            ("/catalog/channels/", st.catalog_manager.channels_dir),
        ):
            if path.startswith(prefix):
                rel = path[len(prefix):].lstrip("/")
                try:
                    target = validate_within_root(root_dir, rel)
                except ValueError:
                    self._send_json(400, {"error": "BadRequest", "detail": "Invalid catalog asset path"})
                    return True
                if not target.exists() or not target.is_file():
                    self._send_json(404, {"error": "NotFound", "detail": f"Catalog artifact not found: {rel}"})
                    return True
                ctype = (
                    "application/json; charset=utf-8"
                    if target.suffix.lower() == ".json"
                    else "application/octet-stream"
                )
                self._send_bytes(
                    200,
                    target.read_bytes(),
                    content_type=ctype,
                    filename=(target.name if target.suffix.lower() == ".apkg" else None),
                )
                return True

        return False

    def _check_origin_and_host(self) -> bool:
        host_hdr = self.headers.get("Host") or ""
        if host_hdr and not _is_loopback_host(host_hdr):
            self._send_json(403, {"error": "ForbiddenHost", "detail": "Loopback Host required"})
            return False

        origin = self.headers.get("Origin")
        if origin is not None:
            parsed = urllib.parse.urlsplit(origin)
            port = self.server.server_port  # type: ignore[attr-defined]
            allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            if f"{parsed.scheme}://{parsed.netloc}".lower() not in allowed_origins:
                self._send_json(403, {"error": "ForbiddenOrigin", "detail": f"Origin rejected: {origin}"})
                return False
        return True

    def _authenticate_api(self) -> bool:
        cookie_hdr = self.headers.get("Cookie") or ""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(cookie_hdr)
        except http.cookies.CookieError:
            jar = http.cookies.SimpleCookie()

        morsel = jar.get("aurasprout_session")
        sid = morsel.value if morsel is not None else None
        expected_csrf = self.ui_state.get_session_csrf(sid)
        if not expected_csrf:
            self._send_json(401, {"error": "Unauthorized", "detail": "Valid session cookie required"})
            return False

        provided_csrf = self.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(provided_csrf, expected_csrf):
            self._send_json(403, {"error": "CsrfValidationFailed", "detail": "Invalid X-CSRF-Token"})
            return False
        return True

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        # Strict loopback: no wildcard CORS headers
        self._send_json(405, {"error": "MethodNotAllowed"})

    def _dispatch(self, method: str) -> None:
        self._body_consumed = False
        if not self._check_origin_and_host():
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        # Static web assets
        if method == "GET" and path in ("/", "/index.html"):
            self._send_static(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if method == "GET" and path == "/app.js":
            self._send_static(_STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return

        # Public health & hosted static HTTP catalog
        if method == "GET" and path in ("/api/health", "/api/v1/health"):
            self._send_json(
                200,
                {"status": "Healthy", "version": "4.3.0", "nodeId": self.ui_state.node_id},
            )
            return

        if method == "GET" and (path == "/catalog" or path.startswith("/catalog/")):
            if self._serve_hosted_catalog(path):
                return

        if method == "POST" and path == "/api/v1/auth/session":
            try:
                body = self._read_json()
            except ValueError as exc:
                self._send_json(400, {"error": "BadRequest", "detail": str(exc)})
                return
            issued = self.ui_state.issue_session(str(body.get("token") or ""))
            if issued is None:
                self._send_json(401, {"error": "Unauthorized", "detail": "Invalid bootstrap token"})
                return
            sid, csrf = issued
            cookie_val = f"aurasprout_session={sid}; HttpOnly; SameSite=Strict; Path=/"
            self._send_json(
                200,
                {"authenticated": True, "csrfToken": csrf},
                extra_headers={"Set-Cookie": cookie_val},
            )
            return

        if not self._authenticate_api():
            return

        try:
            self._route_authenticated(method, path, query)
        except KeyError as exc:
            self._send_json(404, {"error": "NotFound", "detail": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": "NotFound", "detail": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": "BadRequest", "detail": str(exc)})
        except Exception as exc:
            _LOG.exception("Unhandled Management UI error on %s %s", method, path)
            self._send_json(500, {"error": "InternalError", "detail": str(exc)})

    def _route_authenticated(self, method: str, path: str, query: Dict[str, List[str]]) -> None:
        st = self.ui_state

        if method == "GET" and path == "/api/v1/apps":
            apps = [rec.to_summary_dict() for rec in st.registry.list_all()]
            self._send_json(200, {"apps": apps})
            return

        if method == "POST" and path == "/api/v1/apps/install":
            body = self._read_json()
            pkg_path = body.get("packagePath")
            app_id = body.get("appId")
            version = body.get("version")
            channel = str(body.get("channel") or "stable")
            dest_dir = Path(body["destDir"]) if body.get("destDir") else None

            if pkg_path:
                src = Path(str(pkg_path)).resolve()
                if src.is_dir():
                    rec = st.installer.install_from_directory(src, dest_dir=dest_dir, channel=channel)
                else:
                    rec = st.installer.install_from_package(src, dest_dir=dest_dir, channel=channel)
            elif app_id:
                installed_rec = None
                if st.catalog_manager.get_app(str(app_id)) is not None:
                    installed_rec = st.installer.install_from_catalog(
                        source=str(st.catalog_manager.catalog_dir),
                        app_id=str(app_id),
                        version=version,
                        channel=channel,
                        dest_dir=dest_dir,
                    )
                else:
                    for src_item in st.sources_registry.list_sources():
                        try:
                            installed_rec = st.installer.install_from_catalog(
                                source=src_item["url"],
                                app_id=str(app_id),
                                version=version,
                                channel=channel,
                                dest_dir=dest_dir,
                            )
                            break
                        except KeyError:
                            continue
                if installed_rec is None:
                    raise KeyError(f"Application {app_id!r} not found in local or attached catalogs")
                rec = installed_rec
            else:
                raise ValueError("Provide either 'packagePath' or 'appId' to install")
            self._send_json(200, rec.to_summary_dict())
            return

        if path.startswith("/api/v1/apps/"):
            rest = path[len("/api/v1/apps/"):]
            if rest.endswith("/launch") and method == "POST":
                app_id = rest[: -len("/launch")]
                body = self._read_json()
                pid = st.supervisor.launch_app_entrypoint(
                    app_id=app_id,
                    entrypoint_name=body.get("entrypointName"),
                )
                self._send_json(200, {"appId": app_id, "launched": pid is not None, "pid": pid})
                return
            if "/" not in rest:
                app_id = rest
                if method == "GET":
                    details = st.installer.get_app_details(app_id)
                    self._send_json(200, details)
                    return
                if method == "DELETE":
                    removed = st.installer.uninstall_app(app_id)
                    self._send_json(200, {"appId": app_id, "uninstalled": removed})
                    return

        if method == "GET" and path == "/api/v1/catalog":
            q = query.get("q", [None])[0]
            channel = query.get("channel", [None])[0]
            seen: Dict[str, Dict[str, Any]] = {}
            for app in st.catalog_manager.list_apps(query=q, channel=channel):
                v_entry = app.get_version(channel=channel)
                seen[app.app_id] = {
                    "appId": app.app_id,
                    "name": app.name,
                    "displayName": app.display_name,
                    "description": app.description,
                    "latestVersion": v_entry.version if v_entry else app.latest_version,
                    "channel": v_entry.channel if v_entry else app.channel,
                    "bundleSha256": v_entry.bundle_sha256 if v_entry else None,
                    "tags": list(app.tags),
                }
            for src_item in st.sources_registry.list_sources():
                try:
                    remote_manifest = st.catalog_manager.fetch_remote_manifest(src_item["url"])
                except Exception as exc:
                    _LOG.debug("Skipping unreachable catalog source %s: %s", src_item["url"], exc)
                    continue
                for app in remote_manifest.apps.values():
                    if app.app_id in seen:
                        continue
                    v_entry = app.get_version(channel=channel)
                    if channel and (v_entry is None or v_entry.channel != channel):
                        continue
                    if q:
                        hay = " ".join([app.app_id, app.name, app.display_name, app.description, *app.tags]).lower()
                        if q.lower() not in hay:
                            continue
                    seen[app.app_id] = {
                        "appId": app.app_id,
                        "name": app.name,
                        "displayName": app.display_name,
                        "description": app.description,
                        "latestVersion": v_entry.version if v_entry else app.latest_version,
                        "channel": v_entry.channel if v_entry else app.channel,
                        "bundleSha256": v_entry.bundle_sha256 if v_entry else None,
                        "tags": list(app.tags),
                    }
            self._send_json(200, {"apps": list(seen.values())})
            return

        if path == "/api/v1/catalog/sources":
            if method == "GET":
                self._send_json(
                    200,
                    {"sources": st.sources_registry.list_sources()},
                )
                return
            if method == "POST":
                body = self._read_json()
                src = st.sources_registry.attach_source(
                    url=str(body["url"]),
                    label=str(body.get("label") or body["url"]),
                    set_default=bool(body.get("isDefault", False)),
                )
                self._send_json(200, src)
                return
            if method == "DELETE":
                target_url = query.get("url", [None])[0]
                if not target_url:
                    body = self._read_json()
                    target_url = body.get("url")
                if not target_url:
                    raise ValueError("Provide 'url' query param or JSON body to remove a catalog source")
                removed = st.sources_registry.remove_source(str(target_url))
                self._send_json(200, {"url": str(target_url), "removed": removed})
                return

        if method == "POST" and path == "/api/v1/catalog/publish":
            ctype = (self.headers.get("Content-Type") or "application/json").split(";", 1)[0].strip().lower()
            ch = str(query.get("channel", ["stable"])[0] or "stable")
            fname = query.get("filename", [None])[0] or self.headers.get("X-Filename")

            if ctype in ("application/octet-stream", "application/zip", "application/x-gzip"):
                raw_bytes = self._read_raw_body()
                ver_entry = st.catalog_manager.publish_package(
                    raw_bytes,
                    channel=ch,
                    filename=fname or "package.apkg",
                )
            elif ctype == "multipart/form-data":
                full_ctype = self.headers.get("Content-Type") or ""
                raw_bytes = self._read_raw_body()
                msg = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: " + full_ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw_bytes
                )
                pkg_bytes: Optional[bytes] = None
                for part in msg.iter_parts():
                    disp_name = part.get_param("name", header="content-disposition")
                    part_fname = part.get_filename()
                    if disp_name == "channel":
                        ch = part.get_content().strip() or ch
                    elif part_fname or disp_name in ("package", "file"):
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            pkg_bytes = payload
                            if part_fname and not fname:
                                fname = part_fname
                if not pkg_bytes:
                    raise ValueError("Multipart upload missing 'package' or 'file' payload")
                ver_entry = st.catalog_manager.publish_package(
                    pkg_bytes,
                    channel=ch,
                    filename=fname or "package.apkg",
                )
            else:
                body = self._read_json()
                ch = str(body.get("channel") or ch or "stable")
                if body.get("packageBase64"):
                    decoded = base64.b64decode(str(body["packageBase64"]), validate=True)
                    ver_entry = st.catalog_manager.publish_package(
                        decoded,
                        channel=ch,
                        filename=str(body.get("filename") or fname or "package.apkg"),
                    )
                elif body.get("packagePath"):
                    pkg_path = Path(str(body["packagePath"])).resolve()
                    ver_entry = st.catalog_manager.publish_package(pkg_path, channel=ch)
                else:
                    raise ValueError("Provide 'packagePath', 'packageBase64', or binary upload body")

            # Find published catalog entry
            published_app = None
            for app in st.catalog_manager.manifest.apps.values():
                if ver_entry.version in app.versions and app.versions[ver_entry.version].bundle_sha256 == ver_entry.bundle_sha256:
                    published_app = app
                    break
            if published_app is None:
                raise RuntimeError("Published entry missing from catalog manifest")
            self._send_json(
                200,
                {
                    "appId": published_app.app_id,
                    "name": published_app.name,
                    "displayName": published_app.display_name,
                    "description": published_app.description,
                    "latestVersion": published_app.latest_version,
                    "channel": published_app.channel,
                    "bundleSha256": ver_entry.bundle_sha256,
                    "tags": list(published_app.tags),
                },
            )
            return

        if method == "GET" and path.startswith("/api/v1/catalog/apps/") and path.endswith("/download"):
            app_id = validate_identifier(path[len("/api/v1/catalog/apps/") : -len("/download")], "appId")
            ver = query.get("version", [None])[0]
            ch = query.get("channel", [None])[0]
            bundle_path = st.catalog_manager.get_bundle_path(app_id, version=ver, channel=ch)
            if bundle_path is None:
                raise KeyError(f"Bundle for application {app_id!r} not found")
            self._send_bytes(
                200,
                bundle_path.read_bytes(),
                content_type="application/octet-stream",
                filename=bundle_path.name,
            )
            return

        if method == "GET" and path == "/api/v1/volunteer":
            items: List[Dict[str, Any]] = []
            for app in st.registry.list_all():
                for svc in app.services:
                    lease = st.lease_manager.get_lease(svc.mas_id, active_only=False)
                    items.append(
                        {
                            "masId": svc.mas_id,
                            "mode": svc.mode,
                            "volunteerEnabled": bool(svc.volunteer_enabled),
                            "state": lease.get("state", svc.state) if lease else svc.state,
                            "holderNodeId": lease.get("holderNodeId") if lease else None,
                            "holderUser": lease.get("holderUser") if lease else None,
                            "fencingToken": lease.get("fencingToken") if lease else None,
                            "activitySummary": lease.get("activitySummary") if lease else None,
                            "progressPct": lease.get("progressPct") if lease else None,
                        }
                    )
            self._send_json(200, {"services": items})
            return

        if method == "POST" and path.startswith("/api/v1/volunteer/"):
            mas_id = path[len("/api/v1/volunteer/"):]
            body = self._read_json()
            enabled = bool(body["enabled"])
            svc_mode = "CellSingleton"
            matched_app = None
            for app in st.registry.list_all():
                for svc in app.services:
                    if svc.mas_id == mas_id:
                        svc_mode = svc.mode
                        svc.volunteer_enabled = enabled if svc_mode != "LocalOnly" else False
                        matched_app = app
            if matched_app is not None:
                st.registry.register(matched_app)
            lease = st.lease_manager.set_volunteer_enabled(
                mas_id=mas_id,
                enabled=enabled,
                mode=svc_mode,
            )
            self._send_json(
                200,
                {
                    "masId": mas_id,
                    "mode": svc_mode,
                    "volunteerEnabled": bool(lease.get("volunteerEnabled", False)),
                    "state": str(lease.get("state", "Standby")),
                    "holderNodeId": lease.get("holderNodeId"),
                    "holderUser": lease.get("holderUser"),
                    "fencingToken": lease.get("fencingToken"),
                    "activitySummary": lease.get("activitySummary"),
                    "progressPct": lease.get("progressPct"),
                },
            )
            return

        if method == "GET" and path == "/api/v1/conflicts":
            self._send_json(200, {"conflicts": st.lease_manager.list_conflicts()})
            return

        if method == "POST" and path.startswith("/api/v1/conflicts/") and path.endswith("/resolve"):
            cid = path[len("/api/v1/conflicts/") : -len("/resolve")]
            body = self._read_json()
            resolved = st.lease_manager.resolve_conflict(cid, str(body["action"]))
            self._send_json(
                200,
                {
                    "masId": resolved["masId"],
                    "mode": str(resolved.get("mode", "CellSingleton")),
                    "volunteerEnabled": bool(resolved.get("volunteerEnabled", False)),
                    "state": str(resolved.get("state", "Standby")),
                    "holderNodeId": resolved.get("holderNodeId"),
                    "holderUser": resolved.get("holderUser"),
                    "fencingToken": resolved.get("fencingToken"),
                    "activitySummary": resolved.get("activitySummary"),
                    "progressPct": resolved.get("progressPct"),
                },
            )
            return

        if method == "GET" and path == "/api/v1/runtimes":
            runtimes: List[Dict[str, Any]] = []
            for env in st.mvenv_manager.discover_interpreters(probe_capabilities=True, use_cache=True):
                caps: Dict[str, str] = {}
                if env.capabilities is not None:
                    caps = {
                        "hasPip": str(env.capabilities.has_pip),
                        "hasVenv": str(env.capabilities.has_venv),
                        "score": str(env.capabilities.composite_score),
                        "architecture": env.capabilities.architecture,
                    }
                runtimes.append(
                    {
                        "name": env.name,
                        "pythonExecutable": str(env.executable),
                        "version": env.version,
                        "isMvenv": False,
                        "capabilities": caps,
                    }
                )
            for rec in st.mvenv_manager.list_venvs():
                runtimes.append(
                    {
                        "name": rec.name,
                        "pythonExecutable": rec.python_path,
                        "version": rec.python_version,
                        "isMvenv": True,
                        "capabilities": {"sovereign": str(rec.is_sovereign), "packages": str(len(rec.packages))},
                    }
                )
            self._send_json(200, {"runtimes": runtimes})
            return

        self._send_json(404, {"error": "NotFound", "detail": f"Unknown route {method} {path}"})


class SproutManagementServer:
    """Tier 3 Loopback Management Web UI Server."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        state: Optional[ManagementServerState] = None,
    ) -> None:
        resolved_host = host or os.environ.get("AURASPROUT_UI_HOST") or "127.0.0.1"
        if not _is_loopback_host(resolved_host):
            raise ValueError(f"Tier 3 Management UI must bind strictly to loopback, got {resolved_host!r}")
        raw_port = port if port is not None else int(os.environ.get("AURASPROUT_UI_PORT") or "0")
        self.host = resolved_host
        self.state = state or ManagementServerState()
        self._httpd = ThreadingHTTPServer((self.host, int(raw_port)), _ManagementRequestHandler)
        self._httpd.daemon_threads = True
        self._httpd.ui_state = self.state  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/#{self.state.bootstrap_token}"

    @property
    def bootstrap_token(self) -> str:
        return self.state.bootstrap_token

    def start(self) -> "SproutManagementServer":
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                name="aurasprout-management-ui",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "SproutManagementServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        del exc_type, exc_val, exc_tb
        self.stop()
