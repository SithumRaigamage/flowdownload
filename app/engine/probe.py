from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlparse

import requests

_FILENAME_STAR_RE = re.compile(r"filename\*=(?:UTF-8'')?\"?([^\";]+)\"?", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)

PROBE_TIMEOUT = 5


def guess_filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    return os.path.basename(path) or "download"


def parse_content_disposition_filename(header: str) -> str | None:
    if not header:
        return None
    match = _FILENAME_STAR_RE.search(header) or _FILENAME_RE.search(header)
    return unquote(match.group(1).strip()) if match else None


def probe_remote_filename(url: str) -> str | None:
    """Best-effort lookup of the filename the server intends via Content-Disposition.

    Many download links (signed URLs, CDN redirects) have no usable filename in
    the path itself; the server often communicates the real one only in this
    header. Falls back silently to None on any failure — the caller already
    has a URL-based guess to fall back to.
    """
    try:
        response = requests.head(url, allow_redirects=True, timeout=PROBE_TIMEOUT)
        if "Content-Disposition" not in response.headers:
            # Some servers don't implement HEAD properly; try a minimal ranged
            # GET instead, closing the connection immediately without reading
            # the body.
            response = requests.get(
                url, headers={"Range": "bytes=0-0"}, stream=True, timeout=PROBE_TIMEOUT
            )
            response.close()
        return parse_content_disposition_filename(response.headers.get("Content-Disposition", ""))
    except requests.exceptions.RequestException:
        return None
