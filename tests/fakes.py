"""Small stand-ins for Discord objects, enough for the bot's message and button handling."""
import itertools
from types import SimpleNamespace

_ids = itertools.count(1000)


class FakeUser:
    bot = False

    def __init__(self, user_id=1, name="Ana", manage_messages=False):
        self.id, self.display_name, self.name = user_id, name, name
        self.mention = f"<@{user_id}>"
        self.guild_permissions = SimpleNamespace(manage_messages=manage_messages)


class FakeMessage:
    def __init__(self, channel, content="", author=None, embed=None, view=None):
        self.id = next(_ids)
        self.channel, self.content, self.author = channel, content, author
        self.guild = channel.guild
        self.embed, self.view = embed, view
        self.mentions, self.reference = [], None
        self.reactions, self.replies = [], []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, content, **kwargs):
        self.replies.append(content)

    async def edit(self, **kwargs):
        if "view" in kwargs:
            self.view = kwargs["view"]


class FakeChannel:
    """A DM-like channel: quizzes and lessons run right here instead of in a thread."""

    def __init__(self, channel_id=100, guild_id=42):
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id) if guild_id else None
        self.sent: list[FakeMessage] = []

    async def send(self, content=None, *, embed=None, embeds=None, view=None, **kwargs):
        message = FakeMessage(self, content or "", embed=embed or (embeds[0] if embeds else None), view=view)
        self.sent.append(message)
        return message

    def get_partial_message(self, message_id):
        return next((m for m in self.sent if m.id == message_id), FakeMessage(self))

    @property
    def last(self) -> FakeMessage:
        return self.sent[-1]


class FakeResponse:
    def __init__(self, interaction):
        self.interaction, self.done = interaction, False

    async def send_message(self, content=None, *, ephemeral=False, **kwargs):
        self.done = True
        self.interaction.private.append(content)

    async def edit_message(self, **kwargs):
        self.done = True
        if self.interaction.message is not None and "view" in kwargs:
            self.interaction.message.view = kwargs["view"]

    async def defer(self, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


class FakeInteraction:
    """A button click (or slash command) by `user` on `message` in `channel`."""

    def __init__(self, client, channel, user, message=None):
        self.client, self.channel, self.channel_id = client, channel, channel.id
        self.user, self.message, self.guild = user, message, channel.guild
        self.private: list[str] = []  # replies only the clicker sees
        self.response = FakeResponse(self)
        self.followup = SimpleNamespace(send=self._followup)
        self.command = None

    async def _followup(self, content=None, **kwargs):
        self.private.append(content)


async def say(client, channel, user, text) -> FakeMessage:
    """`user` types `text` in `channel`; returns the message (to check reactions and replies)."""
    message = FakeMessage(channel, text, author=user)
    await client.on_message(message)
    return message


async def click(client, message, label_start, user):
    """Press the button on `message` whose label starts with `label_start`, as discord.py would."""
    import discbot
    item = next(i for i in message.view.children if (getattr(i, "item", i).label or "").startswith(label_start))
    custom_id = getattr(item, "item", item).custom_id
    for kind in (discbot.QuestionButton, discbot.RetryButton, discbot.PageButton, discbot.NextLessonButton):
        match = kind.__discord_ui_compiled_template__.fullmatch(custom_id)
        if match:
            interaction = FakeInteraction(client, message.channel, user, message)
            await (await kind.from_custom_id(interaction, None, match)).callback(interaction)
            return interaction
    raise AssertionError(f"no handler for {custom_id}")
