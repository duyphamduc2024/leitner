#!/usr/bin/env python3
"""Build data/concepts.json - the multilingual word table behind every deck.

A *concept* is one meaning with a word in each language we support:

    {"id": 0, "en": ["hello","hi"], "es": ["hola"], "vi": ["xin chào"],
     "zh": ["你好"], "pinyin": "nǐ hǎo", "pos": "interj", "src": "core"}

Because every concept carries all four languages, any (learn, know) pair works
off the same table - Spanish from English, English from Vietnamese, Mandarin
from Vietnamese and so on - and a card is the same idea whichever way round
you drill it.

Two tiers, concatenated in order:

  1. CORE - data/core_concepts.tsv, written by hand. Clean one-sense words in
     all four languages. Spanish nouns carry their article; Mandarin carries
     tone-marked pinyin. These are the words you meet first, so they are worth
     doing by hand.

  2. AUTO - generated out to --limit, ordered by English corpus frequency and
     filled from three public sources:

       English order  OpenSubtitles 2018 frequency list (hermitdave)
       Spanish        FreeDict eng-spa, ~64k headwords (Wiktionary-derived)
       Mandarin       CC-CEDICT, ~120k entries, with pinyin
       Vietnamese     Vietnamese Wiktionary via kaikki.org, ~123k entries

     For each English concept the candidate words in a language are ranked by
     *reverse consistency* first - a word whose own primary sense is this
     concept wins - and by that language's corpus frequency second.

Not every concept exists in every language, so the table is sparse and each
pair has its own usable word count. The app reports that per pair.

    python3 build_concepts.py              # everything the sources support
    python3 build_concepts.py --limit 4000 # stop at 4000 words per language
    python3 build_concepts.py --core-only  # no network, the curated core only
"""
import argparse, collections, gzip, json, os, re, sys, tarfile, unicodedata
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".wordlist-cache")
CORE_TSV = os.path.join(HERE, "data", "core_concepts.tsv")
# Sense-aligned translations harvested from the English Wiktionary's
# translation tables, where one English SENSE carries its Spanish, Vietnamese
# and Mandarin together. Committed to the repo (300 KB) because the source it
# came from is a 3.4 GB download that only covers ~4k English words - too thin
# to be a primary source, but the best answer we have for the words it does
# cover, and those are the common ones.
SENSES_JSON = os.path.join(HERE, "data", "sense_translations.json")
OUT = os.path.join(HERE, "data", "concepts.json")

FREQ = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/%s/%s_50k.txt"
DICT_ES = "https://download.freedict.org/dictionaries/eng-spa/2025.11.23/freedict-eng-spa-2025.11.23.src.tar.xz"
DICT_ES_DICTD = "https://download.freedict.org/dictionaries/eng-spa/2025.11.23/freedict-eng-spa-2025.11.23.dictd.tar.xz"
CEDICT = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
# The Vietnamese Wiktionary's English section: English headwords defined in
# Vietnamese. 123k entries against the 32k of the English Wiktionary's
# Vietnamese section, and the glosses are the everyday word rather than a
# Sino-Vietnamese literary morpheme (tree -> cây, not thụ). The English
# Wiktionary's Vietnamese section was tried first and dropped: it is a third
# the size and its ranking favours literary morphemes (small -> em).
KAIKKI_VI_EN = ("https://kaikki.org/viwiktionary/Ti%E1%BA%BFng%20Anh/"
                "kaikki.org-dictionary-Ti%E1%BA%BFngAnh.jsonl")

LANGS = ("en", "es", "vi", "zh")

# Words we will not put on a card, whatever a dictionary says. These are
# checked against the OUTPUT word in each language, not just the English
# headword - an earlier version only filtered English, which is how
# "john -> putero" reached a card.
PROFANITY = {
    "en": {"shit", "fuck", "fucking", "fucked", "damn", "ass", "asshole",
           "bitch", "crap", "bullshit", "dick", "cunt", "whore", "slut",
           "piss", "bastard", "prick", "wanker", "twat", "bollocks"},
    "es": {"putero", "puta", "puto", "joder", "cojones", "coño", "polla",
           "cabrón", "gilipollas", "follar", "hostia", "pendejo", "chingar",
           "verga", "culo", "zorra", "maricón", "mierda", "cojudo", "pinche"},
    "vi": {"đụ", "địt", "lồn", "cặc", "buồi", "đéo", "đĩ", "điếm"},
    "zh": {"操", "肏", "屌", "妓女", "婊子", "鸡巴", "傻逼", "贱人"},
}

def _plain(text):
    """Lowercase, accent-stripped ASCII.

    Comparing on this keeps the checks immune to how a source composes its
    Unicode accents: the same Vietnamese string can arrive precomposed or
    decomposed, and a literal pattern only ever matches one of them.
    """
    t = unicodedata.normalize("NFD", text.lower()).replace("\u0111", "d")
    return "".join(ch for ch in t if unicodedata.category(ch) != "Mn")


# Wiktionary defines inflected forms by pointing at the base word. Those are
# facts about English grammar rather than translations, so they make useless
# cards: "decorations" -> "so nhieu cua decoration" (plural of decoration).
META_PREFIXES = ("so nhieu", "thi qua khu", "phan tu", "cach viet",
                 "loi viet", "viet tat", "so sanh hon", "cua ")
META_TAIL = re.compile(r"\bcua\s+[a-z]{3,}\s*$")


def is_meta_gloss(text):
    flat = _plain(text).strip()
    return flat.startswith(META_PREFIXES) or bool(META_TAIL.search(flat))


# English words that are overwhelmingly people's names. Every dictionary has
# an entry for the lowercase common noun, but teaching "john" as a word for a
# toilet, or "paul" from whatever sense a dictionary reached for, is noise.
# Only names with no everyday meaning of their own are listed: mark, bill,
# will, rose, grace, frank, may and jack stay, because those are real words.
PERSON_NAMES = set("""
john tom henry joe bob peter paul robert david michael james charles thomas
william richard george edward harry jim tim ted dan carl kevin brian steve
mike chris matt andy tony gary larry jerry ron roy phil alex eric scott adam
jason sam joseph daniel andrew brandon justin ryan jeff greg doug craig
keith derek shawn wayne randy roger ralph eugene arthur albert
mary susan linda nancy karen betty helen sandra donna carol ruth sharon
michelle laura sarah kimberly deborah jessica shirley cynthia angela melissa
brenda amy anna rebecca virginia kathleen pamela martha debra amanda
""".split())


# Place names leak in the same way people's names do - an English frequency
# list built from film subtitles is full of them - and they make poor cards:
# "rome" was producing "thành la mã". Demonyms (german, french) are left in,
# because those are ordinary vocabulary.
PLACE_NAMES = set("""
rome paris london berlin madrid moscow tokyo beijing athens venice vienna
dublin boston chicago vegas hollywood manhattan brooklyn harlem texas
california florida ohio oregon arizona nevada utah kansas alaska hawaii
america africa asia europe australia canada mexico brazil russia germany
france england britain ireland scotland wales spain italy greece egypt
china japan korea india vietnam thailand malaysia indonesia turkey poland
hungary austria norway sweden denmark finland holland netherlands belgium
switzerland portugal cuba chile peru colombia argentina israel iran iraq
syria lebanon jordan kuwait yemen somalia kenya nigeria ghana ethiopia
amazon himalayas sahara atlantis chinatown jerusalem baghdad cairo
""".split())


def usable_word(word, lang):
    """A word that is fit to put on a card in this language."""
    w = word.strip().lower()
    if not w or len(w) > 28:
        return False
    if w in PROFANITY.get(lang, ()):  # also catches it as a whole gloss
        return False
    if lang != "zh" and any(part in PROFANITY.get(lang, ()) for part in w.split()):
        return False
    if lang == "vi" and is_meta_gloss(w):
        return False
    return True

# ------------------------------------------------------------------ helpers

def fetch(url, name):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        sys.stderr.write("downloading %s ...\n" % name)
        urllib.request.urlretrieve(url, path)
    return path


def freq_rank(path):
    rank = {}
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            w = line.split(" ")[0].strip().lower()
            if w and w not in rank:
                rank[w] = i
    return rank


def norm_gloss(text):
    """'(coll.) to run away;  flee' -> ['run away', 'flee']"""
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    out = []
    for piece in re.split(r"[;,]", text):
        piece = re.sub(r"\s+", " ", piece).strip().lower()
        piece = re.sub(r"^(to|a|an|the) ", "", piece)
        if re.fullmatch(r"[a-z][a-z '\-]{0,24}", piece):
            out.append(piece)
    return out


# ------------------------------------------------------------------- pinyin

_TONES = {"a": "āáǎà", "e": "ēéěè", "i": "īíǐì",
          "o": "ōóǒò", "u": "ūúǔù", "ü": "ǖǘǚǜ"}


def pinyin_marks(numbered):
    """CC-CEDICT 'nu:3 ren2' -> 'nǚ rén'."""
    out = []
    for syl in numbered.split():
        m = re.fullmatch(r"([A-Za-z:ü]+)([1-5])", syl)
        if not m:
            out.append(syl)
            continue
        base, tone = m.group(1).lower().replace("u:", "ü").replace("v", "ü"), int(m.group(2))
        if tone == 5:
            out.append(base)
            continue
        if "a" in base:
            i = base.index("a"); v = "a"
        elif "e" in base:
            i = base.index("e"); v = "e"
        elif "ou" in base:
            i = base.index("o"); v = "o"
        else:
            vowels = [(k, c) for k, c in enumerate(base) if c in "aeiouü"]
            if not vowels:
                out.append(base); continue
            i, v = vowels[-1]
        out.append(base[:i] + _TONES[v][tone - 1] + base[i + 1:])
    return " ".join(out)


# -------------------------------------------------------------- the sources

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def b64int(s):
    n = 0
    for c in s:
        n = n * 64 + B64.index(c)
    return n


POS_MAP = {"n": "noun", "v": "verb", "adj": "adj", "adv": "adv", "prep": "prep",
           "pron": "pron", "conj": "conj", "interj": "interj", "num": "num",
           "art": "art", "det": "det", "phrase": "phrase"}
POS_SKIP = {"suffix", "prefix", "name", "pn", "proverb", "abbrev",
            "initialism", "symbol", "punct", "character"}


def load_spanish():
    """english headword -> ([spanish words in sense order], pos)."""
    tar = fetch(DICT_ES_DICTD, "eng-spa.dictd.tar.xz")
    ddir = os.path.join(CACHE, "eng-spa")
    if not os.path.exists(os.path.join(ddir, "eng-spa.index")):
        with tarfile.open(tar) as tf:
            tf.extractall(CACHE)
    raw = gzip.open(os.path.join(ddir, "eng-spa.dict.dz"), "rb").read()
    out = {}
    with open(os.path.join(ddir, "eng-spa.index"), encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) < 3:
                continue
            eng, off, ln = p[0].lower(), b64int(p[1]), b64int(p[2])
            if not re.fullmatch(r"[a-z][a-z '\-]*", eng):
                continue
            body = raw[off:off + ln].decode("utf-8", "replace").split("\n")
            if not body:
                continue
            m = re.search(r"<([a-z]+)>\s*$", body[0])
            tag = m.group(1) if m else ""
            if tag in POS_SKIP:
                continue
            for line2 in body[1:]:
                t = line2.strip()
                if not t:
                    continue
                t = re.sub(r"^\d+\.\s*", "", t)
                t = re.sub(r"\s*\d+\.\s*$", "", t).strip()
                if not t or t.startswith("(") or len(t) > 90:
                    break
                words = []
                for cand in t.split(","):
                    cand = cand.strip().lower()
                    if re.fullmatch(r"[a-záéíóúüñ]+( [a-záéíóúüñ]+)?", cand):
                        words.append(cand)
                if words and eng not in out:
                    out[eng] = (words, POS_MAP.get(tag, tag))
                break
    return out


CJK = re.compile(r"^[一-鿿㐀-䶿]+$")
CEDICT_SKIP = re.compile(
    r"^(surname|see |variant |old variant|abbr\.|CL:|used in|erhua|Japanese |"
    r"Korean |classifier|place in|county in|city in|district in|prefecture)", re.I)


def load_chinese():
    """english -> {simplified: (sense_index, pinyin)}"""
    gz = fetch(CEDICT, "cedict.txt.gz")
    inv = collections.defaultdict(dict)
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            m = re.match(r"^(\S+) (\S+) \[([^\]]*)\] /(.*)/\s*$", line)
            if not m:
                continue
            _trad, simp, pin, body = m.groups()
            if not CJK.match(simp) or len(simp) > 4:
                continue
            for i, gloss in enumerate(body.split("/")):
                gloss = gloss.strip()
                if not gloss or CEDICT_SKIP.match(gloss):
                    continue
                # A gloss that is a single capitalised word is a proper noun -
                # 约翰 "John", 汤姆 "Tom" - and the Chinese is a transliteration.
                if re.fullmatch(r"[A-Z][a-z]{2,15}", gloss):
                    continue
                for piece in norm_gloss(gloss):
                    cur = inv[piece].get(simp)
                    if cur is None or i < cur[0]:
                        inv[piece][simp] = (i, pin)
    return inv


VI_WORD = re.compile(
    r"^[a-zàáâãèéêìíòóôõùúăđĩũơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹý]"
    r"[a-zàáâãèéêìíòóôõùúăđĩũơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹý ]*$")
VN_MARKS = set("àáâãèéêìíòóôõùúăđĩũơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗ"
               "ộớờởỡợụủứừửữựỳỵỷỹý")
VI_EN_SKIP_POS = {"name", "character", "romanization", "prefix", "suffix",
                  "phrase", "proverb", "abbrev"}


def load_vietnamese(vi_rank):
    """english -> {vietnamese word: position}

    Entries are English headwords whose definitions are written in
    Vietnamese, e.g. small -> "Nhỏ, bé, chật." Senses come in reading order,
    so the position doubles as a quality signal the same way it does for the
    other dictionaries.
    """
    path = fetch(KAIKKI_VI_EN, "en_in_vi.jsonl")
    out = collections.defaultdict(dict)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            w = (e.get("word") or "").strip().lower()
            if not re.fullmatch(r"[a-z][a-z'\-]{1,24}", w):
                continue
            if e.get("pos") in VI_EN_SKIP_POS:
                continue
            i = 0
            for sense in (e.get("senses") or []):
                for gloss in (sense.get("glosses") or []):
                    gloss = re.sub(r"\([^)]*\)", " ", gloss).strip().rstrip(".").strip()
                    for part in re.split(r"[;,]", gloss):
                        cand = re.sub(r"\s+", " ", part).strip().lower()
                        if not cand or len(cand) > 24 or cand.count(" ") > 3:
                            continue
                        if not VI_WORD.match(cand) or is_meta_gloss(cand):
                            continue
                        # Reject stray non-Vietnamese tokens the dump carries
                        # ("bridge" -> "brit"). A real word is a compound, or
                        # carries a Vietnamese diacritic, or - for the many
                        # genuine plain-ASCII words like "con" and "tay" - is
                        # common enough to sit high in the frequency list.
                        # Subtitle noise lands far down it.
                        if not (" " in cand or (VN_MARKS & set(cand))
                                or vi_rank.get(cand, 10 ** 6) < 12000):
                            continue
                        if cand not in out[w]:
                            out[w][cand] = i
                            i += 1
            if not out[w]:
                out.pop(w, None)
    return out


# --------------------------------------------------------------------- core

def load_sense_aligned():
    """english word -> {es, vi, zh, _: sense} for the words Wiktionary's
    translation tables cover. Empty if the file is absent."""
    if not os.path.exists(SENSES_JSON):
        return {}
    with open(SENSES_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def load_core():
    concepts = []
    with open(CORE_TSV, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 6:
                sys.stderr.write("core_concepts.tsv line %d: expected 6 columns\n" % n)
                continue
            en, es, vi, zh, pin, pos = parts
            split = lambda s: [x.strip() for x in s.split(",") if x.strip()]
            concepts.append({
                "en": split(en), "es": split(es), "vi": split(vi),
                "zh": split(zh), "pinyin": pin, "pos": pos, "src": "core",
            })
    return concepts


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="stop once every language has this many words; "
                         "0 (the default) builds everything the sources support")
    ap.add_argument("--max-concepts", type=int, default=40000,
                    help="stop growing the table past this, whatever the coverage")
    ap.add_argument("--core-only", action="store_true")
    args = ap.parse_args()

    concepts = load_core()
    sys.stderr.write("core: %d concepts\n" % len(concepts))

    def en_keys(gloss):
        """Every spelling of an English gloss that should block a duplicate.

        The core writes verbs as "to see" and senses as "to be (state,
        location)". Without this the generated tier would happily add a
        second, worse "see" card from the dictionary ("see" -> sede).
        """
        g = re.sub(r"\([^)]*\)", " ", gloss).lower()
        g = re.sub(r"\s+", " ", g).strip()
        out = {g}
        out.add(re.sub(r"^(to|a|an|the) ", "", g))
        return {x for x in out if x}

    taken = {l: set() for l in LANGS}
    have = {l: 0 for l in LANGS}
    for c in concepts:
        for l in LANGS:
            if c[l]:
                have[l] += 1
            if l == "en":
                for g in c[l]:
                    taken["en"].update(en_keys(g))
            else:
                taken[l].update(w.lower() for w in c[l])

    if not args.core_only:
        en_rank = freq_rank(fetch(FREQ % ("en", "en"), "en_50k.txt"))
        es_rank = freq_rank(fetch(FREQ % ("es", "es"), "es_50k.txt"))
        vi_rank = freq_rank(fetch(FREQ % ("vi", "vi"), "vi_50k.txt"))
        zh_rank = freq_rank(fetch(FREQ % ("zh_cn", "zh_cn"), "zh_cn_50k.txt"))

        sys.stderr.write("loading dictionaries ...\n")
        es_dict = load_spanish()
        zh_inv = load_chinese()
        vi_inv = load_vietnamese(vi_rank)
        aligned = load_sense_aligned()
        sys.stderr.write("  es %d  zh %d  vi %d english keys  |  %d sense-aligned\n"
                         % (len(es_dict), len(zh_inv), len(vi_inv), len(aligned)))

        # English function words, interjections and irregular inflections.
        # The curated core already teaches the ones worth knowing, and the
        # dictionaries mistranslate the rest badly ("are" -> área, the unit
        # of 100 m²; "don" -> cabellar).
        STOP_EN = set("""
        the a an of to in on at by for with from into onto upon as is are am
        was were be been being do does did done doing have has had having
        will would shall should can could may might must ought let lets
        i you he she it we they me him her us them my your his its our their
        mine yours hers ours theirs myself yourself himself herself itself
        this that these those there here who whom whose which what when where
        why how not no nor so than then too very just only also even still yet
        but and or if because while although though since until unless
        oh ah eh uh um hey hi yeah yep nope okay ok ya yo huh wow ouch oops
        gonna wanna gotta gotcha kinda sorta lemme gimme aint
        went gone goes going came comes coming got gotten gets getting
        said says saying saw seen sees seeing took taken takes taking
        made makes making knew known knows knowing thought thinks thinking
        told tells telling gave given gives giving found finds finding
        felt feels feeling left leaves leaving put puts putting
        kept keeps keeping held holds holding brought brings bringing
        stood stands standing sat sits sitting ran runs running
        ate eaten eats eating drank drunk drinks drinking
        wrote written writes writing spoke spoken speaks speaking
        broke broken breaks breaking chose chosen chooses choosing
        stole stolen steals stealing wore worn wears wearing
        shit fuck fucking fucked damn damned ass asshole bitch crap bullshit
        dick cunt whore slut piss pissed bastard hell goddamn
        mr mrs ms dr sir madam
        don aint nope yup huh duh hmm shh wa oi ha heh ho
        """.split())

        BAD_EN = {"thee", "thy", "thine", "ye", "doth", "hast", "prithee",
                  "aye", "lest", "yea", "nay"} | STOP_EN

        def inflection_of_taken(w):
            """'studies'/'studied'/'studying' when 'study' is already a card."""
            stems = set()
            for suf, add in (("s", ""), ("es", ""), ("ies", "y"), ("ed", ""),
                             ("ed", "e"), ("ied", "y"), ("ing", ""),
                             ("ing", "e"), ("er", ""), ("est", "")):
                if w.endswith(suf) and len(w) - len(suf) >= 3:
                    stems.add(w[:-len(suf)] + add)
            return bool(stems & taken["en"])

        def pick(cands, rank, key):
            """Best word: primary sense first, then corpus frequency."""
            if not cands:
                return None
            best = sorted(cands.items(),
                          key=lambda kv: (min(key(kv[1]), 2),
                                          rank.get(kv[0], 10 ** 6)))[0]
            return best

        # Keep going until every language has reached the target, or we run
        # out of English frequency list. Languages fill at different rates:
        # the Vietnamese source is the smallest, so it sets the pace.
        for en in sorted(en_rank, key=lambda w: en_rank[w]):
            if args.limit and all(have[l] >= args.limit for l in LANGS):
                break
            if len(concepts) >= args.max_concepts:
                break
            if not re.fullmatch(r"[a-z]{3,}", en) or en in BAD_EN:
                continue
            if not usable_word(en, "en") or en in PERSON_NAMES or en in PLACE_NAMES:
                continue
            if en in taken["en"] or inflection_of_taken(en):
                continue
            # Vocabulary gate, to keep proper nouns ("edwards"), slang and
            # subtitle noise out: the word must be a headword in the
            # Wiktionary-derived English-Spanish dictionary, or else be known
            # to BOTH other dictionaries - two independent sources agreeing
            # that it is a real word is evidence enough.
            if en not in es_dict and not (en in zh_inv and en in vi_inv):
                continue

            row = {"en": [en], "es": [], "vi": [], "zh": [], "pinyin": "",
                   "pos": "", "src": "auto"}

            # A sense-aligned entry wins: its three languages are translations
            # of the SAME English sense, where the per-language dictionaries
            # each pick a sense of their own and can disagree wildly.
            fixed = aligned.get(en)
            if fixed:
                for lang in ("es", "vi", "zh"):
                    w = fixed.get(lang)
                    if w and w not in taken[lang] and usable_word(w, lang):
                        row[lang] = [w]
                row["src"] = "aligned"

            got = es_dict.get(en)
            if got:
                row["pos"] = row["pos"] or got[1]
                if not row["es"]:
                    for w in got[0]:
                        if w not in taken["es"] and usable_word(w, "es"):
                            row["es"] = [w]
                            break

            if not row["zh"]:
                b = pick(zh_inv.get(en), zh_rank, lambda v: v[0])
                if b and b[0] not in taken["zh"] and usable_word(b[0], "zh"):
                    row["zh"] = [b[0]]
                    row["pinyin"] = pinyin_marks(b[1][1])

            if not row["vi"]:
                b = pick(vi_inv.get(en), vi_rank, lambda v: v)
                if b and b[0] not in taken["vi"] and usable_word(b[0], "vi"):
                    row["vi"] = [b[0]]

            if not (row["es"] or row["vi"] or row["zh"]):
                continue  # English only - no pair could use it

            concepts.append(row)
            for l in LANGS:
                if row[l]:
                    have[l] += 1
                if l == "en":
                    for g in row[l]:
                        taken["en"].update(en_keys(g))
                else:
                    taken[l].update(w.lower() for w in row[l])

    for i, c in enumerate(concepts):
        c["id"] = i

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(concepts, fh, ensure_ascii=False, separators=(",", ":"))

    counts = {l: sum(1 for c in concepts if c[l]) for l in LANGS}
    sys.stderr.write("wrote %s\n  %d concepts | %s\n"
                     % (OUT, len(concepts),
                        "  ".join("%s %d" % (l, counts[l]) for l in LANGS)))
    short = [l for l in LANGS if args.limit and counts[l] < args.limit]
    if short:
        sys.stderr.write("  note: %s below the %d target - the source "
                         "dictionary is exhausted\n"
                         % (", ".join(short), args.limit))
    pairs = []
    for a in LANGS:
        for b in LANGS:
            if a < b:
                n = sum(1 for c in concepts if c[a] and c[b])
                pairs.append("%s-%s %d" % (a, b, n))
    sys.stderr.write("  pairs: %s\n" % "  ".join(pairs))


if __name__ == "__main__":
    main()
