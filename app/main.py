from __future__ import annotations

import os
import signal
import sys

from app.engine.instance_lock import InstanceLock

APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")
DB_PATH = os.path.join(APP_DATA_DIR, "downloads.db")
SETTINGS_PATH = os.path.join(APP_DATA_DIR, "settings.json")
PID_FILE = os.path.join(APP_DATA_DIR, "app.pid")


def main() -> None:
    os.makedirs(APP_DATA_DIR, exist_ok=True)

    lock = InstanceLock(PID_FILE)
    if not lock.acquire():
        # Another instance owns the database and dispatches downloads; a
        # second DownloadManager here would race it on the same rows.
        print("FlowDownload is already running.", file=sys.stderr)
        sys.exit(0)

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

    window = WebMainWindow(manager, f"http://127.0.0.1:{api_server.port}/")
    # Let the API server ask the window to open a completed file in the
    # native OS media player. The callback runs on the Qt main thread via
    # the server's MainThreadExecutor, so it is safe to construct QWidgets
    # inside.
    api_server.set_native_player_opener(window.open_native_player)
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
