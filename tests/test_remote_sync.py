"""Unit tests for remote SFTP catalog sync and deployment."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from aurasprout.catalog.remote_sync import (
    SftpCatalogDeployer,
    parse_credentials_file,
)


def test_parse_credentials_file(tmp_path: Path) -> None:
    creds_txt = tmp_path / "creds.txt"
    creds_txt.write_text("username: taytortoter\npassword: secretPassword!\nhost: example.com\nport: 2222\n", encoding="utf-8")
    parsed = parse_credentials_file(creds_txt)
    assert parsed["username"] == "taytortoter"
    assert parsed["password"] == "secretPassword!"
    assert parsed["host"] == "example.com"
    assert parsed["port"] == "2222"


def test_sftp_deployer_sync_with_mock(tmp_path: Path) -> None:
    local_cat = tmp_path / "cat"
    local_cat.mkdir()
    (local_cat / "index.html").write_text("<h1>Catalog</h1>", encoding="utf-8")
    (local_cat / "catalog.json").write_text('{"apps": {}}', encoding="utf-8")
    apps_dir = local_cat / "apps" / "foo"
    apps_dir.mkdir(parents=True)
    (apps_dir / "foo-1.0.0.apkg").write_bytes(b"dummy_apkg_bytes")

    mock_transport = MagicMock()
    mock_sftp = MagicMock()
    mock_sftp.stat.side_effect = Exception("File not found")

    mock_paramiko = MagicMock()
    mock_paramiko.Transport.return_value = mock_transport
    mock_paramiko.SFTPClient.from_transport.return_value = mock_sftp

    with patch.dict(sys.modules, {"paramiko": mock_paramiko}):
        deployer = SftpCatalogDeployer(
            host="example.com",
            username="testuser",
            password="testpass",
            port=22,
        )
        res = deployer.sync_catalog(
            local_catalog_dir=local_cat,
            remote_catalog_dir="remotedir/catalog",
            catalog_base_url="https://example.com/catalog",
        )
        assert res.host == "example.com"
        assert len(res.uploaded_files) >= 3
        assert mock_transport.connect.called
        assert mock_sftp.put.called
        assert mock_sftp.close.called
        assert mock_transport.close.called
