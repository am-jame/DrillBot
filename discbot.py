import asyncio
import difflib
import logging
import os
import random
import re
import unicodedata
import urllib.parse
from dataclasses import asdict, dataclass, field

import discord
from discord import Message, app_commands

import crawler
from stats import Stats
from store import QuizStore

log = logging.getLogger("quizbot")

# Set this in your shell or PyCharm run configuration: DISCORD_TOKEN=...
TOKEN = os.environ.get("DISCORD_TOKEN")
# Optional: a server id to register slash commands on instantly while testing.
# Without it they are registered globally, which can take a while to show up the first time.
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
# The one server where word lists can be edited (/new, /add, /remove, /rename, /deletelist).
# Every server quizzes from the same shared lists, but only this one can change them.
# Defaults to DISCORD_GUILD_ID. With neither set, nobody can edit lists through the bot.
HOME_GUILD_ID = os.environ.get("DISCORD_HOME_GUILD_ID") or GUILD_ID
HOME_GUILD = discord.Object(id=int(HOME_GUILD_ID)) if HOME_GUILD_ID else None

pos_names = {
    'ru': 'Russian Verbs',
    'run': 'Russian Nouns',
    'rua': 'Russian Adjectives',
    'ar': 'Arabic Verbs',
    'fi': 'Finnish Verbs',
    'fin': 'Finnish Nouns',
    'fia': 'Finnish Adjectives',
    'sv': 'Swedish Verbs'
}

# Each Finnish case asks for the singular and plural ('Sing', 'Pl').
FINNISH_CASES = ['Nominative', 'Genitive', 'Partitive', 'Inessive', 'Elative', 'Illative', 'Adessive',
                 'Ablative', 'Allative', 'Essive', 'Translative', 'Abessive', 'Instructive']

avail_quizes = {
    'ru': ['Present', 'Past', 'Imperative', 'Participle'],
    'run': ['Nom Pl', 'Gen Sing', 'Gen Pl', 'Prep Sing', 'Prep Pl', 'Acc Sing', 'Acc Pl', 'Dat Sing', 'Dat Pl', 'Inst Sing', 'Inst Pl'],
    'rua': ['Nom', 'Gen', 'Prep', 'Inan Acc', 'Anim Acc', 'Dat', 'Inst', 'Short'],
    'sv': ['Present', 'Preterite', 'Supine', 'Imperative', 'Perfect', 'Pluperfect', 'Participles', 'Passive'],
    'ar': ['Past', 'Present', 'Subjunctive', 'Jussive', 'Imperative', 'Passive Past', 'Passive Present',
           'Passive Subjunctive', 'Passive Jussive', 'Participles', 'Verbal Noun'],
    'fi': ['Present', 'Negative', 'Past', 'Past Negative', 'Conditional', 'Imperative', 'Potential',
           'Infinitives', 'Participles'],
    'fin': FINNISH_CASES,
    'fia': FINNISH_CASES + ['Comparison'],
}

# Lower-case tense name -> canonical name, so "!quiz 0 gen sing" works.
TENSE_LOOKUP = {t.lower(): t for tenses in avail_quizes.values() for t in tenses}

MAX_MISSED_SHOWN = 15
MAX_PLAYERS_SHOWN = 10
MEDALS = ["🥇", "🥈", "🥉"]


class UsageError(Exception):
    """Raised by a command handler; the message is sent back to the channel."""


# ---- Quiz data ----

@dataclass
class Prompt:
    word: str
    tense: str
    form: str  # person / gender label, '' when the tense has a single form
    answer: str
    pos: str = ""  # part of speech of the list it came from, e.g. 'ru'
    # Full-table mode: every form of the tense, label -> answer, answered in this order.
    # `answer` then holds them all, comma-separated (for display and the chat check).
    forms: dict[str, str] | None = None

    @property
    def question(self) -> str:
        # Arabic goes on its own "## " line: Discord shows it as a heading, i.e. bigger text.
        big = "## " if self.pos == "ar" else ""
        if self.forms:
            separators = "commas" if any(' ' in a for a in self.forms.values()) else "spaces or commas"
            return (f"**{self.tense}** · full table\n{big}({self.word}): {', '.join(self.forms)}\n"
                    f"-# all {len(self.forms)} forms in that order, separated by {separators}")
        label = f"{self.form} " if self.form else ""
        return f"**{self.tense}**\n{big}{label}({self.word})"

    @property
    def key(self) -> str:
        return f"{self.pos}|{self.word}|{self.tense}|{self.form}" + ("|table" if self.forms else "")

    @property
    def answer_display(self) -> str:
        """The answer for a reveal. Arabic is shown as headings (bigger text), on new lines."""
        if self.pos == "ar":
            if self.forms:
                return '\n' + '\n'.join(f"### {label} · {answer}" for label, answer in self.forms.items())
            return f"\n## {self.answer}"
        if self.forms:
            return '\n' + '\n'.join(f"{label} · **{answer}**" for label, answer in self.forms.items())
        return f"**{self.answer}**"

    def parts(self) -> list["Prompt"]:
        """The single forms in this prompt (just itself unless it's a full table)."""
        if not self.forms:
            return [self]
        return [Prompt(self.word, self.tense, label, answer, self.pos) for label, answer in self.forms.items()]


@dataclass
class QuizSetup:
    """What a channel has queued with !quiz, kept between quizzes so !start can be repeated."""
    lists: list[int] = field(default_factory=list)
    tenses: list[str] = field(default_factory=list)
    count: int | None = None  # how many questions; None means all
    table: bool = False  # full-table mode
    tashkeel: bool = False  # Arabic answers must include the vowel marks


@dataclass
class PlayerScore:
    name: str
    correct: int = 0  # questions this player answered first
    wrong: int = 0    # wrong guesses

    @property
    def accuracy(self) -> int:
        tries = self.correct + self.wrong
        return round(100 * self.correct / tries) if tries else 0


@dataclass
class QuizSession:
    prompts: list[Prompt]
    label: str = ""  # lists and tenses, shown in the thread name and results
    total: int = 0
    current: Prompt | None = None
    previous: Prompt | None = None  # so a late answer to the last question isn't marked wrong
    wrong_tries: int = 0
    correct: int = 0
    missed: list[Prompt] = field(default_factory=list)
    players: dict[int, PlayerScore] = field(default_factory=dict)  # keyed by user id
    # Wrong guesses on skipped questions, keyed by Prompt.key, so a question that
    # was guessed wrong and then skipped can't count as "first try" later.
    skipped_tries: dict[str, int] = field(default_factory=dict)
    public_thread: bool = False  # a public thread the bot made: results go to the main channel too
    tashkeel: bool = False  # Arabic answers must include the vowel marks exactly
    # For the buttons: a random id for this quiz, plus a count of questions asked, so a
    # button can tell whether it belongs to the open question (even after a restart).
    token: int = field(default_factory=lambda: random.getrandbits(31))
    serial: int = 0
    question_message_id: int | None = None  # the message with the open question's buttons

    def __post_init__(self):
        random.shuffle(self.prompts)
        self.total = len(self.prompts)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["players"] = {str(uid): p for uid, p in data["players"].items()}  # JSON keys must be strings
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "QuizSession":
        def prompt(d):
            return Prompt(**d) if d else None
        session = cls([])
        for name, value in data.items():
            setattr(session, name, value)
        session.prompts = [prompt(d) for d in data["prompts"]]
        session.current, session.previous = prompt(data["current"]), prompt(data["previous"])
        session.missed = [prompt(d) for d in data["missed"]]
        session.players = {int(uid): PlayerScore(**p) for uid, p in data["players"].items()}
        return session

    @property
    def number(self) -> int:
        return self.total - len(self.prompts)

    def advance(self) -> Prompt | None:
        self.previous = self.current
        self.current = self.prompts.pop() if self.prompts else None
        self.wrong_tries = self.skipped_tries.pop(self.current.key, 0) if self.current else 0
        self.serial += 1
        return self.current

    def skip(self) -> Prompt | None:
        """Send the open question to the back of the queue and move on."""
        prompt = self.current
        if self.wrong_tries:
            self.skipped_tries[prompt.key] = self.wrong_tries
        self.prompts.insert(0, prompt)  # questions are popped from the end
        return self.advance()

    def player(self, user) -> PlayerScore:
        score = self.players.setdefault(user.id, PlayerScore(user.display_name))
        score.name = user.display_name  # keep up to date if they rename mid-quiz
        return score

    def ranking(self) -> list[PlayerScore]:
        return sorted(self.players.values(), key=lambda p: (-p.correct, p.wrong, p.name.lower()))


def results_embed(session: QuizSession, stopped: bool) -> discord.Embed:
    answered = session.number - (1 if session.current else 0)  # an open question doesn't count
    embed = discord.Embed(
        title="Quiz stopped" if stopped else "Quiz finished",
        description=f"**{session.correct}/{answered}** right on the first try",
        colour=discord.Colour.orange() if stopped else discord.Colour.green(),
    )
    ranking = session.ranking()
    if ranking:
        rows = []
        for i, p in enumerate(ranking[:MAX_PLAYERS_SHOWN]):
            place = MEDALS[i] if i < len(MEDALS) and p.correct else f"`{i + 1}.`"
            rows.append(f"{place} **{discord.utils.escape_markdown(p.name)}** · "
                        f"{p.correct} right · {p.wrong} wrong · {p.accuracy}%")
        if len(ranking) > MAX_PLAYERS_SHOWN:
            rows.append(f"...and {len(ranking) - MAX_PLAYERS_SHOWN} more")
        embed.add_field(name="Scoreboard", value='\n'.join(rows)[:1024], inline=False)
    if session.missed:
        rows = []
        for p in session.missed[:MAX_MISSED_SHOWN]:
            form = f" {p.form}" if p.form else ""
            rows.append(f"{p.word} ({p.tense}{form}) → **{p.answer}**")
        if len(session.missed) > MAX_MISSED_SHOWN:
            rows.append(f"...and {len(session.missed) - MAX_MISSED_SHOWN} more")
        embed.add_field(name="To review", value='\n'.join(rows)[:1024], inline=False)
    if session.label:
        embed.set_footer(text=session.label[:2048])
    return embed


def normalize(text: str, keep_marks: bool = False) -> str:
    """For comparing answers: ignores case, ё vs е (most people type е), the Arabic
    tatweel, and Arabic vowel marks (tashkeel) unless `keep_marks`."""
    # NFC puts stacked marks in one order, so shadda+fatha and fatha+shadda compare equal.
    text = unicodedata.normalize("NFC", text.strip().lower()).replace('ё', 'е').replace('\u0640', '')
    return text if keep_marks else crawler.strip_tashkeel(text)


def split_table_answer(text: str) -> list[str]:
    """Commas or new lines if there are any (forms can contain spaces), otherwise spaces."""
    if ',' in text or '\n' in text:
        return [part.strip() for part in re.split(r"[,\n]+", text) if part.strip()]
    return text.split()


def matches(text: str, answer: str, tashkeel: bool = False) -> bool:
    """Whether `text` is the answer, or one of its alternatives ("omenien / omenoiden")."""
    typed = normalize(text, tashkeel)
    return any(typed == normalize(option, tashkeel) for option in answer.split(crawler.SEPARATOR))


def table_marks(prompt: Prompt, text: str, tashkeel: bool = False) -> list[bool] | None:
    """Right/wrong for each form of a full-table answer, or None if the number of forms is off."""
    parts = split_table_answer(text)
    if len(parts) != len(prompt.forms):
        return None
    return [matches(part, answer, tashkeel) for part, answer in zip(parts, prompt.forms.values())]


def is_right(prompt: Prompt, text: str, tashkeel: bool = False) -> bool:
    """`tashkeel`: Arabic vowel marks must match too (otherwise they're ignored)."""
    if prompt.forms:
        marks = table_marks(prompt, text, tashkeel)
        return bool(marks) and all(marks)
    return matches(text, prompt.answer, tashkeel)


def table_feedback(prompt: Prompt, text: str, marks: list[bool] | None) -> str:
    """Which forms of a full-table answer were wrong, without giving the right ones away."""
    if marks is None:
        got = len(split_table_answer(text))
        return (f"That's {got} form{'s' if got != 1 else ''}; this table needs {len(prompt.forms)} "
                f"({', '.join(prompt.forms)}).")
    right = sum(marks)
    return f"{right}/{len(marks)} right: " + ' · '.join(
        f"{label} {'✅' if ok else '❌'}" for label, ok in zip(prompt.forms, marks))


def scripts(text: str) -> set[str]:
    """The alphabets used in some text, e.g. {'CYRILLIC'} or {'LATIN'}."""
    return {unicodedata.name(ch, "?").split()[0] for ch in text if ch.isalpha()}


def is_chatter(message: Message, answer: str, bot_user) -> bool:
    """Whether a wrong-looking message in a quiz is really just chat, and shouldn't count as a guess.

    Only used after checking the message isn't right, so a correct answer is never ignored.
    """
    text = message.content.strip()
    reply_to = message.reference.resolved if message.reference else None
    if message.reference and getattr(reply_to, "author", None) != bot_user:
        return True  # a reply to someone else (or to a message we can't see)
    if any(user != bot_user for user in message.mentions):
        return True  # talking to someone
    if not scripts(text) & scripts(answer):
        return True  # no letters, or a different alphabet: "lol" when the answer is Russian, emoji, "?"
    return len(text.split()) > len(answer.split()) + 1  # a sentence, not a guess


def build_prompts(list_keys: list[int], tenses: list[str], table: bool = False) -> list[Prompt]:
    """One prompt per form; or with `table`, one per word and tense (tenses with a single form stay single)."""
    prompts = []
    for list_key in list_keys:
        pos = crawler.jsondict[list_key]['pos']
        for word, word_tenses in crawler.jsondict[list_key]['words'].items():
            if not word_tenses:  # entries the crawler failed to fetch
                continue
            for tense in tenses:
                forms = word_tenses.get(tense, {})
                if table and len(forms) > 1:
                    prompts.append(Prompt(word, tense, "", ', '.join(forms.values()), pos, forms=dict(forms)))
                    continue
                for form, answer in forms.items():
                    prompts.append(Prompt(word, tense, form, answer, pos))
    return prompts


def parse_list_index(arg: str) -> int:
    try:
        index = int(arg)
    except ValueError:
        raise UsageError(f'"{arg}" is not a list number. Use `/lists` to see them.')
    if not 0 <= index < len(crawler.jsondict):
        raise UsageError(f"No list {index}. Use `/lists` to see them.")
    return index


# ---- Command helpers (shared by ! commands and slash commands) ----

# ---- Paged output (/lists, /words) ----
# Long output is split into embed pages with ◀ ▶ buttons. Like the quiz buttons, their
# custom_id says what to show ("page:words:<list>:<page>:<p|n>"), so they work after a
# restart, and each page is rebuilt from the current lists when someone flips to it.

PAGE_LINES = 20
WORDS_PER_LINE = 5


def paginate(sections: list[tuple[str, list[str]]]) -> list[str]:
    """Pack titled sections of lines into pages of at most PAGE_LINES lines.

    A section that doesn't fit on one page is split, with "(cont.)" on the later parts."""
    chunks = []
    for title, lines in sections:
        for start in range(0, max(len(lines), 1), PAGE_LINES - 1):  # -1 leaves room for the title
            part = lines[start:start + PAGE_LINES - 1]
            heading = f"**{title}**" + (" (cont.)" if start else "") if title else None
            chunks.append(([heading] if heading else []) + part)
    pages, current = [], []
    for chunk in chunks:
        if current and len(current) + 1 + len(chunk) > PAGE_LINES:  # +1 for the blank line between
            pages.append(current)
            current = []
        current += ([""] if current else []) + chunk
    pages.append(current)
    return ['\n'.join(page) for page in pages]


def lists_pages() -> tuple[str, list[str]]:
    if not crawler.jsondict:
        return "Word lists", ["(no lists yet: make one with `/new`)"]
    # Group lists by part of speech, keeping each list's number for quizzing / adding.
    groups: dict[str, list[str]] = {}
    for i, wl in enumerate(crawler.jsondict):
        n = len(wl['words'])
        count = f"{n} word{'s' if n != 1 else ''}" if n else "*empty*"
        groups.setdefault(wl['pos'], []).append(f"`{i:>2}` {wl['name']} · {count}")
    order = [p for p in pos_names if p in groups] + [p for p in groups if p not in pos_names]
    return f"Word lists ({len(crawler.jsondict)})", paginate([(pos_names.get(p, p), groups[p]) for p in order])


def words_pages(list_key: int) -> tuple[str, list[str]]:
    wl = crawler.jsondict[list_key]
    words = list(wl['words'])
    lines = [' · '.join(words[i:i + WORDS_PER_LINE]) for i in range(0, len(words), WORDS_PER_LINE)]
    title = f"{list_key} · {wl['name']} ({len(words)} word{'s' if len(words) != 1 else ''})"
    return title, paginate([("", lines or ["(list empty)"])])


class PageButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"page:(?P<kind>lists|words):(?P<arg>\d*):(?P<page>\d+):(?P<dir>[pn])"):
    def __init__(self, kind: str, arg: str, page: int, direction: str, disabled: bool = False):
        super().__init__(discord.ui.Button(
            emoji="◀️" if direction == "p" else "▶️", style=discord.ButtonStyle.secondary, disabled=disabled,
            custom_id=f"page:{kind}:{arg}:{page}:{direction}"))
        self.kind, self.arg, self.page = kind, arg, page

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["kind"], match["arg"], int(match["page"]), match["dir"])

    async def callback(self, interaction: discord.Interaction):
        try:
            embed, view = paged(self.kind, self.arg, self.page)
        except (UsageError, IndexError):
            await interaction.response.edit_message(content="That list no longer exists.", embed=None, view=None)
            return
        await interaction.response.edit_message(embed=embed, view=view)


def paged(kind: str, arg: str = "", page: int = 0) -> tuple[discord.Embed, discord.ui.View | None]:
    """The embed for one page of /lists or /words, plus ◀ ▶ buttons if there's more than one page."""
    title, pages = lists_pages() if kind == "lists" else words_pages(parse_list_index(arg))
    page = max(0, min(page, len(pages) - 1))  # the output may have shrunk since the button was made
    embed = discord.Embed(title=title, description=pages[page], colour=discord.Colour.blurple())
    if len(pages) == 1:
        return embed, None
    embed.set_footer(text=f"Page {page + 1}/{len(pages)}")
    view = discord.ui.View(timeout=None)  # never stop()ed, see question_view
    view.add_item(PageButton(kind, arg, max(page - 1, 0), "p", disabled=page == 0))
    view.add_item(PageButton(kind, arg, page + 1, "n", disabled=page == len(pages) - 1))
    return embed, view


def send_kwargs(embed, view) -> dict:
    return {"embed": embed, "view": view} if view else {"embed": embed}


# ---- List management (/remove, /rename, /deletelist) ----

def can_manage_lists(user) -> bool:
    """Editing or deleting lists needs Manage Messages (lists are shared by every server the bot is in)."""
    perms = getattr(user, "guild_permissions", None)  # None in DMs
    return bool(perms and perms.manage_messages)


def is_home(guild) -> bool:
    return bool(HOME_GUILD and guild and guild.id == HOME_GUILD.id)


def require_home(guild):
    """For the ! editing commands; the slash versions are only registered in the home server."""
    if not is_home(guild):
        raise UsageError("Word lists are shared by every server, so they can only be edited from the bot's "
                         "home server." + ("" if HOME_GUILD else " (No home server is set up yet: set "
                                           "DISCORD_HOME_GUILD_ID and restart the bot.)"))


def require_manager(user):
    """For the ! commands; slash commands use Discord's own permission settings instead."""
    if not can_manage_lists(user):
        raise UsageError("Only people with the Manage Messages permission can change or delete lists.")


def remove_words(list_key: int, words: list[str]) -> str:
    saved = crawler.jsondict[list_key]['words']
    by_normal = {normalize(w): w for w in saved}
    removed, missing = [], []
    for word in words:
        key = by_normal.get(normalize(word))
        if key in saved:
            del saved[key]
            removed.append(key)
        else:
            missing.append(word)
    if removed:
        crawler.refresh_json_from_dict()
    name = crawler.jsondict[list_key]['name']
    lines = [f"Removed from **{name}**: {' '.join(removed)}"] if removed else []
    if missing:
        lines.append(f"Not in **{name}**: {' '.join(missing)}")
    return '\n'.join(lines) or "Give at least one word to remove."


def rename_list(list_key: int, name: str) -> str:
    name = name.strip()
    if not name:
        raise UsageError("Give the list a new name.")
    old = crawler.jsondict[list_key]['name']
    crawler.jsondict[list_key]['name'] = name
    crawler.refresh_json_from_dict()
    return f"Renamed list {list_key}: **{old}** → **{name}**"


def describe_list(list_key: int) -> str:
    wl = crawler.jsondict[list_key]
    n = len(wl['words'])
    return f"list {list_key} **{wl['name']}** ({n} word{'s' if n != 1 else ''})"


class ConfirmDeleteView(discord.ui.View):
    """Delete / Cancel buttons. Only the person who asked can press them."""

    def __init__(self, client: "Client", list_key: int, user_id: int):
        super().__init__(timeout=120)
        self.client, self.list_key, self.user_id = client, list_key, user_id
        self.name = crawler.jsondict[list_key]['name']  # to notice if the lists change meanwhile

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("Only the person who asked can confirm this.", ephemeral=True)
        return False

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        try:
            if self.list_key >= len(crawler.jsondict) or crawler.jsondict[self.list_key]['name'] != self.name:
                raise UsageError("The lists changed since you asked, so nothing was deleted. Try again.")
            text = self.client.delete_list(self.list_key)
        except UsageError as e:
            text = str(e)
        await interaction.response.edit_message(content=text, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled: nothing was deleted.", view=None)


def tenses_text(pos: str) -> str:
    if pos not in avail_quizes:
        raise UsageError(f'No quizzes for "{pos_names.get(pos, pos)}" yet.')
    return '\n'.join(avail_quizes[pos])


def new_list(pos: str, name: str) -> str:
    name = name.strip()
    if pos not in pos_names or not name:
        raise UsageError("Give a language and a name. Languages: " + ', '.join(pos_names))
    crawler.jsondict.append({"name": name, "pos": pos, "words": {}})
    crawler.refresh_json_from_dict()
    return f"Created list {len(crawler.jsondict) - 1} - {name} for {pos_names[pos]}"


MAX_ADD_WORDS = 10    # per /add, so one command can't fire off a flood of lookups
LOOKUP_DELAY = 1.0    # seconds between lookups, to be polite to the site
lookup_lock = asyncio.Lock()  # one lookup at a time across the whole bot
adding_users: set[int] = set()  # people with an /add still running


def check_add(words: list[str], user_id: int | None):
    """The quick checks for add_words, so /add can fail fast before it says it's thinking."""
    if not words:
        raise UsageError("Give at least one word to add.")
    if len(set(words)) > MAX_ADD_WORDS:
        raise UsageError(f"You can add up to {MAX_ADD_WORDS} words at a time.")
    if user_id in adding_users:
        raise UsageError("You're already adding words. Wait for that to finish first.")


async def add_words(list_key: int, words: list[str], user_id: int | None = None) -> str:
    words = list(dict.fromkeys(words))
    check_add(words, user_id)
    word_list = crawler.jsondict[list_key]  # held by identity: list numbers can shift during lookups
    if user_id is not None:
        adding_users.add(user_id)
    added, failed, already = [], [], []
    try:
        for word in words:
            arabic = word_list['pos'] == 'ar'
            plain = crawler.strip_tashkeel(word) if arabic else word
            if word in word_list['words'] or (plain == word and plain in word_list['words']):
                already.append(word)
                continue
            async with lookup_lock:
                try:
                    # Only the web request runs in a thread. The lists are changed and saved
                    # here on the bot's own thread, so nothing edits them at the same time.
                    if arabic:  # may come from Wiktionary, saved under its vowelled spelling
                        word, forms = await asyncio.to_thread(crawler.lookup_arabic, word)
                    else:
                        forms = await asyncio.to_thread(crawler.lookup, word, word_list['pos'])
                except Exception as e:
                    log.warning("looking up %s failed: %r", word, e)
                    forms = None
                await asyncio.sleep(LOOKUP_DELAY)
            if forms and word in word_list['words']:
                already.append(word)
                continue
            if not any(wl is word_list for wl in crawler.jsondict):
                raise UsageError("That list was deleted while the words were being looked up.")
            if forms:
                word_list['words'][word] = forms
                crawler.refresh_json_from_dict()
                added.append(word)
            else:
                failed.append(word)
    finally:
        adding_users.discard(user_id)
    lines = []
    if added:
        lines.append("Added: " + ' '.join(added))
    if already:
        lines.append("Already in the list: " + ' '.join(already))
    if failed:
        lines.append("Could not add: " + ' '.join(failed))
    return '\n'.join(lines)


def split_items(text: str) -> list[str]:
    return [t.strip() for t in text.split(',') if t.strip()]


def parse_lists(text: str) -> list[int]:
    """'0, 3 5' -> [0, 3, 5]"""
    keys = [parse_list_index(t) for t in text.replace(',', ' ').split()]
    if not keys:
        raise UsageError("Pick at least one list.")
    return list(dict.fromkeys(keys))


def parse_tenses(text: str) -> list[str]:
    """'present, past' -> ['Present', 'Past']"""
    tenses = []
    for name in split_items(text):
        tense = TENSE_LOOKUP.get(name.lower())
        if tense is None:
            raise UsageError(f'Unknown tense "{name}". Use `/tenses` to see them.')
        tenses.append(tense)
    return list(dict.fromkeys(tenses))


def tenses_for_lists(list_keys: list[int]) -> list[str]:
    """Tenses that apply to at least one of the lists (all tenses if no lists given)."""
    poses = {crawler.jsondict[k]['pos'] for k in list_keys} or set(avail_quizes)
    return list(dict.fromkeys(t for pos in avail_quizes if pos in poses for t in avail_quizes[pos]))


def check_tenses(list_keys: list[int], tenses: list[str]):
    valid = tenses_for_lists(list_keys)
    wrong = [t for t in tenses if t not in valid]
    if wrong:
        raise UsageError(f"{', '.join(wrong)} doesn't apply to those lists. "
                         f"Try: {', '.join(valid) or '(none)'}")


def prepare_quiz(list_keys: list[int], tenses: list[str], count: int | None = None, table: bool = False):
    """Returns (prompts, label, available): at most `count` random prompts out of `available`."""
    if not list_keys or not tenses:
        raise UsageError("Pick at least one list and one tense, e.g. `/quiz lists:0 tenses:Present`.")
    prompts = build_prompts(list_keys, tenses, table)
    if not prompts:
        raise UsageError("None of those words have those tenses.")
    if table and not any(p.forms for p in prompts):
        raise UsageError("Full tables need a tense with several forms, like Present or Past. "
                         "Cases like Gen Sing only have one form each.")
    available = len(prompts)
    if count and count < available:
        prompts = random.sample(prompts, count)
    lists = ', '.join(crawler.jsondict[k]['name'] for k in list_keys)
    return prompts, f"{lists} - {', '.join(tenses)}{' (full tables)' if table else ''}", available


WEAK_DEFAULT_COUNT = 20


def weak_prompts(stats: Stats, gid: int, user_id: int, list_keys: list[int], tenses: list[str],
                 count: int) -> list[Prompt]:
    """Up to `count` of the player's weakest forms, optionally only from some lists / tenses."""
    # Look answers up from the current lists, so stats for deleted words are skipped.
    answers = {}
    for i, wl in enumerate(crawler.jsondict):
        if list_keys and i not in list_keys:
            continue
        for word, word_tenses in wl['words'].items():
            for tense, forms in (word_tenses or {}).items():
                for form, answer in forms.items():
                    answers[(wl['pos'], word, tense, form)] = answer
    prompts = []
    for pos, word, tense, form in stats.weakest_forms(gid, user_id):
        answer = answers.get((pos, word, tense, form))
        if answer is None or (tenses and tense not in tenses):
            continue
        prompts.append(Prompt(word, tense, form, answer, pos))
        if len(prompts) == count:
            break
    if not prompts:
        raise UsageError("No missed words to practise yet" + (" for those lists/tenses" if list_keys or tenses
                         else "") + ". Play some quizzes first!")
    return prompts


def parse_start_args(args: str, usage: str, flags: tuple[str, ...] = ("private",)) -> tuple[int | None, set[str]]:
    """'20 private table' -> (20, {'private', 'table'}). Every part is optional."""
    count, found = None, set()
    for token in args.lower().split():
        if token.isdigit() and int(token) > 0:
            count = int(token)
        elif token in flags:
            found.add(token)
        else:
            raise UsageError(usage)
    return count, found


def apply_tashkeel(prompts: list[Prompt], label: str, tashkeel: bool) -> str:
    """Check the tashkeel option makes sense for these questions, and mark it in the quiz name."""
    if not tashkeel:
        return label
    if not any(p.pos == "ar" for p in prompts):
        raise UsageError("The tashkeel option is for Arabic lists: pick at least one.")
    return f"{label} (with tashkeel)"


def question_count(prompts: list[Prompt], available: int) -> str:
    n = len(prompts)
    return f"{n} of {available} questions" if n < available else f"{n} question{'s' if n != 1 else ''}"


def guild_id(channel) -> int:
    """Stats are kept per server; DMs count as server 0."""
    guild = getattr(channel, "guild", None)
    return guild.id if guild else 0


def stats_embed(stats: Stats, gid: int, user) -> discord.Embed:
    totals = stats.totals(gid, user.id)
    if totals is None:
        return discord.Embed(description=f"No stats yet for {user.mention}. Play a quiz first!")
    embed = discord.Embed(
        title=f"Stats for {totals.name}",
        description=(f"**{totals.accuracy}%** accuracy · {totals.correct} right · {totals.wrong} wrong · "
                     f"{totals.revealed} revealed · {totals.quizzes} quiz{'zes' if totals.quizzes != 1 else ''}"),
        colour=discord.Colour.blurple(),
    )
    weak = stats.weakest_tenses(gid, user.id)
    if weak:
        embed.add_field(name="Weakest tenses/cases", inline=False, value='\n'.join(
            f"{t.tense} ({pos_names.get(t.pos, t.pos or '?')}) · {t.accuracy}% of {t.tries}" for t in weak))
    missed = stats.most_missed(gid, user.id)
    if missed:
        embed.add_field(name="Most missed", inline=False, value='\n'.join(
            f"{m.word} ({m.tense}{' ' + m.form if m.form else ''}) · missed {m.misses}×" for m in missed))
    embed.set_footer(text="Accuracy counts wrong guesses and reveals as misses.")
    return embed


def leaderboard_embed(stats: Stats, gid: int) -> discord.Embed:
    rows = stats.leaderboard(gid)
    if not rows:
        return discord.Embed(description="No one has played a quiz here yet.")
    lines = []
    for i, (_, t) in enumerate(rows):
        place = MEDALS[i] if i < len(MEDALS) else f"`{i + 1}.`"
        lines.append(f"{place} **{discord.utils.escape_markdown(t.name)}** · {t.correct} right · "
                     f"{t.accuracy}% · {t.quizzes} quiz{'zes' if t.quizzes != 1 else ''}")
    return discord.Embed(title="Leaderboard", description='\n'.join(lines), colour=discord.Colour.gold())


# ---- Buttons ----
# Each button's custom_id says what it's for (e.g. "quiz:reveal:<quiz token>:<question serial>"),
# and the bot registers these patterns at startup. That way buttons keep working after a
# restart: nothing about them lives only in memory.

class QuestionButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"quiz:(?P<action>reveal|skip|stop):(?P<token>\d+):(?P<serial>\d+)"):
    LOOKS = {
        "reveal": ("Reveal", "👀", discord.ButtonStyle.secondary),
        "skip": ("Skip", "⏭️", discord.ButtonStyle.secondary),
        "stop": ("Stop", "🛑", discord.ButtonStyle.danger),
    }

    def __init__(self, action: str, token: int, serial: int):
        label, emoji, style = self.LOOKS[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"quiz:{action}:{token}:{serial}"))
        self.action, self.token, self.serial = action, token, serial

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["token"]), int(match["serial"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.question_button(interaction, self.action, self.token, self.serial)


def question_view(session: QuizSession) -> discord.ui.View:
    # timeout=None and never stop()ed: stopping a view with dynamic buttons would unregister them.
    view = discord.ui.View(timeout=None)
    for action in QuestionButton.LOOKS:
        view.add_item(QuestionButton(action, session.token, session.serial))
    return view


class RetryButton(discord.ui.DynamicItem[discord.ui.Button], template=r"quiz:retry:(?P<id>\d+)"):
    def __init__(self, retry_id: int, count: int | None = None):
        super().__init__(discord.ui.Button(
            label=f"Retry missed ({count})" if count else "Retry missed", emoji="🔁",
            style=discord.ButtonStyle.primary, custom_id=f"quiz:retry:{retry_id}"))
        self.retry_id = retry_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.retry_button(interaction, self.retry_id)


def retry_view(retry_id: int, count: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RetryButton(retry_id, count))
    return view


def used_retry_view(name: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=f"Retried by {name}"[:80], emoji="🔁", disabled=True,
                                    style=discord.ButtonStyle.primary, custom_id="quiz:retried"))
    return view


# ---- Lessons (/learn) ----
# A lesson takes a few words a person hasn't learned yet for one tense, and goes through the
# tense one form at a time: copy the new form for each word (Я читаю, Я делаю, Я знаю), then a
# short quiz round on those plus a couple of forms from earlier in the lesson. A miss shows the
# answer and comes back before the round ends. After the last round the words count as learned.

LESSON_SIZE = 3
LESSON_REVIEWS = 2  # earlier forms mixed into each quiz round


def big(text: str, pos: str) -> str:
    """Arabic as a heading line (bigger in Discord), other languages in bold."""
    return f"## {text}" if pos == "ar" else f"**{text}**"


@dataclass
class LessonSession:
    user_id: int
    list_key: int
    list_name: str
    pos: str
    tense: str
    words: list[str]
    forms: dict[str, dict[str, str]]  # word -> {form label: answer}
    meanings: dict[str, str]
    # What's left, next first: ["copy" or "quiz", word, form label, 1 if it's a retry after a miss].
    steps: list[list] = field(default_factory=list)
    total: int = 0       # steps in the plan (not counting retries)
    quiz_total: int = 0  # quiz questions in the plan
    first_try: int = 0   # quiz questions answered right the first time
    kind: str = "lesson"

    @classmethod
    def build(cls, user_id: int, list_key: int, tense: str, words: list[str], meanings: dict[str, str]):
        wl = crawler.jsondict[list_key]
        forms = {w: dict(wl['words'][w][tense]) for w in words}
        labels = list(dict.fromkeys(label for w in words for label in forms[w]))
        steps, earlier = [], []
        for label in labels:
            new = [(w, label) for w in words if label in forms[w]]
            steps += [["copy", w, l, 0] for w, l in new]
            round_ = new + random.sample(earlier, min(LESSON_REVIEWS, len(earlier)))
            random.shuffle(round_)
            steps += [["quiz", w, l, 0] for w, l in round_]
            earlier += new
        quizzes = sum(step[0] == "quiz" for step in steps)
        return cls(user_id, list_key, wl['name'], wl['pos'], tense, words, forms, meanings,
                   steps, len(steps), quizzes)

    @property
    def step(self) -> list | None:
        return self.steps[0] if self.steps else None

    @property
    def current(self) -> Prompt | None:
        """The quiz question being asked, if the current step is a quiz."""
        if not self.steps or self.steps[0][0] != "quiz":
            return None
        _, word, label, _ = self.steps[0]
        return Prompt(word, self.tense, label, self.forms[word][label], self.pos)

    def retry_later(self):
        """Move the current quiz question to the end of its round, marked as a retry."""
        kind, word, label, _ = self.steps.pop(0)
        end_of_round = next((i for i, s in enumerate(self.steps) if s[0] == "copy"), len(self.steps))
        self.steps.insert(end_of_round, [kind, word, label, 1])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LessonSession":
        if "steps" not in data:  # saved by the first version of lessons: copy everything, then quiz
            data = dict(data)
            data["steps"] = [["copy", w, l, 0] for w, l in data.pop("copy_queue")] + \
                            [["quiz", p["word"], p["form"], 0] for p in data.pop("quiz_queue")]
            data["total"] = data.pop("copy_total") + data["quiz_total"]
            data.pop("missed", None)
        return cls(**data)

    def step_text(self) -> str:
        kind, word, label, retry = self.steps[0]
        done = min(self.total - len([s for s in self.steps if not s[3]]) + 1, self.total)
        counter = "`↻ again`" if retry else f"`{done}/{self.total}`"
        if kind == "quiz":
            return f"{counter} 📝 {self.current.question}"
        meaning = f" · *{self.meanings[word]}*" if self.meanings.get(word) else ""
        form = f" · {label}" if label else ""
        return (f"{counter} ✏️ **{self.tense}{form}**\n{word}{meaning}\n"
                f"{big(self.forms[word][label], self.pos)}\n-# Type it to copy it")


class NextLessonButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"learn:(?P<list>\d+):(?P<tense>[^:]+)"):
    """Under a finished lesson: start the next one for the same list and tense (works after restarts)."""

    def __init__(self, list_key: int, tense: str, left: int | None = None):
        label = f"Next lesson ({left} more word{'s' if left != 1 else ''})" if left else "Next lesson"
        super().__init__(discord.ui.Button(label=label, emoji="📖", style=discord.ButtonStyle.primary,
                                           custom_id=f"learn:{list_key}:{tense}"[:100]))
        self.list_key, self.tense = list_key, tense

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["list"]), match["tense"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.next_lesson_button(interaction, self.list_key, self.tense)


# ---- Bot ----

class Client(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Quiz state is per channel, so separate channels can quiz at the same time.
        self.setups: dict[int, QuizSetup] = {}
        self.sessions: dict[int, QuizSession] = {}  # keyed by the quiz's thread (or channel) id
        self.stats = Stats()
        self.store = QuizStore()  # saves running quizzes and retry lists across restarts
        self.command_handlers = {
            "!help": self.cmd_help,
            "!lists": self.cmd_lists,
            "!words": self.cmd_words,
            "!tenses": self.cmd_quizzes,
            "!quizzes": self.cmd_quizzes,  # old name for !tenses
            "!new": self.cmd_new,
            "!add": self.cmd_add,
            "!remove": self.cmd_remove,
            "!rename": self.cmd_rename,
            "!deletelist": self.cmd_deletelist,
            "!quiz": self.cmd_quiz,
            "!reset": self.cmd_reset,
            "!start": self.cmd_start,
            "!stats": self.cmd_stats,
            "!leaderboard": self.cmd_leaderboard,
            "!weak": self.cmd_weak,
            "!conjugate": self.cmd_conjugate,
            "!learn": self.cmd_learn,
        }
        crawler.refresh_dict_from_json()
        crawler.load_meanings()
        self.tree = app_commands.CommandTree(self)
        add_slash_commands(self)

    async def setup_hook(self):
        self.add_dynamic_items(QuestionButton, RetryButton, PageButton, NextLessonButton)
        self.restore_sessions()
        # Register the slash commands with Discord. The list-editing commands are registered
        # only in the home server, so they don't show up anywhere else.
        if GUILD_ID:  # testing: everything in one server, instantly
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(f"Synced {len(synced)} slash commands to server {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            log.info(f"Synced {len(synced)} global slash commands")
        if HOME_GUILD and HOME_GUILD_ID != GUILD_ID:
            synced = await self.tree.sync(guild=HOME_GUILD)
            log.info(f"Synced {len(synced)} list-editing commands to home server {HOME_GUILD_ID}")
        if not HOME_GUILD:
            log.warning("No home server set (DISCORD_HOME_GUILD_ID): nobody can edit word lists.")

    async def on_ready(self):
        log.info(f"Logged on as {self.user}")

    def restore_sessions(self):
        """Pick up the quizzes that were running when the bot last stopped."""
        try:
            self.store.prune()
            for channel_id, state in self.store.load_sessions().items():
                kind = LessonSession if state.get("kind") == "lesson" else QuizSession
                self.sessions[channel_id] = kind.from_dict(state)
        except Exception as e:
            log.warning(f"could not restore quizzes: {e!r}")
        if self.sessions:
            log.info(f"Resumed {len(self.sessions)} quiz{'zes' if len(self.sessions) != 1 else ''}")

    def persist(self, channel_id: int):
        """Save (or forget) the quiz in a channel. Synchronous, like all session changes."""
        session = self.sessions.get(channel_id)
        try:
            if session:
                self.store.save_session(channel_id, session.to_dict())
            else:
                self.store.delete_session(channel_id)
        except Exception as e:
            log.warning(f"could not save quiz: {e!r}")

    async def on_message(self, message: Message):
        if message.author.bot:
            return
        session = self.sessions.get(message.channel.id)
        if isinstance(session, LessonSession):
            await self.handle_lesson_message(message, session)
        elif session:
            await self.handle_quiz_message(message, session)
        else:
            await self.handle_command(message)

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        # Drop a quiz whose thread was archived (by hand or after going idle).
        if after.archived and self.sessions.pop(after.id, None):
            self.persist(after.id)

    async def on_thread_delete(self, thread: discord.Thread):
        if self.sessions.pop(thread.id, None):
            self.persist(thread.id)

    # ---- Quiz mode ----

    async def handle_quiz_message(self, message: Message, session: QuizSession):
        # Every change to the session happens before the first await. Discord
        # events run concurrently, so if we awaited first, two people answering
        # at once could both score the same question and skip the next one.
        # question_button follows the same rule.
        text = message.content.strip()
        channel = message.channel
        prompt = session.current
        if text == "!stop":
            self.sessions.pop(channel.id, None)
            self.persist(channel.id)
            await self.end_quiz(channel, session, stopped=True)
            return
        if text == "!ans":
            self.reveal(channel, session, message.author)
            self.persist(channel.id)
            await self.after_reveal(channel, session, prompt)
            return
        if text == "!skip":
            session.skip()
            self.persist(channel.id)
            await self.after_skip(channel, session)
            return
        if text.startswith("!"):
            await channel.send("Quiz in progress: answer, or use the buttons (`!ans`, `!skip`, `!stop`).")
            return
        if is_right(prompt, text, session.tashkeel):
            session.player(message.author).correct += 1
            self.save_stat(channel, message.author, prompt, "correct")
            if session.wrong_tries == 0:
                session.correct += 1
            else:
                session.missed.append(prompt)
            self.next_prompt(channel, session)
            self.persist(channel.id)
            await message.add_reaction("✅")
        elif session.previous and is_right(session.previous, text, session.tashkeel):
            await message.add_reaction("⏱️")  # right, but someone else got there first
            return
        elif is_chatter(message, prompt.answer, self.user):
            return  # just chat: no ❌, and it doesn't count as a guess
        else:
            session.wrong_tries += 1
            session.player(message.author).wrong += 1
            marks = table_marks(prompt, text, session.tashkeel) if prompt.forms else None
            if not prompt.forms:
                self.save_stat(channel, message.author, prompt, "wrong")
            elif marks:  # stats only for the forms that were wrong
                for part, ok in zip(prompt.parts(), marks):
                    if not ok:
                        self.save_stat(channel, message.author, part, "wrong")
            self.persist(channel.id)
            await message.add_reaction("❌")
            if prompt.forms:
                await message.reply(table_feedback(prompt, text, marks), mention_author=False)
            elif session.tashkeel and is_right(prompt, text):
                await message.reply("The letters are right, but the tashkeel isn't.", mention_author=False)
            return
        await self.send_current(channel, session)

    async def question_button(self, interaction: discord.Interaction, action: str, token: int, serial: int):
        """Reveal / Skip / Stop. Like typed answers, all session changes happen before the first await."""
        channel, user = interaction.channel, interaction.user
        session = self.sessions.get(interaction.channel_id)
        if not isinstance(session, QuizSession) or session.token != token or session.serial != serial:
            await interaction.response.send_message("That question is already done.", ephemeral=True)
            return
        prompt = session.current
        session.question_message_id = None  # this click removes the buttons below
        if action == "reveal":
            self.reveal(channel, session, user)
        elif action == "skip":
            session.skip()
        else:
            self.sessions.pop(interaction.channel_id, None)
        self.persist(interaction.channel_id)
        await interaction.response.edit_message(view=None)  # also acknowledges the click
        if action == "reveal":
            await self.after_reveal(channel, session, prompt, user)
        elif action == "skip":
            await self.after_skip(channel, session, user)
        else:
            await self.end_quiz(channel, session, stopped=True, by=user)

    async def retry_button(self, interaction: discord.Interaction, retry_id: int):
        channel_id, user = interaction.channel_id, interaction.user
        if channel_id in self.sessions:
            await interaction.response.send_message("A quiz is already running here.", ephemeral=True)
            return
        retry = self.store.get_retry(retry_id)
        if retry is None:
            await interaction.response.send_message("That retry has expired.", ephemeral=True)
            return
        label, prompts, used_by, tashkeel = retry
        # claim_retry is atomic, so a double click can't start two quizzes.
        if used_by or not self.store.claim_retry(retry_id, user.display_name):
            await interaction.response.send_message("That retry has already started.", ephemeral=True)
            return
        await interaction.response.edit_message(view=used_retry_view(user.display_name))
        label = label if label.endswith("(retry)") else f"{label} (retry)"
        await self.start_quiz(interaction.message, [Prompt(**p) for p in prompts], label, tashkeel=tashkeel)

    def reveal(self, channel, session: QuizSession, user):
        self.save_stat(channel, user, session.current, "revealed")
        session.missed.append(session.current)
        self.next_prompt(channel, session)

    def save_stat(self, channel, user, prompt: Prompt, result: str):
        # Synchronous on purpose (it's a quick local write), so it keeps the
        # "no awaits before the session is updated" rule in handle_quiz_message.
        # A full table counts once per form, so /stats and /weak still see single forms.
        try:
            for part in prompt.parts():
                self.stats.record(guild_id(channel), user.id, user.display_name, part, result)
        except Exception as e:
            log.warning(f"could not save stats: {e!r}")

    async def after_reveal(self, channel, session: QuizSession, prompt: Prompt, user=None):
        by = f" (revealed by {user.display_name})" if user else ""
        display = prompt.answer_display
        await channel.send(f"Answer{by}:{'' if display.startswith(chr(10)) else ' '}{display}")
        await self.send_current(channel, session)

    async def after_skip(self, channel, session: QuizSession, user=None):
        by = f" by {user.display_name}" if user else ""
        later = "it'll come back later" if session.prompts else "it's the last one"
        await channel.send(f"Skipped{by}, {later}.")
        await self.send_current(channel, session)

    def next_prompt(self, channel, session: QuizSession):
        """Move to the next question, ending the session (synchronously) if there is none."""
        if session.advance() is None:
            self.sessions.pop(channel.id, None)

    async def clear_question_buttons(self, channel, session: QuizSession):
        message_id, session.question_message_id = session.question_message_id, None
        if message_id:
            try:
                await channel.get_partial_message(message_id).edit(view=None)
            except discord.HTTPException:
                pass

    async def send_current(self, channel, session: QuizSession):
        await self.clear_question_buttons(channel, session)
        if session.current is None:
            await self.end_quiz(channel, session)
            return
        serial = session.serial
        marks_note = "\n-# with tashkeel" if session.tashkeel and session.current.pos == "ar" else ""
        message = await channel.send(f"`{session.number}/{session.total}` {session.current.question}{marks_note}",
                                     view=question_view(session))
        if session.serial == serial and self.sessions.get(channel.id) is session:  # still the open question
            session.question_message_id = message.id
            self.persist(channel.id)
        else:  # answered while we were sending it
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                pass

    async def end_quiz(self, channel, session: QuizSession, stopped: bool = False, by=None):
        """Post the results. The caller has already removed the session."""
        if session.players:
            try:
                self.stats.finish_quiz(guild_id(channel), {uid: p.name for uid, p in session.players.items()})
            except Exception as e:
                log.warning(f"could not save stats: {e!r}")
        await self.clear_question_buttons(channel, session)
        embed = results_embed(session, stopped)
        if by:
            embed.title += f" by {by.display_name}"
        view = None
        if session.missed:
            # Without repeats (the same form can be missed in two lists).
            missed = list({p.key: p for p in session.missed}.values())
            try:
                retry_id = self.store.add_retry(session.label, [asdict(p) for p in missed], session.tashkeel)
                view = retry_view(retry_id, len(missed))
            except Exception as e:
                log.warning(f"could not save retry: {e!r}")
        # Private threads (and quizzes without a thread) keep their results where they are: the
        # thread isn't archived, so the retry button starts the next round in the same thread.
        if session.public_thread and isinstance(channel, discord.Thread) and channel.parent:
            # The thread is about to be archived, so post the results (and the
            # retry button, which opens a new thread) in the main channel too.
            await channel.send(embed=embed)
            try:
                await channel.parent.send(f"Results from {channel.mention}", embed=embed, view=view)
            except discord.HTTPException as e:
                log.warning(f"could not post results to parent channel: {e!r}")
                if view:
                    await channel.send("Retry the ones you missed:", view=view)
            try:
                await channel.edit(archived=True)
            except discord.HTTPException as e:
                log.warning(f"could not archive quiz thread: {e!r}")
        elif view:
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)

    # ---- Command mode ----

    async def handle_command(self, message: Message):
        command, _, args = message.content.strip().partition(' ')
        handler = self.command_handlers.get(command)
        if not handler:
            return
        try:
            await handler(message, args.strip())
        except UsageError as e:
            await message.channel.send(str(e))
        except Exception:
            log.exception("%s failed (message: %r)", command, message.content)
            try:
                await message.channel.send("Something went wrong running that command.")
            except discord.HTTPException:
                pass

    def setup_for(self, channel) -> QuizSetup:
        return self.setups.setdefault(channel.id, QuizSetup())

    async def cmd_help(self, message: Message, args: str):
        """!help - show this message"""
        # dict.fromkeys drops repeats, so a command with two names is listed once.
        # Editing commands only work in the home server, so only list them there.
        editing = {self.cmd_new, self.cmd_add, self.cmd_remove, self.cmd_rename, self.cmd_deletelist}
        lines = [handler.__doc__ for handler in dict.fromkeys(self.command_handlers.values())
                 if is_home(message.guild) or handler not in editing]
        lines += ["During a quiz: answer, or use the buttons. Chat freely: replies, @mentions, sentences and "
                  "messages in another alphabet don't count as guesses. "
                  "`!ans` reveals the answer, `!skip` saves the question for later, `!stop` ends the quiz.",
                  "All of these are also slash commands: type `/` to see them.",
                  f"-# {CREDITS}"]
        await message.channel.send('\n'.join(lines))

    async def cmd_lists(self, message: Message, args: str):
        """!lists - show all word lists"""
        await message.channel.send(**send_kwargs(*paged("lists")))

    async def cmd_words(self, message: Message, args: str):
        """!words <list> - show the words in a list"""
        await message.channel.send(**send_kwargs(*paged("words", str(parse_list_index(args)))))

    async def cmd_quizzes(self, message: Message, args: str):
        """!tenses <list | pos> - show the tenses/cases you can quiz"""
        if not args:
            raise UsageError("Usage: `!tenses <list>` or `!tenses <pos>` (e.g. `!tenses run`)")
        pos = crawler.jsondict[parse_list_index(args)]['pos'] if args.isdigit() else args
        await message.channel.send(tenses_text(pos))

    async def cmd_new(self, message: Message, args: str):
        """!new <pos> <name> - create a new word list"""
        require_home(message.guild)
        pos, _, name = args.partition(' ')
        await message.channel.send(new_list(pos, name))

    async def cmd_add(self, message: Message, args: str):
        """!add <list> <word> [word ...] - add words to a list"""
        require_home(message.guild)
        if not args:
            raise UsageError("Usage: `!add <list> <word> [word ...]`")
        list_arg, *words = args.split()
        await message.channel.send(await add_words(parse_list_index(list_arg), words, message.author.id))

    async def cmd_remove(self, message: Message, args: str):
        """!remove <list> <word> [word ...] - remove words from a list (Manage Messages only)"""
        require_home(message.guild)
        require_manager(message.author)
        if not args:
            raise UsageError("Usage: `!remove <list> <word> [word ...]`")
        list_arg, *words = args.split()
        await message.channel.send(remove_words(parse_list_index(list_arg), words))

    async def cmd_rename(self, message: Message, args: str):
        """!rename <list> <new name> - rename a list (Manage Messages only)"""
        require_home(message.guild)
        require_manager(message.author)
        list_arg, _, name = args.partition(' ')
        if not list_arg:
            raise UsageError("Usage: `!rename <list> <new name>`")
        await message.channel.send(rename_list(parse_list_index(list_arg), name))

    async def cmd_deletelist(self, message: Message, args: str):
        """!deletelist <list> - delete a list and all its words (Manage Messages only, asks first)"""
        require_home(message.guild)
        require_manager(message.author)
        if not args:
            raise UsageError("Usage: `!deletelist <list>`")
        list_key = parse_list_index(args)
        await message.channel.send(f"Delete {describe_list(list_key)}? This can't be undone.",
                                   view=ConfirmDeleteView(self, list_key, message.author.id))

    def delete_list(self, list_key: int) -> str:
        """Delete a list. Later lists move up a number, so queued quizzes are updated to match."""
        text = f"Deleted {describe_list(list_key)}."
        crawler.jsondict.pop(list_key)
        crawler.refresh_json_from_dict()
        for setup in self.setups.values():
            setup.lists = [k - (k > list_key) for k in setup.lists if k != list_key]
        if list_key < len(crawler.jsondict):
            text += f" Lists after it moved up one number (list {list_key + 1} is now {list_key})."
        return text

    async def cmd_quiz(self, message: Message, args: str):
        """!quiz <list ...> <tense>[, tense ...] - queue lists and/or tenses, e.g. `!quiz 0 1 Present, Past`"""
        if not args:
            raise UsageError(self.cmd_quiz.__doc__)
        setup = self.setup_for(message.channel)
        tokens = args.split()
        list_keys = [parse_list_index(t) for t in tokens if t.isdigit()]
        tenses = parse_tenses(' '.join(t for t in tokens if not t.isdigit()))
        setup.lists += [k for k in dict.fromkeys(list_keys) if k not in setup.lists]
        setup.tenses += [t for t in tenses if t not in setup.tenses]
        lists = ', '.join(crawler.jsondict[k]['name'] for k in setup.lists) or "(none)"
        await message.channel.send(f"Lists: {lists}\nTenses: {', '.join(setup.tenses) or '(none)'}")

    async def cmd_reset(self, message: Message, args: str):
        """!reset - clear the queued lists and tenses"""
        self.setups.pop(message.channel.id, None)
        await message.channel.send("Quiz queue cleared.")

    async def cmd_start(self, message: Message, args: str):
        """!start [count] [private] [table] [tashkeel] - start a quiz on the queued lists and tenses, e.g. `!start 20 table` (table: type every form of a tense in one answer; tashkeel: Arabic answers need the vowel marks)"""
        setup = self.setup_for(message.channel)
        count, flags = parse_start_args(
            args, "Usage: `!start [number of questions] [private] [table] [tashkeel]`, "
                  "e.g. `!start 20` or `!start private table`", ("private", "table", "tashkeel"))
        if count:
            setup.count = count
        if args:
            setup.table = "table" in flags
            setup.tashkeel = "tashkeel" in flags
        prompts, label, available = prepare_quiz(setup.lists, setup.tenses, setup.count, setup.table)
        label = apply_tashkeel(prompts, label, setup.tashkeel)
        if "private" in flags:
            thread = await self.start_private_quiz(message.channel, message.author, prompts, label, setup.tashkeel)
            await message.reply(f"Your private quiz is ready: {thread.mention} ({question_count(prompts, available)})")
            return
        if len(prompts) < available:
            await message.channel.send(f"Quizzing {question_count(prompts, available)}.")
        await self.start_quiz(message, prompts, label, setup.tashkeel)

    async def cmd_stats(self, message: Message, args: str):
        """!stats [@user] - show saved quiz stats"""
        user = message.mentions[0] if message.mentions else message.author
        await message.channel.send(embed=stats_embed(self.stats, guild_id(message.channel), user))

    async def cmd_weak(self, message: Message, args: str):
        """!weak [count] [private] - quiz the words you miss most, e.g. `!weak 10 private`"""
        count, flags = parse_start_args(
            args, "Usage: `!weak [number of questions] [private]`, e.g. `!weak 10 private`")
        private = "private" in flags
        prompts = weak_prompts(self.stats, guild_id(message.channel), message.author.id, [], [],
                               count or WEAK_DEFAULT_COUNT)
        label = f"{message.author.display_name}'s weak words"
        if private:
            thread = await self.start_private_quiz(message.channel, message.author, prompts, label)
            await message.reply(f"Your private quiz is ready: {thread.mention}")
        else:
            await self.start_quiz(message, prompts, label)

    async def cmd_conjugate(self, message: Message, args: str):
        """!conjugate <word> - show every saved form of a word"""
        if not args:
            raise UsageError("Usage: `!conjugate <word>`, e.g. `!conjugate читать`")
        await message.channel.send(embeds=[conjugation_embed(e) for e in find_word(args)][:10])

    async def cmd_leaderboard(self, message: Message, args: str):
        """!leaderboard - top players in this server"""
        await message.channel.send(embed=leaderboard_embed(self.stats, guild_id(message.channel)))

    async def start_quiz(self, message: Message, prompts: list[Prompt], label: str, tashkeel: bool = False):
        """Start a quiz in a thread on `message` (or in its channel if a thread isn't possible)."""
        channel, public_thread = await self.quiz_channel(message, label)
        await self.run_quiz(channel, prompts, label, public_thread, tashkeel)

    # ---- Lessons ----

    async def fetch_meanings(self, pos: str, words: list[str]) -> dict[str, str]:
        """English meanings for some words, looked up (politely) the first time and then cached."""
        missing = [w for w in words if f"{pos}:{w}" not in crawler.meanings]
        for word in missing:
            # Arabic: pass the vowelled past tense, to pick the right verb on a shared page.
            tenses = next((wl['words'][word] for wl in crawler.jsondict if wl['pos'] == pos and word in wl['words']), {})
            lemma = (tenses or {}).get("Past", {}).get("هو") if pos == "ar" else None
            async with lookup_lock:
                try:
                    meaning = await asyncio.to_thread(crawler.english_meaning, word, pos, lemma)
                except Exception as e:
                    log.warning("looking up the meaning of %s failed: %r", word, e)
                    meaning = None
                await asyncio.sleep(LOOKUP_DELAY)
            if meaning is not None:
                crawler.meanings[f"{pos}:{word}"] = meaning
        if missing:
            try:
                crawler.save_meanings()
            except Exception as e:
                log.warning(f"could not save meanings: {e!r}")
        return {w: crawler.meanings.get(f"{pos}:{w}", "") for w in words}

    def lesson_words(self, gid: int, user_id: int, list_key: int, tense: str) -> list[str]:
        """Words in a list that have this tense and the person hasn't learned yet, in list order."""
        wl = crawler.jsondict[list_key]
        if tense not in avail_quizes.get(wl['pos'], []):
            raise UsageError(f"{wl['name']} has no {tense}. Try: {', '.join(avail_quizes.get(wl['pos'], []))}")
        learned = self.stats.learned_words(gid, user_id, wl['pos'], tense)
        return [w for w, t in wl['words'].items() if t and t.get(tense) and w not in learned]

    async def prepare_lesson(self, channel, user, list_key: int, tense: str, size: int = LESSON_SIZE) -> LessonSession:
        words = self.lesson_words(guild_id(channel), user.id, list_key, tense)[:size]
        if not words:
            name = crawler.jsondict[list_key]['name']
            raise UsageError(f"You've learned every word in **{name}** for {tense}. "
                             f"Practise them with `/quiz lists:{list_key} tenses:{tense}`.")
        meanings = await self.fetch_meanings(crawler.jsondict[list_key]['pos'], words)
        return LessonSession.build(user.id, list_key, tense, words, meanings)

    async def lesson_channel(self, channel, user, session: LessonSession):
        """A private thread for the lesson, or the current channel (DMs, threads, no permission)."""
        if not isinstance(channel, discord.TextChannel):
            return channel, False
        try:
            thread = await channel.create_thread(name=f"Lesson: {session.list_name} · {session.tense}"[:100],
                                                 auto_archive_duration=60, type=discord.ChannelType.private_thread)
            await thread.add_user(user)
            return thread, True
        except discord.HTTPException as e:
            log.warning(f"could not create lesson thread: {e!r}")
            return channel, False

    async def run_lesson(self, channel, session: LessonSession):
        if channel.id in self.sessions:
            raise UsageError("A quiz or lesson is already running here.")
        self.sessions[channel.id] = session
        self.persist(channel.id)
        lines = [f"**{w}**" + (f" · *{session.meanings[w]}*" if session.meanings.get(w) else "") for w in session.words]
        embed = discord.Embed(title=f"Lesson · {session.list_name} · {session.tense}", colour=discord.Colour.teal(),
                              description='\n'.join(lines))
        embed.add_field(name="How it works", inline=False, value=(
            "One form at a time:\n"
            "✏️ **Copy** the new form for each word, as it's shown.\n"
            "📝 **Quiz**: answer those, plus a few from earlier, without help. "
            "A miss shows the answer and comes back before the next form.\n"
            "Type `!skip` to skip copying a form, or `!stop` to stop."))
        embed.set_footer(text=f"{len(session.words)} words · {session.total} steps")
        await channel.send(embed=embed)
        await channel.send(session.step_text())

    async def handle_lesson_message(self, message: Message, session: LessonSession):
        # Same rule as quizzes: change the session before the first await.
        if message.author.id != session.user_id:
            return  # other people can chat; only the learner's messages count
        text, channel = message.content.strip(), message.channel
        if text == "!stop":
            self.sessions.pop(channel.id, None)
            self.persist(channel.id)
            await channel.send("Lesson stopped. These words weren't marked as learned, so `/learn` will offer them again.")
            return
        kind, word, label, retry = session.step
        answer = session.forms[word][label]
        if kind == "copy":
            if text == "!skip" or matches(text, answer):
                session.steps.pop(0)
                self.persist(channel.id)
                if text != "!skip":
                    await message.add_reaction("✅")
            elif text.startswith("!") or is_chatter(message, answer, self.user):
                return
            else:
                await message.add_reaction("❌")
                await message.reply(f"Copy it exactly:\n{big(answer, session.pos)}", mention_author=False)
                return
        else:
            prompt = session.current
            if is_right(prompt, text):
                session.steps.pop(0)
                session.first_try += not retry
                self.save_stat(channel, message.author, prompt, "correct")
                self.persist(channel.id)
                await message.add_reaction("✅")
            elif text.startswith("!") or is_chatter(message, answer, self.user):
                return
            else:
                session.retry_later()  # comes back before the next new form
                self.save_stat(channel, message.author, prompt, "wrong")
                self.persist(channel.id)
                await message.add_reaction("❌")
                display = prompt.answer_display
                await message.reply(f"It's:{'' if display.startswith(chr(10)) else ' '}{display}\n"
                                    f"-# It'll come back in a moment.", mention_author=False)
        if session.steps:
            await channel.send(session.step_text())
        else:
            await self.finish_lesson(channel, message.author, session)

    async def finish_lesson(self, channel, user, session: LessonSession):
        self.sessions.pop(channel.id, None)
        self.persist(channel.id)
        gid = guild_id(channel)
        try:
            self.stats.mark_learned(gid, user.id, session.pos, session.tense, session.words)
        except Exception as e:
            log.warning(f"could not save learned words: {e!r}")
        left = len(self.lesson_words(gid, user.id, session.list_key, session.tense)) \
            if session.list_key < len(crawler.jsondict) and crawler.jsondict[session.list_key]['name'] == session.list_name else 0
        first_try = session.first_try
        lines = [f"**{w}**" + (f" · *{session.meanings[w]}*" if session.meanings.get(w) else "") for w in session.words]
        embed = discord.Embed(title=f"Lesson complete · {session.tense}", colour=discord.Colour.green(),
                              description='\n'.join(lines))
        embed.add_field(name="Quiz", value=f"{first_try}/{session.quiz_total} right first time", inline=True)
        embed.add_field(name=session.list_name, inline=True,
                        value=f"{left} word{'s' if left != 1 else ''} left to learn" if left else "All learned! 🎉")
        view = None
        if left:
            view = discord.ui.View(timeout=None)  # never stop()ed, see question_view
            view.add_item(NextLessonButton(session.list_key, session.tense, min(left, LESSON_SIZE)))
        await channel.send(embed=embed, **({"view": view} if view else {}))

    async def start_lesson(self, channel, user, list_key: int, tense: str, size: int = LESSON_SIZE):
        """Prepare a lesson and start it in a private thread (or here). Returns where it runs."""
        session = await self.prepare_lesson(channel, user, list_key, tense, size)
        where, _ = await self.lesson_channel(channel, user, session)
        await self.run_lesson(where, session)
        return where

    async def next_lesson_button(self, interaction: discord.Interaction, list_key: int, tense: str):
        if interaction.channel_id in self.sessions:
            await interaction.response.send_message("A quiz or lesson is already running here.", ephemeral=True)
            return
        await interaction.response.edit_message(view=None)  # acknowledges the click; one press per summary
        try:
            if list_key >= len(crawler.jsondict):
                raise UsageError("That list no longer exists.")
            # Inside the lesson's thread, the next lesson runs right here.
            await self.start_lesson(interaction.channel, interaction.user, list_key, tense)
        except UsageError as e:
            await interaction.followup.send(str(e), ephemeral=True)

    async def cmd_learn(self, message: Message, args: str):
        """!learn <list> <tense> [words] - learn new words: copy each form, then a quiz, e.g. `!learn 0 Present`"""
        tokens = args.split()
        size = int(tokens.pop()) if len(tokens) > 2 and tokens[-1].isdigit() else LESSON_SIZE
        if len(tokens) < 2:
            raise UsageError("Usage: `!learn <list> <tense> [number of words]`, e.g. `!learn 0 Present`")
        list_key = parse_list_index(tokens[0])
        tense = TENSE_LOOKUP.get(' '.join(tokens[1:]).lower())
        if tense is None:
            raise UsageError(f'Unknown tense "{" ".join(tokens[1:])}". Use `!tenses {list_key}` to see them.')
        if message.channel.id in self.sessions:
            raise UsageError("A quiz or lesson is already running here.")
        where = await self.start_lesson(message.channel, message.author, list_key, tense, max(1, min(size, 8)))
        if where is not message.channel:
            await message.reply(f"Your lesson is ready: {where.mention}")

    async def start_private_quiz(self, channel, user, prompts: list[Prompt], label: str,
                                 tashkeel: bool = False) -> discord.Thread:
        """Start a quiz in a new private thread that only `user` (plus anyone they @mention, and mods) can see."""
        if not isinstance(channel, discord.TextChannel):
            raise UsageError("Private quizzes can only be started from a server text channel.")
        try:
            thread = await channel.create_thread(name=f"Practice: {label}"[:100], auto_archive_duration=60,
                                                 type=discord.ChannelType.private_thread)
            await thread.add_user(user)
        except discord.HTTPException as e:
            log.warning(f"could not create private thread: {e!r}")
            raise UsageError("I couldn't create a private thread here. "
                             "Give me the Create Private Threads permission and try again.")
        await self.run_quiz(thread, prompts, label, tashkeel=tashkeel)
        return thread

    async def run_quiz(self, channel, prompts: list[Prompt], label: str, public_thread: bool = False,
                       tashkeel: bool = False):
        session = QuizSession(list(prompts), label=label, public_thread=public_thread,  # a copy: it pops
                              tashkeel=tashkeel)
        self.sessions[channel.id] = session
        session.advance()
        self.persist(channel.id)
        await self.send_current(channel, session)

    async def quiz_channel(self, message: Message, label: str) -> tuple:
        """Open a thread for the quiz, or fall back to the current channel (DMs, threads, no permission).

        Returns (channel, whether it's a new public thread)."""
        if not isinstance(message.channel, discord.TextChannel):
            return message.channel, False
        name = f"Quiz: {label}"[:100]  # Discord's thread name limit
        try:
            thread = await message.create_thread(name=name, auto_archive_duration=60)
        except discord.HTTPException as e:
            log.warning(f"could not create quiz thread: {e!r}")
            await message.channel.send("I can't create threads here, so the quiz will run in this channel. "
                                       "(Give me the Create Public Threads permission to fix this.)")
            return message.channel, False
        return thread, True


# ---- Word lookup (/conjugate) ----

@dataclass
class WordEntry:
    pos: str
    word: str
    tenses: dict  # tense -> {form label: answer}
    lists: list[str]  # names of the lists it's in


def word_entries() -> list[WordEntry]:
    """Every saved word, once per part of speech (the same word can be in several lists)."""
    entries: dict[tuple[str, str], WordEntry] = {}
    for wl in crawler.jsondict:
        for word, word_tenses in wl['words'].items():
            entry = entries.setdefault((wl['pos'], word), WordEntry(wl['pos'], word, {}, []))
            entry.tenses = entry.tenses or (word_tenses or {})
            entry.lists.append(wl['name'])
    return list(entries.values())


def find_word(query: str) -> list[WordEntry]:
    """Look a word up. Autocomplete sends 'pos:word'; typing just the word finds it in any list."""
    pos, _, word = query.strip().rpartition(':')
    matches = [e for e in word_entries()
               if normalize(e.word) == normalize(word) and (not pos or e.pos == pos)]
    # A vowelled word (دَرَّسَ) means that verb, not others with the same letters (درس).
    exact = [e for e in matches if normalize(e.word, keep_marks=True) == normalize(word, keep_marks=True)]
    if exact and normalize(word) != normalize(word, keep_marks=True):
        return exact
    if matches:
        return matches
    close = difflib.get_close_matches(normalize(word), {normalize(e.word) for e in word_entries()}, n=3)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise UsageError(f'"{word}" isn\'t in any list.{hint} Add it with `/add`.')


CREDITS = "Word forms from cooljugator.com and Wiktionary (CC BY-SA 4.0)."


def word_source(entry: WordEntry) -> tuple[str, str]:
    """(credit line, link to the source page) for a word's forms.

    Finnish words all come from Wiktionary. Arabic verbs from Wiktionary are the ones saved
    under their vowelled spelling
    (see crawler.lookup_arabic); everything else comes from cooljugator.com."""
    if entry.pos in crawler.WIKTIONARY_POSES:
        language = "Swedish" if entry.pos == "sv" else "Finnish"
        return ("Forms from Wiktionary contributors, en.wiktionary.org · CC BY-SA 4.0",
                crawler.WIKTIONARY_URL + urllib.parse.quote(entry.word) + "#" + language)
    if entry.pos == 'ar' and crawler.strip_tashkeel(entry.word) != entry.word:
        return ("Forms from Wiktionary contributors, en.wiktionary.org · CC BY-SA 4.0",
                crawler.WIKTIONARY_URL + urllib.parse.quote(crawler.strip_tashkeel(entry.word)))
    return "Forms from cooljugator.com", crawler.get_url(urllib.parse.quote(entry.word), entry.pos)


def conjugation_embed(entry: WordEntry) -> discord.Embed:
    credit, url = word_source(entry)
    meaning = crawler.meanings.get(f"{entry.pos}:{entry.word}")
    embed = discord.Embed(title=entry.word, url=url, colour=discord.Colour.blurple(),
                          description=(f"*{meaning}*\n" if meaning else "") +
                                      f"{pos_names.get(entry.pos, entry.pos)} · in {', '.join(entry.lists)}")
    embed.set_footer(text=credit)
    if not entry.tenses:
        embed.description += "\nNo forms saved: the online lookup failed when it was added."
        return embed
    # Quiz order first (e.g. Present before Past), then anything else the crawler saved.
    order = avail_quizes.get(entry.pos, [])
    tenses = [t for t in order if t in entry.tenses] + [t for t in entry.tenses if t not in order]
    for tense in tenses[:25]:  # Discord's field limit
        forms = entry.tenses[tense]
        value = '\n'.join(f"{label} · **{answer}**" if label else f"**{answer}**" for label, answer in forms.items())
        embed.add_field(name=tense, value=value[:1024] or "-", inline=True)
    return embed


# ---- Right-click "Add to word list" ----

MAX_SELECT_OPTIONS = 25  # Discord's limit per dropdown


def words_in(text: str) -> list[str]:
    """Distinct words in a message, lower-cased, in order. Hyphenated words stay whole."""
    found = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", text)
    return list(dict.fromkeys(w.lower() for w in found if len(w) > 1))


class AddWordsView(discord.ui.View):
    """Pick words from a message and a list to add them to. Only the person who right-clicked sees it."""

    def __init__(self, words: list[str]):
        super().__init__(timeout=600)
        saved = {}
        for e in word_entries():
            saved.setdefault(normalize(e.word), []).extend(e.lists)
        words = words[:MAX_SELECT_OPTIONS]
        preselect = len(words) <= 3  # a word or two: assume they want all of them
        self.chosen_words = list(words) if preselect else []
        self.chosen_list: int | None = None

        self.word_select = discord.ui.Select(
            placeholder="Words to add", min_values=1, max_values=len(words), row=0,
            options=[discord.SelectOption(
                label=w[:100], value=w[:100], default=preselect,
                description=f"Already in {', '.join(saved[normalize(w)])}"[:100] if normalize(w) in saved else None)
                for w in words])
        self.word_select.callback = self.on_words
        self.add_item(self.word_select)

        self.list_select = discord.ui.Select(
            placeholder="Add to which list?", row=1,
            options=[discord.SelectOption(label=f"{i} · {wl['name']}"[:100], value=str(i),
                                          description=pos_names.get(wl['pos'], wl['pos']))
                     for i, wl in enumerate(crawler.jsondict[:MAX_SELECT_OPTIONS])])
        self.list_select.callback = self.on_list
        self.add_item(self.list_select)

    async def on_words(self, interaction: discord.Interaction):
        self.chosen_words = list(self.word_select.values)
        await interaction.response.defer()

    async def on_list(self, interaction: discord.Interaction):
        self.chosen_list = int(self.list_select.values[0])
        await interaction.response.defer()

    @discord.ui.button(label="Add", emoji="➕", style=discord.ButtonStyle.success, row=2)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.chosen_words or self.chosen_list is None:
            await interaction.response.send_message("Pick at least one word and a list first.", ephemeral=True)
            return
        self.stop()
        name = crawler.jsondict[self.chosen_list]['name']
        words = ' '.join(self.chosen_words)
        await interaction.response.edit_message(content=f"Adding {words} to **{name}** (looking them up online)...",
                                                view=None)
        try:
            result = await add_words(self.chosen_list, self.chosen_words, interaction.user.id)
        except UsageError as e:
            result = str(e)
        await interaction.edit_original_response(content=f"{result}\nList: **{name}**")


# ---- Slash commands ----

def list_choice_name(i: int) -> str:
    wl = crawler.jsondict[i]
    n = len(wl['words'])
    return f"{i} · {wl['name']} ({pos_names.get(wl['pos'], wl['pos'])}, {n} word{'s' if n != 1 else ''})"


def matching_lists(text: str) -> list[int]:
    text = text.strip().lower()
    return [i for i, wl in enumerate(crawler.jsondict)
            if not text or str(i).startswith(text) or text in wl['name'].lower()
            or text in pos_names.get(wl['pos'], '').lower()]


async def word_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    text = normalize(current)
    entries = [e for e in word_entries() if text in normalize(e.word)]
    entries.sort(key=lambda e: (not normalize(e.word).startswith(text), e.word))  # prefix matches first
    return [app_commands.Choice(name=f"{e.word} · {pos_names.get(e.pos, e.pos)}"[:100], value=f"{e.pos}:{e.word}"[:100])
            for e in entries[:25]]


async def list_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=list_choice_name(i)[:100], value=str(i))
            for i in matching_lists(current)[:25]]


async def list_words_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Words in the list picked in the `list` option, separated by spaces."""
    try:
        words = list(crawler.jsondict[parse_list_index(interaction.namespace.list or "")]['words'])
    except (UsageError, TypeError):
        return []
    *done, last = current.split(' ') if current else [""]
    prefix = ' '.join(done) + (' ' if done else '')
    last = normalize(last)
    return [app_commands.Choice(name=(prefix + w)[:100], value=(prefix + w)[:100])
            for w in words if last in normalize(w) and w not in done][:25]


async def lists_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Comma-separated lists: completes the last item and keeps the ones before it."""
    *done, last = current.split(',') if current else [""]
    done = [d.strip() for d in done if d.strip()]
    prefix = ', '.join(done) + (', ' if done else '')
    choices = []
    for i in matching_lists(last):
        if str(i) in done:
            continue
        name = prefix + list_choice_name(i) if done else list_choice_name(i)
        choices.append(app_commands.Choice(name=name[:100], value=(prefix + str(i))[:100]))
    return choices[:25]


async def tenses_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Comma-separated tenses, limited to the ones that fit the lists already picked."""
    try:
        list_keys = parse_lists(interaction.namespace.lists or "")
    except UsageError:
        list_keys = []
    *done, last = current.split(',') if current else [""]
    done = [d.strip() for d in done if d.strip()]
    prefix = ', '.join(done) + (', ' if done else '')
    last = last.strip().lower()
    taken = {d.lower() for d in done}
    choices = []
    if not done and not last:
        everything = ', '.join(tenses_for_lists(list_keys))
        if len(everything) <= 100:
            choices.append(app_commands.Choice(name=f"All: {everything}"[:100], value=everything))
    for t in tenses_for_lists(list_keys):
        if last in t.lower() and t.lower() not in taken:
            choices.append(app_commands.Choice(name=(prefix + t)[:100], value=(prefix + t)[:100]))
    return choices[:25]


def add_slash_commands(client: "Client"):
    tree = client.tree

    def home_only(command_kind, **kwargs):
        """Register a list-editing command in the home server only (or not at all if there's none)."""
        if HOME_GUILD:
            return command_kind(guild=HOME_GUILD, **kwargs)
        return lambda func: func

    async def launch(interaction: discord.Interaction, prompts: list[Prompt], label: str, size: str, private: bool,
                     tashkeel: bool = False):
        if private:
            # Only the person who asked sees this reply; the quiz itself is in the private thread.
            await interaction.response.defer(ephemeral=True, thinking=True)
            thread = await client.start_private_quiz(interaction.channel, interaction.user, prompts, label, tashkeel)
            await interaction.followup.send(f"Your private quiz is ready: {thread.mention} ({size})", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"{interaction.user.mention} started a quiz: **{label}** ({size})")
            await client.start_quiz(await interaction.original_response(), prompts, label, tashkeel)

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(original, UsageError):
            text = str(original)
        else:
            log.error("/%s failed", interaction.command.name if interaction.command else "?", exc_info=original)
            text = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @tree.command(description="Show how to use the quiz bot")
    async def help(interaction: discord.Interaction):
        editing = ("**Edit lists:** `/new`, `/add`, or right-click a message → Apps → Add to word list. "
                   "Moderators: `/remove`, `/rename`, `/deletelist`\n" if is_home(interaction.guild)
                   else "**Edit lists:** the word lists are shared, and edited from the bot's home server.\n")
        await interaction.response.send_message(
            "**Start a quiz:** `/quiz`, pick lists and tenses (separate several with commas). "
            "Set `table` to answer every form of a tense at once. "
            "Set `private` to practise alone in a private thread.\n"
            "**During a quiz:** type answers in the quiz thread, or use Reveal / Skip / Stop. "
            "Replies, @mentions, sentences and messages in another alphabet count as chat, not guesses.\n"
            "**Browse:** `/lists`, `/words`, `/tenses`, `/conjugate`\n"
            "**Learn:** `/learn` new words: copy each form, then a quiz on them\n"
            "**Progress:** `/stats`, `/leaderboard`, `/weak` (quiz the words you miss most)\n"
            + editing +
            "The old `!` commands still work too (`!help`).\n"
            f"-# {CREDITS}", ephemeral=True)

    @tree.command(description="Show all word lists")
    async def lists(interaction: discord.Interaction):
        await interaction.response.send_message(**send_kwargs(*paged("lists")), ephemeral=True)

    @tree.command(description="Show the words in a list")
    @app_commands.describe(list="The word list")
    @app_commands.autocomplete(list=list_autocomplete)
    async def words(interaction: discord.Interaction, list: str):
        await interaction.response.send_message(**send_kwargs(*paged("words", str(parse_list_index(list)))),
                                                ephemeral=True)

    @home_only(tree.context_menu, name="Add to word list")
    async def add_to_word_list(interaction: discord.Interaction, message: discord.Message):
        words = words_in(message.content)
        if not words:
            raise UsageError("I couldn't find any words in that message.")
        if not crawler.jsondict:
            raise UsageError("There are no lists yet. Make one with `/new`.")
        extra = len(words) - MAX_SELECT_OPTIONS
        note = f" (showing the first {MAX_SELECT_OPTIONS} of {len(words)} words)" if extra > 0 else ""
        await interaction.response.send_message(f"Add words from this message to a list{note}:",
                                                view=AddWordsView(words), ephemeral=True)

    async def lesson_tense_autocomplete(interaction: discord.Interaction, current: str):
        try:
            pos = crawler.jsondict[parse_list_index(interaction.namespace.list or "")]['pos']
        except (UsageError, TypeError):
            return []
        return [app_commands.Choice(name=t, value=t) for t in avail_quizes.get(pos, [])
                if current.lower() in t.lower()][:25]

    @tree.command(description="Learn new words: copy each form, then a quiz on them")
    @app_commands.describe(list="The word list to learn from", tense="Which tense or case to learn",
                           words=f"How many new words in this lesson (default {LESSON_SIZE})")
    @app_commands.autocomplete(list=list_autocomplete, tense=lesson_tense_autocomplete)
    async def learn(interaction: discord.Interaction, list: str, tense: str,
                    words: app_commands.Range[int, 1, 8] = LESSON_SIZE):
        if interaction.channel_id in client.sessions:
            raise UsageError("A quiz or lesson is already running here.")
        list_key = parse_list_index(list)
        canonical = TENSE_LOOKUP.get(tense.lower())
        if canonical is None:
            raise UsageError(f'Unknown tense "{tense}". Use `/tenses` to see them.')
        client.lesson_words(guild_id(interaction.channel), interaction.user.id, list_key, canonical)  # quick checks
        await interaction.response.defer(ephemeral=True, thinking=True)  # meanings can take a few seconds
        where = await client.start_lesson(interaction.channel, interaction.user, list_key, canonical, words)
        await interaction.followup.send(f"Your lesson is ready: {where.mention}" if where != interaction.channel
                                        else "Your lesson has started.", ephemeral=True)

    @tree.command(description="Show every saved form of a word")
    @app_commands.describe(word="The word (suggestions come from your lists)")
    @app_commands.autocomplete(word=word_autocomplete)
    async def conjugate(interaction: discord.Interaction, word: str):
        embeds = [conjugation_embed(e) for e in find_word(word)][:10]
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    @tree.command(description="Show the tenses/cases you can quiz for a list")
    @app_commands.describe(list="The word list")
    @app_commands.autocomplete(list=list_autocomplete)
    async def tenses(interaction: discord.Interaction, list: str):
        pos = crawler.jsondict[parse_list_index(list)]['pos']
        await interaction.response.send_message(tenses_text(pos), ephemeral=True)

    @home_only(tree.command, description="Create a new word list")
    @app_commands.describe(language="What the list holds", name="A name for the list")
    @app_commands.choices(language=[app_commands.Choice(name=n, value=p) for p, n in pos_names.items()])
    async def new(interaction: discord.Interaction, language: app_commands.Choice[str], name: str):
        await interaction.response.send_message(new_list(language.value, name))

    # Moderators only by default (Manage Messages); server admins can change who can use these
    # under Server Settings → Integrations. (The ! versions check Manage Messages themselves.)
    manager_only = app_commands.default_permissions(manage_messages=True)

    @home_only(tree.command, description="Remove words from a list")
    @app_commands.describe(list="The word list", words="Words to remove, separated by spaces")
    @app_commands.autocomplete(list=list_autocomplete, words=list_words_autocomplete)
    @app_commands.guild_only()
    @manager_only
    async def remove(interaction: discord.Interaction, list: str, words: str):
        await interaction.response.send_message(remove_words(parse_list_index(list), words.split()))

    @home_only(tree.command, description="Rename a list")
    @app_commands.describe(list="The word list", name="Its new name")
    @app_commands.autocomplete(list=list_autocomplete)
    @app_commands.guild_only()
    @manager_only
    async def rename(interaction: discord.Interaction, list: str, name: str):
        await interaction.response.send_message(rename_list(parse_list_index(list), name))

    @home_only(tree.command, description="Delete a list and all its words (asks first)")
    @app_commands.describe(list="The word list to delete")
    @app_commands.autocomplete(list=list_autocomplete)
    @app_commands.guild_only()
    @manager_only
    async def deletelist(interaction: discord.Interaction, list: str):
        list_key = parse_list_index(list)
        await interaction.response.send_message(f"Delete {describe_list(list_key)}? This can't be undone.",
                                                view=ConfirmDeleteView(client, list_key, interaction.user.id),
                                                ephemeral=True)

    @home_only(tree.command, description="Add words to a list (looks up their forms online)")
    @app_commands.describe(list="The word list", words="Words to add, separated by spaces")
    @app_commands.autocomplete(list=list_autocomplete)
    async def add(interaction: discord.Interaction, list: str, words: str):
        list_key = parse_list_index(list)
        check_add(words.split(), interaction.user.id)
        await interaction.response.defer(thinking=True)  # looking words up can take a while
        await interaction.followup.send(await add_words(list_key, words.split(), interaction.user.id))

    @tree.command(description="Start a quiz in a new thread")
    @app_commands.describe(lists="Word lists, separated by commas",
                           tenses="Tenses/cases, separated by commas (suggestions fit the lists you picked)",
                           count="How many questions, picked at random (default: all of them)",
                           private="Practise alone in a private thread only you can see",
                           table="Full tables: type every form of a tense (e.g. all 6 Present forms) in one answer",
                           tashkeel="Arabic: answers must include the vowel marks exactly (otherwise they're optional)")
    @app_commands.autocomplete(lists=lists_autocomplete, tenses=tenses_autocomplete)
    async def quiz(interaction: discord.Interaction, lists: str, tenses: str,
                   count: app_commands.Range[int, 1, 1000] | None = None, private: bool = False,
                   table: bool = False, tashkeel: bool = False):
        if interaction.channel_id in client.sessions:
            raise UsageError("A quiz is already running here.")
        list_keys = parse_lists(lists)
        tense_names = parse_tenses(tenses)
        check_tenses(list_keys, tense_names)
        prompts, label, available = prepare_quiz(list_keys, tense_names, count, table)
        label = apply_tashkeel(prompts, label, tashkeel)
        # Remember it so `!start` can repeat the same quiz.
        client.setups[interaction.channel_id] = QuizSetup(list_keys, tense_names, count, table, tashkeel)
        await launch(interaction, prompts, label, question_count(prompts, available), private, tashkeel)

    @tree.command(description="Show saved quiz stats")
    @app_commands.describe(user="Whose stats to show (default: yours)")
    async def stats(interaction: discord.Interaction, user: discord.User | None = None):
        user = user or interaction.user
        await interaction.response.send_message(
            embed=stats_embed(client.stats, guild_id(interaction.channel), user), ephemeral=True)

    @tree.command(description="Top players in this server")
    async def leaderboard(interaction: discord.Interaction):
        await interaction.response.send_message(embed=leaderboard_embed(client.stats, guild_id(interaction.channel)))

    @tree.command(description="Quiz the words you miss most, from your saved stats")
    @app_commands.describe(count=f"How many questions (default {WEAK_DEFAULT_COUNT})",
                           lists="Only words from these lists (optional, separated by commas)",
                           tenses="Only these tenses/cases (optional, separated by commas)",
                           private="Practise alone in a private thread only you can see")
    @app_commands.autocomplete(lists=lists_autocomplete, tenses=tenses_autocomplete)
    async def weak(interaction: discord.Interaction, count: app_commands.Range[int, 1, 200] = WEAK_DEFAULT_COUNT,
                   lists: str | None = None, tenses: str | None = None, private: bool = False):
        if interaction.channel_id in client.sessions:
            raise UsageError("A quiz is already running here.")
        list_keys = parse_lists(lists) if lists else []
        tense_names = parse_tenses(tenses) if tenses else []
        if list_keys and tense_names:
            check_tenses(list_keys, tense_names)
        prompts = weak_prompts(client.stats, guild_id(interaction.channel), interaction.user.id,
                               list_keys, tense_names, count)
        label = f"{interaction.user.display_name}'s weak words"
        await launch(interaction, prompts, label, question_count(prompts, len(prompts)), private)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    # Log to the console and to bot.log (5 files of 1 MB, oldest dropped).
    from logging.handlers import RotatingFileHandler
    console = logging.StreamHandler()
    logfile = RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    for handler in (console, logfile):
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[console, logfile])
    intents = discord.Intents.default()
    intents.message_content = True
    Client(intents=intents).run(TOKEN, log_handler=None)  # log_handler=None: use the setup above
