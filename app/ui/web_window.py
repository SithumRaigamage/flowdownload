from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QUrl, Qt
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

from app.engine.manager import DownloadManager


def _log(msg: str) -> None:
    """Unbuffered logging to stderr — PySide6 GUI apps don't flush stdout by default."""
    print(msg, file=sys.stderr, flush=True)


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
        # below the OS-managed title bar.
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
        settings.setAttribute(QWebEngineSettings.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        # Allow media playback without user gesture (autoplay once the
        # user has clicked a card to open the modal).
        settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)

        self.view.load(self._appUrl)
        self.setCentralWidget(self.view)

    def open_native_player(self, taskId: str) -> None:
        """No-op kept for API compatibility with ApiServer.set_native_player_opener.

        Earlier revisions used this to open a QMediaPlayer window. With the
        on-the-fly VP9 transcoding endpoint, all playback happens in-page
        via the <video> element, so this callback is a no-op. The
        /api/downloads/{id}/open-in-player endpoint still returns 200;
        playback begins the moment the web UI's <video> element starts
        loading /stream-webm.
        """
        _log(f"[WebMainWindow] open_native_player (no-op, transcoded stream): {taskId!r}")

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
