from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

# VERSION sits at the project root in a source checkout, and is bundled to the
# same place relative to the app package by FlowDownload.spec -- the same
# layout FRONTEND_DIR in app/api/server.py relies on for index.html.
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
# BUILD_INFO is written by FlowDownload.spec at build time (commit count +
# build date) so a packaged build reports the revision it was built from even
# though git history isn't shipped with the bundle. It's absent in a source
# checkout, where we fall back to running git ourselves.
_BUILD_INFO_FILE = Path(__file__).resolve().parent.parent / "BUILD_INFO"


def _read_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def _read_build_info() -> tuple[str, str]:
    """Return (build_number, build_date_iso). Never raises."""
    try:
        text = _BUILD_INFO_FILE.read_text(encoding="utf-8").strip()
        number, date = (p.strip() for p in text.split(",", 1))
        if number and date:
            return number, date
    except (OSError, ValueError):
        pass
    # Source checkout: derive from git instead. The build number is the
    # commit count, matching what the release workflow tags against.
    repo = _VERSION_FILE.parent
    try:
        number = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        date = subprocess.run(
            ["git", "show", "-s", "--format=%cI", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if number and date:
            return number, date
    except (OSError, subprocess.SubprocessError):
        pass
    return "dev", ""


APP_VERSION = _read_version()
APP_BUILD, APP_BUILD_DATE = _read_build_info()


def build_label() -> str:
    """The "(35)" style label shown next to the version in the About pane."""
    return f"({APP_BUILD})"


def build_date_label() -> str:
    """Human-readable build date for the About footer, e.g. '20 Oct 2026'.

    Empty when neither BUILD_INFO nor git could supply a date (a checkout
    with no history and no build), so the footer degrades gracefully instead
    of showing a blank or a wrong date.
    """
    if not APP_BUILD_DATE:
        return ""
    try:
        # BUILD_INFO stores a plain YYYY-MM-DD; git's %cI gives a full
        # timestamp with a timezone offset, so keep only the date part.
        return datetime.date.fromisoformat(APP_BUILD_DATE[:10]).strftime("%d %b %Y")
    except ValueError:
        return ""