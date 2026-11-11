"""How typed answers are judged: normalising, alternatives, tashkeel, tables, chat."""
from types import SimpleNamespace

from discbot import Prompt, is_chatter, is_right, matches, normalize, table_marks

BOT = object()


def chat(text, reply_to="none", mentions=()):
    reference = None if reply_to == "none" else SimpleNamespace(resolved=SimpleNamespace(author=reply_to) if reply_to else None)
    return SimpleNamespace(content=text, reference=reference, mentions=list(mentions))


def test_normalize_ignores_case_yo_and_arabic_marks():
    assert normalize(" Ёлка ") == "елка"
    assert normalize("تَكْتُبِينَ") == "تكتبين"
    assert normalize("تَكْتُبِيـنَ", keep_marks=True) == normalize("تَكْتُبِينَ", keep_marks=True)  # tatweel


def test_alternative_answers():
    assert matches("omenoiden", "omenien / omenoiden / omenoitten")
    assert matches("Omenien", "omenien / omenoiden / omenoitten")
    assert not matches("omenojen", "omenien / omenoiden / omenoitten")


def test_tashkeel_strict_and_lenient():
    p = Prompt("كتب", "Present", "أنتِ", "تَكْتُبِينَ", "ar")
    assert is_right(p, "تكتبين") and not is_right(p, "تكتبين", tashkeel=True)
    assert is_right(p, "تَكْتُبِينَ", tashkeel=True)
    assert not is_right(p, "تَكْتَبِينَ", tashkeel=True)
    shadda_first, fatha_first = "تَكَلَّمَ", "تَكَلَّمَ"
    assert is_right(Prompt("تكلم", "Past", "هو", shadda_first, "ar"), fatha_first, tashkeel=True)


def test_full_table_answers():
    p = Prompt("читать", "Past", "", "читал, читала, читало, читали", "ru",
               forms={"Masc": "читал", "Fem": "читала", "Neu": "читало", "Pl": "читали"})
    assert is_right(p, "читал читала читало читали")
    assert is_right(p, "читал, читала,\nчитало, читали")
    assert table_marks(p, "читал читала читала читали") == [True, True, False, True]
    assert table_marks(p, "читал читала") is None  # wrong number of forms


def test_chat_is_not_a_guess():
    answer = "читаю"
    assert not is_chatter(chat("читаешь"), answer, BOT)
    assert not is_chatter(chat("я читаю"), answer, BOT)
    assert not is_chatter(chat("читаю", reply_to=BOT), answer, BOT)
    assert is_chatter(chat("lol"), answer, BOT)            # other alphabet
    assert is_chatter(chat("😂"), answer, BOT)             # no letters
    assert is_chatter(chat("ой, я не знаю это слово"), answer, BOT)  # a sentence
    assert is_chatter(chat("читаешь", reply_to=object()), answer, BOT)  # reply to someone else
    assert is_chatter(chat("читаешь", mentions=[object()]), answer, BOT)
