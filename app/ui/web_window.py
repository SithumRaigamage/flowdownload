from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

from app.engine.manager import DownloadManager


class WebMainWindow(QMainWindow):
    """Hosts the web frontend in a QWebEngineView instead of Qt widgets.

    All the actual UI lives in app/web/index.html, talking to the
    DownloadManager exclusively through the local API server -- this window
    is just a native frame around that page (plus the same close-to-tray
    behavior the old widget-based MainWindow had).
    """

    def __init__(self, manager: DownloadManager, url: str):
        super().__init__()
        self.manager = manager

        # Frameless so the macOS native titlebar disappears — the HTML
        # .titlebar (driven by -webkit-app-region: drag) becomes the only
        # title bar, and it carries content (traffic-light decoration, total
        # speed, theme toggle) that the macOS titlebar can't.
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle("FlowDownload")
        self.resize(1180, 760)

        self.view = QWebEngineView()
        self.view.load(QUrl(url))
        self.setCentralWidget(self.view)

    def closeEvent(self, event) -> None:
        # Read live off the manager rather than a snapshot taken at
        # construction time -- settings can change via the web UI itself
        # (PATCH /api/settings replaces manager.settings wholesale) for as
        # long as this window stays open.
        if self.manager.settings.minimize_to_tray:
            event.ignore()
            self.hide()
        else:
            event.accept()
            self.manager.shutdown()
            QApplication.instance().quit()
