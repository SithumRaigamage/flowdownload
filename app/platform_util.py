"""The few things FlowDownload asks of the OS, for macOS, Windows and Linux.

Deliberately Qt-free: the Chrome native host imports this too, and it has to
start fast on every hand-off.
"""
from __future__ import annotations

import os
import subprocess
import sys

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

# Windows: keep a console window from flashing up for each helper process.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def subprocess_env() -> dict[str, str]:
    """The environment to run system tools (xdg-open, zenity, ffmpeg) with.

    A frozen Linux build points LD_LIBRARY_PATH at its own bundled libraries
    and keeps the user's original in LD_LIBRARY_PATH_ORIG; system binaries
    loading the bundle's Qt/ssl libraries instead of their own crash, so put
    the original back for them.
    """
    env = dict(os.environ)
    if getattr(sys, "frozen", False) and not IS_MAC and not IS_WINDOWS:
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def run_tool(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for an external tool: the system library path on
    Linux, and no console window on Windows."""
    return subprocess.run(cmd, env=subprocess_env(), creationflags=_NO_WINDOW, **kwargs)


def reveal(path: str) -> None:
    """Show `path` in the file manager (selected, where the OS supports it)."""
    if IS_MAC:
        subprocess.run(["open", "-R", path], check=False)
    elif IS_WINDOWS:
        # explorer.exe exits 1 even on success, so don't check it.
        subprocess.run(["explorer", "/select,", os.path.normpath(path)], check=False)
    else:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            subprocess.Popen(["xdg-open", folder], env=subprocess_env(), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass  # no desktop file manager integration installed


def open_url(url: str) -> None:
    """Open a URL in the user's default browser."""
    if IS_MAC:
        subprocess.run(["open", url], check=False)
    elif IS_WINDOWS:
        try:
            os.startfile(url)  # type: ignore[attr-defined]
        except Exception:
            subprocess.run(["cmd", "/c", "start", "", url], check=False)
    else:
        try:
            subprocess.Popen(["xdg-open", url], env=subprocess_env(), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass



_WINDOWS_PICK_FOLDER = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.ShowNewFolderButton = $true;"
    "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true};"
    "if ($d.ShowDialog($owner) -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
)


def pick_folder() -> str | None:
    """Ask the user for a folder with the native dialog; None if cancelled.

    The dialog runs in a separate process on every OS, so waiting on the
    user never blocks the Qt main thread or the download workers.
    """
    if IS_MAC:
        commands = [["osascript", "-e", "POSIX path of (choose folder)"]]
    elif IS_WINDOWS:
        commands = [["powershell", "-NoProfile", "-STA", "-Command", _WINDOWS_PICK_FOLDER]]
    else:
        commands = [
            ["zenity", "--file-selection", "--directory"],
            ["kdialog", "--getexistingdirectory", os.path.expanduser("~")],
        ]
    for cmd in commands:
        try:
            result = run_tool(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            continue  # this dialog tool isn't installed; try the next one
        path = result.stdout.strip()
        return path if result.returncode == 0 and path else None
    return None


def launch_detached(cmd: list[str], cwd: str | None = None) -> None:
    """Start a process that outlives this one (the Chrome host exits right away)."""
    if IS_WINDOWS:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, cwd=cwd, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(cmd, cwd=cwd, start_new_session=True, env=subprocess_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
