from __future__ import annotations

import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

from app.engine.manager import DownloadManager
from app.ui.macos_window import activate_macos_app, blend_title_bar

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

        self._setup_menu_bar()

        manager.settings_changed.connect(self.apply_theme)
        hints = QApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):  # macOS switched light/dark
            hints.colorSchemeChanged.connect(lambda _scheme: self.apply_theme())
        self.apply_theme()

    def _setup_menu_bar(self) -> None:
        """Create a native macOS/OS menu bar with standard actions."""
        mb = self.menuBar()

        # 1. FlowDownload (Application Menu on macOS)
        app_menu = mb.addMenu("FlowDownload")
        about_act = app_menu.addAction("About FlowDownload")
        about_act.triggered.connect(lambda: self.run_js("typeof showAbout==='function'&&showAbout()"))
        app_menu.addSeparator()
        prefs_act = app_menu.addAction("Preferences...")
        prefs_act.setShortcut(QKeySequence("Ctrl+,"))
        prefs_act.triggered.connect(lambda: self.run_js("document.getElementById('settingsBtn')?.click()"))
        app_menu.addSeparator()
        quit_act = app_menu.addAction("Quit FlowDownload")
        quit_act.setShortcut(QKeySequence("Ctrl+Q"))
        quit_act.triggered.connect(QApplication.instance().quit)

        # 2. File
        file_menu = mb.addMenu("File")
        new_act = file_menu.addAction("New Download...")
        new_act.setShortcut(QKeySequence("Ctrl+N"))
        new_act.triggered.connect(lambda: self.run_js("document.getElementById('addBtn')?.click()"))
        file_menu.addSeparator()
        start_all_act = file_menu.addAction("Start All")
        start_all_act.setShortcut(QKeySequence("Ctrl+Shift+S"))
        start_all_act.triggered.connect(lambda: self.run_js("document.getElementById('startAllBtn')?.click()"))
        pause_all_act = file_menu.addAction("Pause All")
        pause_all_act.setShortcut(QKeySequence("Ctrl+Shift+P"))
        pause_all_act.triggered.connect(lambda: self.run_js("document.getElementById('pauseAllBtn')?.click()"))
        file_menu.addSeparator()
        close_act = file_menu.addAction("Close Window")
        close_act.setShortcut(QKeySequence("Ctrl+W"))
        close_act.triggered.connect(self.close)

        # 3. Edit
        edit_menu = mb.addMenu("Edit")
        edit_menu.addAction("Cut").setShortcut(QKeySequence("Ctrl+X"))
        edit_menu.addAction("Copy").setShortcut(QKeySequence("Ctrl+C"))
        edit_menu.addAction("Paste").setShortcut(QKeySequence("Ctrl+V"))
        edit_menu.addAction("Select All").setShortcut(QKeySequence("Ctrl+A"))

        # 4. View
        view_menu = mb.addMenu("View")
        dl_view_act = view_menu.addAction("Downloads")
        dl_view_act.setShortcut(QKeySequence("Ctrl+1"))
        dl_view_act.triggered.connect(lambda: self.run_js("typeof switchView==='function'&&switchView('downloads')"))
        lib_view_act = view_menu.addAction("Library")
        lib_view_act.setShortcut(QKeySequence("Ctrl+2"))
        lib_view_act.triggered.connect(lambda: self.run_js("typeof switchView==='function'&&switchView('library')"))
        view_menu.addSeparator()
        tiles_act = view_menu.addAction("Tiles View")
        tiles_act.setShortcut(QKeySequence("Ctrl+Alt+1"))
        tiles_act.triggered.connect(lambda: self.run_js("typeof switchLayout==='function'&&switchLayout('tiles')"))
        list_act = view_menu.addAction("List View")
        list_act.setShortcut(QKeySequence("Ctrl+Alt+2"))
        list_act.triggered.connect(lambda: self.run_js("typeof switchLayout==='function'&&switchLayout('list')"))
        view_menu.addSeparator()
        theme_act = view_menu.addAction("Toggle Appearance")
        theme_act.setShortcut(QKeySequence("Ctrl+T"))
        theme_act.triggered.connect(lambda: self.run_js("document.getElementById('themeBtn')?.click()"))
        reload_act = view_menu.addAction("Reload")
        reload_act.setShortcut(QKeySequence("Ctrl+R"))
        reload_act.triggered.connect(self.view.reload)

        # 5. Window
        win_menu = mb.addMenu("Window")
        min_act = win_menu.addAction("Minimize")
        min_act.setShortcut(QKeySequence("Ctrl+M"))
        min_act.triggered.connect(self.showMinimized)
        zoom_act = win_menu.addAction("Zoom")
        zoom_act.triggered.connect(self._toggle_zoom)
        win_menu.addSeparator()
        front_act = win_menu.addAction("Bring All to Front")
        front_act.triggered.connect(lambda: activate_macos_app(self))

        # 6. Help
        help_menu = mb.addMenu("Help")
        shortcuts_act = help_menu.addAction("Keyboard Shortcuts")
        shortcuts_act.setShortcut(QKeySequence("Ctrl+/"))
        shortcuts_act.triggered.connect(lambda: self.run_js("document.getElementById('helpBtn')?.click()"))

    def run_js(self, script: str) -> None:
        if self.view and self.view.page():
            self.view.page().runJavaScript(script)

    def _toggle_zoom(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

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
        activate_macos_app(self)

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
