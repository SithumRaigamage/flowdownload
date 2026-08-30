from __future__ import annotations

import os
from collections.abc import Collection


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
