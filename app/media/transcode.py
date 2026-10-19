"""In-app preview transcoding: ffprobe/ffmpeg, and the WebM cache.

Qt WebEngine's open-source Chromium build can't decode proprietary codecs
(H.264, AAC, ...), so the preview player gets those files re-encoded to
VP9/Opus WebM. Everything here shells out and can take seconds -- never
call it on the Qt main thread.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


FFMPEG_MISSING = "Preview needs ffmpeg — install it with `brew install ffmpeg`, or use Show in Finder"

# ffprobe results per transcode cache key (source name+mtime+size): the UI
# polls once a second, and re-probing every time cost a subprocess each.
_PROBE_CACHE: dict[str, tuple[bool, float]] = {}
_PROBE_CACHE_MAX = 1000
# Transcodes are full re-encodes of the user's media; without a cap the
# cache grew by every file ever previewed. Least recently used goes first.
TRANSCODE_CACHE_MAX_BYTES = 5 * 1024 ** 3


def probe_media(path: str) -> tuple[bool, float]:
    """(needs_transcode, duration_seconds), memoized. Never call this on the
    Qt main thread -- ffprobe can take seconds."""
    key = cache_key(path)
    if key not in _PROBE_CACHE:
        if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
            _PROBE_CACHE.clear()  # cheap to rebuild; just never unbounded
        _PROBE_CACHE[key] = (needs_transcode(path), probe_duration(path))
    return _PROBE_CACHE[key]


def needs_transcode(path: str) -> bool:
    """Probe a file's codecs to decide if FFmpeg transcoding is required.

    Qt WebEngine ships with the open-source Chromium FFmpeg build, which
    deliberately omits proprietary codecs (H.264, AAC, MPEG-2, ...). For
    those files, an in-page <video> element fires DEMUXER_ERROR_NO_SUPPORTED_STREAMS
    and shows a black frame. We use FFprobe to inspect the streams and
    return True if any of them use a codec the open-source Chromium build
    cannot decode. WebM/VP9/VP8/Opus/Vorbis files are returned untouched
    (no CPU cost) so they play directly.
    """
    # A conservative whitelist of codecs supported by the open-source
    # Chromium FFmpeg that ships with Qt WebEngine. Anything not in this
    # set needs transcoding to VP9/Opus.
    SUPPORTED_VIDEO = {"vp8", "vp9", "av1"}
    SUPPORTED_AUDIO = {"opus", "vorbis", "pcm_*", "flac"}
    try:
        out = subprocess.run(
            [find_binary("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return True  # can't probe — assume worst case
        info = json.loads(out.stdout or "{}")
        for s in info.get("streams", []):
            codec = (s.get("codec_name") or "").lower()
            kind = s.get("codec_type")
            if kind == "video" and codec not in SUPPORTED_VIDEO:
                return True
            if kind == "audio" and not any(
                codec == p.rstrip("*") or codec.startswith(p.rstrip("*"))
                for p in SUPPORTED_AUDIO
            ):
                return True
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return True  # ffprobe missing or hung — transcode to be safe


def find_binary(name: str) -> str:
    """Return the path to an executable, searching well-known macOS Homebrew
    locations before falling back to a bare name (using PATH from the
    current environment).

    macOS GUI apps launched from Finder/Dock run under launchd with a
    minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that excludes Homebrew's
    /opt/homebrew/bin. This helper makes the dist .app bundle find
    ffmpeg/ffprobe regardless of how it was launched.
    """
    candidates = [
        f"/opt/homebrew/bin/{name}",      # Apple Silicon Homebrew
        f"/usr/local/bin/{name}",         # Intel Homebrew / older installs
        f"/opt/local/bin/{name}",         # MacPorts
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return name  # fallback: let subprocess find it via PATH


def transcode_to_webm(src_path: str, dest_path: str, duration_s: float) -> str | None:
    """Transcode src_path (H.264/AAC) → dest_path (VP9/Opus WebM) using FFmpeg.

    Returns None on success, or a user-facing reason on failure. Runs
    synchronously — the caller is expected to invoke this on a background
    thread so HTTP requests stay responsive while FFmpeg does its work.

    FFmpeg writes to <dest>.tmp, renamed into place only once it succeeds:
    everything treats a file at dest_path as a finished transcode, so
    writing there directly served half-encoded files as complete -- and a
    quit mid-encode left a truncated one cached for good.

    Why we transcode to a file (not stream stdout):
      WebM is a seekable container. The `<video>` element reads the
      total duration from the WebM header at the start of the file; an
      on-the-fly stdout stream has no fixed duration, so the browser
      shows 0:00 / 0:00 and the seek bar can't render properly. By
      writing to a file first, the duration is known and the browser
      gets full seek/Range support via FastAPI's FileResponse.
    """
    cmd = [
        find_binary("ffmpeg"),
        "-hide_banner", "-loglevel", "error",
        "-y",  # overwrite existing file
        "-i", src_path,
        # Video: VP9 in WebM. `cpu-used 8` is realtime-fast; quality is
        # acceptable for in-app preview.
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
        "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
        # Audio: Opus at 96k is plenty for spoken-word video and light music.
        "-c:a", "libopus", "-b:a", "96k",
        # WebM container.
        "-f", "webm",
        dest_path + ".tmp",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=int(duration_s) + 60)
        if result.returncode != 0:
            _log(f"[transcode] ffmpeg failed: {result.stderr.decode(errors='replace')[-500:]}")
            return "This file couldn't be converted for preview"
        if not os.path.getsize(dest_path + ".tmp"):
            return "This file couldn't be converted for preview"
        os.replace(dest_path + ".tmp", dest_path)
        return None
    except FileNotFoundError:
        return FFMPEG_MISSING
    except subprocess.TimeoutExpired:
        _log(f"[transcode] ffmpeg timed out for {os.path.basename(src_path)}")
        return "Converting this file for preview took too long"
    except Exception as exc:
        _log(f"[transcode] error: {exc}")
        return "This file couldn't be converted for preview"
    finally:
        try:
            os.remove(dest_path + ".tmp")  # gone already after a successful replace
        except OSError:
            pass


def probe_duration(path: str) -> float:
    """Return the duration of a media file in seconds, or 0 on error."""
    try:
        out = subprocess.run(
            [find_binary("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return 0.0
        info = json.loads(out.stdout or "{}")
        return float(info.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def cache_key(src_path: str) -> str:
    """Build a cache key from the source path and mtime so a re-encoded
    file is regenerated when the source changes."""
    st = os.stat(src_path)
    return f"{os.path.basename(src_path)}.{int(st.st_mtime)}.{st.st_size}.webm"


def evict_cache(cache_dir: str, keep: str, max_bytes: int | None = None) -> None:
    """Delete least-recently-used transcodes until the cache fits max_bytes.
    `keep` (the one just built) is never evicted."""
    limit = TRANSCODE_CACHE_MAX_BYTES if max_bytes is None else max_bytes
    try:
        # mtime, refreshed on every cache hit (see stream_transcode): macOS
        # doesn't reliably update access times.
        entries = [(e.stat().st_mtime, e.stat().st_size, e.path)
                   for e in os.scandir(cache_dir) if e.name.endswith(".webm") and e.is_file()]
    except OSError:
        return
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):  # least recently used first
        if total <= limit:
            break
        if path == keep:
            continue
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


def remove_cached(src_path: str, cache_dir: str) -> None:
    """Delete the cached WebM transcode for a download that's being removed.

    Once the task is gone, nothing will ever look this cache entry up
    again (lookups only happen via an active task's preview action) --
    without this, every previewed video/audio file left an orphaned
    transcode behind forever, with no eviction anywhere in the app. Must
    run *before* the source file itself is deleted: the cache key is
    derived from the source's mtime+size, which requires stat'ing a file
    that still exists.
    """
    try:
        key = cache_key(src_path)
    except OSError:
        return  # source already gone -- nothing to key the cache entry by
    cached = os.path.join(cache_dir, key)
    try:
        if os.path.exists(cached):
            os.remove(cached)
    except OSError:
        pass


def get_or_build(src_path: str, cache_dir: str,
                              max_wait_s: float = 600.0) -> str | None:
    """Return the path to a transcoded WebM, building it if needed.

    Caches the result in cache_dir by source-path+mtime+size. Concurrent
    requests for the same source are coalesced via a per-key lock + a
    shared "is this source already being transcoded" flag, so we don't
    run FFmpeg twice for the same file.

    If the cache is empty, this function KICKS OFF the transcode in a
    background daemon thread and blocks the caller (up to max_wait_s)
    waiting for the result. Returning a path that doesn't exist yet
    is intentional: the HTTP handler can show a "transcoding" status
    to the JS, which will keep polling /transcode-status until the
    file appears, then load <video src=...> on the same URL.
    """
    key = cache_key(src_path)
    dest = os.path.join(cache_dir, key)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest  # cache hit, instant return

    os.makedirs(cache_dir, exist_ok=True)
    # Module-level state: lock + "already running" flag, keyed by source key.
    state = get_or_build.__dict__.setdefault("_state", {})
    entry = state.setdefault(key, {
        "lock": threading.Lock(),
        "thread": None,
        "started": False,
        # Why the last attempt failed. A failure is final for this source
        # (until it changes, which changes the key): retrying on every
        # status poll just re-ran a doomed ffmpeg once a second.
        "error": None,
    })

    # If no thread is running for this key, start one.
    if not entry["started"] and entry["error"] is None:
        with entry["lock"]:
            if not entry["started"] and entry["error"] is None:
                duration = probe_media(src_path)[1]
                _log(f"[transcode] starting: {os.path.basename(src_path)} → {key} ({duration:.1f}s)")
                def _run():
                    try:
                        entry["error"] = transcode_to_webm(src_path, dest, duration)
                        if entry["error"] is None:
                            _log(f"[transcode] done: {key} ({os.path.getsize(dest)} bytes)")
                            evict_cache(cache_dir, keep=dest)
                        else:
                            _log(f"[transcode] FAILED: {key}: {entry['error']}")
                    finally:
                        entry["started"] = False
                        if entry["error"] is None:
                            state.pop(key, None)  # the cached file speaks for itself now
                entry["started"] = True
                entry["thread"] = threading.Thread(target=_run, daemon=True)
                entry["thread"].start()

    # Block the caller (the HTTP handler) until the transcode is done.
    # We don't hold the lock here -- multiple HTTP requests for the
    # same source can all wait on the same background thread.
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        if entry["error"] is not None:
            return None
        time.sleep(0.5)
    return None


def transcode_error(src_path: str) -> str | None:
    """Why the last transcode of this source failed, or None."""
    state = get_or_build.__dict__.get("_state", {})
    entry = state.get(cache_key(src_path))
    return entry["error"] if entry else None
