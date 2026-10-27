"""One Headroom process per user. A second launch exits instead of sampling twice."""

from __future__ import annotations

import os
from pathlib import Path

from headroom.logsetup import default_log_path


def default_lock_path(env=None, home: Path | None = None) -> Path:
    return default_log_path(env, home).parent / "headroom.lock"


def read_lock_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    token = text.split()[0] if text.split() else ""
    if token.isdigit():
        return int(token)
    return None


class InstanceLock:
    """Exclusive lock file. The holder writes its pid. Release is idempotent."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Return False when another live process already holds the lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            # Windows locks are mandatory, so the second open can fail before flock.
            if _lock_is_busy(exc):
                return False
            raise
        try:
            self._try_lock(fd)
        except OSError as exc:
            os.close(fd)
            if _lock_is_busy(exc):
                return False
            raise
        self._fd = fd
        self._write_pid(fd)
        return True

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            self._unlock(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _try_lock(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)

    def _write_pid(self, fd: int) -> None:
        payload = f"{os.getpid()}\n".encode("ascii")
        if os.name == "nt":
            payload = payload.ljust(32, b" ")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
        else:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
        try:
            os.fsync(fd)
        except OSError:
            pass


def _lock_is_busy(exc: OSError) -> bool:
    if isinstance(exc, (BlockingIOError, PermissionError)):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror in {32, 33, 36}:
        return True
    return exc.errno in {11, 13, 35, 36}
