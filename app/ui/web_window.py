from __future__ import annotations

import os
import sys

from PySide6.QtCore import QUrl, Qt, Signal, QObject
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

from app.engine.manager import DownloadManager


def _log(msg: str) -> None:
    """Unbuffered logging to stderr — PySide6 GUI apps don't flush stdout by default."""
    print(msg, file=sys.stderr, flush=True)


class _FlowDownloadInterceptor(QWebEngineUrlRequestInterceptor):
    """Intercepts requests matching flowdownload:// URLs and fires signals.

    This is the most reliable way to bridge JavaScript → Python in QWebEngineView,
    since urlChanged doesn't reliably fire for custom URL schemes.
    """
    openVideoRequested = Signal(str)  # task_id

    def __init__(self):
        super().__init__()

    def interceptRequest(self, info):
        url = info.requestUrl()
        if url.scheme() == "flowdownload":
            _log(f"[Interceptor] Got URL: {url.toString()}, host={url.host()}")
        if url.scheme() == "flowdownload" and url.host() == "openVideo":
            # Read the ?id=... query parameter
            task_id = url.queryItemValue("id")
            _log(f"[Interceptor] openVideo requested, task_id={task_id!r}")
            if task_id:
                self.openVideoRequested.emit(task_id)
            # Block the navigation so the page doesn't try to load this URL
            info.block(True)


class QtBridge(QObject):
    """Exposes Qt methods to the web UI via runJavaScript bridge.

    The web UI can call window.qt_openVideo(taskId) to open a completed
    video download in a native Qt media player window.
    """
    # Signal emitted when the web UI wants to preview a video
    previewRequested = Signal(str)  # task_id

    def __init__(self, parent=None):
        super().__init__(parent)

    def openVideo(self, taskId: str) -> None:
        """Called from JavaScript: window.qt_openVideo(taskId)"""
        self.previewRequested.emit(taskId)


class VideoPlayerWindow(QWidget):
    """A native Qt video player using QMediaPlayer + QVideoWidget.

    This bypasses QWebEngineView's finicky HTML5 media pipeline entirely,
    giving reliable in-app video playback for downloaded files.
    """

    def __init__(self, filePath: str, title: str):
        super().__init__()
        self.setWindowTitle(f"FlowDownload — {title}")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumSize(640, 360)
        self.resize(960, 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)

        self.videoWidget = QVideoWidget()
        self.videoWidget.setMinimumSize(640, 360)
        layout.addWidget(self.videoWidget)
        self.player.setVideoOutput(self.videoWidget)

        # Connect error/loading signals so we can surface failures.
        self.player.errorOccurred.connect(self._onError)
        self.player.mediaStatusChanged.connect(self._onStatus)
        self.player.playbackStateChanged.connect(self._onPlayback)

        # Use the local file URL. setSource expects a QUrl.
        self._filePath = os.path.abspath(filePath)
        _log(f"[VideoPlayer] Opening: {self._filePath}, exists={os.path.exists(self._filePath)}, size={os.path.getsize(self._filePath) if os.path.exists(self._filePath) else 0}")
        self.player.setSource(QUrl.fromLocalFile(self._filePath))
        self.videoWidget.show()
        self.player.play()
        self.show()

    def _onError(self, error, errorString=""):
        _log(f"[VideoPlayer] Error: {error} - {errorString}")

    def _onStatus(self, status):
        from PySide6.QtMultimedia import QMediaPlayer
        names = {
            QMediaPlayer.MediaStatus.NoMedia: "NoMedia",
            QMediaPlayer.MediaStatus.LoadingMedia: "LoadingMedia",
            QMediaPlayer.MediaStatus.LoadedMedia: "LoadedMedia",
            QMediaPlayer.MediaStatus.BufferingMedia: "BufferingMedia",
            QMediaPlayer.MediaStatus.BufferedMedia: "BufferedMedia",
            QMediaPlayer.MediaStatus.EndOfMedia: "EndOfMedia",
            QMediaPlayer.MediaStatus.InvalidMedia: "InvalidMedia",
        }
        _log(f"[VideoPlayer] MediaStatus: {names.get(status, status)}")

    def _onPlayback(self, state):
        from PySide6.QtMultimedia import QMediaPlayer
        names = {
            QMediaPlayer.PlaybackState.StoppedState: "Stopped",
            QMediaPlayer.PlaybackState.PlayingState: "Playing",
            QMediaPlayer.PlaybackState.PausedState: "Paused",
        }
        _log(f"[VideoPlayer] PlaybackState: {names.get(state, state)}")


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
        self._videoPlayers: list = []  # keep refs alive

        # Use Qt's standard window decoration so macOS draws its native
        # traffic-light buttons (close/minimize/zoom). The HTML frontend
        # does NOT draw a custom titlebar of its own; the entire UI sits
        # below the OS-managed title bar.
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle("FlowDownload")
        self.resize(1180, 760)

        self._appUrl = QUrl(url)
        self.view = QWebEngineView()

        # Install the URL interceptor BEFORE loading any page — this must be done
        # before load() so it can intercept custom-scheme URLs from the start.
        self.interceptor = _FlowDownloadInterceptor()
        self.interceptor.openVideoRequested.connect(self._onPreviewRequested)
        from PySide6.QtWebEngineCore import QWebEngineProfile
        QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(self.interceptor)

        # Enable video/audio playback in WebEngine
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        # Allow media playback without user gesture (important for auto-play)
        settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)

        self.view.load(self._appUrl)
        self.setCentralWidget(self.view)

    def _onPreviewRequested(self, taskId: str) -> None:
        """Called when the web UI requests a video preview via window.qt.openVideo()."""
        _log(f"[WebMainWindow] _onPreviewRequested: {taskId!r}")
        task = self.manager.tasks.get(taskId)
        if task is None:
            _log(f"[WebMainWindow] Task not found: {taskId}")
            return
        _log(f"[WebMainWindow] Task found: status={task.status}, dest={task.dest_path}")
        if task.status.value != "completed":
            _log(f"[WebMainWindow] Task not completed: {task.status.value}")
            return
        if not os.path.exists(task.dest_path):
            _log(f"[WebMainWindow] File not found: {task.dest_path}")
            return
        player = VideoPlayerWindow(task.dest_path, task.filename)
        self._videoPlayers.append(player)

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
