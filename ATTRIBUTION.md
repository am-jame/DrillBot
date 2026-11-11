# Attribution and data sources

The **code** in this repository is under the MIT License (see `LICENSE`).

## Word data

This repository doesn't include the bot's word lists (`words.json`). The bot builds them by
looking words up at run time, from two sources:

- **Wiktionary** (https://en.wiktionary.org): Finnish and Swedish forms, some Arabic verbs, and
  English meanings. Wiktionary content is by Wiktionary contributors and licensed under
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The bot credits Wiktionary
  in `/help` and on `/conjugate` cards, and links each card to the page its forms came from.
  If you share word lists built from Wiktionary, share them under CC BY-SA 4.0 with the same
  credit.
- **cooljugator.com** (https://cooljugator.com): Russian forms and most Arabic forms. Its content
  isn't openly licensed, so none of it is included here; the bot only looks words up for its
  own use, one at a time.

## Test fixtures

`tests/fixtures/wikt_*.html` are trimmed copies (scripts and page furniture removed) of these
English Wiktionary pages, by Wiktionary contributors, licensed under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/):
[talo](https://en.wiktionary.org/wiki/talo),
[omena](https://en.wiktionary.org/wiki/omena),
[vanha](https://en.wiktionary.org/wiki/vanha),
[puhua](https://en.wiktionary.org/wiki/puhua),
[tala](https://en.wiktionary.org/wiki/tala),
[درس](https://en.wiktionary.org/wiki/%D8%AF%D8%B1%D8%B3).
Those files are shared under CC BY-SA 4.0.

`tests/fixtures/cooljugator_sample.html` and `tests/fixtures/words_test.json` are hand-written
samples (in the formats the bot reads), not copies of any site's content.
