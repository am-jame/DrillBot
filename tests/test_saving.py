"""words.json is saved safely: atomic writes and backups."""
import json
import os

import pytest

import crawler


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(crawler, "json_file", str(tmp_path / "words.json"))
    monkeypatch.setattr(crawler, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(crawler, "jsondict", [{"name": "A", "pos": "ru", "words": {}}])
    with open(crawler.json_file, "w") as f:
        json.dump([{"name": "old", "pos": "ru", "words": {}}], f)
    return tmp_path


def test_save_keeps_a_backup(files):
    crawler.refresh_json_from_dict()
    assert json.load(open(crawler.json_file))[0]["name"] == "A"
    assert json.load(open(crawler.json_file + ".bak"))[0]["name"] == "old"
    assert len(os.listdir(crawler.BACKUP_DIR)) == 1


def test_a_crash_mid_save_leaves_the_old_file(files, monkeypatch):
    def disk_full(*args):
        raise OSError("disk full")
    monkeypatch.setattr(crawler.os, "replace", disk_full)
    with pytest.raises(OSError):
        crawler.refresh_json_from_dict()
    assert json.load(open(crawler.json_file))[0]["name"] == "old"


def test_files_live_next_to_the_code():
    here = os.path.dirname(os.path.abspath(crawler.__file__))
    assert crawler.json_file == os.path.join(here, "words.json")
    assert crawler.MEANINGS_FILE == os.path.join(here, "meanings.json")
