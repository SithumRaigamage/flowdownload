#!/Users/sithumraigamage/projects/flowdownload/venv/bin/python3
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
import subprocess
import sys
import time
import traceback

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app.engine.instance_lock import InstanceLock  # noqa: E402
from app.engine.models import DownloadStatus, DownloadTask  # noqa: E402
from app.engine.paths import dedupe_path  # noqa: E402
from app.engine.probe import guess_filename_from_url  # noqa: E402
from app.engine.settings import Settings  # noqa: E402
from app.engine.storage import Storage  # noqa: E402

APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")
DB_PATH = os.path.join(APP_DATA_DIR, "downloads.db")
SETTINGS_PATH = os.path.join(APP_DATA_DIR, "settings.json")
PID_FILE = os.path.join(APP_DATA_DIR, "app.pid")
VENV_PYTHON = os.path.join(PROJECT_ROOT, "venv", "bin", "python3")
RUN_SCRIPT = os.path.join(PROJECT_ROOT, "run.py")
APP_BUNDLE = "/Applications/FlowDownload.app"
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


def launch_app_if_needed() -> None:
    if InstanceLock.is_running(PID_FILE):
        return
    if os.path.isdir(APP_BUNDLE):
        # `open -a` launches through the app bundle, so the Dock shows the
        # real FlowDownload icon -- launching venv/bin/python3 directly (the
        # fallback below) makes macOS show a bare Python interpreter instead.
        subprocess.Popen(["open", "-a", APP_BUNDLE], start_new_session=True)
    else:
        subprocess.Popen(
            [VENV_PYTHON, RUN_SCRIPT],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _bring_to_front() -> None:
    # `open -a` on macOS activates the app if it's already running
    # (raising its window to the foreground) or launches it if not. This
    # is the "show_window" path -- separate from launch_app_if_needed
    # because that one refuses to relaunch when the app is already up.
    if os.path.isdir(APP_BUNDLE):
        subprocess.Popen(["open", "-a", APP_BUNDLE], start_new_session=True)
    else:
        subprocess.Popen(
            [VENV_PYTHON, RUN_SCRIPT],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


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

    _write_extension_state(profile_id, profile_name)

    os.makedirs(APP_DATA_DIR, exist_ok=True)
    settings = Settings.load(SETTINGS_PATH)

    # basename() strips any directory components, so a crafted filename
    # can't escape settings.download_dir via the joined path.
    filename = os.path.basename(message.get("filename") or "") or guess_filename_from_url(url)

    storage = Storage(DB_PATH)
    # Dedupe check: if the URL is already represented by a completed task,
    # surface that to the extension so the popup can say "Already downloaded"
    # instead of silently queueing a duplicate. What happens next depends on
    # settings.duplicate_policy -- mirrors DownloadManager.add_download so
    # the Chrome hand-off and the in-app "Add download" dialog agree.
    existing_match = None
    duplicate = None
    for existing in storage.load_all():
        if existing.status == DownloadStatus.COMPLETED and existing.url == url:
            existing_match = existing
            duplicate = {
                "task_id": existing.id,
                "dest_path": existing.dest_path,
                "size": existing.total_bytes,
                "digest": existing.digest_sha256,
            }
            break

    policy = getattr(settings, "duplicate_policy", "rename")
    if existing_match is not None and policy == "skip":
        launch_app_if_needed()
        return {"ok": True, "filename": existing_match.filename, "duplicate": duplicate}

    # Auto-folder rule (matches the engine's apply_auto_folder):
    # when the user has opted in, route by extension into a subfolder
    # so videos land in <Downloads>/Video, archives in /Archives, etc.
    base_dir = settings.download_dir
    if getattr(settings, "auto_folder_by_type", False):
        from app.engine.manager import apply_auto_folder_for_path
        base_dir = apply_auto_folder_for_path(filename, base_dir)

    # Tasks already queued (here or in the running app) that haven't written
    # a byte yet have no .part file for dedupe_path's filesystem check to
    # see -- without this, two downloads handed off in quick succession with
    # the same suggested filename would collide and corrupt each other.
    reserved = {t.dest_path for t in storage.load_all()}
    dest_path = os.path.join(base_dir, filename)

    if existing_match is not None and policy == "overwrite":
        if os.path.exists(existing_match.dest_path):
            os.remove(existing_match.dest_path)
        storage.delete(existing_match.id)
        reserved.discard(existing_match.dest_path)
        dest_path = existing_match.dest_path
    # "rename" (default) falls through unchanged -- dedupe_path below already
    # picks a fresh filename on any collision.

    dest_path = dedupe_path(dest_path, reserved)
    task = DownloadTask(
        url=url, dest_path=dest_path, filename=os.path.basename(dest_path),
        status=DownloadStatus.QUEUED, cookie_header=message.get("cookie") or "",
    )
    storage.save(task)

    launch_app_if_needed()
    response = {"ok": True, "filename": task.filename}
    if duplicate is not None:
        response["duplicate"] = duplicate
    return response


def main() -> None:
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
