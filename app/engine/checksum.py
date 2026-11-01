"""Checking a finished file against a checksum: one the user typed in, or one
published next to the download (`file.zip.sha256`, `SHA256SUMS`).

Pure helpers plus one small fetch; DownloadWorker decides which to use.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

# Hex digest length -> algorithm. Checksums are pasted from release pages
# without a label far more often than with one, so the length decides.
_ALGO_BY_LENGTH = {32: "md5", 40: "sha1", 64: "sha256"}
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_LABEL_RE = re.compile(r"^(md5|sha1|sha-1|sha256|sha-256)\s*[:=]?\s*", re.IGNORECASE)
# Checksum files are a few lines; anything bigger isn't one (or is a page).
SIDECAR_MAX_BYTES = 64 * 1024
SIDECAR_TIMEOUT = 5
SUMS_FILENAME = "SHA256SUMS"


def parse_expected(text: str) -> str:
    """Normalise a typed checksum to "algo:hex", or "" for an empty field.

    Accepts a bare hex digest, an "sha256:"-style label, or a whole line
    copied from a checksum file ("<hex>  file.zip"). Raises ValueError for
    anything else, with a message the Add dialog can show as is.
    """
    text = (text or "").strip()
    if not text:
        return ""
    label = _LABEL_RE.match(text)
    named = None
    if label:
        named = label.group(1).lower().replace("-", "")
        text = text[label.end():]
    digest = text.split()[0].lower() if text.split() else ""
    algo = _ALGO_BY_LENGTH.get(len(digest))
    if not _HEX_RE.match(digest) or algo is None:
        raise ValueError("Checksum must be an MD5, SHA-1 or SHA-256 hex value (32, 40 or 64 characters)")
    if named and named != algo:
        raise ValueError(f"That's {len(digest)} characters, which isn't a {named.upper()} checksum")
    return f"{algo}:{digest}"


def split_expected(expected: str) -> tuple[str, str]:
    algo, _, digest = expected.partition(":")
    return algo, digest


def hash_file(path: str, algorithms: Iterable[str], chunk_size: int = 1024 * 1024) -> dict[str, str]:
    """Hex digests of `path` for each algorithm, in a single read."""
    hashers = {algo: hashlib.new(algo) for algo in set(algorithms)}
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            for h in hashers.values():
                h.update(chunk)
    return {algo: h.hexdigest() for algo, h in hashers.items()}


def matches(expected_hex: str, computed_hex: str) -> bool:
    return hmac.compare_digest(expected_hex.lower(), computed_hex.lower())


def find_in_sums(text: str, filename: str) -> str:
    """The SHA-256 listed for `filename` in sha256sum-style output, or "".

    Lines look like "<hex>  name" or "<hex> *name" (binary mode). A file
    with a single bare digest (the usual `file.zip.sha256`) matches any name.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    for line in lines:
        parts = line.split(None, 1)
        digest = parts[0].lower()
        if len(digest) != 64 or not _HEX_RE.match(digest):
            continue
        if len(parts) == 1:
            if len(lines) == 1:
                return digest
            continue
        name = parts[1].lstrip("*").strip()
        if name == filename or name.rsplit("/", 1)[-1] == filename:
            return digest
    return ""


def sidecar_urls(url: str) -> list[str]:
    """Where a published SHA-256 for `url` would be: `<file>.sha256`, then
    `SHA256SUMS` in the same folder.

    Empty for URLs with a query string: those are usually signed or
    generated links, where a guessed sibling URL only earns a 403 and adds
    requests the server never expected.
    """
    parts = urlsplit(url)
    if parts.query or not parts.path or parts.path.endswith("/"):
        return []
    folder = parts.path.rsplit("/", 1)[0] + "/"
    return [
        urlunsplit((parts.scheme, parts.netloc, parts.path + ".sha256", "", "")),
        urlunsplit((parts.scheme, parts.netloc, folder + SUMS_FILENAME, "", "")),
    ]


def fetch_sidecar_sha256(session: requests.Session, url: str, filename: str,
                         headers: dict[str, str] | None = None, cookies=None) -> str:
    """The SHA-256 published next to `url`, or "" if there isn't one.

    Never raises for network trouble: a missing checksum file is the normal
    case, and it only means the download stays "not checked".
    """
    for candidate in sidecar_urls(url):
        try:
            with session.get(candidate, headers=headers or {}, cookies=cookies, timeout=SIDECAR_TIMEOUT,
                             stream=True, allow_redirects=True) as response:
                if response.status_code != 200:
                    continue
                body = response.raw.read(SIDECAR_MAX_BYTES + 1, decode_content=True)
        except (requests.RequestException, OSError):
            continue
        if len(body) > SIDECAR_MAX_BYTES:
            continue
        digest = find_in_sums(body.decode("utf-8", errors="replace"), filename)
        if digest:
            return digest
    return ""
