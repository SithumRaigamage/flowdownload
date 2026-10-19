"""The app window's theme handling (the page itself is covered in tests/ui)."""
from __future__ import annotations

from app.ui.web_window import PAGE_BACKGROUND, WebMainWindow


def test_window_follows_the_theme_setting(make_manager, qapp):
    mgr = make_manager()
    window = WebMainWindow(mgr, "about:blank")
    try:
        for theme, dark in (("dark", True), ("light", False)):
            mgr.apply_settings(mgr.settings.__class__(**{**mgr.settings.__dict__, "theme": theme}))
            assert window.is_dark() is dark
            assert window.view.page().backgroundColor().name().upper() == PAGE_BACKGROUND[dark]
    finally:
        window.view.setPage(None)
        window.deleteLater()
        qapp.processEvents()
