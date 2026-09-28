from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Cookie, Request
from fastapi.responses import HTMLResponse, FileResponse
import mimetypes

from app.api.events import EventBroadcaster
from app.api.main_thread import MainThreadExecutor
from app.api.schemas import (
    ChromeStatus, DedupeCheckRequest, DedupeCheckResponse, DiskInfo, DownloadOut,
    HistoryEntry, NewDownloadRequest, PickFolderResponse, ProbeResponse, RefreshUrlRequest,
    SettingsOut, SettingsPatch,
)
from app.engine.manager import DownloadManager, prune_empty_auto_folder_dir
from app.engine.models import DownloadStatus
from app.engine.probe import guess_filename_from_url, probe_remote_filename
from app.engine.settings import Settings
from app.version import APP_VERSION

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "web"
NATIVE_HOST_MANIFEST = Path.home() / (
    "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.flowdownload.host.json"
)


def _log(msg: str) -> None:
    """Unbuffered stderr logger (uvicorn captures stdout by default)."""
    import sys
    print(msg, file=sys.stderr, flush=True)


def _sniff_media_type(path: str) -> str | None:
    """Return the right Content-Type for a downloaded file.

    Some hosting sites serve real MP4 files with a .vid extension (or
    other misnamings), which would defeat mimetypes.guess_type's purely
    extension-based lookup and have the browser refuse to play the file.
    Sniff the first few bytes for known media magic numbers and return
    a definitive MIME type when we recognize them; otherwise fall back
    to the extension-based guess.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return mimetypes.guess_type(path)[0]

    # ISO Base Media (MP4, MOV, M4V, 3GP, etc.): 4-byte size + "ftyp".
    if len(head) >= 12 and head[4:8] == b"ftyp":
        # The major_brand at offset 8 disambiguates MP4 vs. MOV vs. M4V.
        brand = head[8:12]
        if brand in (b"qt  ", b"QTIM"):  # QuickTime Movie
            return "video/quicktime"
        if brand in (b"M4V ", b"mp4v", b"M4VP"):
            return "video/x-m4v"
        return "video/mp4"
    # EBML header (Matroska / WebM).
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        # The DocType at offset 0-4 of the EBML body is "matroska" or "webm".
        try:
            with open(path, "rb") as f:
                f.seek(0)
                ebml = f.read(64)
            # The DocType string is at a known offset; quick check: look
            # for "webm" or "matroska" within the first 64 bytes.
            if b"webm" in ebml:
                return "video/webm"
            if b"matroska" in ebml:
                return "video/x-matroska"
        except OSError:
            pass
    # RIFF ... AVI.
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video/x-msvideo"
    # MPEG-TS sync byte 0x47 every 188 bytes (best-effort: just check
    # the first byte; this can be a false positive but those are rare).
    if head and head[0] == 0x47:
        return "video/mp2t"
    # MPEG-1/2 Program Stream (older .mpg/.mpeg).
    if head.startswith(b"\x00\x00\x01\xba"):
        return "video/mpeg"

    return mimetypes.guess_type(path)[0]


def _needs_transcode(path: str) -> bool:
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
            [_find_binary("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return True  # can't probe — assume worst case
        import json as _json
        info = _json.loads(out.stdout or "{}")
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


def _find_binary(name: str) -> str:
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


def _transcode_to_webm(src_path: str, dest_path: str, duration_s: float) -> bool:
    """Transcode src_path (H.264/AAC) → dest_path (VP9/Opus WebM) using FFmpeg.

    Returns True on success, False on failure. Runs synchronously — the
    caller is expected to invoke this on a background thread so HTTP
    requests stay responsive while FFmpeg does its work.

    Why we transcode to a file (not stream stdout):
      WebM is a seekable container. The `<video>` element reads the
      total duration from the WebM header at the start of the file; an
      on-the-fly stdout stream has no fixed duration, so the browser
      shows 0:00 / 0:00 and the seek bar can't render properly. By
      writing to a file first, the duration is known and the browser
      gets full seek/Range support via FastAPI's FileResponse.
    """
    cmd = [
        _find_binary("ffmpeg"),
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
        dest_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=int(duration_s) + 60)
        if result.returncode != 0:
            _log(f"[transcode] ffmpeg failed: {result.stderr.decode(errors='replace')[-500:]}")
            return False
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    except subprocess.TimeoutExpired:
        _log(f"[transcode] ffmpeg timed out for {os.path.basename(src_path)}")
        return False
    except Exception as exc:
        _log(f"[transcode] error: {exc}")
        return False


def _probe_duration(path: str) -> float:
    """Return the duration of a media file in seconds, or 0 on error."""
    try:
        out = subprocess.run(
            [_find_binary("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return 0.0
        import json as _json
        info = _json.loads(out.stdout or "{}")
        return float(info.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def _transcode_cache_key(src_path: str) -> str:
    """Build a cache key from the source path and mtime so a re-encoded
    file is regenerated when the source changes."""
    st = os.stat(src_path)
    return f"{os.path.basename(src_path)}.{int(st.st_mtime)}.{st.st_size}.webm"


def _remove_transcode_cache(src_path: str, cache_dir: str) -> None:
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
        key = _transcode_cache_key(src_path)
    except OSError:
        return  # source already gone -- nothing to key the cache entry by
    cached = os.path.join(cache_dir, key)
    try:
        if os.path.exists(cached):
            os.remove(cached)
    except OSError:
        pass


def _get_or_build_transcoded(src_path: str, cache_dir: str,
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
    import threading
    key = _transcode_cache_key(src_path)
    dest = os.path.join(cache_dir, key)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest  # cache hit, instant return

    os.makedirs(cache_dir, exist_ok=True)
    # Module-level state: lock + "already running" flag, keyed by source key.
    state = _get_or_build_transcoded.__dict__.setdefault("_state", {})
    entry = state.setdefault(key, {
        "lock": threading.Lock(),
        "thread": None,
        "started": False,
    })

    # If no thread is running for this key, start one.
    if not entry["started"]:
        with entry["lock"]:
            if not entry["started"]:
                duration = _probe_duration(src_path)
                _log(f"[transcode] starting: {os.path.basename(src_path)} → {key} ({duration:.1f}s)")
                def _run():
                    try:
                        ok = _transcode_to_webm(src_path, dest, duration)
                        if ok:
                            _log(f"[transcode] done: {key} ({os.path.getsize(dest)} bytes)")
                        else:
                            _log(f"[transcode] FAILED: {key}")
                            try: os.remove(dest)
                            except OSError: pass
                    finally:
                        entry["started"] = False
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
        time.sleep(0.5)
    _log(f"[transcode] wait timeout for {key}")
    return None


def create_app(
    manager: DownloadManager,
    executor: MainThreadExecutor,
    broadcaster: EventBroadcaster,
    settings_path: str,
    token: str,
    app_data_dir: str,
    get_native_player_opener: "Callable[[], Optional[Callable[[str], None]]] | None" = None,
) -> FastAPI:
    app = FastAPI(title="FlowDownload API")

    @app.middleware("http")
    async def _reject_dns_rebinding(request: Request, call_next):
        # uvicorn binds 127.0.0.1-only, but that alone doesn't stop DNS
        # rebinding: a page on attacker-controlled-domain.com can resolve
        # that hostname to 127.0.0.1 and fetch same-origin, landing here
        # with a Host header of "attacker-controlled-domain.com:8756"
        # instead of "127.0.0.1:8756". Validating the Host header closes
        # that gap -- a real browser navigating to this app always sends
        # the host it actually connected to.
        host = (request.headers.get("host") or "").split(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            # Returned directly (not raised) -- an HTTPException raised from
            # inside @app.middleware("http") isn't routed through FastAPI's
            # exception handlers and would otherwise surface as an opaque
            # 500 instead of this explicit rejection.
            return HTMLResponse("Invalid Host header", status_code=421)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        # The token is embedded directly in the page rather than fetched
        # separately -- there's no bootstrap credential to fetch it *with*,
        # and this endpoint is 127.0.0.1-only, loaded solely by the app's own
        # QWebEngineView.
        html = (FRONTEND_DIR / "index.html").read_text()
        html = html.replace("__FLOWDOWNLOAD_TOKEN__", token).replace("__FLOWDOWNLOAD_VERSION__", APP_VERSION)
        response = HTMLResponse(html)
        # QWebEngineView keeps a persistent on-disk HTTP cache across app
        # launches (it's not a throwaway browser tab) -- without this, an
        # earlier revision of this page served during development could get
        # replayed from cache on a later launch instead of the current file.
        response.headers["Cache-Control"] = "no-store"
        # Set an HttpOnly cookie so the <video>/<audio> elements can stream
        # files without needing an Authorization header (they can't send one).
        response.set_cookie(
            key="flowdownload_token", value=token,
            httponly=True, samesite="strict", path="/",
        )
        return response

    def require_token(
        authorization: Optional[str] = Header(default=None),
        flowdownload_token: Optional[str] = Cookie(default=None),
    ) -> None:
        # 127.0.0.1-only, but any other local process (or a malicious page,
        # if this were ever opened outside the app's own webview) could
        # otherwise reach it -- the token is regenerated every launch and
        # only the app itself knows it. Either the Authorization header
        # (used by JS fetch() calls) or the HttpOnly cookie set on / (used
        # by the <video>/<audio> element when streaming a file) is accepted.
        expected = f"Bearer {token}"
        if authorization == expected or flowdownload_token == token:
            return
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    @app.get("/api/downloads", response_model=list[DownloadOut])
    def list_downloads(status: Optional[str] = None, _=Depends(require_token)):
        def _read():
            tasks = list(manager.tasks.values())
            if status:
                tasks = [t for t in tasks if t.status.value == status]
            return [DownloadOut.from_task(t) for t in tasks]

        return executor.call(_read)

    @app.post("/api/downloads", response_model=DownloadOut, status_code=201)
    def create_download(body: NewDownloadRequest, _=Depends(require_token)):
        def _create():
            task_id = manager.add_download(body.url, body.filename)
            return DownloadOut.from_task(manager.tasks[task_id])

        try:
            return executor.call(_create)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/probe", response_model=ProbeResponse)
    def probe(url: str, _=Depends(require_token)):
        # Pure I/O against the remote server, not the manager -- safe to run
        # directly on the request thread (FastAPI's threadpool) without
        # going through the main-thread executor.
        filename = probe_remote_filename(url) or guess_filename_from_url(url)
        return ProbeResponse(filename=filename)

    @app.post("/api/dedupe-check", response_model=DedupeCheckResponse)
    def dedupe_check(body: DedupeCheckRequest, _=Depends(require_token)):
        """Returns whether the given URL has already been completed.

        Used by the Chrome extension popup so it can surface an
        "Already downloaded" toast instead of silently queueing a
        duplicate.
        """
        def _check():
            result = manager.check_dedupe(body.url)
            return DedupeCheckResponse(
                is_duplicate=result.is_duplicate,
                existing_task_id=result.existing_task_id,
                existing_path=result.existing_path,
                existing_size=result.existing_size,
                existing_digest=result.existing_digest,
            )

        return executor.call(_check)

    @app.post("/api/downloads/{task_id}/pause", response_model=DownloadOut)
    def pause_download(task_id: str, _=Depends(require_token)):
        return executor.call(lambda: _act_and_return(manager.pause, task_id))

    @app.post("/api/downloads/{task_id}/resume", response_model=DownloadOut)
    def resume_download(task_id: str, _=Depends(require_token)):
        return executor.call(lambda: _act_and_return(manager.start, task_id))

    @app.post("/api/downloads/{task_id}/retry", response_model=DownloadOut)
    def retry_download(task_id: str, _=Depends(require_token)):
        return executor.call(lambda: _act_and_return(manager.retry, task_id))

    @app.post("/api/downloads/{task_id}/cancel", response_model=DownloadOut)
    def cancel_download(task_id: str, _=Depends(require_token)):
        return executor.call(lambda: _act_and_return(manager.cancel, task_id))

    @app.post("/api/downloads/{task_id}/refresh-url", response_model=DownloadOut)
    def refresh_download_url(task_id: str, body: RefreshUrlRequest, _=Depends(require_token)):
        try:
            return executor.call(lambda: _act_and_return(lambda tid: manager.refresh_url(tid, body.url), task_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def _act_and_return(fn, task_id: str) -> DownloadOut:
        if task_id not in manager.tasks:
            raise HTTPException(status_code=404, detail="No such download")
        fn(task_id)
        return DownloadOut.from_task(manager.tasks[task_id])

    @app.post("/api/downloads/{task_id}/reveal")
    def reveal_download(task_id: str, _=Depends(require_token)):
        def _get_path():
            task = manager.tasks.get(task_id)
            return task.dest_path if task is not None else None

        dest_path = executor.call(_get_path)
        if dest_path is None:
            raise HTTPException(status_code=404, detail="No such download")
        path = dest_path if os.path.exists(dest_path) else os.path.dirname(dest_path)
        subprocess.run(["open", "-R", path], check=False)
        return {"ok": True}

    @app.post("/api/downloads/{task_id}/open-in-player", response_model=dict)
    def open_in_player(task_id: str, _=Depends(require_token)):
        """Open a completed download in the native OS media player via the Qt UI."""
        def _open():
            opener = get_native_player_opener() if get_native_player_opener else None
            if opener is None:
                return None  # No player registered (dev mode without UI maybe)
            opener(task_id)
            return {"ok": True, "player": "native"}
        # The opener runs on the Qt main thread; block briefly for result.
        result = executor.call(_open)
        if result is None:
            raise HTTPException(status_code=503, detail="Native media player not available")
        return result

    @app.get("/api/downloads/{task_id}/stream")
    def stream_download(task_id: str, _=Depends(require_token)):
        """Stream a completed download file for in-app preview.

        FileResponse handles HTTP Range requests (seeking in video/audio) natively.
        The caller carries the flowdownload_token cookie so no Authorization header
        is needed (the <video>/<audio> element can't set one).
        """
        def _get():
            task = manager.tasks.get(task_id)
            if task is None or task.status != DownloadStatus.COMPLETED:
                return None
            if not os.path.exists(task.dest_path):
                return None
            return task.dest_path

        dest_path = executor.call(_get)
        if dest_path is None:
            raise HTTPException(status_code=404, detail="File not found or not ready")

        media_type = _sniff_media_type(dest_path)
        response = FileResponse(
            dest_path,
            media_type=media_type or "application/octet-stream",
            filename=os.path.basename(dest_path),
        )
        # FileResponse defaults to "attachment" which some browsers treat as a
        # download even when the URL is set on a <video> element. Force inline
        # so the in-app library player shows the media rather than triggering
        # a download.
        response.headers["Content-Disposition"] = (
            f'inline; filename="{os.path.basename(dest_path)}"'
        )
        return response

    @app.api_route("/api/downloads/{task_id}/stream-webm", methods=["GET", "HEAD"])
    def stream_transcode(task_id: str, request: Request, _=Depends(require_token)):
        """Stream a media file as WebM/VP9/Opus, transcoding on first request.

        Qt WebEngine's open-source Chromium FFmpeg build lacks proprietary
        codecs (H.264, AAC), so an in-page <video src="/stream"> would fail
        with DEMUXER_ERROR_NO_SUPPORTED_STREAMS. This endpoint transcodes
        to VP9/Opus (both supported by the open-source build) and serves
        the result as a file with HTTP Range support.

        Returns 200 with the WebM file on a cache hit. On a miss, kicks
        off the transcode in a background thread and returns 202 with
        JSON {"state": "transcoding", "status_url": "..."} so the JS
        can poll /transcode-status and show a progress bar while
        FFmpeg does its work. The JS retries this endpoint once the
        status shows "cached" -- that second request is instant.

        HEAD requests return the same status code (200 or 202) and
        headers but no body, so the JS can probe without committing
        to a full GET.

        Why not stream stdout: stdout streams have no fixed duration in
        the WebM header, so the browser shows 0:00 / 0:00 and the seek
        bar can't render. A complete file has a known duration, so the
        browser's native <video> controls (including the seek bar) work
        perfectly.
        """
        from fastapi.responses import JSONResponse
        is_head = request.method == "HEAD"

        # Read the file path on the manager thread (same mechanism as /stream)
        def _get():
            task = manager.tasks.get(task_id)
            if task is None or task.status != DownloadStatus.COMPLETED:
                return None
            if not os.path.exists(task.dest_path):
                return None
            return task.dest_path
        dest_path = executor.call(_get)
        if dest_path is None:
            raise HTTPException(status_code=404, detail="File not found or not ready")

        # If the file is already in a WebEngine-compatible codec, just
        # stream the original -- no CPU cost, full Range support.
        if not _needs_transcode(dest_path):
            response = FileResponse(
                dest_path, media_type="video/webm",
                filename=os.path.basename(dest_path),
            )
            response.headers["Content-Disposition"] = (
                f'inline; filename="{os.path.basename(dest_path)}"'
            )
            response.headers["Cache-Control"] = "no-store"
            if is_head:
                # Strip the body for HEAD; Starlette does this for FileResponse
                # automatically, but be explicit so future readers understand.
                response.body = b""
            return response

        # Needs transcoding. Check the cache.
        cache_dir = os.path.join(app_data_dir, "transcoded")
        os.makedirs(cache_dir, exist_ok=True)
        key = _transcode_cache_key(dest_path)
        final = os.path.join(cache_dir, key)
        if os.path.exists(final) and os.path.getsize(final) > 0:
            response = FileResponse(
                final, media_type="video/webm",
                filename=os.path.basename(final),
            )
            response.headers["Content-Disposition"] = (
                f'inline; filename="{os.path.basename(dest_path)}"'
            )
            response.headers["Cache-Control"] = "no-store"
            if is_head:
                response.body = b""
            return response

        # Cache miss. Start the transcode in the background (idempotent
        # across concurrent requests) and return 202 so the JS knows to
        # poll. The 202 response carries the transcode status URL so the
        # JS doesn't have to construct it.
        _get_or_build_transcoded(dest_path, cache_dir, max_wait_s=0.1)
        body = {
            "state": "transcoding",
            "status_url": f"/api/downloads/{task_id}/transcode-status",
            "stream_url": f"/api/downloads/{task_id}/stream-webm",
        }
        if is_head:
            # HEAD: return the same headers but no body. JSONResponse
            # renders an empty body when content isn't included.
            return JSONResponse(
                status_code=202, content=None,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            status_code=202,
            content=body,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/downloads/{task_id}/log", response_model=list[HistoryEntry])
    def download_log(task_id: str, _=Depends(require_token)):
        return executor.call(lambda: manager.get_history(task_id))

    @app.get("/api/downloads/{task_id}/transcode-status")
    def transcode_status(task_id: str, _=Depends(require_token)):
        """Return the transcode cache state for a task.

        States: 'cached' (WebM is on disk, instant play), 'pending'
        (transcode queued / FFmpeg just started, no bytes yet),
        'transcoding' (FFmpeg is running, with progress percent),
        'failed' (last attempt errored). The JS polls this while
        waiting for the first-play transcode to complete so it can
        show a progress indicator.

        Calling this endpoint ALSO starts the transcode if it isn't
        already running. This means the JS doesn't have to call
        /stream-webm just to kick FFmpeg off; it can poll status
        first and switch over to <video src=...> when state="cached".
        """
        def _get():
            task = manager.tasks.get(task_id)
            if task is None or task.status != DownloadStatus.COMPLETED:
                return None, None
            return task.dest_path, _probe_duration(task.dest_path)
        result = executor.call(_get)
        if result == (None, None):
            raise HTTPException(status_code=404, detail="No such download")
        dest_path, duration = result
        if not _needs_transcode(dest_path):
            return {"state": "cached", "needs_transcode": False}
        cache_dir = os.path.join(app_data_dir, "transcoded")
        os.makedirs(cache_dir, exist_ok=True)
        key = _transcode_cache_key(dest_path)
        final = os.path.join(cache_dir, key)
        if os.path.exists(final) and os.path.getsize(final) > 0:
            return {"state": "cached", "needs_transcode": True}
        # Make sure the transcode is running. _get_or_build_transcoded
        # is idempotent: a second call while one is in progress just
        # returns the in-flight thread. Pass max_wait_s=0 to return
        # immediately so this endpoint stays cheap to poll.
        _get_or_build_transcoded(dest_path, cache_dir, max_wait_s=0)
        if os.path.exists(final) and duration > 0:
            final_size = os.path.getsize(final)
            # VP9 at CRF 32 produces roughly 600-800 kbps for typical
            # 480p content; we use 700 kbps as the mid estimate. The
            # output size depends on content complexity, so the
            # percentage is approximate -- but it's monotonic and
            # gives the user meaningful feedback during the wait.
            estimated_total = int(duration * 700_000 / 8)
            if final_size >= estimated_total:
                # FFmpeg finished; the file is ready.
                return {"state": "cached", "needs_transcode": True}
            # Don't return 0% when FFmpeg is clearly working -- show at
            # least 1% so the progress bar is visibly animating.
            pct = min(99, max(1, int(final_size * 100 / max(estimated_total, 1))))
            return {
                "state": "transcoding",
                "needs_transcode": True,
                "partial_bytes": final_size,
                "estimated_total_bytes": estimated_total,
                "percent": pct,
                "duration_seconds": duration,
            }
        return {"state": "pending", "needs_transcode": True}

    @app.post("/api/downloads/clear-completed")
    def clear_completed(_=Depends(require_token)):
        cache_dir = os.path.join(app_data_dir, "transcoded")

        def _clear():
            ids = [t.id for t in manager.tasks.values() if t.status == DownloadStatus.COMPLETED]
            for task_id in ids:
                task = manager.tasks.get(task_id)
                if task is not None:
                    _remove_transcode_cache(task.dest_path, cache_dir)
                manager.remove(task_id)
            return len(ids)

        return {"removed": executor.call(_clear)}

    @app.post("/api/downloads/resume-all")
    def resume_all_downloads(_=Depends(require_token)):
        return executor.call(lambda: (manager.resume_all(), {"ok": True})[1])

    @app.delete("/api/downloads/{task_id}")
    def delete_download(task_id: str, delete_file: bool = False, _=Depends(require_token)):
        cache_dir = os.path.join(app_data_dir, "transcoded")

        def _remove():
            task = manager.tasks.get(task_id)
            dest_path = task.dest_path if task is not None else None
            if dest_path:
                _remove_transcode_cache(dest_path, cache_dir)
            manager.remove(task_id)
            return dest_path

        dest_path = executor.call(_remove)
        if delete_file and dest_path and os.path.exists(dest_path):
            os.remove(dest_path)
            prune_empty_auto_folder_dir(dest_path, manager.settings.download_dir)
        return {"ok": True}

    @app.get("/api/settings", response_model=SettingsOut)
    def get_settings(_=Depends(require_token)):
        return executor.call(lambda: SettingsOut.from_settings(manager.settings))

    @app.patch("/api/settings", response_model=SettingsOut)
    def patch_settings(body: SettingsPatch, _=Depends(require_token)):
        def _apply():
            patch = {k: v for k, v in body.model_dump().items() if v is not None}
            new_settings = dataclasses.replace(manager.settings, **patch)
            new_settings.save(settings_path)
            manager.apply_settings(new_settings)
            return SettingsOut.from_settings(new_settings)

        return executor.call(_apply)

    @app.post("/api/settings/reset", response_model=SettingsOut)
    def reset_settings(_=Depends(require_token)):
        def _reset():
            fresh = Settings()
            fresh.save(settings_path)
            manager.apply_settings(fresh)
            return SettingsOut.from_settings(fresh)

        return executor.call(_reset)

    @app.get("/api/disk", response_model=DiskInfo)
    def disk_info(_=Depends(require_token)):
        def _read():
            path = manager.settings.download_dir
            usage = shutil.disk_usage(path if os.path.isdir(path) else os.path.expanduser("~"))
            return DiskInfo(path=path, free_bytes=usage.free, total_bytes=usage.total)

        return executor.call(_read)

    @app.get("/api/chrome-status", response_model=ChromeStatus)
    def chrome_status(_=Depends(require_token)):
        # host_registered stays as the "is the wiring set up" signal (the
        # manifest file exists). The per-profile fields are the new "is the
        # extension actually alive, and which profile is it in" signals --
        # the native host writes a heartbeat every 20s with the active
        # profile identity, so a stale last_ping_time means the extension
        # was disabled or removed even though the manifest is still on disk.
        state = {
            "host_registered": NATIVE_HOST_MANIFEST.exists(),
            "last_ping_time": None,
            "last_profile_name": None,
            "all_known_profiles": {},
        }
        extension_state_file = os.path.join(app_data_dir, "chrome_extension_state.json")
        try:
            with open(extension_state_file) as f:
                raw = json.load(f)
                state["last_ping_time"] = raw.get("last_ping")
                state["last_profile_name"] = raw.get("last_profile_name")
                state["all_known_profiles"] = raw.get("profiles", {})
        except (OSError, json.JSONDecodeError):
            pass
        return ChromeStatus(**state)

    @app.post("/api/pick-folder", response_model=PickFolderResponse)
    def pick_folder(_=Depends(require_token)):
        # Qt's own QFileDialog would need to run on the main thread and
        # block it for as long as the user takes to choose -- osascript's
        # dialog runs out-of-process instead, so it doesn't stall the
        # DownloadManager or its workers while it's open.
        result = subprocess.run(
            ["osascript", "-e", "POSIX path of (choose folder)"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return PickFolderResponse(path=None)  # user cancelled
        return PickFolderResponse(path=result.stdout.strip())

    @app.websocket("/api/events")
    async def events(websocket: WebSocket):
        if websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = broadcaster.subscribe()
        try:
            while True:
                message = await queue.get()
                await websocket.send_json(message)
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.unsubscribe(queue)

    return app
