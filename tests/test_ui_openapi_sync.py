"""Contract sync tests verifying Tier 3 Management Web UI matches `aurasprout-management.openapi.json`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from auraseed.mvenv.manager import MvenvManager
from auraseed.mvenv.models import MvenvCreateRequest

from aurasprout.catalog.packager import SproutPackager
from aurasprout.contracts.paths import SproutPaths
from aurasprout.ui.server import ManagementServerState, SproutManagementServer


def _load_openapi_contract() -> dict:
    candidates = [
        Path(__file__).resolve().parent.parent / "src" / "aurasprout" / "openapi.json",
        Path(__file__).resolve().parent.parent / "openapi.json",
        Path(r"C:\projects\auracore\auragrid-api\openapi\aurasprout-management.openapi.json"),
    ]
    for p in candidates:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"OpenAPI contract not found in candidates: {candidates}")


def test_management_server_auth_csrf_origin_and_openapi_routes(tmp_path: Path) -> None:
    contract = _load_openapi_contract()
    paths = SproutPaths(cell_root=tmp_path / "cell", user_root=tmp_path / "user")
    mvenv_mgr = MvenvManager(tmp_path / "mvenvs")
    mvenv_mgr.create_venv(MvenvCreateRequest(name="sprout-web-demo", base_python=sys.executable, sovereign=False))

    state = ManagementServerState(paths=paths, mvenv_manager=mvenv_mgr)

    # Prepare a sample .apkg to publish & install through Management API
    app_dir = tmp_path / "web_demo_app"
    (app_dir / "web_demo").mkdir(parents=True)
    (app_dir / "web_demo" / "__init__.py").write_text("__version__ = '1.0.0'\n", encoding="utf-8")
    (app_dir / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='web-demo'\nversion='1.0.0'\n",
        encoding="utf-8",
    )
    (app_dir / "app.manifest.json").write_text(
        json.dumps(
            {
                "appId": "web-demo",
                "name": "Web Demo",
                "version": "1.0.0",
                "services": [
                    {
                        "masId": "web-demo-svc",
                        "mode": "CellSingleton",
                        "runtime": "Python",
                        "pythonConfig": {"arguments": "-c \"import time; time.sleep(10)\""},
                    }
                ],
                "distribution": {"channels": ["stable"]},
            }
        ),
        encoding="utf-8",
    )
    apkg_path, _, _ = SproutPackager(app_dir).package(tmp_path / "dist")

    with SproutManagementServer(host="127.0.0.1", port=0, state=state) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=60.0) as client:
            # Verify all OpenAPI static health endpoints + static UI
            for health_path in ("/api/health", "/api/v1/health"):
                assert health_path in contract["paths"]
                r = client.get(health_path)
                assert r.status_code == 200
                body = r.json()
                assert set(contract["components"]["schemas"]["ManagementHealthResponse"]["required"]).issubset(
                    body.keys()
                )

            index_resp = client.get("/")
            assert index_resp.status_code == 200
            assert "Content-Security-Policy" in index_resp.headers
            assert "cdn." not in index_resp.text.lower()

            # Unauthenticated access rejected with 401
            assert client.get("/api/v1/apps").status_code == 401

            # Bad origin rejected with 403
            bad_origin = client.post(
                "/api/v1/auth/session",
                json={"token": srv.bootstrap_token},
                headers={"Origin": "https://evil.example"},
            )
            assert bad_origin.status_code == 403
            assert "Access-Control-Allow-Origin" not in bad_origin.headers

            # Invalid bootstrap token rejected with 401
            assert client.post("/api/v1/auth/session", json={"token": "wrong"}).status_code == 401

            # Valid session exchange sets HttpOnly SameSite=Strict cookie + returns csrfToken
            auth_resp = client.post("/api/v1/auth/session", json={"token": srv.bootstrap_token})
            assert auth_resp.status_code == 200
            set_cookie = auth_resp.headers.get("set-cookie", "")
            assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie and "aurasprout_session=" in set_cookie
            csrf = auth_resp.json()["csrfToken"]

            # Missing CSRF header blocked with 403
            assert client.get("/api/v1/apps").status_code == 403

            headers = {"X-CSRF-Token": csrf}

            # Publish via /api/v1/catalog/publish
            pub = client.post(
                "/api/v1/catalog/publish",
                json={"packagePath": str(apkg_path), "channel": "stable"},
                headers=headers,
            )
            assert pub.status_code == 200
            assert pub.json()["appId"] == "web-demo"

            # Add catalog source & query catalog
            src_post = client.post(
                "/api/v1/catalog/sources",
                json={"url": str(state.catalog_manager.catalog_dir), "label": "Local", "isDefault": True},
                headers=headers,
            )
            assert src_post.status_code == 200
            assert len(client.get("/api/v1/catalog/sources", headers=headers).json()["sources"]) >= 1
            assert len(client.get("/api/v1/catalog?q=web-demo", headers=headers).json()["apps"]) == 1

            # Install, inspect, toggle volunteer, list conflicts, list runtimes, uninstall
            inst = client.post("/api/v1/apps/install", json={"appId": "web-demo"}, headers=headers)
            assert inst.status_code == 200
            assert inst.json()["appId"] == "web-demo"

            apps_list = client.get("/api/v1/apps", headers=headers).json()["apps"]
            assert any(a["appId"] == "web-demo" for a in apps_list)
            detail_resp = client.get("/api/v1/apps/web-demo", headers=headers)
            assert detail_resp.status_code == 200
            detail_body = detail_resp.json()
            assert detail_body["appId"] == "web-demo"
            assert "guiEntryPoints" in detail_body and len(detail_body["guiEntryPoints"]) >= 1

            vol_list = client.get("/api/v1/volunteer", headers=headers).json()["services"]
            assert vol_list[0]["volunteerEnabled"] is False
            vol_on = client.post(
                "/api/v1/volunteer/web-demo-svc",
                json={"enabled": True},
                headers=headers,
            )
            assert vol_on.status_code == 200
            assert vol_on.json()["volunteerEnabled"] is True

            assert client.get("/api/v1/conflicts", headers=headers).json()["conflicts"] == []
            assert len(client.get("/api/v1/runtimes", headers=headers).json()["runtimes"]) >= 1

            uninst = client.delete("/api/v1/apps/web-demo", headers=headers)
            assert uninst.status_code == 200 and uninst.json()["uninstalled"] is True

