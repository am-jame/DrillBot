"""Saves running quizzes and "Retry missed" lists, so they survive a bot restart.

Uses the same SQLite file as the stats (stats.db), in separate tables.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from stats import DB_PATH

SCHEMA = """
-- One row per running quiz, keyed by the thread (or channel) it runs in.
CREATE TABLE IF NOT EXISTS quiz_sessions (
    channel_id INTEGER PRIMARY KEY,
    state      TEXT NOT NULL,  -- JSON from QuizSession.to_dict()
    updated    TEXT NOT NULL
);
-- The missed words behind each "Retry missed" button.
CREATE TABLE IF NOT EXISTS retries (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    label    TEXT NOT NULL,
    prompts  TEXT NOT NULL,  -- JSON list of prompt dicts
    used_by  TEXT,           -- name of whoever pressed it; NULL until then
    created  TEXT NOT NULL,
    tashkeel INTEGER NOT NULL DEFAULT 0  -- the retry also requires Arabic vowel marks
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QuizStore:
    def __init__(self, path: str = DB_PATH):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        # Databases made before the tashkeel option don't have its column yet.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(retries)")}
        if "tashkeel" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE retries ADD COLUMN tashkeel INTEGER NOT NULL DEFAULT 0")

    # ---- running quizzes ----

    def save_session(self, channel_id: int, state: dict):
        with self.db:
            self.db.execute("""INSERT INTO quiz_sessions (channel_id, state, updated) VALUES (?, ?, ?)
                               ON CONFLICT DO UPDATE SET state = excluded.state, updated = excluded.updated""",
                            (channel_id, json.dumps(state, ensure_ascii=False), _now()))

    def delete_session(self, channel_id: int):
        with self.db:
            self.db.execute("DELETE FROM quiz_sessions WHERE channel_id = ?", (channel_id,))

    def load_sessions(self) -> dict[int, dict]:
        return {cid: json.loads(state) for cid, state in self.db.execute("SELECT channel_id, state FROM quiz_sessions")}

    # ---- retry buttons ----

    def add_retry(self, label: str, prompts: list[dict], tashkeel: bool = False) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO retries (label, prompts, created, tashkeel) VALUES (?, ?, ?, ?)",
                                  (label, json.dumps(prompts, ensure_ascii=False), _now(), int(tashkeel)))
        return cur.lastrowid

    def get_retry(self, retry_id: int) -> tuple[str, list[dict], str | None, bool] | None:
        """(label, prompts, used_by, tashkeel), or None if there's no such retry."""
        row = self.db.execute("SELECT label, prompts, used_by, tashkeel FROM retries WHERE id = ?",
                              (retry_id,)).fetchone()
        return (row[0], json.loads(row[1]), row[2], bool(row[3])) if row else None

    def claim_retry(self, retry_id: int, name: str) -> bool:
        """Mark a retry as used. False if someone already used it."""
        with self.db:
            cur = self.db.execute("UPDATE retries SET used_by = ? WHERE id = ? AND used_by IS NULL", (name, retry_id))
        return cur.rowcount == 1

    # ---- housekeeping ----

    def prune(self, session_days: int = 7, retry_days: int = 30):
        """Forget quizzes nobody has touched in a week, and retry buttons older than a month."""
        now = datetime.now(timezone.utc)
        with self.db:
            self.db.execute("DELETE FROM quiz_sessions WHERE updated < ?",
                            ((now - timedelta(days=session_days)).isoformat(timespec="seconds"),))
            self.db.execute("DELETE FROM retries WHERE created < ?",
                            ((now - timedelta(days=retry_days)).isoformat(timespec="seconds"),))
