"""Lightweight cross-process resource slots.

The desktop GUI can dispatch long-running worker processes. In-process semaphores do
not protect ordinary PCs once multiple workers exist, so this module uses small
lock files under data/runtime/locks. Stale locks are cleaned by checking whether
the owning PID is still alive.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import DATA_DIR, config


LOCK_DIR = DATA_DIR / "runtime" / "locks"


def pid_alive(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        # `kill(pid, 0)` also succeeds for a zombie.  That is especially
        # visible on macOS when the GUI has not reaped a just-stopped worker:
        # treating it as alive makes Stop wait forever and blocks a resume.
        if sys.platform == "darwin":
            try:
                state = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(pid)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=1,
                    check=False,
                ).stdout.strip().upper()
                if state.startswith("Z"):
                    return False
            except (OSError, subprocess.SubprocessError):
                # Keep the conservative answer if `ps` itself is unavailable.
                pass
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_query_limited_information = 0x1000
        error_invalid_parameter = 87
        error_access_denied = 5
        still_active = 259

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == error_invalid_parameter:
                return False
            if error == error_access_denied:
                return True
            return False
        exit_code = ctypes.c_ulong()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # Prefer waiting over accidentally deleting a live lock on Windows.
        return True


def _coerce_limit(value, default: int, hard_cap: int) -> int:
    try:
        raw = int(value)
    except Exception:
        raw = default
    return max(1, min(hard_cap, raw))


def _timeout_value() -> float | None:
    try:
        raw = float(config.resource_wait_timeout)
    except Exception:
        raw = 0.0
    return raw if raw > 0 else None


class SlotPool:
    def __init__(self, name: str, config_key: str, default: int, hard_cap: int = 32):
        self.name = name
        self.config_key = config_key
        self.default = default
        self.hard_cap = hard_cap

    @property
    def lock_dir(self) -> Path:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        return LOCK_DIR

    def limit(self) -> int:
        return _coerce_limit(config.get(self.config_key, self.default), self.default, self.hard_cap)

    def _lock_path(self, index: int) -> Path:
        return self.lock_dir / f"{self.name}_{index}.lock"

    @staticmethod
    def _read_pid(path: Path) -> int:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            return int(text.splitlines()[0]) if text else 0
        except Exception:
            return 0

    def _cleanup_stale(self, path: Path) -> None:
        pid = self._read_pid(path)
        if pid_alive(pid):
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _try_acquire(self, action: str) -> Path | None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        safe_action = str(action or "").replace("\r", " ").replace("\n", " ").strip()
        for index in range(self.limit()):
            path = self._lock_path(index)
            try:
                fd = os.open(path, flags)
            except FileExistsError:
                self._cleanup_stale(path)
                try:
                    fd = os.open(path, flags)
                except FileExistsError:
                    continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n{time.time():.3f}\nslot={self.name}\naction={safe_action}\n")
            return path
        return None

    @contextmanager
    def acquire(self, *, action: str = "", timeout_seconds: float | None = None) -> Iterator[None]:
        timeout = _timeout_value() if timeout_seconds is None else timeout_seconds
        started = time.monotonic()
        path: Path | None = None
        while path is None:
            path = self._try_acquire(action)
            if path is None:
                if timeout is not None and time.monotonic() - started >= timeout:
                    raise TimeoutError(f"Timed out waiting for {self.name} slot while {action or 'working'}")
                time.sleep(1.0)
        try:
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def owners(self) -> dict[int, str]:
        owners: dict[int, str] = {}
        for path in self.lock_dir.glob(f"{self.name}_*.lock"):
            self._cleanup_stale(path)
            if not path.exists():
                continue
            pid = self._read_pid(path)
            if pid_alive(pid):
                owners[pid] = str(path)
        return owners


EXTERNAL_API_SLOT = SlotPool("external_api", "max_concurrent_external_api", default=2, hard_cap=32)
FFMPEG_SLOT = SlotPool("ffmpeg", "max_concurrent_ffmpeg", default=1, hard_cap=8)
MEDIA_PROBE_SLOT = SlotPool("media_probe", "max_concurrent_media_probe", default=2, hard_cap=16)


@contextmanager
def external_api_slot(*, action: str = "", timeout_seconds: float | None = None) -> Iterator[None]:
    with EXTERNAL_API_SLOT.acquire(action=action, timeout_seconds=timeout_seconds):
        yield


@contextmanager
def ffmpeg_slot(*, action: str = "", timeout_seconds: float | None = None) -> Iterator[None]:
    with FFMPEG_SLOT.acquire(action=action, timeout_seconds=timeout_seconds):
        yield


@contextmanager
def media_probe_slot(*, action: str = "", timeout_seconds: float | None = None) -> Iterator[None]:
    with MEDIA_PROBE_SLOT.acquire(action=action, timeout_seconds=timeout_seconds):
        yield


def active_slot_owners() -> dict[str, dict[int, str]]:
    return {
        "external_api": EXTERNAL_API_SLOT.owners(),
        "ffmpeg": FFMPEG_SLOT.owners(),
        "media_probe": MEDIA_PROBE_SLOT.owners(),
    }
