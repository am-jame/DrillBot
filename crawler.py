from bs4 import BeautifulSoup as soup
import requests
import json
import os
import re
import shutil
import threading
import unicodedata
import urllib.parse
from datetime import date, timedelta


tenses = []
jsondict = []
# Files live next to this script, whatever folder the bot is started from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
json_file = os.path.join(BASE_DIR, "words.json")

BACKUP_DIR = os.path.join(BASE_DIR, "backups")
BACKUP_DAYS = 14         # keep one snapshot per day for this many days
REQUEST_TIMEOUT = 15     # seconds; a stalled site shouldn't hang /add forever
HEADERS = {"User-Agent": "CooljuCrawler language quiz bot (Discord)"}
BLANK_FORMS = ("", "—", "-")

# Only one save at a time (the bot and scripts can both write words.json).
save_lock = threading.Lock()

# ---- Arabic ----
# cooljugator.com shows Arabic forms without vowel marks, but each cell's data-stressed
# attribute has the fully vowelled form, and its id names the tense and person
# (present1, past3m, jussiveD2, ...), so forms are read by name rather than position.

# Vowel marks (tashkeel) and other Quranic annotation marks.
TASHKEEL = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")

AR_PERSONS = {
    "1": "أنا", "2m": "أنتَ", "2f": "أنتِ", "D2": "أنتما", "3m": "هو", "3f": "هي",
    "D3m": "هما (م)", "D3f": "هما (ث)", "4": "نحن", "5m": "أنتم", "5f": "أنتنّ", "6m": "هم", "6f": "هنّ",
}
AR_TENSES = {
    "past": "Past", "present": "Present", "subjunctive": "Subjunctive", "jussive": "Jussive",
    "imperative": "Imperative", "passive_past": "Passive Past", "passive_present": "Passive Present",
    "passive_subjunctive": "Passive Subjunctive", "passive_jussive": "Passive Jussive",
}
# In these moods the masculine plural ends in -ُوا, but the site drops the silent alif (يكتبو).
AR_NEEDS_ALIF = {"subjunctive", "jussive", "imperative", "passive_subjunctive", "passive_jussive"}


# A word ending in -ُو, with or without a shadda on the letter before (يكتبو, يمدّو).
ENDS_IN_UU = re.compile("(\u064F\u0651?|\u0651\u064F)\u0648$")


def strip_tashkeel(text):
    return TASHKEEL.sub("", text)


def add_silent_alif(form):
    """يَكْتُبُو -> يَكْتُبُوا, يَمُدُّو -> يَمُدُّوا (the site leaves the alif off)."""
    return form + "ا" if ENDS_IN_UU.search(form) else form


def parse_arabic(page):
    """All forms of an Arabic verb with full tashkeel, or None if the page has no verb."""
    cells = {cell.get("id"): cell["data-stressed"].strip()
             for cell in page.find_all(attrs={"data-stressed": True}) if cell.get("id")}
    if not cells:
        return None
    result = {}
    for prefix, tense in AR_TENSES.items():
        forms = {}
        for person, label in AR_PERSONS.items():
            form = cells.get(prefix + person)
            if not form:
                continue
            if prefix in AR_NEEDS_ALIF and person in ("5m", "6m"):
                form = add_silent_alif(form)
            forms[label] = form
        if forms:
            result[tense] = forms
    participles = {label: cells[cell_id] for cell_id, label in
                   (("active_participle", "Active"), ("passive_participle", "Passive")) if cells.get(cell_id)}
    if participles:
        result["Participles"] = participles
    if cells.get("verbal_noun"):
        result["Verbal Noun"] = {"": cells["verbal_noun"]}
    return result


# ---- Arabic from Wiktionary ----
# cooljugator.com has one verb per unvowelled spelling, so e.g. دَرَّسَ (Form II, to teach)
# can't be told apart from دَرَسَ (Form I, to study). Wiktionary has a table for each verb
# on the page for the shared spelling, titled with the vowelled verb, so it can.
# robots.txt allows /wiki/ pages (but not the API). Content is CC BY-SA 4.0.

WIKTIONARY_URL = "https://en.wiktionary.org/wiki/"
# Wikimedia asks for a User-Agent with a way to contact the operator; set this to a URL or
# address you're happy to share (e.g. the bot's GitHub page) before heavy use.
WIKTIONARY_CONTACT = os.environ.get("WIKTIONARY_CONTACT", "")

WIKT_TENSES = {  # header (without tashkeel) -> our tense name, per voice
    "الماضي": "Past", "المضارع المرفوع": "Present", "المضارع المنصوب": "Subjunctive",
    "المضارع المجزوم": "Jussive", "الأمر": "Imperative",
}
WIKT_NUMBERS = {"المفرد": "s", "المثنى": "d", "الجمع": "p"}
WIKT_PERSONS = {"المتكلم": "1", "المخاطب": "2", "الغائب": "3"}
# (number, person, gender row) -> our person label; gender is None for cells shared by m and f.
WIKT_LABELS = {
    ("s", "1"): "أنا", ("s", "2", "m"): "أنتَ", ("s", "2", "f"): "أنتِ", ("s", "3", "m"): "هو",
    ("s", "3", "f"): "هي", ("d", "2"): "أنتما", ("d", "3", "m"): "هما (م)", ("d", "3", "f"): "هما (ث)",
    ("p", "1"): "نحن", ("p", "2", "m"): "أنتم", ("p", "2", "f"): "أنتنّ", ("p", "3", "m"): "هم", ("p", "3", "f"): "هنّ",
}


def _nfc(text):
    return unicodedata.normalize("NFC", text.strip())


def _arabic_text(cell):
    """The first Arabic form in a table cell (cells can list alternatives)."""
    span = cell.find(lang="ar") if cell else None
    return _nfc(span.get_text()) if span else ""


def _table_grid(table):
    """The table as a grid of cells, with rowspan/colspan cells repeated where they reach."""
    grid, pending = [], {}  # pending: (row, col) -> cell continued from a rowspan above
    for r, tr in enumerate(table.find_all("tr")):
        row, col = [], 0
        cells = iter(tr.find_all(["th", "td"], recursive=False))
        while True:
            if (r, col) in pending:
                row.append(pending.pop((r, col))); col += 1; continue
            cell = next(cells, None)
            if cell is None:
                break
            span_c, span_r = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
            if span_c > 50:  # full-width separator rows
                span_c = 1
            for c in range(span_c):
                row.append(cell)
                for extra in range(1, span_r):
                    pending[(r + extra, col + c)] = cell
            col += span_c
        grid.append(row)
    return grid


def parse_wiktionary_table(table):
    """Our tense/person layout from one Wiktionary Arabic conjugation table."""
    result, voice, columns = {}, "", {}
    grid = _table_grid(table)
    for r, row in enumerate(grid):
        header = strip_tashkeel(_nfc(row[0].get_text(" "))) if row else ""
        texts = [strip_tashkeel(_arabic_text(c) or _nfc(c.get_text(" "))) for c in row]
        if any("المعلوم" in t for t in texts):
            voice = ""
        elif any("المجهول" in t for t in texts):
            voice = "Passive "
        if any(t in WIKT_PERSONS for t in texts):  # the person header row; the number row is above it
            numbers = [strip_tashkeel(_arabic_text(c)) for c in grid[r - 1]]
            columns = {c: (WIKT_NUMBERS[numbers[c]], WIKT_PERSONS[t]) for c, t in enumerate(texts)
                       if t in WIKT_PERSONS and c < len(numbers) and numbers[c] in WIKT_NUMBERS}
            continue
        for name, label in (("المصدر", "Verbal Noun"), ("اسم الفاعل", "Active"), ("اسم المفعول", "Passive")):
            if any(t.startswith(name) for t in texts[:1]) and len(row) > 1:
                form = _arabic_text(row[-1])
                if form:
                    if label == "Verbal Noun":
                        result["Verbal Noun"] = {"": form}
                    else:
                        result.setdefault("Participles", {})[label] = form
        tense_header = strip_tashkeel(_arabic_text(row[0])) if row else ""
        if tense_header not in WIKT_TENSES or len(row) < 2 or not columns:
            continue
        gender = {"m": "m", "f": "f"}.get(row[1].get_text(strip=True))
        tense = voice + WIKT_TENSES[tense_header]
        if voice and tense == "Passive Imperative":
            continue
        for c, (number, person) in columns.items():
            if c >= len(row):
                continue
            label = WIKT_LABELS.get((number, person)) or WIKT_LABELS.get((number, person, gender))
            form = _arabic_text(row[c])
            if label and form and (label not in result.get(tense, {})):
                result.setdefault(tense, {})[label] = form
    # Same tense and person order as cooljugator's data.
    tenses = {t: {label: result[t][label] for label in AR_PERSONS.values() if label in result[t]}
              for t in AR_TENSES.values() if t in result}
    for t in ("Participles", "Verbal Noun"):
        if t in result:
            tenses[t] = result[t]
    return tenses


def _shadda_only(text):
    """Letters plus shadda only, so درّس can find دَرَّسَ."""
    return TASHKEEL.sub(lambda m: m.group() if m.group() == "\u0651" else "", _nfc(text))


def wiktionary_arabic(lemma):
    """(vowelled verb, forms) for the Arabic verb `lemma` from Wiktionary, or None.

    `lemma` is the past tense (هو) with full tashkeel (دَرَّسَ), or with just the shadda (درّس)."""
    lemma = _nfc(lemma)
    contact = f" ({WIKTIONARY_CONTACT})" if WIKTIONARY_CONTACT else ""
    headers = {"User-Agent": HEADERS["User-Agent"] + contact}
    url = WIKTIONARY_URL + urllib.parse.quote(strip_tashkeel(lemma))
    response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
    if response.status_code != 200:
        return None
    page = soup(response.text, "html.parser")
    tables = []
    for caption in page.select("table.inflection-table caption"):
        verb = caption.find(lang="ar")
        if caption.get_text(" ", strip=True).startswith("Conjugation of") and verb:
            tables.append((_nfc(verb.get_text()), caption.find_parent("table")))
    exact = [t for t in tables if t[0] == lemma]
    loose = [t for t in tables if _shadda_only(t[0]) == _shadda_only(lemma)] if lemma == _shadda_only(lemma) else []
    for verb, table in exact or loose[:1]:
        forms = strip_blanks(parse_wiktionary_table(table))
        return (verb, forms) if forms else None
    return None


def lookup_arabic(word):
    """(key to save it under, forms) for an Arabic verb, or (key, None) if it can't be found.

    Plain spellings (درس) come from cooljugator and are saved as typed. If the verb is typed
    with marks (دَرَّسَ, درّس) and cooljugator has a different verb with those letters,
    Wiktionary is used instead, and it's saved under its vowelled spelling so it can sit
    next to the other verb (stats, /weak and /conjugate tell them apart by that key)."""
    plain = strip_tashkeel(_nfc(word))
    forms = lookup(plain, 'ar')
    if _nfc(word) == plain:
        return plain, forms
    past = _nfc((forms or {}).get("Past", {}).get("هو", ""))
    if forms and (past == _nfc(word) or _shadda_only(past) == _shadda_only(word) == _nfc(word)):
        return plain, forms
    found = wiktionary_arabic(word)
    return (found[0], found[1]) if found else (_nfc(word), None)


# ---- Finnish from Wiktionary ----
# Each Finnish entry has one main table: fi-decl for nouns/adjectives, fi-conj for verbs.
# Its title names the Kotus inflection class, e.g. "Inflection of talo (Kotus type 1/valo,
# no gradation)", which is how words are sorted into class lists. Cells can hold several
# correct forms; common ones are kept as "omenien / omenoiden", rare ones are left out.

FI_POS = {"fi": "Verb", "fin": "Noun", "fia": "Adjective"}
FI_CASES = ["nominative", "genitive", "partitive", "inessive", "elative", "illative", "adessive",
            "ablative", "allative", "essive", "translative", "abessive", "instructive"]
FI_PERSONS = {"1st sing.": "minä", "2nd sing.": "sinä", "3rd sing.": "hän", "1st plur.": "me",
              "2nd plur.": "te", "3rd plur.": "he", "passive": "passiivi"}
# (mood, tense header) -> (positive tense name, negative tense name or None)
FI_TENSES = {
    ("indicative", "present tense"): ("Present", "Negative"),
    ("indicative", "past tense"): ("Past", "Past Negative"),
    ("conditional", "present"): ("Conditional", None),
    ("imperative", "present"): ("Imperative", None),
    ("potential", "present"): ("Potential", None),
}
SEPARATOR = " / "  # between alternative correct forms


def _fi_forms(cell):
    """The common forms in a cell, joined with SEPARATOR ('' for none or '—')."""
    forms = []
    for span in cell.find_all(lang="fi"):
        if any(c.startswith("rare-") for c in span.get("class", [])):
            continue
        text = " ".join(span.get_text(" ").split())
        if text and text != "—" and text not in forms:
            forms.append(text)
    return SEPARATOR.join(forms)


def _top_rows(table):
    """Rows of `table` itself, not of tables nested inside it (possessive forms)."""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _cells(tr):
    return tr.find_all(["th", "td"], recursive=False)


def _label(cell):
    """A header cell's text without footnote numbers: 'inessive 2' -> 'inessive'."""
    return re.sub(r"\s*\d+$", "", " ".join(cell.get_text(" ").split())).strip().lower()


def finnish_class(table):
    """{'kotus': 1, 'model': 'valo', 'gradation': 't – d gradation'} from a table's title row.

    Titles look like "Inflection of katu (Kotus type 1*F/valo, t – d gradation)", where
    *F is the consonant gradation pattern (left out when there's no gradation)."""
    title = " ".join(table.find("tr").get_text(" ").split())
    match = re.search(r"Kotus type (\d+)(?:\*[A-Z])?\s*/\s*([^\s,)]+)\s*(?:,\s*([^)]*))?\)", title)
    if not match:
        return {}
    return {"kotus": int(match.group(1)), "model": match.group(2), "gradation": (match.group(3) or "").strip()}


def parse_finnish_declension(table):
    """Case -> {'Sing': ..., 'Pl': ...} from a fi-decl table."""
    result = {}
    for tr in _top_rows(table):
        cells = _cells(tr)
        if len(cells) < 3 or cells[0].name != "th":
            continue
        case = _label(cells[0])
        if case in FI_CASES and case.capitalize() not in result:
            sing, plur = _fi_forms(cells[1]), _fi_forms(cells[2])
            result[case.capitalize()] = {"Sing": sing, "Pl": plur}
    return {case.capitalize(): result[case.capitalize()] for case in FI_CASES if case.capitalize() in result}


def parse_finnish_conjugation(table):
    """Tense -> {person: form}, plus Participles and Infinitives, from a fi-conj table."""
    result, mood, tense, infinitive = {}, "", "", None
    for tr in _top_rows(table):
        cells = _cells(tr)
        labels = [_label(c) for c in cells]
        if len(cells) == 1 and labels[0].endswith("mood"):
            mood = labels[0].replace(" mood", "")
            continue
        if labels and labels[0] in ("present tense", "past tense", "present"):
            tense = labels[0]
            continue
        if labels and labels[0] in FI_PERSONS and (mood, tense) in FI_TENSES and len(cells) >= 3:
            positive, negative = FI_TENSES[(mood, tense)]
            person = FI_PERSONS[labels[0]]
            result.setdefault(positive, {})[person] = _fi_forms(cells[1])
            if negative:
                result.setdefault(negative, {})[person] = _fi_forms(cells[2])
            continue
        # Nominal forms: walk the row's cells, reading the value cells after each label.
        i = 0
        while i < len(cells):
            label = labels[i]
            values = [c for c in cells[i + 1:i + 3] if c.name == "td"]
            if label in ("2nd", "3rd", "4th"):
                infinitive = label
            if cells[i].name == "th" and values:
                if label == "1st" and infinitive is None:
                    result.setdefault("Infinitives", {})["1st"] = _fi_forms(values[0])
                elif label in ("present", "past") and len(values) == 2:
                    result.setdefault("Participles", {})[f"{label.capitalize()} Act"] = _fi_forms(values[0])
                    result["Participles"][f"{label.capitalize()} Pass"] = _fi_forms(values[1])
                elif label == "agent":
                    result.setdefault("Participles", {})["Agent"] = _fi_forms(values[0])
                elif label == "negative":
                    result.setdefault("Participles", {})["Negative"] = _fi_forms(values[0])
                elif label in ("inessive", "instructive", "elative", "illative", "adessive", "abessive") \
                        and infinitive in ("2nd", "3rd"):
                    key = f"{infinitive} {label.capitalize()}"
                    result.setdefault("Infinitives", {}).setdefault(key, _fi_forms(values[0]))
                elif label == "verbal noun":
                    result.setdefault("Infinitives", {})["4th"] = _fi_forms(values[0])
            i += 1
    return result


def _comparison(section_nodes):
    """{'Comparative': ..., 'Superlative': ...} from an adjective's headword line, if given."""
    for node in section_nodes:
        line = node.find_parent("p") if node.name == "strong" else None
        if line is None:
            continue
        text = " ".join(line.get_text(" ").split())
        comp = re.search(r"comparative ([^\s,)]+)", text)
        sup = re.search(r"superlative ([^\s,)]+)", text)
        if comp and sup:
            return {"Comparative": comp.group(1), "Superlative": sup.group(1)}
    return {}


def wiktionary_finnish(word, pos):
    """(class info, forms) for a Finnish noun ('fin'), adjective ('fia') or verb ('fi'), or None."""
    contact = f" ({WIKTIONARY_CONTACT})" if WIKTIONARY_CONTACT else ""
    response = requests.get(WIKTIONARY_URL + urllib.parse.quote(word), timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": HEADERS["User-Agent"] + contact})
    if response.status_code != 200:
        return None
    page = soup(response.text, "html.parser")
    heading = page.find(id="Finnish")
    if heading is None:
        return None
    wanted, current_pos, headwords = FI_POS[pos], None, []
    start = heading.find_parent(class_="mw-heading2") or heading
    for node in start.find_all_next(["div", "h2", "h3", "h4", "h5", "table", "strong"]):
        if start in node.parents:
            continue  # the Finnish heading itself
        classes = node.get("class") or []
        if node.name == "h2" or "mw-heading2" in classes:
            break  # next language
        if node.name in ("h3", "h4", "h5"):
            title = node.get_text(" ", strip=True)
            if title in ("Noun", "Adjective", "Verb", "Numeral", "Pronoun", "Adverb", "Proper noun"):
                current_pos, headwords = title, []
        elif node.name == "strong" and "headword" in classes and node.get("lang") == "fi":
            headwords.append(node)
        elif node.name == "table" and current_pos == wanted:
            if "fi-conj" in classes and pos == "fi":
                return finnish_class(node), parse_finnish_conjugation(node)
            if "fi-decl" in classes and "fi-decl-poss-subtable" not in classes and pos != "fi":
                forms = parse_finnish_declension(node)
                if pos == "fia":
                    comparison = _comparison(headwords)
                    if comparison:
                        forms["Comparison"] = comparison
                return finnish_class(node), forms
    return None


# ---- English meanings (for lesson cards), from Wiktionary ----

MEANINGS_FILE = os.path.join(BASE_DIR, "meanings.json")
MEANING_LANGUAGES = {"ru": "Russian", "run": "Russian", "rua": "Russian", "ar": "Arabic", "sv": "Swedish",
                     "fi": "Finnish", "fin": "Finnish", "fia": "Finnish"}
MEANING_POS = {"ru": "Verb", "ar": "Verb", "sv": "Verb", "fi": "Verb",
               "run": "Noun", "fin": "Noun", "rua": "Adjective", "fia": "Adjective"}
meanings = {}  # "pos:word" -> short English meaning ("" when none was found)
meanings_lock = threading.Lock()


def load_meanings():
    meanings.clear()
    if os.path.exists(MEANINGS_FILE):
        with open(MEANINGS_FILE, encoding="utf-8") as file:
            meanings.update(json.load(file))


def save_meanings():
    with meanings_lock:
        _atomic_write(MEANINGS_FILE, json.dumps(meanings, ensure_ascii=False, indent=0))


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding="utf-8") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def _short_sense(li):
    """'( intransitive ) to speak , talk ( communicate … )' -> 'to speak, talk'."""
    li = soup(str(li), "html.parser")  # a copy, so the page isn't changed
    for junk in li.find_all(["ul", "ol", "dl", "sup"]):
        junk.decompose()
    text = " ".join(li.get_text(" ").split())
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\s*\([^()]*\)", "", text)
    text = re.sub(r"\s+([,;:.])", r"\1", text).split(";")[0].strip(" .,:")
    return ", ".join(part.strip() for part in text.split(",")[:3])[:80]  # 'to erase, to efface, to obliterate'


def _join_senses(senses, limit=100):
    """Senses shortened and joined, cut at a sense boundary: 'to erase, to efface; to study, to learn'."""
    joined = ""
    for sense in dict.fromkeys(part.strip() for s in senses for part in s.split("; ")):
        short = ", ".join(sense.split(", ")[:2])
        if short and len(joined) + len(short) + 2 <= limit:
            joined = f"{joined}; {short}" if joined else short
    return joined


def english_meaning(word, pos, lemma=None):
    """A short English meaning from Wiktionary, or "" if none was found.

    For Arabic, `lemma` is the vowelled past tense, to pick the right verb on a page
    shared by several (دَرَسَ "to study" vs دَرَّسَ "to teach")."""
    language, wanted = MEANING_LANGUAGES.get(pos), MEANING_POS.get(pos)
    if not language:
        return ""
    page_title = strip_tashkeel(word) if pos == "ar" else word
    contact = f" ({WIKTIONARY_CONTACT})" if WIKTIONARY_CONTACT else ""
    response = requests.get(WIKTIONARY_URL + urllib.parse.quote(page_title), timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": HEADERS["User-Agent"] + contact})
    heading = soup(response.text, "html.parser").find(id=language) if response.status_code == 200 else None
    if heading is None:
        return ""
    start = heading.find_parent(class_="mw-heading2") or heading
    current_pos, headword, senses = None, None, []  # senses: (headword, first sense) under the wanted POS
    for node in start.find_all_next(["div", "h3", "h4", "h5", "strong", "ol"]):
        if start in node.parents:
            continue
        classes = node.get("class") or []
        if "mw-heading2" in classes:
            break
        if node.name in ("h3", "h4", "h5"):
            current_pos = node.get_text(" ", strip=True)
        elif node.name == "strong" and "headword" in classes:
            headword = unicodedata.normalize("NFC", node.get_text(strip=True))
        elif node.name == "ol" and current_pos == wanted and not node.find_parent("ol") and node.find("li"):
            # Arabic verbs often have several unrelated senses, not ordered by how common they
            # are (دَرَسَ: to erase; to thresh; to study), so keep a few; elsewhere the first.
            items = node.find_all("li", recursive=False)[:3 if pos == "ar" else 1]
            senses.append((headword, _join_senses(_short_sense(li) for li in items)))
    if lemma:
        lemma = unicodedata.normalize("NFC", lemma)
        exact = [sense for head, sense in senses if head == lemma]
        if exact or len(senses) > 1:
            return _join_senses(exact)  # several verbs and none is ours: better nothing than wrong
    return senses[0][1] if senses else ""


# ---- Swedish verbs from Wiktionary ----
# The Swedish table has the forms learners drill (present talar, preterite talade, supine
# talat, imperative tala), plus participles and the passive. Perfect and pluperfect are
# built from the supine (har talat, hade talat).

WIKTIONARY_POSES = set(FI_POS) | {"sv"}  # languages whose forms all come from Wiktionary
PARTS_OF_SPEECH = {"Noun", "Adjective", "Verb", "Numeral", "Pronoun", "Adverb", "Proper noun", "Preposition"}


def _forms(cell, lang):
    """The common forms in a cell, joined with SEPARATOR ('' for none or '—')."""
    forms = []
    for span in cell.find_all(lang=lang) if cell else []:
        if any(c.startswith("rare-") for c in span.get("class", [])):
            continue
        text = " ".join(span.get_text(" ").split())
        if text and text != "—" and text not in forms:
            forms.append(text)
    return SEPARATOR.join(forms)


def parse_swedish_conjugation(table):
    rows = {}
    for tr in _top_rows(table):
        cells = _cells(tr)
        if cells and cells[0].name == "th":
            rows.setdefault(_label(cells[0]), [c for c in cells[1:] if c.name == "td"])
    get = lambda row, i: _forms(rows.get(row, [None] * 9)[i] if len(rows.get(row, [])) > i else None, "sv")
    supine = get("supine", 0)
    with_aux = lambda aux: SEPARATOR.join(f"{aux} {s}" for s in supine.split(SEPARATOR)) if supine else ""
    return {
        "Present": {"": get("indicative", 0)},
        "Preterite": {"": get("indicative", 1)},
        "Supine": {"": supine},
        "Imperative": {"": get("imperative", 0)},
        "Perfect": {"": with_aux("har")},
        "Pluperfect": {"": with_aux("hade")},
        "Participles": {"Present": get("present participle", 0), "Past": get("past participle", 0)},
        "Passive": {"Infinitive": get("infinitive", 1), "Present": get("indicative", 2),
                    "Preterite": get("indicative", 3), "Supine": get("supine", 1)},
    }


def wiktionary_swedish(word):
    """Forms of the Swedish verb `word` from Wiktionary (the first Verb entry's table), or None."""
    contact = f" ({WIKTIONARY_CONTACT})" if WIKTIONARY_CONTACT else ""
    response = requests.get(WIKTIONARY_URL + urllib.parse.quote(word), timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": HEADERS["User-Agent"] + contact})
    heading = soup(response.text, "html.parser").find(id="Swedish") if response.status_code == 200 else None
    if heading is None:
        return None
    start = heading.find_parent(class_="mw-heading2") or heading
    current_pos = None
    for node in start.find_all_next(["div", "h3", "h4", "h5", "table"]):
        if start in node.parents:
            continue
        classes = node.get("class") or []
        if "mw-heading2" in classes:
            break
        if node.name in ("h3", "h4", "h5"):
            title = node.get_text(" ", strip=True)
            if title in PARTS_OF_SPEECH:  # not sub-headings like "Conjugation"
                current_pos = title
        elif node.name == "table" and "inflection-table" in classes and current_pos == "Verb":
            forms = parse_swedish_conjugation(node)
            return forms if forms["Present"][""] else None
    return None


# ---- Russian stress ----
# Each cooljugator.com cell shows the plain form, and its data-stressed attribute has the
# same form with an acute accent (U+0301) after the stressed vowel: де́лаю, столы́.
# Forms with ё have no attribute (ё is always stressed), so those keep the plain text.
STRESS_MARK = "\u0301"
GRAVE_MARK = "\u0300"  # secondary stress, occasionally used in compounds


def strip_stress(text):
    return text.replace(STRESS_MARK, "").replace(GRAVE_MARK, "")


def stressed_form(entry):
    """The form in a meta-form div, with its stress mark when the site has one."""
    cell = entry.find_parent(attrs={"data-stressed": True})
    if cell is not None:
        stressed = cell["data-stressed"].strip()
        if stressed and strip_stress(stressed) == entry.text.strip():  # the same form, just marked
            return stressed
    return entry.text


# Cell ids on cooljugator.com's Russian pages ("_no_accent" removed) -> {tense: {label: id}}.
# Forms are read by id, not position: some pages have extra cells in the middle, e.g. the
# locative and partitive of час (в часу́, часу́), which would shift every case after them.
RU_PERSONS = {"Я": "1", "Ты": "2", "Он": "3", "Мы": "4", "Вы": "5", "Они": "6"}
RU_CASES = {"Nom": "nom", "Gen": "gen", "Dat": "dat", "Acc": "acc", "Inst": "instr", "Prep": "prep"}
RU_GENDERS = {"Masc": "M", "Fem": "F", "Neu": "N", "Pl": "P"}
RU_CELLS = {
    "ru": {
        "Present": {label: f"present{n}" for label, n in RU_PERSONS.items()},
        "Imperative": {"Ты": "imperative2", "Вы": "imperative5"},
        "Past": {"Masc": "past_singM", "Fem": "past_singF", "Neu": "past_singN", "Pl": "past_plur"},
        "Participle": {"Pres Act": "present_active_participle", "Past Act": "past_active_participle",
                       "Pres Past": "present_passive_participle", "Past Pass": "past_passive_participle",
                       "Pres Adv": "present_adverbial_participle", "Past Adv": "past_adverbial_participle"},
    },
    # Nom Sing is left out: it's the word itself.
    "run": {**{f"{case} Pl": {"": f"{cid}_P"} for case, cid in RU_CASES.items()},
            **{f"{case} Sing": {"": f"{cid}_S"} for case, cid in RU_CASES.items() if case != "Nom"}},
    "rua": {tense: {gender: f"{cid}_{g}" for gender, g in RU_GENDERS.items()}
            for tense, cid in {"Nom": "nom", "Gen": "gen", "Dat": "dat", "Anim Acc": "acc_anim",
                               "Inan Acc": "acc_inanim", "Inst": "instr", "Prep": "prep",
                               "Short": "short"}.items()},
}


def parse_russian(page, pos):
    """All forms of a Russian verb, noun or adjective with stress marks, or None if the page has none."""
    found = {}
    for entry in page.find_all("div", class_="meta-form"):
        cell = entry.find_parent(id=True)
        if cell is not None:
            found.setdefault(cell["id"].removesuffix("_no_accent"), stressed_form(entry))
    for n in RU_PERSONS.values():  # perfective verbs have a future (прочитаю) instead; it's quizzed as Present
        if f"future{n}" in found:
            found.setdefault(f"present{n}", found[f"future{n}"])
    result = {tense: {label: found[cid] for label, cid in labels.items() if cid in found}
              for tense, labels in RU_CELLS[pos].items()}
    result = {tense: forms for tense, forms in result.items() if forms}
    return result or None


def get_page(word, pos):
    if pos == "sv":
        return wiktionary_swedish(word)
    if pos in FI_POS:
        found = wiktionary_finnish(word, pos)
        return found[1] if found else None
    if pos == 'ar':
        word = strip_tashkeel(word)  # the site only finds verbs written without vowel marks
    url = get_url(word, pos)
    response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
    page = soup(response.text, "html.parser")
    if pos == 'ar':
        return parse_arabic(page)
    return parse_russian(page, pos)



def refresh_dict_from_json():
    jsondict.clear()
    with open(json_file, 'r', encoding='utf-8') as file:
        data:list = json.load(file)
        for i in range(len(data)):
            jsondict.append(data[i])


def refresh_json_from_dict():
    """Save jsondict to words.json without ever leaving a half-written file.

    Writes to a temporary file first and swaps it in, so a crash mid-save keeps the old
    file. The previous version is kept as words.json.bak, plus one snapshot per day in
    backups/ for the last BACKUP_DAYS days."""
    with save_lock:
        data = json.dumps(list(jsondict), ensure_ascii=False)
        if os.path.exists(json_file):
            shutil.copy2(json_file, json_file + ".bak")
            _daily_backup()
        _atomic_write(json_file, data)


def _daily_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = os.path.join(BACKUP_DIR, f"words-{date.today().isoformat()}.json")
    if not os.path.exists(today):
        shutil.copy2(json_file, today)
    oldest = f"words-{(date.today() - timedelta(days=BACKUP_DAYS)).isoformat()}.json"
    for name in os.listdir(BACKUP_DIR):
        if name.startswith("words-") and name < oldest:
            os.remove(os.path.join(BACKUP_DIR, name))


def strip_blanks(word_dict):
    """Drop forms the site leaves blank (e.g. passive participles of intransitive verbs),
    and tenses left with no forms. Blank answers would be impossible quiz questions."""
    if not word_dict:
        return word_dict
    cleaned = {}
    for tense, forms in word_dict.items():
        forms = {label: answer for label, answer in forms.items() if answer.strip() not in BLANK_FORMS}
        if forms:
            cleaned[tense] = forms
    return cleaned


def lookup(word, pos):
    """The word's forms with blanks removed, or None if the site doesn't have it."""
    word_dict = strip_blanks(get_page(word, pos))
    return word_dict or None


def add_to_json_list(list_key, word):
    """For scripts. The bot looks words up with lookup() and saves them itself."""
    pos = jsondict[int(list_key)]['pos']
    if pos == 'ar':
        word, word_dict = lookup_arabic(word)
    else:
        word_dict = lookup(word, pos)
    if word_dict:
        jsondict[int(list_key)]['words'][word] = word_dict
        refresh_json_from_dict()
        return True
    return False


def get_url(verb, pos):
    if pos in FI_POS:
        return WIKTIONARY_URL + verb + "#Finnish"
    if pos == "sv":
        return WIKTIONARY_URL + verb + "#Swedish"
    return "https://cooljugator.com/" + pos + "/" + verb


#refresh_dict_from_json()
#add_to_json_list(1, "работать")
#get_page('читать')

def refresh_layout():
    refresh_dict_from_json()
    for word_dict in jsondict:
        for word_key in word_dict['words'].keys():
            word_dict['words'][word_key] = get_page(word_key)
    refresh_json_from_dict()

def remove_blanks():
    refresh_dict_from_json()
    for word_list in jsondict:
        for word_key, word_dict in word_list['words'].items():
            if word_dict:  # words saved with no forms (failed lookups) stay as they are
                word_list['words'][word_key] = strip_blanks(word_dict)
    refresh_json_from_dict()

#remove_blanks()


#print(str(get_page("знать")).replace("'", '"'))