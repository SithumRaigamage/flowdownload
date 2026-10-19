from __future__ import annotations

import faulthandler
import os
import signal
import sys

from app.engine.instance_lock import InstanceLock

APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")
DB_PATH = os.path.join(APP_DATA_DIR, "downloads.db")
SETTINGS_PATH = os.path.join(APP_DATA_DIR, "settings.json")
PID_FILE = os.path.join(APP_DATA_DIR, "app.pid")
LOG_PATH = os.path.join(APP_DATA_DIR, "flowdownload.log")
LOG_MAX_BYTES = 5_000_000


def _log_to_file() -> None:
    """Send stdout/stderr to LOG_PATH unless we're attached to a terminal.

    Launched from Finder/the Dock, the bundled app has no console
    (console=False in FlowDownload.spec), so every diagnostic the code
    prints -- swallowed exceptions, ffmpeg failures, uvicorn errors -- was
    simply lost. One previous log is kept as .1 when it gets large.
    """
    if sys.stderr is not None and sys.stderr.isatty():
        return  # `python run.py` in a terminal: keep output where it's seen
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass  # no log yet
    log = open(LOG_PATH, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = log
    faulthandler.enable(log)  # Qt aborts and segfaults leave a traceback too


def main() -> None:
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    # Holds browser cookies (in the DB) and transcoded copies of downloaded
    # media; every local account shares the "staff" group, so keep it private.
    os.chmod(APP_DATA_DIR, 0o700)

    lock = InstanceLock(PID_FILE)
    if not lock.acquire():
        # Another instance owns the database and dispatches downloads; a
        # second DownloadManager here would race it on the same rows.
        print("FlowDownload is already running.", file=sys.stderr)
        sys.exit(0)
    _log_to_file()  # only after the lock: the running instance owns the log

    # Imported here, after the lock check, so a losing process exits before
    # paying for PySide6/QtWebEngine's slow import instead of racing it.
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.api.runner import ApiServer
    from app.engine.manager import DownloadManager
    from app.engine.settings import Settings
    from app.engine.storage import Storage
    from app.ui.tray import TrayController, make_app_icon
    from app.ui.web_window import WebMainWindow

    app = QApplication(sys.argv)
    app.setWindowIcon(make_app_icon(128))  # sets the macOS Dock icon, not just the titlebar
    app.setQuitOnLastWindowClosed(False)  # closing to tray shouldn't end the app
    app.aboutToQuit.connect(lock.release)

    settings = Settings.load(SETTINGS_PATH)
    storage = Storage(DB_PATH)
    manager = DownloadManager(storage, settings)
    app.aboutToQuit.connect(manager.shutdown)

    # Serves app/web/index.html plus the JSON/WebSocket API it talks to; the
    # window below is just a QWebEngineView pointed at this.
    api_server = ApiServer(manager, SETTINGS_PATH)
    api_server.start()
    app.aboutToQuit.connect(api_server.stop)

    window = WebMainWindow(manager, api_server.page_url)

    window.show()

    tray = TrayController(window, manager)  # noqa: F841 -- keeps the tray icon alive

    # Ctrl+C in the terminal (SIGINT) is otherwise swallowed: Python only
    # checks for a pending signal between bytecode instructions, but Qt's
    # C++ event loop rarely hands control back to the interpreter, so the
    # signal handler never runs and the process just sits there. A SIGINT
    # handler that calls app.quit(), plus a Python-level timer that fires
    # often enough to give the interpreter a chance to notice, fixes it.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    wakeup_timer = QTimer()
    wakeup_timer.timeout.connect(lambda: None)
    wakeup_timer.start(200)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
