# DrillBot (CooljuCrawler)

A Discord bot for drilling verb, noun and adjective forms in Russian, Arabic, Finnish and
Swedish: quizzes, lessons (`/learn`), stats, and word lists crawled from cooljugator.com and
Wiktionary. It runs as an always-on service (a systemd user service on the Raspberry Pi, a
launchd agent on the Mac), and is usually tweaked remotely through Claude Code.

## Rules

- **Never print, `cat`, or echo `bot.env`, and never put the token in output, commits or logs.**
  It holds `DISCORD_TOKEN`. To check a setting exists, test it without showing it, e.g.
  `(source bot.env && [ -n "$DISCORD_TOKEN" ] && echo set)`.
- **Don't edit `words.json` by hand while the bot is running.** The bot keeps its own copy in
  memory and overwrites the file on its next save. Change lists through the bot (`/add`,
  `/remove`, …), or stop the service, edit, then start it again.
- **Run the tests before restarting the live bot** after any code change: `.venv/bin/python -m pytest -q`.
  They're offline and take about a second. Add or update tests for what you change.
- **Restart the service after code or `bot.env` changes**, then check the log for a new
  "Logged on as" line (and no traceback).
- **Commit each finished change** with a clear message. Don't push unless asked.
- **Be polite to the sites the crawler uses**: one lookup at a time, a second or more apart.
  Respect robots.txt: conjugator.reverso.net disallows AI crawlers, so it isn't used.
  Wiktionary content is CC BY-SA 4.0 and is credited in `/help` and `/conjugate`.
- **The repository is public. Don't commit third-party data**: no `words.json`, no saved
  cooljugator.com pages. Wiktionary pages may be added as test fixtures if listed in
  `ATTRIBUTION.md`. Write cooljugator-format test data by hand.

## Running it

| | Raspberry Pi (systemd user service) | Mac (launchd agent) |
|---|---|---|
| Restart | `systemctl --user restart drillbot` | `launchctl kickstart -k gui/$(id -u)/local.drillbot` |
| Stop | `systemctl --user stop drillbot` | `launchctl bootout gui/$(id -u)/local.drillbot` |
| Status | `systemctl --user status drillbot` | `launchctl print gui/$(id -u)/local.drillbot` |

- First-time setup on the Pi: `bash deploy/setup_pi.sh` (venv, packages, service; safe to re-run).
- `run_bot.sh` loads `bot.env` and starts `discbot.py`; the services run it.
- Only one copy may run at a time, or every message gets answered twice:
  `pgrep -fl discbot.py`.
- Logs: `bot.log` (the bot's own log, rotated) and `service.log` (console output).
  A good start looks like: `Synced N global slash commands`, `Synced 6 list-editing commands
  to home server …`, `Logged on as DrillBot#4243`.

## Settings (`bot.env`, see `bot.env.example`)

`DISCORD_TOKEN`; `DISCORD_HOME_GUILD_ID` (the only server where lists can be edited; lists are
shared by every server); `WIKTIONARY_CONTACT` (sent with Wiktionary requests);
`DISCORD_GUILD_ID` (testing only: registers every command in that one server, so leave it unset
in production).

## Files

- `discbot.py`: the bot. Commands (`!` text commands and slash commands), quizzes, lessons,
  buttons, results, stats commands.
- `crawler.py`: word lists (`words.json`, kept in memory as `crawler.jsondict`), safe saving
  with backups, and the crawlers: cooljugator.com (Russian; Arabic plain spellings) and
  Wiktionary (Arabic verbs it can't separate, e.g. Form II دَرَّسَ; Finnish; Swedish;
  English meanings).
- `stats.py`: `stats.db` tables for answers, players and learned words.
- `store.py`: `stats.db` tables for running quizzes/lessons and retry buttons (survive restarts).
- `words.json`: all word lists. **Not in git** (it's built from cooljugator.com and Wiktionary
  content; see `ATTRIBUTION.md`), so copy it between machines like `stats.db`. `words.json.bak`
  and `backups/` (one snapshot a day, 14 days) are made automatically.
- `meanings.json`: cached English meanings. `stats.db`: player data. Both git-ignored.
- `tests/`: pytest suite. `tests/fixtures/` has trimmed Wiktionary pages for the parser tests, a
  hand-written cooljugator-format sample, and hand-written word lists (`words_test.json`) the
  tests use instead of `words.json`.
- `deploy/`: the systemd unit template and the Pi setup script.

## How the code works (things that are easy to break)

- **Change session state before the first `await`** in message/button handlers. Discord events
  run concurrently; this is what stops two simultaneous answers from both scoring or skipping a
  question. Saving (`persist`, stats) is synchronous for the same reason.
- **Buttons are `DynamicItem`s** whose `custom_id` encodes what they do (e.g.
  `quiz:reveal:<token>:<serial>`, `learn:<list>:<tense>`), registered in `setup_hook`. That's
  what makes them work after restarts. Never call `stop()` on a view holding them (it
  unregisters the pattern).
- **Every running quiz/lesson is saved** to `stats.db` after each change (`Client.persist`) and
  reloaded at startup (`restore_sessions`). New session fields need to survive
  `to_dict`/`from_dict`, including sessions saved by older code.
- **Word data**: `words.json` is a list of `{"name", "pos", "words": {word: {tense: {form label: answer}}}}`.
  `pos` codes: `ru`/`run`/`rua` Russian verbs/nouns/adjectives, `ar` Arabic verbs,
  `fi`/`fin`/`fia` Finnish, `sv` Swedish. Quiz categories per `pos` are in `avail_quizes`.
  Several correct answers are stored as `"a / b"` (`crawler.SEPARATOR`). Arabic forms keep full
  tashkeel; Arabic verbs from Wiktionary are keyed by their vowelled spelling.
- **Answer checking** (`normalize`, `matches`, `is_right`, `is_chatter`): case, ё/е and Arabic
  marks are ignored unless a quiz uses the `tashkeel` option; chat (other alphabet, replies,
  @mentions, sentences) is ignored instead of marked wrong.
- **List editing** (`/new`, `/add`, `/remove`, `/rename`, `/deletelist`, right-click Add to word
  list) is registered only in the home server; the `!` versions check it in `require_home`.

## Adding words or a language

Prefer `/add` in the home server. For bulk additions, write a script that fetches politely,
**checks each result** (e.g. the expected past tense or inflection class, since one spelling can
be several words), shows what it found, and only then writes `words.json` with the bot stopped.
A new language needs: a `pos` code in `pos_names`, its categories in `avail_quizes`, a crawler
returning `{tense: {form label: answer}}`, and parser tests against a saved page.
