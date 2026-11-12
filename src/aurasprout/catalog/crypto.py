"""Cryptographic SHA-256 verification and traversal-safe archive extraction."""

from __future__ import annotations

import hashlib
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import List

from aurasprout.contracts.paths import validate_within_root


def compute_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_sha256(file_path: Path) -> str:
    target = Path(file_path).resolve()
    h = hashlib.sha256()
    with open(target, "rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(file_path: Path, expected_sha256: str) -> str:
    """Compute SHA-256 of ``file_path`` and raise ValueError on mismatch."""
    expected = (expected_sha256 or "").strip().lower()
    if len(expected) != 64:
        raise ValueError(f"Invalid SHA-256 checksum format: {expected_sha256!r}")
    actual = compute_sha256(file_path).lower()
    if actual != expected:
        raise ValueError(f"SHA-256 mismatch for {file_path}: expected {expected}, got {actual}")
    return actual


def _validate_archive_member_name(name: str) -> None:
    if not name or "\x00" in name:
        raise ValueError(f"Invalid archive member name: {name!r}")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ValueError(f"Absolute member path rejected in archive: {name!r}")
    parts = [p for p in normalized.split("/") if p]
    if ".." in parts:
        raise ValueError(f"Path traversal member rejected in archive: {name!r}")


def extract_archive_safely(archive_path: Path, dest_dir: Path) -> List[Path]:
    """Safely extract a `.apkg` / `.zip` / `.tar.gz` archive into ``dest_dir``.

    Rejects Zip-Slip, Tar-Slip (`..`, rooted paths, Windows drive letters),
    symlinks, hardlinks, and device nodes BEFORE writing any files to ``dest_dir``.
    """
    src = Path(archive_path).resolve()
    dst = Path(dest_dir).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    extracted: List[Path] = []

    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src, "r") as zf:
            members = zf.infolist()
            for info in members:
                _validate_archive_member_name(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode):
                    raise ValueError(f"ZIP symlink member rejected: {info.filename!r}")
                validate_within_root(dst, info.filename)

            for info in members:
                target = validate_within_root(dst, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as reader, open(target, "wb") as writer:
                        writer.write(reader.read())
                    extracted.append(target)
        return extracted

    if tarfile.is_tarfile(src):
        with tarfile.open(src, "r:*") as tf:
            members = tf.getmembers()
            for member in members:
                _validate_archive_member_name(member.name)
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ValueError(f"Unsafe tar member type rejected: {member.name!r}")
                validate_within_root(dst, member.name)

            tf.extractall(path=dst, members=members, filter="data")
            for member in members:
                if member.isfile():
                    extracted.append(validate_within_root(dst, member.name))
        return extracted

    raise ValueError(f"Unsupported package archive format: {src}")

