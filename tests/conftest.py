import asyncio
import copy
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord  # noqa: E402
import crawler  # noqa: E402
import discbot  # noqa: E402
import stats  # noqa: E402
import store  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch the internet; parser tests use saved pages (see `page`)."""
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not use the network")
    monkeypatch.setattr(crawler.requests, "get", refuse)


@pytest.fixture
def page(monkeypatch):
    """page("wikt_talo.html") makes every web request return that saved page."""
    def serve(name):
        html = open(os.path.join(FIXTURES, name), encoding="utf-8").read()
        response = type("Response", (), {"status_code": 200, "text": html})()
        monkeypatch.setattr(crawler.requests, "get", lambda url, **kwargs: response)
    return serve


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """A bot with its own throwaway database, word lists and meanings. Nothing it does is
    saved to the project, and word lookups / meanings never go online. The word lists are the
    small hand-written ones in tests/fixtures/words_test.json (words.json isn't in git)."""
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(discbot, "Stats", lambda: stats.Stats(db))
    monkeypatch.setattr(discbot, "QuizStore", lambda: store.QuizStore(db))
    monkeypatch.setattr(crawler, "json_file", os.path.join(FIXTURES, "words_test.json"))
    monkeypatch.setattr(crawler, "MEANINGS_FILE", str(tmp_path / "meanings.json"))
    monkeypatch.setattr(crawler, "meanings", {})
    monkeypatch.setattr(crawler, "refresh_json_from_dict", lambda: None)
    monkeypatch.setattr(crawler, "save_meanings", lambda: None)
    monkeypatch.setattr(crawler, "english_meaning", lambda word, pos, lemma=None: f"meaning of {word}")
    monkeypatch.setattr(discbot, "LOOKUP_DELAY", 0)
    monkeypatch.setattr(discbot, "lookup_lock", asyncio.Lock())
    client = discbot.Client(intents=discord.Intents.default())  # loads the test word lists
    monkeypatch.setattr(crawler, "jsondict", copy.deepcopy(crawler.jsondict))
    return client


def list_index(name_start: str) -> int:
    """The number of the first list whose name starts with `name_start`."""
    return next(i for i, wl in enumerate(crawler.jsondict) if wl["name"].startswith(name_start))
