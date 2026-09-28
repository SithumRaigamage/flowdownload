from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Collection
from urllib.parse import urlparse

_DEDUPE_SUFFIX_RE = re.compile(r"^(.*) \((\d+)\)(\.[^.]*)?$")


def domain_key(url: str) -> str:
    """Group key for per-site limits and matching: the last two hostname labels.

    c7.trafficdeposit.com and c10.trafficdeposit.com both map to
    trafficdeposit.com. Without a public-suffix list, foo.co.uk and
    bar.co.uk share a key too -- that only makes a per-site limit stricter.
    """
    host = (urlparse(url).hostname or "").lower()
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return ".".join(host.split(".")[-2:])


def strip_dedupe_suffix(filename: str) -> str:
    """'movie (2).mp4' -> 'movie.mp4': undo what dedupe_path added."""
    match = _DEDUPE_SUFFIX_RE.match(filename)
    return match.group(1) + (match.group(3) or "") if match else filename


def dedupe_path(path: str, reserved: Collection[str] = ()) -> str:
    """If path already exists (or a .part for it is mid-download), append
    ' (1)', ' (2)', ... before the extension until it's unique.

    `reserved` is the set of dest_paths already claimed by other tasks that
    haven't written a byte to disk yet (e.g. still QUEUED behind the
    concurrency limit) -- a filesystem-only check can't see those, so two
    downloads queued back-to-back with the same suggested filename would
    otherwise both resolve to the same path and corrupt each other once
    both start writing.
    """
    def _taken(p: str) -> bool:
        return p in reserved or os.path.exists(p) or os.path.exists(p + ".part")

    if not _taken(path):
        return path
    root, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = f"{root} ({n}){ext}"
        if not _taken(candidate):
            return candidate
        n += 1
