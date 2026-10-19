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


def test_new_download_only_surfaces_a_hidden_window(make_manager, qapp):
    window = _Window()
    tray = TrayController(window, make_manager())
    window.show()
    tray._on_task_added("t1")
    assert window.activations == 0  # already visible: don't steal focus
    window.hide()
    tray._on_task_added("t2")
    assert window.isVisible() and window.activations == 1
    tray.icon.hide()
