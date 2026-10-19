from __future__ import annotations

import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

from app.engine.manager import DownloadManager
from app.ui.macos_window import blend_title_bar

# The page's --background in each theme (app/web/index.html :root / .dark):
# the title bar and the web view's own backdrop take the same colour, so the
# window reads as one surface and never flashes white while loading.
PAGE_BACKGROUND = {False: "#F4F4F3", True: "#0F0F10"}


class WebMainWindow(QMainWindow):
    """Hosts the web frontend in a QWebEngineView.

    All the actual UI lives in app/web/index.html, talking to the
    DownloadManager exclusively through the local API server. Media
    playback is also handled in-page: the /stream-webm endpoint
    transcodes H.264/AAC to VP9/Opus (WebM) on the fly, so the
    in-app <video>/<audio> elements decode natively in Qt WebEngine's
    open-source Chromium FFmpeg build (which lacks H.264/AAC).

    Earlier revisions used a Qt QMediaPlayer (with native OS codec
    support) and a QStackedWidget to embed the player. That worked but
    spawned a separate native player instance; the new approach keeps
    everything inside the existing web view.
    """

    def __init__(self, manager: DownloadManager, url: str):
        super().__init__()
        self.manager = manager

        # Use Qt's standard window decoration so macOS draws its native
        # traffic-light buttons (close/minimize/zoom). The HTML frontend
        # does NOT draw a custom titlebar of its own; the entire UI sits
        # below the OS-managed title bar, which apply_theme() blends into
        # the page (transparent, no title text, page-coloured).
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle("FlowDownload")
        self.resize(1180, 760)

        # Append a build-version query string so the Qt WebEngine cache doesn't
        # keep serving a stale index.html after a code change. The host's
        # /api server ignores the query string, but it makes the URL unique
        # per build and bypasses the disk cache.
        self._appUrl = QUrl(url + ("&" if "?" in url else "?") + "v=" + str(int(time.time())))
        self.view = QWebEngineView()

        # Enable video/audio playback in WebEngine. The transcoded VP9/Opus
        # stream is supported by the open-source Chromium FFmpeg build that
        # ships with Qt WebEngine, so the in-page <video> element decodes
        # it natively without a custom media backend.
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        # Allow media playback without user gesture (autoplay once the
        # user has clicked a card to open the modal).
        settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)

        self.view.load(self._appUrl)
        self.setCentralWidget(self.view)

        manager.settings_changed.connect(self.apply_theme)
        hints = QApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):  # macOS switched light/dark
            hints.colorSchemeChanged.connect(lambda _scheme: self.apply_theme())
        self.apply_theme()

    def is_dark(self) -> bool:
        """The effective theme: the setting, or macOS's appearance for "system"."""
        theme = getattr(self.manager.settings, "theme", "system")
        if theme in ("light", "dark"):
            return theme == "dark"
        hints = QApplication.styleHints()
        return hasattr(hints, "colorScheme") and hints.colorScheme() == Qt.ColorScheme.Dark

    def apply_theme(self) -> None:
        dark = self.is_dark()
        self.view.page().setBackgroundColor(QColor(PAGE_BACKGROUND[dark]))
        blend_title_bar(self, PAGE_BACKGROUND[dark], dark)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The native window only exists once shown; blend it now too.
        self.apply_theme()

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
