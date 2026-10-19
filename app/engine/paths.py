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


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def safe_filename(name: str, fallback: str = "download") -> str:
    """A filename that can only ever name a file inside its folder.

    basename() strips directories, but leaves "." and ".." (which joined
    onto the download folder mean the folder itself, or its parent), and
    leading dots would make the download a hidden file. Control
    characters are dropped too, since they can't be typed or displayed.
    """
    name = _CONTROL_CHARS_RE.sub("", os.path.basename(name or "")).strip().lstrip(".")
    return name or fallback


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


# File-type extensions that go into subfolders when auto-folder is enabled.
AUTO_FOLDER_VIDEO = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".vid", ".m4v"}
AUTO_FOLDER_AUDIO = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"}
AUTO_FOLDER_DOCUMENT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
AUTO_FOLDER_ARCHIVE = {".zip", ".tar", ".gz", ".7z", ".rar", ".dmg", ".pkg", ".iso"}


def apply_auto_folder_for_path(filename: str, base_dir: str) -> str:
    """<base_dir>/<type subfolder>/<filename> for the auto-folder setting.

    Lives here, not in manager.py, so the native host (which never loads
    Qt) shares the exact routing rules with the app.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in AUTO_FOLDER_VIDEO:
        sub = "Video"
    elif ext in AUTO_FOLDER_AUDIO:
        sub = "Audio"
    elif ext in AUTO_FOLDER_DOCUMENT:
        sub = "Documents"
    elif ext in AUTO_FOLDER_ARCHIVE:
        sub = "Archives"
    else:
        sub = "Other"
    return os.path.join(base_dir, sub, filename)


_AUTO_FOLDER_SUBDIRS = {"Video", "Audio", "Documents", "Archives", "Other"}


def prune_empty_auto_folder_dir(dest_path: str, download_dir: str) -> None:
    """Remove the auto-folder subfolder (Video/Audio/.../Other) a just-
    deleted file lived in, if that was the last thing in it.

    apply_auto_folder_for_path creates these on demand but nothing ever
    cleaned one up, so a download folder accumulates empty Video/Audio/...
    directories forever as files move through it. Deliberately narrow: only
    touches a directory whose name is one of the fixed auto-folder names
    and whose *parent* is exactly download_dir, and os.rmdir refuses to
    remove a non-empty directory -- so this can never reach outside that
    known structure or delete anything that still has content.
    """
    parent = os.path.dirname(dest_path)
    if os.path.basename(parent) not in _AUTO_FOLDER_SUBDIRS:
        return
    if os.path.dirname(parent) != os.path.normpath(download_dir):
        return
    try:
        os.rmdir(parent)
    except OSError:
        pass  # not empty, or already gone -- either way, nothing to do
