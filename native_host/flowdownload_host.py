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


def handle_message(message: dict) -> dict:
    url = message.get("url")
    if not url:
        return {"ok": False, "error": "missing url"}

    os.makedirs(APP_DATA_DIR, exist_ok=True)
    settings = Settings.load(SETTINGS_PATH)

    # basename() strips any directory components, so a crafted filename
    # can't escape settings.download_dir via the joined path.
    filename = os.path.basename(message.get("filename") or "") or guess_filename_from_url(url)

    storage = Storage(DB_PATH)
    # Tasks already queued (here or in the running app) that haven't written
    # a byte yet have no .part file for dedupe_path's filesystem check to
    # see -- without this, two downloads handed off in quick succession with
    # the same suggested filename would collide and corrupt each other.
    reserved = {t.dest_path for t in storage.load_all()}
    dest_path = dedupe_path(os.path.join(settings.download_dir, filename), reserved)
    task = DownloadTask(
        url=url, dest_path=dest_path, filename=os.path.basename(dest_path),
        status=DownloadStatus.QUEUED, cookie_header=message.get("cookie") or "",
    )
    storage.save(task)

    launch_app_if_needed()
    return {"ok": True, "filename": task.filename}


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
