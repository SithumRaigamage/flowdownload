from __future__ import annotations

import os


class InstanceLock:
    """Simple PID-file based single-instance guard.

    Needs to be checkable from a plain Python script with no Qt dependency
    (the Chrome native messaging host does this too, to decide whether it
    needs to launch FlowDownload or can rely on the running instance to pick
    up a new row via its polling), so this deliberately avoids QLockFile.
    """

    def __init__(self, pid_file: str):
        self.pid_file = pid_file
        self._acquired = False

    @staticmethod
    def is_running(pid_file: str) -> bool:
        if not os.path.exists(pid_file):
            return False
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
        except (ValueError, OSError):
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False  # stale pid file, process is gone
        return True

    def acquire(self) -> bool:
        if self.is_running(self.pid_file):
            return False
        os.makedirs(os.path.dirname(self.pid_file), exist_ok=True)
        with open(self.pid_file, "w") as f:
            f.write(str(os.getpid()))
        self._acquired = True
        return True

    def release(self) -> None:
        if self._acquired and os.path.exists(self.pid_file):
            os.remove(self.pid_file)
            self._acquired = False
