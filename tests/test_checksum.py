"""Checking finished files against a typed or published checksum."""
from __future__ import annotations

import hashlib

import pytest

from app.api.schemas import DownloadOut
from app.engine import checksum
from app.engine.models import DownloadStatus as S
from app.engine.storage import Storage
from tests.conftest import DATA, REQUESTS, SIDECARS

SHA256 = hashlib.sha256(DATA).hexdigest()
MD5 = hashlib.md5(DATA).hexdigest()
WRONG = "0" * 64


@pytest.fixture(autouse=True)
def clean_server():
    SIDECARS.clear()
    REQUESTS.clear()
    yield
    SIDECARS.clear()


# ---------- parsing ----------

@pytest.mark.parametrize("typed, expected", [
    ("", ""),
    ("   ", ""),
    (SHA256.upper(), f"sha256:{SHA256}"),
    (f"sha256:{SHA256}", f"sha256:{SHA256}"),
    (f"SHA-256 = {SHA256}", f"sha256:{SHA256}"),
    (f"{SHA256}  file.bin", f"sha256:{SHA256}"),
    ("d41d8cd98f00b204e9800998ecf8427e", "md5:d41d8cd98f00b204e9800998ecf8427e"),
    ("da39a3ee5e6b4b0d3255bfef95601890afd80709", "sha1:da39a3ee5e6b4b0d3255bfef95601890afd80709"),
])
def test_typed_checksums_are_recognised_by_length(typed, expected):
    assert checksum.parse_expected(typed) == expected


@pytest.mark.parametrize("typed", ["abc", "z" * 64, "1" * 63, f"md5:{SHA256}"])
def test_malformed_checksums_are_rejected(typed):
    with pytest.raises(ValueError):
        checksum.parse_expected(typed)


def test_sums_file_lookup_by_name():
    sums = f"# release\n{WRONG}  other.bin\n{SHA256} *dist/file.bin\n"
    assert checksum.find_in_sums(sums, "file.bin") == SHA256
    assert checksum.find_in_sums(sums, "missing.bin") == ""
    assert checksum.find_in_sums(f"{SHA256}\n", "anything.bin") == SHA256  # a bare .sha256 file


def test_no_checksum_file_lookup_for_signed_links():
    assert checksum.sidecar_urls("https://h/a/f.zip?sig=1") == []
    assert checksum.sidecar_urls("https://h/a/f.zip") == ["https://h/a/f.zip.sha256", "https://h/a/SHA256SUMS"]


def test_hash_file_computes_several_algorithms_in_one_pass(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(DATA)
    assert checksum.hash_file(str(path), ["sha256", "md5"]) == {"sha256": SHA256, "md5": MD5}


# ---------- end to end ----------

def _finish(mgr, wait, url, typed=""):
    task = mgr.tasks[mgr.add_download(url, "file.bin", typed)]
    assert wait(lambda: task.status == S.COMPLETED and task.digest_sha256)
    return task


def test_typed_checksum_that_matches_is_verified(make_manager, server, wait):
    task = _finish(make_manager(), wait, f"{server}/file.bin?delay=0", MD5)
    assert (task.checksum_status, task.checksum_source) == ("verified", "you")
    assert task.expected_checksum == f"md5:{MD5}"


def test_typed_checksum_that_differs_keeps_the_file_and_flags_it(make_manager, server, wait):
    mgr = make_manager()
    task = _finish(mgr, wait, f"{server}/file.bin?delay=0", WRONG)
    assert (task.checksum_status, task.checksum_source) == ("mismatch", "you")
    assert task.status == S.COMPLETED
    assert open(task.dest_path, "rb").read() == DATA
    assert any(e["level"] == "error" and "checksum you entered" in e["message"] for e in mgr.get_history(task.id))


def test_bad_typed_checksum_is_refused_before_queueing(make_manager, server):
    mgr = make_manager()
    with pytest.raises(ValueError):
        mgr.add_download(f"{server}/file.bin", "file.bin", "not-a-checksum")
    assert not mgr.tasks


def test_published_sha256_file_is_used(make_manager, server, wait):
    SIDECARS["/rel/file.bin.sha256"] = f"{SHA256}  file.bin\n".encode()
    task = _finish(make_manager(), wait, f"{server}/rel/file.bin")
    assert (task.checksum_status, task.checksum_source) == ("verified", "checksum file")


def test_sums_file_in_the_folder_is_used(make_manager, server, wait):
    SIDECARS["/rel/SHA256SUMS"] = f"{WRONG}  other.bin\n{SHA256}  file.bin\n".encode()
    task = _finish(make_manager(), wait, f"{server}/rel/file.bin")
    assert task.checksum_status == "verified"


def test_published_checksum_that_differs_is_a_mismatch(make_manager, server, wait):
    SIDECARS["/rel/file.bin.sha256"] = f"{WRONG}\n".encode()
    task = _finish(make_manager(), wait, f"{server}/rel/file.bin")
    assert (task.checksum_status, task.checksum_source) == ("mismatch", "checksum file")


def test_nothing_published_means_not_checked(make_manager, server, wait):
    task = _finish(make_manager(), wait, f"{server}/rel/file.bin")
    assert (task.checksum_status, task.checksum_source) == ("", "")
    assert task.digest_sha256 == SHA256


def test_signed_links_get_no_extra_requests(make_manager, server, wait):
    _finish(make_manager(), wait, f"{server}/rel/file.bin?sig=abc")
    assert not [p for p, _ in REQUESTS if p.endswith((".sha256", "SHA256SUMS"))]


def test_checksum_result_survives_a_relaunch(make_manager, server, wait, tmp_path):
    mgr = make_manager()
    task = _finish(mgr, wait, f"{server}/file.bin?delay=0", SHA256)
    mgr.shutdown()
    again = Storage(str(tmp_path / "t.db")).load_all()[0]
    assert (again.expected_checksum, again.checksum_status, again.checksum_source) == (
        f"sha256:{SHA256}", "verified", "you")
    out = DownloadOut.from_task(again)
    assert (out.checksum_status, out.checksum_source) == ("verified", "you")
    assert task.id == again.id
