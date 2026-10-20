from __future__ import annotations

import errno
import os
import sys

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        # Byte 0, non-blocking. Raises OSError if another process holds it.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    # msvcrt reports a held lock as EACCES or EDEADLOCK (36).
    _HELD = (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLOCK", 36))
else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    _HELD = (errno.EACCES, errno.EAGAIN)


class InstanceLock:
    """OS-lock-based single-instance guard.

    Needs to be checkable from a plain Python script with no Qt dependency
    (the Chrome native messaging host does this too, to decide whether it
    needs to launch FlowDownload or can rely on the running instance to pick
    up a new row via its polling), so this deliberately avoids QLockFile.

    Uses an OS file lock (fcntl.flock; msvcrt.locking on Windows) rather
    than a plain PID-file check: it's atomic (no check-then-write gap for
    concurrent launches to race through) and is released automatically on
    process exit or crash, so it can't go stale the way a PID file can if
    the PID gets recycled.
    """

    def __init__(self, pid_file: str):
        self.pid_file = pid_file
        self._fd: int | None = None

    @staticmethod
    def is_running(pid_file: str) -> bool:
        if not os.path.exists(pid_file):
            return False
        fd = os.open(pid_file, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            _try_lock(fd)
            _unlock(fd)
            return False
        except OSError as e:
            return e.errno in _HELD
        finally:
            os.close(fd)

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
        fd = os.open(self.pid_file, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o644)
        try:
            _try_lock(fd)
        except OSError as e:
            os.close(fd)
            if e.errno in _HELD:
                return False
            raise
        if sys.platform == "win32":
            # Byte 0 is the locked region on Windows; keep the PID after it.
            os.ftruncate(fd, 1)
            os.lseek(fd, 1, os.SEEK_SET)
        else:
            os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            _unlock(self._fd)
            os.close(self._fd)
            self._fd = None
