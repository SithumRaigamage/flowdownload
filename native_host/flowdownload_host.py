#!/usr/bin/env python3
"""Chrome native messaging host for FlowDownload.

Chrome launches this script (per the manifest registered in Chrome's
NativeMessagingHosts directory) when the extension calls
chrome.runtime.sendNativeMessage. It speaks Chrome's native messaging
framing (4-byte little-endian length prefix + UTF-8 JSON) over stdio,
inserts a queued download row directly into FlowDownload's database, and
launches the app if it isn't already running. Deliberately imports only
from app.engine (no PySide6/Qt) so this responds quickly.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import time
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.engine.instance_lock import InstanceLock  # noqa: E402
from app.engine.models import DownloadStatus, DownloadTask  # noqa: E402
from app.engine.paths import domain_key, safe_filename, strip_dedupe_suffix  # noqa: E402
from app.engine.probe import guess_filename_from_url, probe_identity, validate_download_url  # noqa: E402
from app.engine.queueing import find_completed, queue_download  # noqa: E402
from app.engine.settings import Settings  # noqa: E402
from app.engine.storage import Storage  # noqa: E402
from app.platform_util import IS_MAC, IS_WINDOWS, launch_detached  # noqa: E402

APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")
DB_PATH = os.path.join(APP_DATA_DIR, "downloads.db")
SETTINGS_PATH = os.path.join(APP_DATA_DIR, "settings.json")
PID_FILE = os.path.join(APP_DATA_DIR, "app.pid")
VENV_PYTHON = (os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe") if IS_WINDOWS
               else os.path.join(PROJECT_ROOT, "venv", "bin", "python3"))
RUN_SCRIPT = os.path.join(PROJECT_ROOT, "run.py")
# Where build_app.sh can put the app: --install-system, --install, or the
# plain build output. First existing one wins; none -> run from source.
APP_BUNDLE_CANDIDATES = (
    "/Applications/FlowDownload.app",
    os.path.join(os.path.expanduser("~"), "Applications", "FlowDownload.app"),
    os.path.join(PROJECT_ROOT, "dist", "FlowDownload.app"),
) if IS_MAC else ()
EXTENSION_STATE_FILE = os.path.join(APP_DATA_DIR, "chrome_extension_state.json")


def read_message() -> dict | None:
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        return None
    length = struct.unpack("<I", raw_length)[0]
    data = sys.stdin.buffer.read(length)
    return json.loads(data.decode("utf-8"))


def send_message(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _find_app_bundle() -> str | None:
    if getattr(sys, "frozen", False):
        # The copy bundled in FlowDownload.app (Contents/MacOS/flowdownload-host):
        # launch the app it ships in, not whichever one happens to be installed.
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))))
    return next((p for p in APP_BUNDLE_CANDIDATES if os.path.isdir(p)), None)


def _app_command() -> list[str]:
    """How to start FlowDownload from this host."""
    if IS_MAC:
        bundle = _find_app_bundle()
        if bundle:
            # `open -a` launches through the app bundle, so the Dock shows the
            # real FlowDownload icon -- launching venv/bin/python3 directly (the
            # fallback below) makes macOS show a bare Python interpreter instead.
            # If the app is already running it just activates it.
            return ["open", "-a", bundle]
    elif getattr(sys, "frozen", False):
        # Inside an AppImage, re-run the AppImage itself: this host's own
        # directory is a temporary mount that goes away when the host exits.
        if os.environ.get("APPIMAGE"):
            return [os.environ["APPIMAGE"]]
        # Windows/Linux builds ship the app right next to this host.
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        return [os.path.join(exe_dir, "FlowDownload.exe" if IS_WINDOWS else "FlowDownload")]
    return [VENV_PYTHON, RUN_SCRIPT]


def _open_app() -> None:
    frozen = getattr(sys, "frozen", False)
    launch_detached(_app_command(), cwd=None if frozen else PROJECT_ROOT)


def launch_app_if_needed() -> None:
    if InstanceLock.is_running(PID_FILE):
        return
    _open_app()


def _bring_to_front() -> None:
    # The "show_window" path: unlike launch_app_if_needed, this runs even
    # when the app is already up, so `open -a` raises its window.
    _open_app()


def _write_extension_state(profile_id: str, profile_name: str) -> None:
    """Record a heartbeat from the extension so the app can tell which
    Chrome profile is alive and warn when the extension has been disabled
    or removed. Preserves any previously-known profiles from the file.
    """
    state = {
        "last_ping": time.time(),
        "last_profile_id": profile_id,
        "last_profile_name": profile_name,
        "profiles": {},
    }
    try:
        with open(EXTENSION_STATE_FILE) as f:
            old = json.load(f)
            state["profiles"] = old.get("profiles", {})
    except (OSError, json.JSONDecodeError):
        pass
    state["profiles"][profile_id] = {
        "name": profile_name,
        "last_seen": state["last_ping"],
    }
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        with open(EXTENSION_STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        # Don't take down the download path just because we couldn't
        # write the heartbeat file -- the worst case is a stale status.
        traceback.print_exc(file=sys.stderr)


def _safe_referrer(value) -> str:
    """Only an http(s) page URL is worth sending on as a Referer header."""
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return ""


def _find_refresh_target(storage: Storage, url: str, filename: str,
                         cookie: str, referrer: str) -> DownloadTask | None:
    """The one unfinished download this new link is most likely a fresh copy of.

    Candidates: failed or paused downloads on the same site, with partial
    data on disk, under a different URL. The first unique match wins:
      1. same filename (ignoring a " (n)" suffix dedupe_path added), unless
         a quick probe of the new link shows a different size -- sites
         reuse generic names (video.mp4, download), and a name alone used
         to hand an unrelated file's link to an old download, which then
         never showed up as its own, else
      2. same ETag, else same size, per that probe -- re-signed CDN links
         often change the path, and so the filename guessed from it, while
         serving the identical file.
    Anything ambiguous returns None, and the link is queued as a new download.
    """
    site = domain_key(url)
    candidates = [
        t for t in storage.load_all()
        if t.status in (DownloadStatus.FAILED, DownloadStatus.PAUSED)
        and t.url != url and domain_key(t.url) == site and os.path.exists(t.part_path)
    ]
    if not candidates:
        return None  # the common case: no network probe at all
    headers = {k: v for k, v in (("Cookie", cookie), ("Referer", referrer)) if v}
    identity = probe_identity(url, headers)
    by_name = [t for t in candidates if strip_dedupe_suffix(t.filename) == filename]
    if len(by_name) == 1:
        # Size, not ETag: CDN edge nodes often give the same file different
        # ETags, which is exactly the re-signed-link case this exists for.
        new_size = identity[1] if identity else 0
        if not (new_size and by_name[0].total_bytes and new_size != by_name[0].total_bytes):
            return by_name[0]
    if identity is None:
        return None
    etag, size = identity
    for matches in ([t for t in candidates if etag and t.etag == etag],
                    [t for t in candidates if size and t.total_bytes == size]):
        if len(matches) == 1:
            return matches[0]
    return None


def handle_message(message: dict) -> dict:
    action = message.get("action")
    profile_id = message.get("profile_id") or "unknown"
    profile_name = message.get("profile_name") or "Unknown"

    # Heartbeat from the extension -- no UI effect, just keeps the state
    # file fresh so the app can show which profile is alive.
    if action == "ping":
        _write_extension_state(profile_id, profile_name)
        return {"ok": True}

    # "Bring FlowDownload to the front" from the extension popup.
    if action == "show_window":
        _bring_to_front()
        return {"ok": True}

    url = message.get("url")
    if not url:
        return {"ok": False, "error": "missing url"}
    try:
        validate_download_url(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    _write_extension_state(profile_id, profile_name)

    os.makedirs(APP_DATA_DIR, exist_ok=True)
    settings = Settings.load(SETTINGS_PATH)

    # safe_filename() strips directory components (and "."/".."), so a
    # crafted filename can't escape settings.download_dir via the joined path.
    filename = safe_filename(message.get("filename") or "", fallback=safe_filename(guess_filename_from_url(url)))

    storage = Storage(DB_PATH)
    # Dedupe check: if the URL is already represented by a completed task,
    # surface that to the extension so the popup can say "Already downloaded"
    # instead of silently queueing a duplicate. What happens next depends on
    # settings.duplicate_policy -- mirrors DownloadManager.add_download so
    # the Chrome hand-off and the in-app "Add download" dialog agree.
    existing_match = find_completed(storage.load_all(), url)
    duplicate = None
    if existing_match is not None:
        duplicate = {
            "task_id": existing_match.id,
            "dest_path": existing_match.dest_path,
            "size": existing_match.total_bytes,
            "digest": existing_match.digest_sha256,
        }

    cookie = message.get("cookie") or ""
    referrer = _safe_referrer(message.get("referrer"))
    target = _find_refresh_target(storage, url, filename, cookie, referrer)
    if target is not None:
        # A fresh link for a download that stalled (typically an expired
        # signed URL): hand it to the app to resume the existing partial
        # file instead of starting a duplicate from zero.
        storage.add_refresh(target.id, url, cookie, referrer)
        launch_app_if_needed()
        return {"ok": True, "filename": target.filename, "refreshed": True}

    policy = getattr(settings, "duplicate_policy", "rename")
    if existing_match is not None and policy == "skip":
        launch_app_if_needed()
        return {"ok": True, "filename": existing_match.filename, "duplicate": duplicate}

    # Same path rules (auto-folder, dedupe, cross-process collision retry)
    # as the in-app "Add download" dialog -- both go through queue_download.
    replace = existing_match if policy == "overwrite" else None
    try:
        task = queue_download(storage, settings, url, filename, cookie=cookie, referrer=referrer, replace=replace)
    except RuntimeError:
        return {"ok": False, "error": "could not find a free filename"}

    launch_app_if_needed()
    response = {"ok": True, "filename": task.filename}
    if duplicate is not None:
        response["duplicate"] = duplicate
    return response


def main() -> None:
    if IS_WINDOWS:
        # Chrome's framing is binary; never let the C runtime translate \n.
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    try:
        message = read_message()
        if message is None:
            return
        response = handle_message(message)
    except Exception as exc:  # noqa: BLE001 -- always report failure back to the extension
        traceback.print_exc(file=sys.stderr)
        response = {"ok": False, "error": str(exc)}
    send_message(response)


if __name__ == "__main__":
    main()
