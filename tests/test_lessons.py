"""/learn lessons: copy each form, quiz rounds mixed in, retries, finishing and restarts."""
import asyncio

import discord

import discbot
from conftest import list_index
from discbot import LessonSession
from fakes import FakeChannel, FakeUser, click, say

ANA, BO = FakeUser(1, "Ana"), FakeUser(2, "Bo")


def test_plan_alternates_copying_and_quiz_rounds(bot):
    ru = list_index("Regular -ать")
    words = bot.lesson_words(42, ANA.id, ru, "Present")[:3]
    s = LessonSession.build(ANA.id, ru, "Present", words, {})
    kinds = [step[0] for step in s.steps]
    assert kinds[:3] == ["copy"] * 3 and kinds[3:6] == ["quiz"] * 3  # Я: copy 3, quiz 3
    assert kinds[6:9] == ["copy"] * 3 and kinds[9:14] == ["quiz"] * 5  # Ты: copy 3, quiz 3 + 2 earlier
    assert s.total == len(s.steps)


def test_a_whole_lesson(bot):
    async def run():
        ch = FakeChannel()
        ru = list_index("Regular -ать")
        await say(bot, ch, ANA, f"!learn {ru} Present")
        s = bot.sessions[ch.id]
        assert len(s.words) == discbot.LESSON_SIZE == 3
        assert "meaning of" in ch.sent[0].embed.description

        assert (await say(bot, ch, BO, "читаю")).reactions == []  # only the learner counts
        kind, word, label, _ = s.step
        wrong = await say(bot, ch, ANA, s.forms[word][label] + "x")
        assert wrong.reactions == ["❌"] and wrong.replies[0].startswith("Copy it exactly")

        missed = False
        while s.steps:
            kind, word, label, retry = s.step
            if kind == "quiz" and not missed:
                await say(bot, ch, ANA, "неправильно")
                missed = True
                assert any(step[3] for step in s.steps)  # it comes back
                continue
            await say(bot, ch, ANA, s.forms[word][label])
        assert ch.id not in bot.sessions
        summary = ch.last.embed
        assert summary.title == "Lesson complete · Present"
        assert bot.stats.learned_words(42, ANA.id, "ru", "Present") == set(s.words)
        assert s.first_try == s.quiz_total - 1

        await click(bot, ch.last, "Next lesson", ANA)
        assert set(bot.sessions[ch.id].words).isdisjoint(s.words)
    asyncio.run(run())


def test_lesson_survives_a_restart(bot):
    async def run():
        ch = FakeChannel()
        await say(bot, ch, ANA, f"!learn {list_index('Regular -ать')} Present 2")
        s = bot.sessions[ch.id]
        await say(bot, ch, ANA, s.forms[s.step[1]][s.step[2]])
        restarted = discbot.Client(intents=discord.Intents.default())
        restarted.restore_sessions()
        r = restarted.sessions[ch.id]
        assert isinstance(r, LessonSession) and r.steps == s.steps
    asyncio.run(run())


def test_stop_keeps_words_unlearned(bot):
    async def run():
        ch = FakeChannel()
        await say(bot, ch, ANA, f"!learn {list_index('Regular -ать')} Present")
        await say(bot, ch, ANA, "!stop")
        assert ch.id not in bot.sessions
        assert bot.stats.learned_words(42, ANA.id, "ru", "Present") == set()
    asyncio.run(run())
