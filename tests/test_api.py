"""The web page and settings payload the UI depends on."""
from __future__ import annotations

from app.api.schemas import SettingsOut
from app.api.server import create_app
from app.engine.settings import Settings
from app.version import APP_VERSION


def _index_html() -> str:
    app = create_app(None, None, None, "/unused/settings.json", "test-token", "/unused")
    index = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/")
    return index().body.decode()


def test_index_injects_version_and_token():
    html = _index_html()
    assert f'const APP_VERSION = "{APP_VERSION}"' in html
    assert 'const TOKEN = "test-token"' in html
    assert "__FLOWDOWNLOAD_" not in html


def test_version_matches_version_file():
    with open("VERSION") as f:
        assert APP_VERSION == f.read().strip()


def test_settings_out_exposes_per_site_limit():
    assert SettingsOut.from_settings(Settings()).site_connection_limit == 0
    assert SettingsOut.from_settings(Settings(site_connection_limit=25)).site_connection_limit == 25


def test_old_saved_per_site_limit_is_dropped(tmp_path):
    # 1.1.8/1.1.9 saved max_connections_per_site=10 by default; it must not
    # keep capping downloads after the setting became opt-in.
    path = tmp_path / "settings.json"
    path.write_text('{"max_concurrent_downloads": 30, "max_connections_per_site": 10}')
    settings = Settings.load(str(path))
    assert settings.max_concurrent_downloads == 30
    assert settings.site_connection_limit == 0
