"""Register FlowDownload's native messaging host with Chrome, on any OS.

Chrome finds a native host through a small JSON manifest. Where that file
lives differs per OS (a folder on macOS and Linux, a registry key pointing
at it on Windows); everything else is the same. Used by
scripts/setup_chrome_integration.py for source checkouts, and by a packaged
build on every launch (register_installed_app), so a downloaded release
works with the extension without running any script -- and keeps working
after the user moves the app.

Qt-free on purpose, like app/platform_util.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from app.platform_util import IS_MAC, IS_WINDOWS

HOST_NAME = "com.flowdownload.host"
APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")
# Same layout trick as app/version.py: chrome_extension/ sits next to the
# app package in a checkout and is bundled to the same place.
EXTENSION_DIR = Path(__file__).resolve().parent.parent / "chrome_extension"
_WINDOWS_REG_KEY = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"


def extension_id_from_key(pubkey_der: bytes) -> str:
    """Chrome's extension ID for a public key: the first 16 bytes of its
    SHA-256, each nibble written as a letter a-p."""
    digest = hashlib.sha256(pubkey_der).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)


def bundled_extension_id() -> str:
    """The ID of the extension shipped with this copy of FlowDownload, which
    is fixed by the "key" in its manifest.json wherever it's loaded from."""
    with open(EXTENSION_DIR / "manifest.json") as f:
        key = json.load(f)["key"]
    return extension_id_from_key(base64.b64decode(key))


def manifest_dirs() -> list[str]:
    """Folders Chrome (and Chromium, on Linux) read host manifests from."""
    home = os.path.expanduser("~")
    if IS_MAC:
        return [os.path.join(home, "Library", "Application Support", "Google", "Chrome", "NativeMessagingHosts")]
    if IS_WINDOWS:
        # Any folder works on Windows -- the registry key says where it is.
        return [APP_DATA_DIR]
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [os.path.join(config, browser, "NativeMessagingHosts") for browser in ("google-chrome", "chromium")]


def register(extension_id: str, host_path: str) -> list[str]:
    """Write the host manifest pointing Chrome at `host_path`; returns the
    manifest paths written."""
    manifest = {
        "name": HOST_NAME,
        "description": "FlowDownload native messaging host",
        "path": host_path,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }
    written = []
    for folder in manifest_dirs():
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, f"{HOST_NAME}.json")
        with open(dest, "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")
        written.append(dest)
    if IS_WINDOWS:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WINDOWS_REG_KEY) as key:
            winreg.SetValue(key, "", winreg.REG_SZ, written[0])
    return written


def is_registered() -> bool:
    if IS_WINDOWS:
        import winreg

        try:
            path = winreg.QueryValue(winreg.HKEY_CURRENT_USER, _WINDOWS_REG_KEY)
        except OSError:
            return False
        return os.path.exists(path)
    return any(os.path.exists(os.path.join(d, f"{HOST_NAME}.json")) for d in manifest_dirs())


def installed_host_path() -> str:
    """The host executable shipped next to this packaged app's executable.

    Inside an AppImage that path is a temporary mount that's gone once the
    app quits, so Chrome is pointed at a small launcher that re-enters the
    AppImage (its AppRun runs the host when given --native-host) instead.
    """
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        launcher = os.path.join(APP_DATA_DIR, "flowdownload-host")
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        with open(launcher, "w") as f:
            f.write(f'#!/bin/sh\nexec "{appimage}" --native-host "$@"\n')
        os.chmod(launcher, 0o755)
        return launcher
    return os.path.join(exe_dir, "flowdownload-host.exe" if IS_WINDOWS else "flowdownload-host")


def register_installed_app() -> None:
    """Point Chrome at this packaged build's host. Cheap and idempotent, so
    it simply runs on every launch of a frozen build; a failure only means
    the Chrome hand-off isn't available, so it never stops the app."""
    if not getattr(sys, "frozen", False):
        return  # a source checkout registers via scripts/setup_chrome_integration.py
    try:
        host = installed_host_path()
        if os.path.exists(host):
            register(bundled_extension_id(), host)
    except Exception as exc:  # noqa: BLE001
        print(f"Chrome integration registration failed: {exc}", file=sys.stderr)
