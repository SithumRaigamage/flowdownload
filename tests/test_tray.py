"""Tray behaviour that affects the user's focus."""
from __future__ import annotations

from PySide6.QtWidgets import QWidget

from app.ui.tray import TrayController


class _Window(QWidget):
    def __init__(self):
        super().__init__()
        self.activations = 0

    def activateWindow(self):  # noqa: N802 -- Qt name
        self.activations += 1


def test_new_download_never_surfaces_the_window(make_manager, qapp):
    # A download added from Chrome must not reopen a window the user
    # closed to the tray or minimized, nor steal focus from a visible one.
    window = _Window()
    manager = make_manager()
    tray = TrayController(window, manager)
    window.show()
    manager.task_added.emit("t1")
    assert window.activations == 0
    window.hide()
    manager.task_added.emit("t2")
    assert not window.isVisible() and window.activations == 0
    tray.icon.hide()
