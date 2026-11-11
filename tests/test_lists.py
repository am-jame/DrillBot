"""Word list commands: who may edit, /add limits, deleting, and paged output."""
import asyncio

import crawler
import discbot
from conftest import list_index
from fakes import FakeChannel, FakeUser, say

HOME = 42


def test_list_editing_only_in_the_home_server(bot, monkeypatch):
    monkeypatch.setattr(discbot, "HOME_GUILD", discord_object(HOME))
    mod = FakeUser(1, "Mod", manage_messages=True)

    async def run():
        elsewhere = FakeChannel(guild_id=7)
        await say(bot, elsewhere, mod, "!new ru Test")
        assert "home server" in elsewhere.last.content
        home = FakeChannel(guild_id=HOME)
        await say(bot, home, mod, "!new ru Test")
        assert elsewhere.last.content != home.last.content and "Created list" in home.last.content
        await say(bot, home, FakeUser(2, "Bo"), "!rename 0 Nope")
        assert "Manage Messages" in home.last.content
    asyncio.run(run())


def test_add_words_limits_and_lookups(bot, monkeypatch):
    looked_up = []
    monkeypatch.setattr(crawler, "lookup", lambda w, pos: looked_up.append(w) or ({"Present": {"Я": w}} if w != "zzz" else None))
    ru = list_index("Regular -ать")

    async def run():
        result = await discbot.add_words(ru, ["новое", "zzz", "читать"], user_id=5)
        assert "Added: новое" in result and "Could not add: zzz" in result and "Already in the list: читать" in result
        assert "читать" not in looked_up  # no lookup for words already there
        try:
            await discbot.add_words(ru, [f"w{i}" for i in range(11)], user_id=5)
        except discbot.UsageError as e:
            assert "up to 10" in str(e)
        else:
            raise AssertionError("11 words should be refused")
    asyncio.run(run())


def test_deleting_a_list_renumbers_queued_quizzes(bot):
    bot.setups[5] = discbot.QuizSetup([0, 1, 2], ["Present"])
    count = len(crawler.jsondict)
    bot.delete_list(1)
    assert len(crawler.jsondict) == count - 1 and bot.setups[5].lists == [0, 1]
    assert crawler.jsondict[1]["name"] == "Test Swedish"


def test_long_output_is_paged():
    pages = discbot.paginate([("Section", [f"line {i}" for i in range(50)])])
    assert len(pages) == 3 and all(len(p.splitlines()) <= discbot.PAGE_LINES for p in pages)
    assert pages[1].startswith("**Section** (cont.)")


def discord_object(guild_id):
    import discord
    return discord.Object(id=guild_id)
