"""Split-file downloads: several ranged connections filling one preallocated .part.

DownloadWorker.run() tries run_segmented() first. It only takes over when
splitting is both useful and safe:

  * the manager granted more than one connection (see Settings.site_connection_limit -- the
    per-site limit is a connection budget, not a file count),
  * the server answers a ranged probe with 206 + a Content-Range total of at
    least MIN_TOTAL, and
  * it sends an ETag or Last-Modified -- every segment request carries
    If-Range, so a file that changes underneath us comes back as 200 and is
    detected instead of silently mixing old and new bytes.

Otherwise it returns False and the worker's ordinary single-stream path runs.

Progress lives in a JSON sidecar next to the .part (<dest>.part.segments),
rewritten atomically about once a second and whenever the download stops. A
download that has a sidecar always resumes segmented; one that has a plain
.part and no sidecar always resumes single-stream -- the two never mix,
because a preallocated .part is already full-size and its length says
nothing about progress.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time

import requests

from app.engine.models import DownloadStatus

MIN_TOTAL = 8 * 1024 * 1024  # smaller files gain little from splitting
MIN_PART = 2 * 1024 * 1024   # never split into pieces smaller than this
CHUNK_SIZE = 64 * 1024
TICK = 0.25                  # progress emit interval, same as single-stream
SAVE_INTERVAL = 1.0
SPEED_SMOOTHING = 0.3


class _Changed(Exception):
    """The server answered an If-Range request with 200: the file changed."""


class _Segment:
    __slots__ = ("start", "end", "done", "waiting")

    def __init__(self, start: int, end: int, done: int = 0):
        self.start, self.end, self.done = start, end, done  # end is inclusive
        self.waiting = False  # sleeping before a retry

    @property
    def remaining(self) -> int:
        return self.end + 1 - self.start - self.done


def segments_path(part_path: str) -> str:
    return part_path + ".segments"


def load_progress(part_path: str) -> int | None:
    """Bytes downloaded so far per the sidecar, or None if there isn't one."""
    state = _load(segments_path(part_path))
    return None if state is None else sum(done for _, _, done in state["segments"])


def _load(path: str) -> dict | None:
    try:
        with open(path) as f:
            state = json.load(f)
        state["total"] = int(state["total"])
        state["segments"] = [(int(a), int(b), int(d)) for a, b, d in state["segments"]]
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _discard(part_path: str) -> None:
    for path in (part_path, segments_path(part_path)):
        try:
            os.remove(path)
        except OSError:
            pass


def _split(total: int, n: int) -> list[_Segment]:
    size = total // n
    bounds = [i * size for i in range(n)] + [total]
    return [_Segment(bounds[i], bounds[i + 1] - 1) for i in range(n)]


def _probe(worker) -> tuple[int, str, str] | None:
    """(total, etag, last_modified) if the server supports safe ranged resumes."""
    from app.engine import downloader as dl

    headers = worker._base_headers()
    headers["Range"] = "bytes=0-"
    try:
        response = worker._get(headers)
    except requests.exceptions.RequestException:
        return None  # let the single-stream path deal with (and retry) it
    try:
        if response.status_code != 206:
            return None
        total = dl.DownloadWorker._parse_range_total(response.headers.get("Content-Range", ""))
        etag = response.headers.get("ETag", "")
        last_modified = response.headers.get("Last-Modified", "")
        if not total or not (etag or last_modified):
            return None
        return total, etag, last_modified
    finally:
        response.close()


def run_segmented(worker) -> bool:
    """Run the download split across connections if possible.

    Returns True when it has reported the final status itself (completed,
    paused, cancelled or failed), False when the caller should run the
    single-stream path instead.
    """
    part = worker.part_path
    sidecar = segments_path(part)
    state = _load(sidecar)
    if state is not None and os.path.exists(part) and os.path.getsize(part) == state["total"]:
        total, etag, last_modified = state["total"], state.get("etag", ""), state.get("last_modified", "")
        segments = [_Segment(a, b, d) for a, b, d in state["segments"]]
    else:
        if state is not None or os.path.exists(sidecar):
            _discard(part)  # unusable sidecar: its .part can't be trusted either
        if worker.max_connections <= 1 or os.path.exists(part):
            worker.connections_resolved.emit(worker.task_id, 1)
            return False
        probed = _probe(worker)
        if probed is None:
            worker.connections_resolved.emit(worker.task_id, 1)
            return False
        total, etag, last_modified = probed
        n = min(worker.max_connections, total // MIN_PART)
        if total < MIN_TOTAL or n < 2:
            worker.connections_resolved.emit(worker.task_id, 1)
            return False
        segments = _split(total, n)
        os.makedirs(os.path.dirname(part), exist_ok=True)
        with open(part, "wb") as f:
            f.truncate(total)
        _save(sidecar, total, etag, last_modified, segments)

    worker.etag, worker.last_modified, worker._total_bytes = etag, last_modified, total
    worker._server_digest_sha256 = None  # a ranged response's Digest can't describe the whole file
    worker.metadata_resolved.emit(worker.task_id, total, etag, last_modified, True)  # the probe got a 206
    unfinished = [s for s in segments if s.remaining > 0]
    # Every granted connection, even if fewer pieces are left (e.g. resuming
    # with one unfinished): idle connections split the remaining work.
    threads = max(1, worker.max_connections) if unfinished else 1
    worker.connections_resolved.emit(worker.task_id, threads)

    outcome = _Run(worker, segments, total, etag, last_modified, sidecar).run(threads) if unfinished else "done"

    if outcome == "done":
        try:
            os.remove(sidecar)  # every byte is in; nothing left to resume
            worker._complete(total)
        except OSError as exc:
            # A full-size .part with no sidecar would later look like a
            # finished single-stream file, so never leave one behind.
            _discard(part)
            worker.status_changed.emit(worker.task_id, DownloadStatus.FAILED.value, str(exc))
        return True
    if outcome == "paused":
        status = DownloadStatus.CANCELLED if worker._cancel_event.is_set() else DownloadStatus.PAUSED
        worker.status_changed.emit(worker.task_id, status.value, "")
        return True
    if outcome == "changed":
        # The file changed on the server mid-download. Start over as a single
        # stream: no .part means a plain GET, and its response sets fresh
        # validators.
        _discard(part)
        worker.etag = worker.last_modified = ""
        worker._total_bytes = 0
        worker.connections_resolved.emit(worker.task_id, 1)
        return False
    worker.status_changed.emit(worker.task_id, DownloadStatus.FAILED.value, outcome)
    return True


def _save(path: str, total: int, etag: str, last_modified: str, segments: list[_Segment]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"total": total, "etag": etag, "last_modified": last_modified,
                   "segments": [[s.start, s.end, s.done] for s in segments]}, f)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written sidecar


class _Run:
    """One pass of a segmented download: worker threads pull unfinished
    segments off a queue while this (the QThread) reports progress."""

    def __init__(self, worker, segments, total, etag, last_modified, sidecar):
        self.w = worker
        self.segments = segments
        self.total = total
        self.validator = etag or last_modified
        self.etag, self.last_modified, self.sidecar = etag, last_modified, sidecar
        self.abort = threading.Event()  # set on failure or a changed file
        self.error: str | None = None
        self.changed = False
        self.last_error = ""
        self.todo: queue.SimpleQueue[_Segment] = queue.SimpleQueue()
        for segment in segments:
            if segment.remaining > 0:
                self.todo.put(segment)
        # Guards splitting a segment (shrinking its end) against its owner
        # thread reserving bytes from it, and the segments list itself.
        self.lock = threading.Lock()

    def stopped(self) -> bool:
        return self.abort.is_set() or self.w._pause_event.is_set()

    def downloaded(self) -> int:
        with self.lock:
            return sum(s.done for s in self.segments)

    def save(self) -> None:
        with self.lock:
            segments = [_Segment(s.start, s.end, s.done) for s in self.segments]
        _save(self.sidecar, self.total, self.etag, self.last_modified, segments)

    def _steal(self) -> _Segment | None:
        """Split the unfinished segment with the most left and take its back
        half, so a connection that ran out of queued work helps the slowest
        piece instead of exiting and leaving it to finish alone."""
        with self.lock:
            victim = max(self.segments, key=lambda seg: seg.remaining)
            if victim.remaining < 2 * MIN_PART:
                return None  # not worth a new connection
            middle = victim.start + victim.done + victim.remaining // 2
            tail = _Segment(middle, victim.end)
            victim.end = middle - 1  # its owner stops here (it re-reads end per chunk)
            self.segments.append(tail)
            return tail

    def run(self, thread_count: int) -> str:
        self.fd = os.open(self.w.part_path, os.O_WRONLY)
        threads = [threading.Thread(target=self._pull, name=f"segment-{i}", daemon=True)
                   for i in range(thread_count)]
        try:
            for t in threads:
                t.start()
            self._report_until_done(threads)
            for t in threads:
                t.join()
        finally:
            os.close(self.fd)
            self.save()
        if self.changed:
            return "changed"
        if self.error is not None:
            return self.error
        if all(s.remaining <= 0 for s in self.segments):
            return "done"
        if self.w._pause_event.is_set():
            return "paused"
        return "A download connection stopped unexpectedly"

    def _report_until_done(self, threads) -> None:
        w = self.w
        last_emit = last_save = time.monotonic()
        last_bytes = self.downloaded()
        retry_reported = False
        while any(t.is_alive() for t in threads):
            time.sleep(0.05)
            now = time.monotonic()
            if now - last_emit >= TICK:
                done = self.downloaded()
                speed = (done - last_bytes) / (now - last_emit)
                w._smoothed_speed = (speed if w._smoothed_speed == 0.0
                                     else SPEED_SMOOTHING * speed + (1 - SPEED_SMOOTHING) * w._smoothed_speed)
                w.progress.emit(w.task_id, done, self.total, w._smoothed_speed)
                last_bytes, last_emit = done, now
                # RETRYING only while *every* running connection is waiting
                # to retry -- one slow segment isn't a stalled download.
                alive = sum(t.is_alive() for t in threads)
                waiting = sum(s.waiting for s in self.segments)
                if waiting and waiting >= alive and not retry_reported:
                    w._smoothed_speed = 0.0
                    w.status_changed.emit(w.task_id, DownloadStatus.RETRYING.value,
                                          f"{self.last_error} — retrying")
                    retry_reported = True
                elif retry_reported and waiting < alive:
                    w.status_changed.emit(w.task_id, DownloadStatus.DOWNLOADING.value, "")
                    retry_reported = False
            if now - last_save >= SAVE_INTERVAL:
                self.save()
                last_save = now

    def _pull(self) -> None:
        while not self.stopped():
            try:
                segment = self.todo.get_nowait()
            except queue.Empty:
                segment = self._steal()
                if segment is None:
                    return
            self._fetch(segment)

    def _fail(self, message: str) -> None:
        if self.error is None:
            self.error = message
        self.abort.set()

    def _fetch(self, seg: _Segment) -> None:
        from app.engine import downloader as dl  # looked up at call time so tests can patch the retry policy

        attempt = 0
        while seg.remaining > 0 and not self.stopped():
            got = 0
            headers = self.w._base_headers()
            headers["Range"] = f"bytes={seg.start + seg.done}-{seg.end}"
            headers["If-Range"] = self.validator
            try:
                response = self.w._get(headers)
                try:
                    if response.status_code == 200:
                        raise _Changed()
                    if response.status_code != 206:
                        response.raise_for_status()
                        raise requests.exceptions.RequestException(f"Unexpected status {response.status_code}")
                    # Chunks are pwrite()n at the offset we asked for, so a
                    # server answering from anywhere else would scramble the
                    # file. Treat it like a changed file: start over as a
                    # single stream, which doesn't depend on ranges.
                    start = dl.DownloadWorker._parse_range_start(response.headers.get("Content-Range", ""))
                    if start != seg.start + seg.done:
                        raise _Changed()
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if self.stopped():
                            return
                        if not chunk:
                            continue
                        with self.lock:  # reserve, so a concurrent split sees the bytes as taken
                            chunk = chunk[:max(0, seg.remaining)]  # never write past this segment
                            offset = seg.start + seg.done
                            seg.done += len(chunk)
                        if chunk:
                            try:
                                os.pwrite(self.fd, chunk, offset)
                            except OSError:
                                with self.lock:  # never let the sidecar count unwritten bytes
                                    seg.done -= len(chunk)
                                raise
                        got += len(chunk)
                        if seg.remaining <= 0:
                            break
                finally:
                    response.close()
                if seg.remaining > 0 and got == 0:
                    raise requests.exceptions.ConnectionError("Connection closed before sending any data")
                attempt = 0  # made progress; if the stream ended early, just ask for the rest
            except _Changed:
                self.changed = True
                self.abort.set()
                return
            except requests.exceptions.RequestException as exc:
                if dl._is_permanent_http_error(exc):
                    self._fail(str(exc))
                    return
                if got > 0:
                    attempt = 0
                attempt += 1
                delay = dl.compute_retry_delay(attempt)
                if delay is None:
                    self._fail(str(exc))
                    return
                self.last_error = exc.__class__.__name__
                seg.waiting = True
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not self.stopped():
                    time.sleep(0.05)
                seg.waiting = False
            except OSError as exc:  # writing to disk failed: retrying won't help
                self._fail(str(exc))
                return
