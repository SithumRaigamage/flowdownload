"""The web page and settings payload the UI depends on."""
from __future__ import annotations

from app.api.schemas import SettingsOut
from app.api.server import create_app
from app.engine.settings import Settings
from app.version import APP_VERSION


def _index(query_token=None, cookie=None):
    app = create_app(None, None, None, "/unused/settings.json", "test-token", "/unused")
    index = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/")
    return index(query_token=query_token, flowdownload_token=cookie)


def _index_html() -> str:
    return _index(query_token="test-token").body.decode()


def test_index_injects_version_and_token():
    html = _index_html()
    assert f'const APP_VERSION = "{APP_VERSION}"' in html
    assert 'const TOKEN = "test-token"' in html
    assert "__FLOWDOWNLOAD_" not in html


def test_index_withholds_the_token_without_it():
    # Any local process can reach 127.0.0.1; the page must not be how
    # it learns the token.
    for response in (_index(), _index(query_token="wrong"), _index(cookie="wrong")):
        assert response.status_code == 401
        assert b"test-token" not in response.body
    assert _index(cookie="test-token").status_code == 200  # reload of the app's own page


def test_version_matches_version_file():
    with open("VERSION") as f:
        assert APP_VERSION == f.read().strip()


def test_settings_out_exposes_per_site_limit():
    assert SettingsOut.from_settings(Settings()).site_connection_limit == 0
    assert SettingsOut.from_settings(Settings(site_connection_limit=25)).site_connection_limit == 25


def test_old_saved_per_site_limit_is_dropped(tmp_path):
    # 1.1.8/1.1.9 saved max_connections_per_site=10 by default; it must not
    # keep capping downloads after the setting became opt-in.
    path = tmp_path / "settings.json"
    path.write_text('{"max_concurrent_downloads": 30, "max_connections_per_site": 10}')
    settings = Settings.load(str(path))
    assert settings.max_concurrent_downloads == 30
    assert settings.site_connection_limit == 0


def test_stream_handles_non_ascii_filenames(tmp_path):
    # Starlette encodes headers as latin-1, so a hand-built
    # Content-Disposition crashed (500) on names like this one.
    from types import SimpleNamespace

    from app.engine.models import DownloadStatus, DownloadTask

    path = tmp_path / 'тест "1".mp4'
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    task = DownloadTask(url="http://x/", dest_path=str(path), status=DownloadStatus.COMPLETED)
    manager = SimpleNamespace(tasks={task.id: task})
    executor = SimpleNamespace(call=lambda fn: fn())
    app = create_app(manager, executor, None, "/unused/settings.json", "t", str(tmp_path))
    stream = next(r.endpoint for r in app.routes if getattr(r, "path", "").endswith("/stream"))
    disposition = stream(task.id).headers["content-disposition"]
    assert disposition.startswith("inline;")
    assert "filename*=utf-8''" in disposition


def test_settings_patch_rejects_unusable_values(tmp_path):
    import pytest
    from pydantic import ValidationError

    from app.api.schemas import SettingsPatch

    assert SettingsPatch(download_dir=str(tmp_path), max_concurrent_downloads=50).download_dir == str(tmp_path)
    for bad in ({"download_dir": "relative/dir"}, {"download_dir": str(tmp_path / "missing")},
                {"max_concurrent_downloads": 0}, {"max_concurrent_downloads": 51}):
        with pytest.raises(ValidationError):
            SettingsPatch(**bad)


def _media_app(tmp_path, name="clip.mp4"):
    from types import SimpleNamespace

    from app.engine.models import DownloadStatus, DownloadTask

    path = tmp_path / name
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    task = DownloadTask(url="http://x/", dest_path=str(path), status=DownloadStatus.COMPLETED)
    manager = SimpleNamespace(tasks={task.id: task})
    executor = SimpleNamespace(call=lambda fn: fn())
    app = create_app(manager, executor, None, "/unused/settings.json", "t", str(tmp_path / "appdata"))
    status = next(r.endpoint for r in app.routes if getattr(r, "path", "").endswith("/transcode-status"))
    return task, lambda: status(task.id)


def test_transcode_without_ffmpeg_reports_failure(tmp_path, monkeypatch):
    # It used to say "pending" forever while re-running ffmpeg every poll.
    import time

    from app.media import transcode

    monkeypatch.setattr(transcode, "find_binary", lambda name: "/nonexistent/" + name)
    task, status = _media_app(tmp_path, "nofmpeg.mp4")
    deadline = time.time() + 10
    while (result := status())["state"] != "failed" and time.time() < deadline:
        time.sleep(0.05)
    assert result == {"state": "failed", "needs_transcode": True, "error": transcode.FFMPEG_MISSING}


def test_half_written_transcode_is_not_served_as_cached(tmp_path, monkeypatch):
    from app.media import transcode

    task, status = _media_app(tmp_path, "slow.mp4")
    monkeypatch.setattr(transcode, "probe_media", lambda path: (True, 60.0))
    monkeypatch.setattr(transcode, "get_or_build", lambda *a, **k: None)  # "ffmpeg is running"
    tmp = tmp_path / "appdata" / "transcoded" / (transcode.cache_key(task.dest_path) + ".tmp")
    tmp.parent.mkdir(parents=True)
    tmp.write_bytes(b"x" * 10_000_000)  # bigger than the size estimate
    assert status()["state"] == "transcoding"


def test_settings_load_ignores_wrongly_typed_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"max_concurrent_downloads": "3", "minimize_to_tray": true, "connections_per_download": true}')
    settings = Settings.load(str(path))
    assert settings.max_concurrent_downloads == 3  # the default, not the string
    assert settings.minimize_to_tray is True
    assert settings.connections_per_download == 4


def test_settings_save_leaves_no_temp_file(tmp_path):
    path = tmp_path / "settings.json"
    Settings(max_concurrent_downloads=7).save(str(path))
    assert Settings.load(str(path)).max_concurrent_downloads == 7
    assert not (tmp_path / "settings.json.tmp").exists()



def test_cookie_only_authenticates_media_streams(make_manager, tmp_path, wait):
    # Browsers attach the cookie on their own, and another 127.0.0.1 port
    # is same-site: with only the cookie, actions must be refused.
    import threading

    import requests

    from app.api.runner import ApiServer

    server = ApiServer(make_manager(), str(tmp_path / "settings.json"), app_data_dir=str(tmp_path / "appdata"))
    server.start()
    base = f"http://127.0.0.1:{server.port}"
    cookie = {"flowdownload_token": server.token}
    bearer = {"Authorization": f"Bearer {server.token}"}
    statuses = {}

    def client():  # off the main thread: the API calls back into it
        statuses["cookie_post"] = requests.post(f"{base}/api/downloads/clear-completed", cookies=cookie).status_code
        statuses["bearer_post"] = requests.post(f"{base}/api/downloads/clear-completed", headers=bearer).status_code
        # 404 = no such download, i.e. the cookie got past auth.
        statuses["cookie_stream"] = requests.get(f"{base}/api/downloads/nope/stream", cookies=cookie).status_code
        statuses["bare_stream"] = requests.get(f"{base}/api/downloads/nope/stream").status_code
        statuses["bare_page"] = requests.get(f"{base}/").status_code

    thread = threading.Thread(target=client)
    try:
        thread.start()
        assert wait(lambda: not thread.is_alive(), timeout=30)  # pumps Qt events meanwhile
    finally:
        server.stop()
    assert statuses == {"cookie_post": 401, "bearer_post": 200, "cookie_stream": 404,
                        "bare_stream": 401, "bare_page": 401}


def test_api_call_times_out_when_main_thread_is_stuck(qapp):
    import threading

    from app.api.main_thread import MainThreadBusy, MainThreadExecutor

    executor = MainThreadExecutor()
    outcome = []

    def api_thread():
        try:
            executor.call(lambda: 1, timeout=0.2)
        except MainThreadBusy:
            outcome.append("busy")

    # The main thread joins instead of pumping Qt events, so the call can
    # never run -- the API thread used to block forever.
    thread = threading.Thread(target=api_thread)
    thread.start()
    thread.join(5)
    assert outcome == ["busy"]
    qapp.processEvents()  # let the queued call run now, so nothing lingers


def test_transcode_cache_evicts_least_recently_used(tmp_path):
    import os

    from app.media import transcode

    for i, name in enumerate(["old.webm", "mid.webm", "new.webm"]):
        (tmp_path / name).write_bytes(b"x" * 100)
        os.utime(tmp_path / name, (1000 + i, 1000 + i))
    transcode.evict_cache(str(tmp_path), keep=str(tmp_path / "old.webm"), max_bytes=200)
    # "old" is the one just built, so it stays; the next least recent goes.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["new.webm", "old.webm"]


def test_slow_websocket_client_is_cut_off_not_buffered_forever():
    import asyncio

    from app.api import events

    loop = asyncio.new_event_loop()
    try:
        broadcaster = events.EventBroadcaster(loop)
        q = broadcaster.subscribe()
        for i in range(events.QUEUE_LIMIT + 50):
            broadcaster.publish({"n": i})
        loop.run_until_complete(asyncio.sleep(0))  # run the scheduled deliveries
        assert q.qsize() == events.QUEUE_LIMIT + 1
        items = [q.get_nowait() for _ in range(q.qsize())]
        assert items[-1] is events.OVERFLOW and items[-2] == {"n": events.QUEUE_LIMIT - 1}
    finally:
        loop.close()
