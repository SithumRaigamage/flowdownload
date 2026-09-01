from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Cookie
from fastapi.responses import HTMLResponse, FileResponse
import mimetypes

from app.api.events import EventBroadcaster
from app.api.main_thread import MainThreadExecutor
from app.api.schemas import (
    ChromeStatus, DedupeCheckRequest, DedupeCheckResponse, DiskInfo, DownloadOut,
    HistoryEntry, NewDownloadRequest, PickFolderResponse, ProbeResponse,
    SettingsOut, SettingsPatch,
)
from app.engine.manager import DownloadManager
from app.engine.models import DownloadStatus
from app.engine.probe import guess_filename_from_url, probe_remote_filename
from app.engine.settings import Settings

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "web"
NATIVE_HOST_MANIFEST = Path.home() / (
    "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.flowdownload.host.json"
)


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


def create_app(
    manager: DownloadManager,
    executor: MainThreadExecutor,
    broadcaster: EventBroadcaster,
    settings_path: str,
    token: str,
    app_data_dir: str,
) -> FastAPI:
    app = FastAPI(title="FlowDownload API")

    @app.get("/", response_class=HTMLResponse)
    def index():
        # The token is embedded directly in the page rather than fetched
        # separately -- there's no bootstrap credential to fetch it *with*,
        # and this endpoint is 127.0.0.1-only, loaded solely by the app's own
        # QWebEngineView.
        html = (FRONTEND_DIR / "index.html").read_text()
        response = HTMLResponse(html.replace("__FLOWDOWNLOAD_TOKEN__", token))
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
            task_id = manager.add_download(body.url, body.dest_path)
            return DownloadOut.from_task(manager.tasks[task_id])

        return executor.call(_create)

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
        return FileResponse(
            dest_path,
            media_type=media_type or "application/octet-stream",
            filename=os.path.basename(dest_path),
        )

    @app.get("/api/downloads/{task_id}/log", response_model=list[HistoryEntry])
    def download_log(task_id: str, _=Depends(require_token)):
        return executor.call(lambda: manager.get_history(task_id))

    @app.post("/api/downloads/clear-completed")
    def clear_completed(_=Depends(require_token)):
        def _clear():
            ids = [t.id for t in manager.tasks.values() if t.status == DownloadStatus.COMPLETED]
            for task_id in ids:
                manager.remove(task_id)
            return len(ids)

        return {"removed": executor.call(_clear)}

    @app.post("/api/downloads/resume-all")
    def resume_all_downloads(_=Depends(require_token)):
        return executor.call(lambda: (manager.resume_all(), {"ok": True})[1])

    @app.delete("/api/downloads/{task_id}")
    def delete_download(task_id: str, delete_file: bool = False, _=Depends(require_token)):
        def _remove():
            task = manager.tasks.get(task_id)
            dest_path = task.dest_path if task is not None else None
            manager.remove(task_id)
            return dest_path

        dest_path = executor.call(_remove)
        if delete_file and dest_path and os.path.exists(dest_path):
            os.remove(dest_path)
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
