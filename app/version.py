from __future__ import annotations

from pathlib import Path

# VERSION sits at the project root in a source checkout, and is bundled to the
# same place relative to the app package by FlowDownload.spec -- the same
# layout FRONTEND_DIR in app/api/server.py relies on for index.html.
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

try:
    APP_VERSION = _VERSION_FILE.read_text().strip() or "dev"
except OSError:
    APP_VERSION = "dev"
