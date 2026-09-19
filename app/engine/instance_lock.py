from __future__ import annotations

import errno
import fcntl
import os


class InstanceLock:
    """flock-based single-instance guard.

    Needs to be checkable from a plain Python script with no Qt dependency
    (the Chrome native messaging host does this too, to decide whether it
    needs to launch FlowDownload or can rely on the running instance to pick
    up a new row via its polling), so this deliberately avoids QLockFile.

    Uses fcntl.flock rather than a plain PID-file check: flock is atomic (no
    check-then-write gap for concurrent launches to race through) and is
    released automatically by the kernel on process exit or crash, so it
    can't go stale the way a PID file can if the PID gets recycled.
    """

    def __init__(self, pid_file: str):
        self.pid_file = pid_file
        self._fd: int | None = None

    @staticmethod
    def is_running(pid_file: str) -> bool:
        if not os.path.exists(pid_file):
            return False
        fd = os.open(pid_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return True
            return False
        finally:
            os.close(fd)

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
        fd = os.open(self.pid_file, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
