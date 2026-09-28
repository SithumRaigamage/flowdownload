from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import unquote, urlparse

import requests

_FILENAME_STAR_RE = re.compile(r"filename\*=(?:UTF-8'')?\"?([^\";]+)\"?", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)

PROBE_TIMEOUT = 5

# 169.254.0.0/16 is where every major cloud provider (AWS, GCP, Azure,
# DigitalOcean, ...) serves instance credentials from -- there's no
# legitimate reason a download URL would point there. Deliberately not
# blocking all of RFC1918/loopback: downloading from a device on the
# user's own LAN (a home NAS, a local dev server) is a normal use of a
# download manager, so only this specific metadata range is rejected.
_BLOCKED_NETWORKS = (ipaddress.ip_network("169.254.0.0/16"),)


def validate_download_url(url: str) -> None:
    """Reject URLs that are obvious SSRF bait, before anything fetches them.

    Raises ValueError (caller turns it into a user-facing error) on
    rejection. Only catches an IP literal in the blocked range -- a
    hostname that *resolves* to one isn't pinned/re-checked here, since
    doing that safely needs resolving the host and connecting to the
    resolved address directly (not just re-resolving at request time,
    which a malicious DNS server could answer differently for).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// links are supported")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname, not an IP literal -- nothing more to check here
    if any(addr in net for net in _BLOCKED_NETWORKS):
        raise ValueError("This host can't be downloaded from")


def guess_filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    return os.path.basename(path) or "download"


def parse_content_disposition_filename(header: str) -> str | None:
    if not header:
        return None
    match = _FILENAME_STAR_RE.search(header) or _FILENAME_RE.search(header)
    return unquote(match.group(1).strip()) if match else None


def probe_identity(url: str, headers: dict | None = None) -> tuple[str, int] | None:
    """Best-effort (etag, total_size) for a URL, used to recognise the same
    file behind a different (e.g. re-signed) link. None on any failure."""
    try:
        validate_download_url(url)
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=PROBE_TIMEOUT)
        etag = response.headers.get("ETag", "")
        size = int(response.headers.get("Content-Length") or 0)
        if not response.ok or not (etag or size):
            # Some servers don't implement HEAD; a one-byte ranged GET gets
            # the same validators plus the full size in Content-Range.
            response = requests.get(url, headers={**(headers or {}), "Range": "bytes=0-0"},
                                    stream=True, timeout=PROBE_TIMEOUT)
            response.close()
            if not response.ok:
                return None
            etag = response.headers.get("ETag", "")
            total = response.headers.get("Content-Range", "").rpartition("/")[2]
            size = int(total) if total.isdigit() else 0
        return (etag, size) if (etag or size) else None
    except (requests.exceptions.RequestException, ValueError):
        return None


def probe_remote_filename(url: str) -> str | None:
    """Best-effort lookup of the filename the server intends via Content-Disposition.

    Many download links (signed URLs, CDN redirects) have no usable filename in
    the path itself; the server often communicates the real one only in this
    header. Falls back silently to None on any failure — the caller already
    has a URL-based guess to fall back to.
    """
    try:
        validate_download_url(url)
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
    except (requests.exceptions.RequestException, ValueError):
        return None
