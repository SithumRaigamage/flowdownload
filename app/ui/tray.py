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

        menu = QMenu()
        show_action = menu.addAction("Show FlowDownload")
        show_action.triggered.connect(self._show_window)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(QApplication.instance().quit)
        self.icon.setContextMenu(menu)

        self.icon.activated.connect(self._on_activated)
        self.icon.show()

        self.manager.task_finished.connect(self._on_task_finished)

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
