"""How typed answers are judged: normalising, alternatives, tashkeel, stress, tables, chat."""
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


def test_stress_lenient_by_default():
    p = Prompt("делать", "Present", "Я", "де́лаю", "ru")
    assert is_right(p, "делаю") and is_right(p, "де́лаю") and is_right(p, "де'лаю")
    assert is_right(p, "дела'ю")  # a wrong stress doesn't matter without the option
    assert is_right(Prompt("идти", "Present", "Ты", "идёшь", "ru"), "идешь")


def test_stress_required():
    p = Prompt("делать", "Present", "Я", "де́лаю", "ru")
    assert is_right(p, "де'лаю", stress=True) and is_right(p, "Де́лаю", stress=True)
    assert is_right(p, "де’лаю", stress=True)  # a curly apostrophe from a phone keyboard
    assert not is_right(p, "делаю", stress=True)
    assert not is_right(p, "дела'ю", stress=True) and not is_right(p, "'делаю", stress=True)
    assert not is_right(p, "де'ла'ю", stress=True)


def test_stress_with_yo_and_one_vowel():
    yo = Prompt("идти", "Present", "Ты", "идёшь", "ru")
    assert is_right(yo, "идёшь", stress=True) and is_right(yo, "иде'шь", stress=True)
    assert not is_right(yo, "идешь", stress=True)
    one_vowel = Prompt("стол", "Nom Sing", "", "сто́л", "run")
    assert is_right(one_vowel, "стол", stress=True) and is_right(one_vowel, "сто'л", stress=True)
    assert is_right(Prompt("новый", "Short", "Masc", "нов", "rua"), "но'в", stress=True)


def test_stress_only_checked_where_it_is_known():
    # Answers saved before the crawler kept stress have none, so they can't require it.
    old = Prompt("делать", "Past", "Masc", "делал бы", "ru")
    assert is_right(old, "делал бы", stress=True) and is_right(old, "де'лал бы", stress=True)
    assert matches("vaa'an", "vaa'an", stress=True)  # apostrophes in other languages are letters
    assert not matches("vaan", "vaa'an")


def test_stress_in_full_tables():
    p = Prompt("делать", "Past", "", "", "ru",
               forms={"Masc": "де́лал", "Fem": "де́лала", "Neu": "де́лало", "Pl": "де́лали"})
    assert is_right(p, "де'лал, де'лала, де'лало, де'лали", stress=True)
    assert table_marks(p, "де'лал де'лала дела'ло де'лали", stress=True) == [True, True, False, True]


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
