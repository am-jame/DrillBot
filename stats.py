"""Saved quiz stats, kept in a small SQLite file next to the bot.

Stats are per server: the same person has separate stats in each server.
DMs use server id 0.
"""
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    quizzes  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
-- One row per player per word form, e.g. (читать, Present, Я).
CREATE TABLE IF NOT EXISTS form_stats (
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    pos       TEXT NOT NULL,
    word      TEXT NOT NULL,
    tense     TEXT NOT NULL,
    form      TEXT NOT NULL,
    correct   INTEGER NOT NULL DEFAULT 0,
    wrong     INTEGER NOT NULL DEFAULT 0,
    revealed  INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, pos, word, tense, form)
);
-- Words someone has learned in a lesson (/learn), per tense.
CREATE TABLE IF NOT EXISTS learned (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    pos        TEXT NOT NULL,
    word       TEXT NOT NULL,
    tense      TEXT NOT NULL,
    learned_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, pos, word, tense)
);
"""

# Which column each kind of result adds to.
RESULT_COLUMNS = {"correct": "correct", "wrong": "wrong", "revealed": "revealed"}


@dataclass
class Totals:
    name: str
    quizzes: int
    correct: int
    wrong: int
    revealed: int

    @property
    def accuracy(self) -> int:
        """Right answers out of everything: wrong guesses and reveals count against you."""
        tries = self.correct + self.wrong + self.revealed
        return round(100 * self.correct / tries) if tries else 0


@dataclass
class TenseStat:
    pos: str
    tense: str
    correct: int
    tries: int

    @property
    def accuracy(self) -> int:
        return round(100 * self.correct / self.tries) if self.tries else 0


@dataclass
class MissedForm:
    word: str
    tense: str
    form: str
    misses: int


class Stats:
    def __init__(self, path: str = DB_PATH):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def record(self, guild_id: int, user_id: int, name: str, prompt, result: str):
        """Count one answer. `result` is "correct", "wrong" or "revealed"."""
        column = RESULT_COLUMNS[result]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db:
            self._touch_player(guild_id, user_id, name)
            self.db.execute(
                f"""INSERT INTO form_stats (guild_id, user_id, pos, word, tense, form, {column}, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT DO UPDATE SET {column} = {column} + 1, last_seen = excluded.last_seen""",
                (guild_id, user_id, prompt.pos, prompt.word, prompt.tense, prompt.form, now))

    def finish_quiz(self, guild_id: int, players: dict[int, str]):
        """Add one to the quiz count of everyone who answered. `players` maps user id -> name."""
        with self.db:
            for user_id, name in players.items():
                self._touch_player(guild_id, user_id, name)
                self.db.execute("UPDATE players SET quizzes = quizzes + 1 WHERE guild_id = ? AND user_id = ?",
                                (guild_id, user_id))

    def _touch_player(self, guild_id: int, user_id: int, name: str):
        self.db.execute("""INSERT INTO players (guild_id, user_id, name) VALUES (?, ?, ?)
                           ON CONFLICT DO UPDATE SET name = excluded.name""", (guild_id, user_id, name))

    def totals(self, guild_id: int, user_id: int) -> Totals | None:
        row = self.db.execute(
            """SELECT p.name, p.quizzes, COALESCE(SUM(f.correct), 0), COALESCE(SUM(f.wrong), 0),
                      COALESCE(SUM(f.revealed), 0)
               FROM players p LEFT JOIN form_stats f USING (guild_id, user_id)
               WHERE p.guild_id = ? AND p.user_id = ?
               GROUP BY p.guild_id, p.user_id""", (guild_id, user_id)).fetchone()
        return Totals(*row) if row else None

    def weakest_tenses(self, guild_id: int, user_id: int, limit: int = 3, min_tries: int = 5) -> list[TenseStat]:
        rows = self.db.execute(
            """SELECT pos, tense, SUM(correct) AS c, SUM(correct + wrong + revealed) AS t
               FROM form_stats WHERE guild_id = ? AND user_id = ?
               GROUP BY pos, tense HAVING t >= ? AND c < t
               ORDER BY CAST(c AS REAL) / t, t DESC LIMIT ?""",
            (guild_id, user_id, min_tries, limit)).fetchall()
        return [TenseStat(*r) for r in rows]

    def weakest_forms(self, guild_id: int, user_id: int) -> list[tuple[str, str, str, str]]:
        """Every form the player has missed at least once, weakest first, as (pos, word, tense, form).

        "Weakest" is the share of tries that were misses, smoothed so one miss out of
        one try doesn't outrank 5 misses out of 6. Ties go to the more recently seen form.
        """
        return self.db.execute(
            """SELECT pos, word, tense, form FROM form_stats
               WHERE guild_id = ? AND user_id = ? AND wrong + revealed > 0
               ORDER BY (wrong + revealed + 1.0) / (correct + wrong + revealed + 2) DESC,
                        wrong + revealed DESC, last_seen DESC""", (guild_id, user_id)).fetchall()

    def most_missed(self, guild_id: int, user_id: int, limit: int = 5) -> list[MissedForm]:
        rows = self.db.execute(
            """SELECT word, tense, form, wrong + revealed AS m
               FROM form_stats WHERE guild_id = ? AND user_id = ? AND m > 0
               ORDER BY m DESC, last_seen DESC LIMIT ?""", (guild_id, user_id, limit)).fetchall()
        return [MissedForm(*r) for r in rows]

    def mark_learned(self, guild_id: int, user_id: int, pos: str, tense: str, words: list[str]):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db:
            self.db.executemany("""INSERT OR IGNORE INTO learned (guild_id, user_id, pos, word, tense, learned_at)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                [(guild_id, user_id, pos, word, tense, now) for word in words])

    def learned_words(self, guild_id: int, user_id: int, pos: str, tense: str) -> set[str]:
        rows = self.db.execute("SELECT word FROM learned WHERE guild_id = ? AND user_id = ? AND pos = ? AND tense = ?",
                               (guild_id, user_id, pos, tense))
        return {row[0] for row in rows}

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[tuple[int, Totals]]:
        rows = self.db.execute(
            """SELECT p.user_id, p.name, p.quizzes, SUM(f.correct) AS c, SUM(f.wrong), SUM(f.revealed)
               FROM players p JOIN form_stats f USING (guild_id, user_id)
               WHERE p.guild_id = ?
               GROUP BY p.user_id HAVING c > 0
               ORDER BY c DESC, p.name LIMIT ?""", (guild_id, limit)).fetchall()
        return [(r[0], Totals(*r[1:])) for r in rows]
