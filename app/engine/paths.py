from __future__ import annotations

import ipaddress
import os
import re
import sys
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


def link_expiry(url: str) -> float | None:
    """When a signed link stops working, as epoch seconds, or None if the
    URL carries no expiry.

    CDNs hand out links that expire after a while, e.g.
    https://c10.trafficdeposit.com/vidi/<sig>/<token>/1793389545/<id>/x.vid
    (epoch seconds in the path), or ...?expires=1793389545 in the query.
    """
    parsed = urlparse(url)
    candidates = parsed.path.split("/") + [value for _, _, value in
                                           (pair.partition("=") for pair in parsed.query.split("&"))]
    for value in candidates:
        # 10 digits from 2001-09-09 up to 2286: a long numeric id elsewhere
        # in a path would need to be both 10 digits and land in that range.
        if len(value) == 10 and value.isdigit() and int(value) >= 1_000_000_000:
            return float(value)
    return None


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*]')


def safe_filename(name: str, fallback: str = "download") -> str:
    """A filename that can only ever name a file inside its folder.

    basename() strips directories, but leaves "." and ".." (which joined
    onto the download folder mean the folder itself, or its parent), and
    leading dots would make the download a hidden file. Control
    characters are dropped too, since they can't be typed or displayed.
    On Windows, characters it doesn't allow in names become "_".
    """
    name = _CONTROL_CHARS_RE.sub("", os.path.basename(name or "")).strip().lstrip(".")
    if sys.platform == "win32":
        # Characters Windows refuses in a filename, and the trailing dots and
        # spaces it silently drops (which would break the dedupe checks).
        name = _WINDOWS_ILLEGAL_RE.sub("_", name).rstrip(". ")
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


# Where files go when "Sort into category folders" is on, unless the user
# edits the list in Settings. These are the folders the fixed Video/Audio/
# Documents/Archives routing used, so turning the feature into editable
# categories moved nobody's files.
DEFAULT_CATEGORIES: tuple[dict, ...] = (
    {"name": "Video", "extensions": ["mp4", "mkv", "mov", "webm", "avi", "vid", "m4v"], "folder": "Video"},
    {"name": "Audio", "extensions": ["mp3", "wav", "aac", "m4a", "flac", "ogg"], "folder": "Audio"},
    {"name": "Documents", "extensions": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"], "folder": "Documents"},
    {"name": "Archives", "extensions": ["zip", "tar", "gz", "7z", "rar", "dmg", "pkg", "iso"], "folder": "Archives"},
)
# Files no category claims.
OTHER_FOLDER = "Other"


def default_categories() -> list[dict]:
    return [{**c, "extensions": list(c["extensions"])} for c in DEFAULT_CATEGORIES]


MAX_CATEGORIES = 30
_EXTENSION_RE = re.compile(r"^[a-z0-9][a-z0-9_+-]{0,15}$")


def validate_categories(raw: object, *, check_folders: bool = True) -> list[dict]:
    """A clean category list, or ValueError saying what's wrong with it.

    Shared by the settings API (check_folders=True: an absolute folder must
    exist now) and Settings.load (False: a folder that's gone since is
    recreated on the next download, like a subfolder). A subfolder must be
    one plain name, so a category can never point outside the download
    folder through "../".
    """
    if not isinstance(raw, list):
        raise ValueError("Categories must be a list")
    if len(raw) > MAX_CATEGORIES:
        raise ValueError(f"At most {MAX_CATEGORIES} categories")
    clean, names, owners = [], set(), {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each category needs a name, extensions and a folder")
        name, folder, exts = item.get("name"), item.get("folder"), item.get("extensions")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            raise ValueError("Each category needs a name of up to 40 characters")
        name = name.strip()
        if name.lower() in names:
            raise ValueError(f"There are two categories called {name}")
        names.add(name.lower())
        if not isinstance(exts, list) or not all(isinstance(e, str) for e in exts):
            raise ValueError(f"{name}: extensions must be a list of names like mp4")
        extensions = []
        for ext in exts:
            ext = ext.strip().lower().lstrip(".")
            if not ext or ext in extensions:
                continue
            if not _EXTENSION_RE.match(ext):
                raise ValueError(f"{name}: \"{ext}\" isn't a file extension")
            if ext in owners:
                raise ValueError(f".{ext} is in both {owners[ext]} and {name}")
            owners[ext] = name
            extensions.append(ext)
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError(f"{name}: choose a folder")
        folder = folder.strip()
        if os.path.isabs(folder):
            if check_folders and not os.path.isdir(folder):
                raise ValueError(f"{name}: the folder {folder} doesn't exist")
        elif folder in (".", "..") or "/" in folder or "\\" in folder or folder != safe_filename(folder):
            raise ValueError(f"{name}: a folder inside the download folder must be one plain name, like Video")
        clean.append({"name": name, "extensions": extensions, "folder": folder})
    return clean


def category_for(filename: str, categories: list[dict]) -> dict | None:
    """The first category whose extensions include this file's, or None."""
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if not ext:
        return None
    return next((c for c in categories if ext in c.get("extensions", ())), None)


def category_folder(category: dict | None, base_dir: str) -> str:
    """A category's folder: an absolute folder as is, otherwise a subfolder
    of base_dir (OTHER_FOLDER for files no category claims)."""
    folder = category["folder"] if category else OTHER_FOLDER
    return folder if os.path.isabs(folder) else os.path.join(base_dir, folder)


def apply_auto_folder_for_path(filename: str, base_dir: str, categories: list[dict]) -> str:
    """<category folder>/<filename> for the "Sort into category folders" setting.

    Lives here, not in manager.py, so the native host (which never loads
    Qt) shares the exact routing rules with the app.
    """
    return os.path.join(category_folder(category_for(filename, categories), base_dir), filename)


def prune_empty_auto_folder_dir(dest_path: str, download_dir: str, categories: list[dict]) -> None:
    """Remove the category subfolder (Video/Audio/.../Other) a just-deleted
    file lived in, if that was the last thing in it.

    apply_auto_folder_for_path creates these on demand but nothing ever
    cleaned one up, so a download folder accumulates empty Video/Audio/...
    directories forever as files move through it. Deliberately narrow: only
    touches a directory named like one of the categories' subfolders (never
    an absolute category folder the user picked) whose *parent* is exactly
    download_dir, and os.rmdir refuses to remove a non-empty directory -- so
    this can never reach outside that known structure or delete anything
    that still has content.
    """
    parent = os.path.dirname(dest_path)
    names = {c["folder"] for c in categories if not os.path.isabs(c["folder"])} | {OTHER_FOLDER}
    if os.path.basename(parent) not in names:
        return
    if os.path.dirname(parent) != os.path.normpath(download_dir):
        return
    try:
        os.rmdir(parent)
    except OSError:
        pass  # not empty, or already gone -- either way, nothing to do
