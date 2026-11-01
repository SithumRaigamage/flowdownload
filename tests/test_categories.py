"""Editable categories: which folder each file type goes to."""
from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from app.api.schemas import SettingsOut, SettingsPatch
from app.engine.models import DownloadStatus as S
from app.engine.paths import (
    apply_auto_folder_for_path,
    category_for,
    default_categories,
    prune_empty_auto_folder_dir,
    validate_categories,
)
from app.engine.settings import Settings

CUSTOM = [{"name": "Programs", "extensions": ["exe", "msi"], "folder": "Programs"},
          {"name": "Books", "extensions": ["epub", "pdf"], "folder": "Reading"}]


def test_defaults_keep_the_old_type_folders(tmp_path):
    cats = default_categories()
    assert [c["folder"] for c in cats] == ["Video", "Audio", "Documents", "Archives"]
    assert apply_auto_folder_for_path("movie.MP4", "/dl", cats) == os.path.join("/dl", "Video", "movie.MP4")
    assert apply_auto_folder_for_path("notes.xyz", "/dl", cats) == os.path.join("/dl", "Other", "notes.xyz")
    assert apply_auto_folder_for_path("README", "/dl", cats) == os.path.join("/dl", "Other", "README")


def test_custom_categories_route_by_extension(tmp_path):
    cats = validate_categories(CUSTOM)
    assert category_for("setup.EXE", cats)["name"] == "Programs"
    assert apply_auto_folder_for_path("book.pdf", "/dl", cats) == os.path.join("/dl", "Reading", "book.pdf")


def test_absolute_category_folder_is_used_as_is(tmp_path):
    cats = validate_categories([{"name": "ISOs", "extensions": ["iso"], "folder": str(tmp_path)}])
    assert apply_auto_folder_for_path("x.iso", "/dl", cats) == str(tmp_path / "x.iso")


def test_extensions_are_normalised():
    (cat,) = validate_categories([{"name": " Pics ", "extensions": [".JPG", "png", "jpg", " "], "folder": "Pics"}])
    assert cat == {"name": "Pics", "extensions": ["jpg", "png"], "folder": "Pics"}


@pytest.mark.parametrize("bad", [
    [{"name": "A", "extensions": ["mp4"], "folder": "A"}, {"name": "B", "extensions": ["mp4"], "folder": "B"}],
    [{"name": "A", "extensions": [], "folder": "A"}, {"name": "a", "extensions": [], "folder": "B"}],
    [{"name": "A", "extensions": ["mp4"], "folder": "../escape"}],
    [{"name": "A", "extensions": ["mp4"], "folder": "sub/dir"}],
    [{"name": "A", "extensions": ["mp4"], "folder": ".."}],
    [{"name": "A", "extensions": ["mp4"], "folder": "/no/such/folder/anywhere"}],
    [{"name": "A", "extensions": ["not an ext"], "folder": "A"}],
    [{"name": "", "extensions": ["mp4"], "folder": "A"}],
])
def test_unsafe_or_ambiguous_categories_are_refused(bad):
    with pytest.raises(ValueError):
        validate_categories(bad)
    with pytest.raises(ValidationError):
        SettingsPatch(categories=bad)


def test_categories_round_trip_through_settings_json(tmp_path):
    path = str(tmp_path / "settings.json")
    Settings(categories=validate_categories(CUSTOM)).save(path)
    assert Settings.load(path).categories == CUSTOM
    assert [c.name for c in SettingsOut.from_settings(Settings.load(path)).categories] == ["Programs", "Books"]


def test_broken_hand_edit_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"categories": [{"name": "X", "folder": "../../etc"}], "max_concurrent_downloads": 5}))
    loaded = Settings.load(str(path))
    assert loaded.categories == default_categories()
    assert loaded.max_concurrent_downloads == 5  # the rest of the file still applies


def test_download_lands_in_its_custom_category(make_manager, server, wait, tmp_path):
    mgr = make_manager(auto_folder_by_type=True, categories=validate_categories(CUSTOM))
    task = mgr.tasks[mgr.add_download(f"{server}/setup.exe?delay=0", "setup.exe")]
    assert task.dest_path == str(tmp_path / "dl" / "Programs" / "setup.exe")
    assert wait(lambda: task.status == S.COMPLETED)


def test_categories_do_nothing_while_sorting_is_off(make_manager, server, tmp_path):
    mgr = make_manager(auto_folder_by_type=False, categories=validate_categories(CUSTOM))
    task = mgr.tasks[mgr.add_download(f"{server}/setup.exe?delay=0", "setup.exe")]
    assert task.dest_path == str(tmp_path / "dl" / "setup.exe")


def test_pruning_covers_custom_folders_but_never_absolute_ones(tmp_path):
    (tmp_path / "ext").mkdir()
    cats = validate_categories([*CUSTOM, {"name": "Ext", "extensions": ["iso"], "folder": str(tmp_path / "ext")}])
    (tmp_path / "Reading").mkdir()
    prune_empty_auto_folder_dir(str(tmp_path / "Reading" / "book.pdf"), str(tmp_path), cats)
    assert not (tmp_path / "Reading").exists()
    prune_empty_auto_folder_dir(str(tmp_path / "ext" / "x.iso"), str(tmp_path), cats)
    assert (tmp_path / "ext").exists()
