"""SMB-safe atomic directory mutex (`mkdir` + owner heartbeat + race-free stale reclaim)."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_LOG = logging.getLogger(__name__)

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: Dict[Path, threading.RLock] = {}


def _get_process_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[resolved] = lock
        return lock


class MutexTimeoutError(TimeoutError):
    """Raised when ``SproutMutex`` cannot acquire its shared lock within timeout."""


def _is_pid_alive(pid: int) -> Optional[bool]:
    """Return True/False when determinable on the local host without signals on Windows."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    sys_name = platform.system().lower()
    if sys_name == "windows":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
                shell=False,
            )
            if res.returncode != 0:
                return None
            out = res.stdout.strip()
            if not out or "no tasks are running" in out.lower():
                return False
            return f" {int(pid)} " in f" {out} " or str(int(pid)) in out.split()
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


class SproutMutex:
    """Atomic directory creation mutex (`os.mkdir`) suitable for local & SMB shares.

    To avoid TOCTOU races when reclaiming a stale lock held by a crashed peer,
    stale eviction renames ``lock_dir`` atomically into a unique graveyard path
    before deleting it; only the peer whose rename succeeds performs cleanup.
    """

    def __init__(
        self,
        lock_dir: Path,
        stale_seconds: float = 10.0,
        timeout_seconds: float = 10.0,
        owner_id: Optional[str] = None,
    ) -> None:
        self.lock_dir = Path(lock_dir).resolve()
        self.stale_seconds = float(stale_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.owner_id = owner_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._thread_lock = _get_process_lock(self.lock_dir)
        self._held = False
        self._monotonic_seq = 0
        self._depth = 0

    @property
    def owner_file(self) -> Path:
        return self.lock_dir / "owner.json"

    def _read_owner(self) -> Optional[Dict[str, Any]]:
        try:
            if not self.owner_file.exists():
                return None
            return json.loads(self.owner_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_owner(self) -> None:
        self._monotonic_seq += 1
        now = datetime.now(timezone.utc)
        payload = {
            "ownerId": self.owner_id,
            "pid": os.getpid(),
            "hostName": socket.gethostname(),
            "acquiredAt": now.isoformat(),
            "heartbeatAt": now.isoformat(),
            "heartbeatEpoch": time.time(),
            "monotonicSeq": self._monotonic_seq,
        }
        tmp = self.lock_dir / f".owner.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.owner_file)

    def _is_stale(self) -> bool:
        if not self.lock_dir.exists():
            return False
        owner = self._read_owner()
        now_epoch = time.time()
        if owner is None:
            try:
                age = now_epoch - self.lock_dir.stat().st_mtime
                return age > self.stale_seconds
            except OSError:
                return False

        hb_epoch = owner.get("heartbeatEpoch")
        if isinstance(hb_epoch, (int, float)):
            age = now_epoch - float(hb_epoch)
        else:
            age = 0.0

        if age > self.stale_seconds:
            return True

        same_host = owner.get("hostName") == socket.gethostname()
        pid = owner.get("pid")
        if same_host and isinstance(pid, int) and age > 1.5:
            alive = _is_pid_alive(pid)
            if alive is False:
                return True
        return False

    def _try_reclaim_stale(self) -> bool:
        graveyard = self.lock_dir.with_name(
            f".reclaimed_{self.lock_dir.name}_{uuid.uuid4().hex[:10]}"
        )
        try:
            os.replace(self.lock_dir, graveyard)
        except OSError:
            return False
        try:
            shutil.rmtree(graveyard, ignore_errors=True)
        except OSError as exc:
            _LOG.debug("Cleanup of reclaimed lock %s deferred: %s", graveyard, exc)
        return True

    def acquire(self, timeout_seconds: Optional[float] = None) -> "SproutMutex":
        timeout = self.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        deadline = time.monotonic() + max(0.05, timeout)

        if not self._thread_lock.acquire(timeout=max(0.05, timeout)):
            raise MutexTimeoutError(f"Timed out waiting on thread lock for {self.lock_dir}")

        try:
            if self._held:
                self._depth += 1
                return self

            self.lock_dir.parent.mkdir(parents=True, exist_ok=True)
            backoff = 0.01
            while True:
                try:
                    os.mkdir(self.lock_dir)
                    try:
                        self._write_owner()
                    except Exception:
                        shutil.rmtree(self.lock_dir, ignore_errors=True)
                        raise
                    self._held = True
                    self._depth = 1
                    return self
                except FileExistsError:
                    if self._is_stale():
                        if self._try_reclaim_stale():
                            continue
                except PermissionError:
                    # Windows SMB / AV brief handle contention while sibling renames or removes
                    pass

                if time.monotonic() >= deadline:
                    raise MutexTimeoutError(
                        f"Timed out acquiring SproutMutex at {self.lock_dir} after {timeout:.2f}s"
                    )
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 0.1)
        except BaseException:
            self._thread_lock.release()
            raise

    def refresh_heartbeat(self) -> None:
        if self._held and self.lock_dir.exists():
            self._write_owner()

    def release(self) -> None:
        try:
            if not self._held:
                return
            self._depth = max(0, self._depth - 1)
            if self._depth > 0:
                return

            owner = self._read_owner()
            if owner is None or owner.get("ownerId") == self.owner_id:
                graveyard = self.lock_dir.with_name(
                    f".release_{self.lock_dir.name}_{uuid.uuid4().hex[:8]}"
                )
                try:
                    os.replace(self.lock_dir, graveyard)
                    shutil.rmtree(graveyard, ignore_errors=True)
                except OSError:
                    shutil.rmtree(self.lock_dir, ignore_errors=True)
            self._held = False
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "SproutMutex":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        del exc_type, exc_val, exc_tb
        self.release()

