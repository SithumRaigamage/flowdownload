from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from app.ui.theme import ACCENT


def make_app_icon(size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    painter.drawEllipse(QRectF(0, 0, size, size))

    painter.setBrush(QColor("white"))
    stem_width = size * 0.14
    stem = QRectF(size / 2 - stem_width / 2, size * 0.22, stem_width, size * 0.32)
    painter.drawRoundedRect(stem, stem_width / 2, stem_width / 2)

    arrow = QPolygonF([
        QPointF(size * 0.30, size * 0.52),
        QPointF(size * 0.70, size * 0.52),
        QPointF(size * 0.50, size * 0.76),
    ])
    painter.drawPolygon(arrow)
    painter.end()

    return QIcon(pixmap)


class TrayController:
    def __init__(self, window, manager):
        self.window = window
        self.manager = manager

        self.icon = QSystemTrayIcon(make_app_icon(), window)
        self.icon.setToolTip("FlowDownload")

        # Must be stored as self.menu so Python's reference count keeps the
        # underlying QMenu alive for the lifetime of this TrayController.
        # Without this, the menu is a local that goes out of scope when
        # __init__ returns, leaving the QSystemTrayIcon with a dangling pointer
        # to a freed QMenu -- causing a SIGSEGV in QAction destructor on quit.
        self.menu = QMenu()
        show_action = self.menu.addAction("Show FlowDownload")
        show_action.triggered.connect(self._show_window)
        self.menu.addSeparator()
        quit_action = self.menu.addAction("Quit")
        quit_action.triggered.connect(QApplication.instance().quit)
        self.icon.setContextMenu(self.menu)

        self.icon.activated.connect(self._on_activated)
        self.icon.show()

        self.manager.task_finished.connect(self._on_task_finished)
        # A download that arrives while the window is hidden (started
        # minimized at launch, or closed to tray) should still surface the
        # window -- the whole point of hiding on a bare cold start is that
        # there's nothing to show yet, not that downloads should stay silent.
        self.manager.task_added.connect(self._show_window)

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_window()

    def _show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _on_task_finished(self, task_id: str, status_value: str) -> None:
        if not self.manager.settings.notifications_enabled:
            return
        task = self.manager.tasks.get(task_id)
        if task is None:
            return
        if status_value == "completed":
            self.icon.showMessage(
                "Download complete", task.filename, QSystemTrayIcon.MessageIcon.Information, 5000
            )
        else:
            self.icon.showMessage(
                "Download failed", f"{task.filename}: {task.error_message}",
                QSystemTrayIcon.MessageIcon.Warning, 5000,
            )
