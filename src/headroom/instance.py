"""One Headroom process per user. A second launch exits instead of sampling twice."""

from __future__ import annotations

import os
from pathlib import Path

from headroom.logsetup import default_log_path

# Windows byte-range locks are mandatory: nobody else can read the locked byte 0.
# The pid is written after it so other processes can still read it.
_PID_OFFSET = 8
_RECORD_BYTES = 48


def default_lock_path(env=None, home: Path | None = None) -> Path:
    return default_log_path(env, home).parent / "headroom.lock"


def read_lock_pid(path: Path) -> int | None:
    for offset in (0, _PID_OFFSET):
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(_RECORD_BYTES)
        except OSError:
            continue
        tokens = data.split()
        if tokens and tokens[0].isdigit():
            return int(tokens[0])
        return None
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
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
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
        record = b" " * _PID_OFFSET + f"{os.getpid()}\n".encode("ascii")
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, record.ljust(_RECORD_BYTES, b" "))
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
