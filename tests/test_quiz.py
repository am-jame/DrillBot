"""Quizzes: answering, scores, races, buttons, results, retries and restarts."""
import asyncio

import discord

import discbot
from conftest import list_index
from discbot import Prompt, QuizSession
from fakes import FakeChannel, FakeMessage, FakeUser, click, say

ANA, BO = FakeUser(1, "Ana"), FakeUser(2, "Bo")


def prompts(n=3):
    return [Prompt("читать", "Present", f"P{i}", f"answer{i}", "ru") for i in range(n)]


async def start(bot, channel, items=None, **kwargs):
    await bot.run_quiz(channel, items or prompts(), "Test quiz", **kwargs)
    return bot.sessions[channel.id]


def test_answers_scores_and_results(bot):
    async def run():
        ch = FakeChannel()
        s = await start(bot, ch)
        wrong = await say(bot, ch, BO, "answerx")  # same alphabet, so it's a guess
        right = await say(bot, ch, ANA, s.current.answer)
        assert wrong.reactions == ["❌"] and right.reactions == ["✅"]
        await say(bot, ch, ANA, s.current.answer)
        await say(bot, ch, BO, s.current.answer)
        assert ch.id not in bot.sessions
        results = ch.last.embed
        assert results.title == "Quiz finished"
        assert "**2/3** right on the first try" in results.description
        assert [p.name for p in s.ranking()] == ["Ana", "Bo"]
    asyncio.run(run())


def test_two_people_answering_at_once_dont_skip_a_question(bot):
    async def run():
        ch = FakeChannel()
        s = await start(bot, ch)
        first = s.current.answer
        await asyncio.gather(say(bot, ch, ANA, first), say(bot, ch, BO, first))
        assert s.number == 2 and s.correct == 1 and s.wrong_tries == 0  # the late answer got ⏱️, not ❌
    asyncio.run(run())


def test_buttons_reveal_skip_stop_and_stale_clicks(bot):
    async def run():
        ch = FakeChannel()
        s = await start(bot, ch)
        question = ch.last
        not_refreshed = FakeMessage(ch, view=question.view)  # someone still seeing the old buttons
        skipped = s.current
        await click(bot, question, "Skip", ANA)
        assert question.view is None  # buttons removed once the question is done
        assert s.prompts[0] is skipped  # back of the queue
        stale = await click(bot, not_refreshed, "Reveal", BO)
        assert stale.private == ["That question is already done."]
        await click(bot, ch.last, "Reveal", ANA)
        await click(bot, ch.last, "Stop", BO)
        assert ch.id not in bot.sessions and ch.last.embed.title == "Quiz stopped by Bo"
    asyncio.run(run())


def test_retry_button_starts_one_quiz_on_the_missed_words(bot):
    async def run():
        ch = FakeChannel()
        s = await start(bot, ch, prompts(2))
        await say(bot, ch, ANA, "!ans")
        await say(bot, ch, ANA, s.current.answer)
        results = ch.last
        assert results.view is not None
        await click(bot, results, "Retry missed", ANA)
        assert bot.sessions[ch.id].total == 1
        assert bot.sessions[ch.id].label.endswith("(retry)")
    asyncio.run(run())


def test_quiz_survives_a_restart(bot, monkeypatch):
    async def run():
        ch = FakeChannel()
        s = await start(bot, ch, prompts(3))
        await say(bot, ch, ANA, s.current.answer)
        restarted = discbot.Client(intents=discord.Intents.default())
        restarted.restore_sessions()
        r = restarted.sessions[ch.id]
        assert isinstance(r, QuizSession) and r.number == 2 and r.current == s.current
        assert r.players[ANA.id].correct == 1
        await click(restarted, ch.last, "Reveal", ANA)  # the old message's button still works
        assert r.number == 3
    asyncio.run(run())


def test_quiz_from_real_lists_and_full_tables(bot):
    ru = list_index("Regular -ать")
    items, label, available = discbot.prepare_quiz([ru], ["Present"], count=5)
    assert len(items) == 5 and available == 42
    sv = list_index("Test Swedish")
    saga = next(p for p in discbot.prepare_quiz([sv], ["Preterite"])[0] if p.word == "säga")
    assert discbot.is_right(saga, "sa") and discbot.is_right(saga, "sade")
    tables, _, _ = discbot.prepare_quiz([ru], ["Present"], table=True)
    assert all(t.forms and len(t.forms) == 6 for t in tables)
