"""The crawlers, run against saved copies of real pages (tests/fixtures)."""
import crawler
from bs4 import BeautifulSoup
from conftest import FIXTURES


def test_cooljugator_arabic_with_tashkeel_and_silent_alif():
    # A hand-written sample in cooljugator.com's format (see the comment in the file).
    page = BeautifulSoup(open(f"{FIXTURES}/cooljugator_sample.html", encoding="utf-8").read(), "html.parser")
    forms = crawler.strip_blanks(crawler.parse_arabic(page))
    assert forms["Present"] == {"أنا": "أَكْتُبُ", "أنتِ": "تَكْتُبِينَ", "هو": "يَكْتُبُ"}  # in person order
    assert forms["Past"]["هي"] == "كَتَبَتْ"
    assert forms["Jussive"]["هم"] == "يَكْتُبُوا"      # the site leaves off the alif
    assert forms["Imperative"]["أنتم"] == "اُكْتُبُوا"
    assert forms["Participles"] == {"Active": "كَاتِب", "Passive": "مَكْتُوب"}
    assert forms["Verbal Noun"] == {"": "كِتَابَة"}
    assert crawler.parse_arabic(BeautifulSoup("<html><title>Error 404</title></html>", "html.parser")) is None


def test_silent_alif_after_shadda():
    assert crawler.add_silent_alif("يَمُدُّو") == "يَمُدُّوا"
    assert crawler.add_silent_alif("يَدْعُوا") == "يَدْعُوا"


def test_wiktionary_arabic_tells_form_i_and_ii_apart(page):
    page("wikt_drs.html")
    verb, forms = crawler.wiktionary_arabic("درّس")  # shadda only is enough
    assert verb == "دَرَّسَ" and forms["Present"]["هو"] == "يُدَرِّسُ"
    assert forms["Verbal Noun"] == {"": "تَدْرِيس"}
    _, form_one = crawler.wiktionary_arabic("دَرَسَ")
    assert form_one["Present"]["هو"] == "يَدْرُسُ"
    assert "to teach" in crawler.english_meaning("دَرَّسَ", "ar", "دَرَّسَ")


def test_finnish_noun_with_alternatives(page):
    page("wikt_omena.html")
    info, forms = crawler.wiktionary_finnish("omena", "fin")
    assert info["kotus"] == 11
    assert forms["Genitive"]["Pl"] == "omenien / omenoiden / omenoitten"  # rare forms left out
    assert crawler.english_meaning("omena", "fin") == "apple"


def test_finnish_adjective_has_comparison(page):
    page("wikt_vanha.html")
    info, forms = crawler.wiktionary_finnish("vanha", "fia")
    assert info == {"kotus": 9, "model": "kala", "gradation": "no gradation"}
    assert forms["Comparison"] == {"Comparative": "vanhempi", "Superlative": "vanhin"}


def test_finnish_verb(page):
    page("wikt_puhua.html")
    info, forms = crawler.wiktionary_finnish("puhua", "fi")
    assert info["kotus"] == 52
    assert forms["Present"]["passiivi"] == "puhutaan"
    assert forms["Negative"]["minä"] == "en puhu"
    assert forms["Infinitives"]["3rd Illative"] == "puhumaan"
    assert crawler.wiktionary_finnish("puhua", "fin") is None  # not a noun


def test_finnish_gradation_class_in_title():
    table = BeautifulSoup("<table><tr><th>Inflection of katu (Kotus type 1*F/valo, t – d gradation)</th></tr></table>",
                          "html.parser")
    assert crawler.finnish_class(table) == {"kotus": 1, "model": "valo", "gradation": "t – d gradation"}


def test_swedish_verb(page):
    page("wikt_tala.html")
    forms = crawler.wiktionary_swedish("tala")
    assert (forms["Present"][""], forms["Preterite"][""], forms["Supine"][""]) == ("talar", "talade", "talat")
    assert forms["Perfect"][""] == "har talat"
    assert forms["Passive"]["Preterite"] == "talades"


def test_strip_blanks():
    assert crawler.strip_blanks({"Participle": {"a": "x", "b": "", "c": "—"}, "Empty": {"d": " "}}) == {"Participle": {"a": "x"}}


def test_cooljugator_russian_keeps_stress(page):
    # A hand-written sample in cooljugator.com's format (see the comment in the file).
    page("cooljugator_ru_sample.html")
    forms = crawler.lookup("ёж", "run")
    assert forms["Gen Sing"] == {"": "ежа́"} and forms["Inst Pl"] == {"": "ежа́ми"}
    assert crawler.strip_stress(forms["Prep Sing"][""]) == "еже"


def test_stressed_form_falls_back_to_the_plain_text():
    html = ('<div data-stressed="шла́"><div class="meta-form">шла</div></div>'
            '<div><div class="meta-form">шёл</div></div>'                          # ё: no mark needed
            '<div data-stressed="шёл бы"><div class="meta-form">шла бы</div></div>')  # a different form
    entries = BeautifulSoup(html, "html.parser").find_all("div", class_="meta-form")
    assert [crawler.stressed_form(e) for e in entries] == ["шла́", "шёл", "шла бы"]


def test_russian_cases_are_read_by_cell_id_not_position():
    # Hand-written, in cooljugator.com's format: some nouns have a locative (в году́) between
    # the plural and singular cells, which used to shift every singular case by one.
    cells = [("nom_P", "го́ды"), ("gen_P", "годо́в"), ("dat_P", "года́м"), ("acc_P", "го́ды"),
             ("instr_P", "года́ми"), ("prep_P", "года́х"), ("loc", "году́"), ("nom_S", "го́д"),
             ("gen_S", "го́да"), ("dat_S", "го́ду"), ("acc_S", "го́д"), ("instr_S", "го́дом"),
             ("prep_S", "го́де")]
    html = "".join(f'<div class="conjugation-cell" id="{cid}_no_accent" data-stressed="{form}">'
                   f'<div class="meta-form">{crawler.strip_stress(form)}</div></div>' for cid, form in cells)
    forms = crawler.parse_russian(BeautifulSoup(html, "html.parser"), "run")
    assert forms["Gen Sing"] == {"": "го́да"} and forms["Dat Sing"] == {"": "го́ду"}
    assert forms["Acc Sing"] == {"": "го́д"} and forms["Prep Sing"] == {"": "го́де"}
    assert "Nom Sing" not in forms and len(forms) == 11


def test_perfective_future_is_saved_as_present():
    html = "".join(f'<div class="conjugation-cell" id="future{n}" data-stressed="{form}">'
                   f'<div class="meta-form">{crawler.strip_stress(form)}</div></div>'
                   for n, form in enumerate(["прочита́ю", "прочита́ешь", "прочита́ет",
                                             "прочита́ем", "прочита́ете", "прочита́ют"], 1))
    forms = crawler.parse_russian(BeautifulSoup(html, "html.parser"), "ru")
    assert forms == {"Present": {"Я": "прочита́ю", "Ты": "прочита́ешь", "Он": "прочита́ет",
                                 "Мы": "прочита́ем", "Вы": "прочита́ете", "Они": "прочита́ют"}}
