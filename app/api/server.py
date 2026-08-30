from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.api.events import EventBroadcaster
from app.api.main_thread import MainThreadExecutor
from app.api.schemas import (
    ChromeStatus, DiskInfo, DownloadOut, HistoryEntry, NewDownloadRequest, PickFolderResponse,
    ProbeResponse, SettingsOut, SettingsPatch,
)
from app.engine.manager import DownloadManager
from app.engine.models import DownloadStatus
from app.engine.probe import guess_filename_from_url, probe_remote_filename
from app.engine.settings import Settings

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "web"
NATIVE_HOST_MANIFEST = Path.home() / (
    "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.flowdownload.host.json"
)


def create_app(
    manager: DownloadManager,
    executor: MainThreadExecutor,
    broadcaster: EventBroadcaster,
    settings_path: str,
    token: str,
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
        return response

    def require_token(authorization: Optional[str] = Header(default=None)) -> None:
        # 127.0.0.1-only, but any other local process (or a malicious page,
        # if this were ever opened outside the app's own webview) could
        # otherwise reach it -- the token is regenerated every launch and
        # only the app itself knows it.
        expected = f"Bearer {token}"
        if authorization != expected:
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
        return ChromeStatus(host_registered=NATIVE_HOST_MANIFEST.exists())

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
