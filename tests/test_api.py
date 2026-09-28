"""The web page and settings payload the UI depends on."""
from __future__ import annotations

from app.api.schemas import SettingsOut
from app.api.server import create_app
from app.engine.manager import MAX_PER_DOMAIN
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


def test_settings_out_exposes_per_domain_limit():
    assert SettingsOut.from_settings(Settings()).max_per_domain == MAX_PER_DOMAIN
