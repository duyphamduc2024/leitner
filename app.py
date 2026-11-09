#!/usr/bin/env python3
"""Leitner - a 6-box spaced-repetition vocabulary trainer.

Pick a language to learn and the language you already know; the pair is a
*learning session* with its own boxes and progress. One session drills one
language. Switch sessions freely - they do not share cards.

Flask + SQLite, single machine, no auth. State lives in data/leitner.db.

    python3 app.py            # http://localhost:5100
    python3 app.py --port 8000
"""
import argparse
import json
import os
import random
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

from flask import Flask, g, jsonify, render_template, request

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "leitner.db")
CONCEPTS_PATH = os.path.join(HERE, "data", "concepts.json")

DECK_SIZE = 20          # cards drawn per deck
ACTIVE_CAP = 100        # hard cap on cards sitting in boxes 1-4
NEW_DECK_MIN_BOX = 4    # every card of the current deck must reach this box
BOXES = 6
LEARNED_BOX = 6
SCHEMA_VERSION = 4

# Words-learned milestones. The first is one deck, so there is something to
# reach on day one; after that they roughly double.
MILESTONES = (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)

# Vocabulary size mapped onto the CEFR ladder. These are the usual rough
# estimates for how many words sit behind each level - real CEFR also covers
# grammar, listening and speaking, so this is a vocabulary gauge and the UI
# says as much.
# Twenty picture choices for a profile. Emoji rather than image files: they
# need no assets, no licensing and no network, they scale to any size, and
# they stay sharp on every display.
AVATARS = (
    ("fox",       "🦊", "#e8833a"), ("panda",    "🐼", "#8e9aaf"),
    ("penguin",   "🐧", "#4d96ff"), ("owl",      "🦉", "#a9744f"),
    ("frog",      "🐸", "#5bbf6a"), ("turtle",   "🐢", "#3f9d7a"),
    ("butterfly", "🦋", "#6c8cff"), ("octopus",  "🐙", "#c9568f"),
    ("lion",      "🦁", "#e0a52b"), ("tiger",    "🐯", "#e4762c"),
    ("koala",     "🐨", "#8a9ba8"), ("unicorn",  "🦄", "#b07ae0"),
    ("bee",       "🐝", "#d8a81d"), ("dolphin",  "🐬", "#3aa3c9"),
    ("eagle",     "🦅", "#7a6a55"), ("hedgehog", "🦔", "#9c7b5a"),
    ("rabbit",    "🐰", "#d98fa8"), ("whale",    "🐳", "#3f7fc4"),
    ("parrot",    "🦜", "#4fae5e"), ("wolf",     "🐺", "#6f7d8c"),
)
AVATAR_COUNT = len(AVATARS)


def avatar_json(i):
    i = i if 0 <= i < AVATAR_COUNT else 0
    name, glyph, colour = AVATARS[i]
    return {"id": i, "name": name, "glyph": glyph, "colour": colour}


PLACEMENT_QUESTIONS = 20
# Band boundaries for the placement test, in words. They line up with the
# level thresholds below, so the test spends its questions exactly where the
# answer changes which level you are: purely logarithmic bands would have put
# six of twenty questions inside the first eighteen words.
PLACEMENT_EDGES = (0, 50, 100, 250, 500, 1000, 2000, 4000, 7000, 10000)

LEVELS = (
    (0,     "Starting out", "Nothing banked yet"),
    (100,   "Survival",     "Enough for greetings and basic needs"),
    (500,   "A1 · Breakthrough", "Familiar everyday expressions"),
    (1000,  "A2 · Waystage",     "Routine exchanges on familiar matters"),
    (2000,  "B1 · Threshold",    "Most everyday conversation"),
    (4000,  "B2 · Vantage",      "Complex text and abstract topics"),
    (7000,  "C1 · Advanced",     "Fluent, flexible, implicit meaning"),
    (10000, "C2 · Mastery",      "Virtually everything you read or hear"),
)


def level_for(learned):
    """(current level, next level or None, % of the way to the next)."""
    cur = LEVELS[0]
    nxt = None
    for i, lv in enumerate(LEVELS):
        if learned >= lv[0]:
            cur = lv
            nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
        else:
            break
    if nxt is None:
        pct = 100
    else:
        span = nxt[0] - cur[0]
        pct = int(round(100.0 * (learned - cur[0]) / span)) if span else 100
    return {
        "name": cur[1],
        "blurb": cur[2],
        "at": cur[0],
        "next_name": nxt[1] if nxt else None,
        "next_at": nxt[0] if nxt else None,
        "to_next": (nxt[0] - learned) if nxt else 0,
        "pct": max(0, min(100, pct)),
        "top": nxt is None,
    }


LANGUAGES = {
    "en": {"name": "English",          "native": "English",    "voice": "en-US"},
    "es": {"name": "Spanish",          "native": "Español",    "voice": "es-ES"},
    "vi": {"name": "Vietnamese",       "native": "Tiếng Việt", "voice": "vi-VN"},
    "zh": {"name": "Mandarin Chinese", "native": "中文",        "voice": "zh-CN"},
}
LANG_CODES = tuple(LANGUAGES)

app = Flask(__name__)
# local app, edited in place - never serve a stale stylesheet or script
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ---------------------------------------------------------------- concepts

with open(CONCEPTS_PATH, encoding="utf-8") as fh:
    CONCEPTS = json.load(fh)
CONCEPT_BY_ID = {c["id"]: c for c in CONCEPTS}

# For each (learn, know) pair, the concepts that have a word in both, in
# frequency order. This is the deck pool, and every pair has its own length.
#
# Two concepts can share a written form in the language being learned - 是 is
# both "yes" and "to be", bajo is both "low" and "under" - and showing the
# same prompt twice with two different expected answers is unanswerable. So a
# pool keeps only the first concept for any given prompt.
PAIRS = {}
for _a in LANG_CODES:
    for _b in LANG_CODES:
        if _a == _b:
            continue
        _ids, _seen = [], set()
        for _c in CONCEPTS:
            if not (_c[_a] and _c[_b]):
                continue
            _key = ", ".join(_c[_a]).lower()
            if _key in _seen:
                continue
            _seen.add(_key)
            _ids.append(_c["id"])
        PAIRS[(_a, _b)] = _ids


POOL_INDEX = {}


def pair_pool(learn, know):
    return PAIRS.get((learn, know), [])


def pool_index(learn, know):
    """concept id -> its position in this pair's frequency-ordered pool."""
    key = (learn, know)
    if key not in POOL_INDEX:
        POOL_INDEX[key] = {cid: i for i, cid in enumerate(pair_pool(learn, know))}
    return POOL_INDEX[key]


def band_edges(pool_len):
    """Band boundaries for this pair, clipped to what the pool actually has."""
    edges = [e for e in PLACEMENT_EDGES if e < pool_len]
    edges.append(pool_len)
    return edges


def band_quotas(n_bands, total=PLACEMENT_QUESTIONS):
    """Split the questions across the bands as evenly as the count allows."""
    base, extra = divmod(total, n_bands)
    # Spare questions go to the deepest bands, where the bands are widest and
    # a single answer is extrapolated over the most words.
    return [base + (1 if b >= n_bands - extra else 0) for b in range(n_bands)]


def estimate_vocabulary(graded, edges):
    """Estimate how many words of the pool are known.

    graded is [(pool position, was it right)]. Each band is judged on its own
    sample and credited that share of its width, which is the standard way a
    vocabulary-size test extrapolates from a small number of questions.
    """
    total = 0.0
    per_band = []
    for b in range(len(edges) - 1):
        lo, hi = edges[b], edges[b + 1]
        sample = [ok for pos, ok in graded if lo <= pos < hi]
        if not sample:
            per_band.append({"band": b, "from": lo, "to": hi,
                             "asked": 0, "right": 0, "credited": 0})
            continue
        share = sum(1 for ok in sample if ok) / float(len(sample))
        credited = share * (hi - lo)
        total += credited
        per_band.append({"band": b, "from": lo, "to": hi, "asked": len(sample),
                         "right": sum(1 for ok in sample if ok),
                         "credited": int(round(credited))})
    return int(round(total)), per_band


# ---------------------------------------------------------------- grading

_VI_LETTERS = str.maketrans({"đ": "d", "Đ": "D"})


def strip_accents(s):
    s = s.translate(_VI_LETTERS)
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def pinyin_key(s):
    """'nǚ rén' / 'nu:3 ren2' / 'nuren' all reduce to the same key."""
    return re.sub(r"[^a-z]+", "", strip_accents(s.lower()))


def normalize(text, lang):
    """Fold an answer down to what we actually compare."""
    s = text.strip().lower()
    if lang == "zh":
        # Hanzi: ignore spacing and punctuation, compare the characters.
        return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)
    s = strip_accents(s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if lang == "en":
        s = re.sub(r"^(to|a|an|the) ", "", s)
    elif lang == "es":
        s = re.sub(r"^(el|la|los|las|un|una|unos|unas) ", "", s)
    return s


def accepted_answers(concept, lang):
    """Every spelling we will accept when `lang` is the answer side."""
    out = set()
    for word in concept[lang]:
        bare = re.sub(r"\([^)]*\)", " ", word)
        for piece in [word, bare] + bare.split(","):
            n = normalize(piece, lang)
            if n:
                out.add(n)
    return out


def check_answer(concept, lang, answer):
    if not answer.strip():
        return False
    if normalize(answer, lang) in accepted_answers(concept, lang):
        return True
    # Mandarin: accept pinyin too, with or without tone marks, since typing
    # hanzi needs an IME.
    if lang == "zh" and concept.get("pinyin"):
        key = pinyin_key(answer)
        if key and key == pinyin_key(concept["pinyin"]):
            return True
    return False


# ----------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS profile (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    avatar     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    learn_lang TEXT NOT NULL,
    know_lang  TEXT NOT NULL,
    next_index INTEGER NOT NULL DEFAULT 0,
    deck_count INTEGER NOT NULL DEFAULT 0,
    placement  INTEGER NOT NULL DEFAULT 0,
    placed_at  TEXT,
    created_at TEXT NOT NULL,
    last_used  TEXT NOT NULL,
    UNIQUE (profile_id, learn_lang, know_lang)
);
CREATE TABLE IF NOT EXISTS card (
    session_id    INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    concept_id    INTEGER NOT NULL,
    box           INTEGER NOT NULL DEFAULT 1,
    deck          INTEGER NOT NULL,
    seen_in_learn INTEGER NOT NULL DEFAULT 0,
    retired       INTEGER NOT NULL DEFAULT 0,
    n_correct     INTEGER NOT NULL DEFAULT 0,
    n_wrong       INTEGER NOT NULL DEFAULT 0,
    learned_at    TEXT,
    PRIMARY KEY (session_id, concept_id)
);
CREATE INDEX IF NOT EXISTS card_box ON card(session_id, box, retired);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    """Create the schema, rebuilding it if an older layout is in the file."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    version = 0
    if "meta" in have:
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        version = int(row[0]) if row else 0
    if have and version == 3:
        print("  migrating database to schema 4 (profile pictures)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(profile)").fetchall()}
        if "avatar" not in cols:
            conn.execute("ALTER TABLE profile ADD COLUMN avatar INTEGER NOT NULL DEFAULT 0")
        version = SCHEMA_VERSION
    if have and version == 2:
        # v2 -> v3 only adds placement columns, so migrate rather than wipe.
        print("  migrating database to schema 3 (placement)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session)").fetchall()}
        if "placement" not in cols:
            conn.execute("ALTER TABLE session ADD COLUMN placement INTEGER NOT NULL DEFAULT 0")
        if "placed_at" not in cols:
            conn.execute("ALTER TABLE session ADD COLUMN placed_at TEXT")
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(profile)").fetchall()}
        if "avatar" not in pcols:
            conn.execute("ALTER TABLE profile ADD COLUMN avatar INTEGER NOT NULL DEFAULT 0")
        version = SCHEMA_VERSION
    if have and version != SCHEMA_VERSION:
        # Pre-session database (single hard-coded language). Nothing in it maps
        # onto language sessions, so start clean rather than guess.
        print("  database uses an older layout - rebuilding it")
        for t in ("card", "session", "profile", "meta"):
            conn.execute("DROP TABLE IF EXISTS %s" % t)
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------- profile/session

def get_profile(name=None):
    """The named profile, else the only one, else None.

    Deliberately does not invent a profile: the app opens by asking who you
    are, and silently creating a "default" behind that would make the
    greeting a lie.
    """
    conn = db()
    if name:
        # A name that does not exist is an error, not a reason to hand back
        # somebody else's profile.
        return conn.execute("SELECT * FROM profile WHERE name = ?", (name,)).fetchone()
    rows = conn.execute("SELECT * FROM profile ORDER BY id").fetchall()
    return rows[0] if len(rows) == 1 else None


def profile_json(row):
    return {"name": row["name"],
            "avatar": avatar_json(row["avatar"] if "avatar" in row.keys() else 0),
            "created_at": (row["created_at"] or "")[:10]}


def all_profiles():
    return [profile_json(r) for r in
            db().execute("SELECT * FROM profile ORDER BY id").fetchall()]


def get_session(prof, session_id=None):
    """The requested session, else the most recently used one, else None."""
    conn = db()
    if session_id:
        row = conn.execute("SELECT * FROM session WHERE id = ? AND profile_id = ?",
                           (session_id, prof["id"])).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM session WHERE profile_id = ? ORDER BY last_used DESC LIMIT 1",
        (prof["id"],)).fetchone()


def touch_session(sess):
    db().execute("UPDATE session SET last_used = ? WHERE id = ?", (now(), sess["id"]))
    db().commit()


def session_learned(session_id):
    return db().execute(
        "SELECT COUNT(*) FROM card WHERE session_id = ? AND learned_at IS NOT NULL",
        (session_id,)).fetchone()[0]


def session_json(row):
    learned = session_learned(row["id"])
    placement = row["placement"] if "placement" in row.keys() else 0
    vocab = placement + learned
    return {
        "id": row["id"],
        "learned": learned,
        "placement": placement,
        "placed_at": (row["placed_at"] or "")[:10] if "placed_at" in row.keys() else "",
        "vocab": vocab,
        "level": level_for(vocab),
        "learn": row["learn_lang"],
        "know": row["know_lang"],
        "learn_name": LANGUAGES[row["learn_lang"]]["name"],
        "know_name": LANGUAGES[row["know_lang"]]["name"],
        "learn_native": LANGUAGES[row["learn_lang"]]["native"],
        "voice": LANGUAGES[row["learn_lang"]]["voice"],
        "answer_voice": LANGUAGES[row["know_lang"]]["voice"],
        "deck": row["deck_count"],
        "pool": len(pair_pool(row["learn_lang"], row["know_lang"])),
        "used": row["next_index"],
        "label": "%s from %s" % (LANGUAGES[row["learn_lang"]]["name"],
                                 LANGUAGES[row["know_lang"]]["name"]),
        "last_used": row["last_used"],
        "cards": db().execute(
            "SELECT COUNT(*) FROM card WHERE session_id = ? AND retired = 0",
            (row["id"],)).fetchone()[0],
    }


# -------------------------------------------------------------- state model

def card_rows(sid):
    return db().execute(
        "SELECT * FROM card WHERE session_id = ? AND retired = 0 ORDER BY deck, concept_id",
        (sid,)).fetchall()


def card_json(row, sess, reveal=False):
    c = CONCEPT_BY_ID[row["concept_id"]]
    learn, know = sess["learn_lang"], sess["know_lang"]
    out = {
        "concept_id": row["concept_id"],
        "prompt": ", ".join(c[learn]),
        # What audio should say. The prompt lists every accepted variant
        # ("hello, hi"), and speaking all of them sounds like the word is
        # being repeated, so audio gets the primary one only.
        "speak": c[learn][0] if c[learn] else "",
        "pinyin": c.get("pinyin", "") if learn == "zh" else "",
        "pos": c.get("pos", ""),
        "box": row["box"],
        "deck": row["deck"],
        "n_correct": row["n_correct"],
        "n_wrong": row["n_wrong"],
    }
    if reveal:
        out["answer"] = ", ".join(c[know])
        out["answer_pinyin"] = c.get("pinyin", "") if know == "zh" else ""
    return out


def build_state(prof, sess):
    conn = db()
    sessions = [] if prof is None else [session_json(r) for r in conn.execute(
        "SELECT * FROM session WHERE profile_id = ? ORDER BY last_used DESC",
        (prof["id"],)).fetchall()]

    base = {
        "profile": profile_json(prof) if prof is not None else None,
        "profiles": all_profiles(),
        "avatars": [avatar_json(i) for i in range(AVATAR_COUNT)],
        "sessions": sessions,
        "languages": {k: dict(v, code=k) for k, v in LANGUAGES.items()},
        "pair_counts": {"%s|%s" % (a, b): len(ids) for (a, b), ids in PAIRS.items()},
        "levels": [{"at": a, "name": n, "blurb": bl} for a, n, bl in LEVELS],
        "milestones": list(MILESTONES),
        "config": {
            "deck_size": DECK_SIZE,
            "active_cap": ACTIVE_CAP,
            "new_deck_min_box": NEW_DECK_MIN_BOX,
            "boxes": BOXES,
        },
    }
    if prof is None or sess is None:
        base["session"] = None
        return base

    sid = sess["id"]
    rows = card_rows(sid)
    learn = sess["learn_lang"]

    boxes = []
    for b in range(1, BOXES + 1):
        in_box = [r for r in rows if r["box"] == b]
        boxes.append({
            "box": b,
            "count": len(in_box),
            "words": [", ".join(CONCEPT_BY_ID[r["concept_id"]][learn]) for r in in_box],
        })

    current_deck = sess["deck_count"]
    deck_rows = [r for r in rows if r["deck"] == current_deck]
    active = [r for r in rows if r["box"] <= NEW_DECK_MIN_BOX]
    unlearned_box1 = [r for r in rows if r["box"] == 1 and not r["seen_in_learn"]]

    pool = pair_pool(learn, sess["know_lang"])
    words_left = sess["next_index"] < len(pool)
    deck_ready = bool(deck_rows) and all(r["box"] >= NEW_DECK_MIN_BOX for r in deck_rows)
    room = len(active) + DECK_SIZE <= ACTIVE_CAP

    if current_deck == 0:
        reason = "Load your first deck of %d words to begin." % DECK_SIZE
        can_new_deck = words_left
    elif not words_left:
        reason = ("You have reached the end of the %d words available for %s."
                  % (len(pool), session_json(sess)["label"]))
        can_new_deck = False
    elif not deck_ready:
        behind = sum(1 for r in deck_rows if r["box"] < NEW_DECK_MIN_BOX)
        reason = ("%d card%s of deck %d still below box %d."
                  % (behind, "" if behind == 1 else "s", current_deck, NEW_DECK_MIN_BOX))
        can_new_deck = False
    elif not room:
        reason = ("Boxes 1-4 hold %d of %d cards. Promote %d more into box 5 or 6 "
                  "to make room for a new deck."
                  % (len(active), ACTIVE_CAP, len(active) + DECK_SIZE - ACTIVE_CAP))
        can_new_deck = False
    else:
        reason = ("Deck %d is at box %d or better. Ready for %d more."
                  % (current_deck, NEW_DECK_MIN_BOX, DECK_SIZE))
        can_new_deck = True

    learned = session_learned(sid)

    sj = session_json(sess)
    base.update({
        "session": sj,
        "boxes": boxes,
        "deck": {
            "number": current_deck,
            "size": DECK_SIZE,
            "can_new_deck": can_new_deck,
            "reason": reason,
        },
        "learned": learned,
        "placement": sj["placement"],
        "vocab": sj["vocab"],
        "level": sj["level"],
        "next_milestone": next((m for m in MILESTONES if m > learned), None),
        "active_count": len(active),
        "active_cap": ACTIVE_CAP,
        "total_cards": len(rows),
        "words_available": len(pool),
        "words_used": sess["next_index"],
        "needs_learn": len(unlearned_box1),
        "can_test_box1": len(unlearned_box1) == 0 and any(r["box"] == 1 for r in rows),
    })
    return base


def body():
    return request.get_json(silent=True) or {}


def current(payload=None):
    """(profile, session) for this request. Either may be None."""
    src = payload if payload is not None else request.args
    prof = get_profile(src.get("profile"))
    if prof is None:
        return None, None
    sid = src.get("session")
    try:
        sid = int(sid) if sid not in (None, "") else None
    except (TypeError, ValueError):
        sid = None
    return prof, get_session(prof, sid)


# ------------------------------------------------------------------- routes

@app.context_processor
def inject_static_version():
    def static_v(name):
        try:
            stamp = int(os.path.getmtime(os.path.join(HERE, "static", name)))
        except OSError:
            stamp = 0
        return "/static/%s?v=%d" % (name, stamp)
    return {"static_v": static_v}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    prof, sess = current()
    return jsonify(build_state(prof, sess))


@app.route("/api/profile", methods=["POST"])
def api_profile():
    """Create a profile, or switch to an existing one by name."""
    b = body()
    name = str(b.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Give the profile a name."}), 400
    if len(name) > 40:
        return jsonify({"error": "That name is too long (40 characters max)."}), 400
    try:
        avatar = int(b.get("avatar", 0))
    except (TypeError, ValueError):
        avatar = 0
    avatar = avatar if 0 <= avatar < AVATAR_COUNT else 0

    conn = db()
    existing = conn.execute("SELECT * FROM profile WHERE name = ?", (name,)).fetchone()
    if existing is None:
        conn.execute("INSERT INTO profile (name, avatar, created_at) VALUES (?, ?, ?)",
                     (name, avatar, now()))
        conn.commit()
    elif b.get("avatar") is not None and b.get("update_avatar"):
        conn.execute("UPDATE profile SET avatar = ? WHERE id = ?",
                     (avatar, existing["id"]))
        conn.commit()
    prof = get_profile(name)
    return jsonify(build_state(prof, get_session(prof)))


@app.route("/api/profile/exists")
def api_profile_exists():
    name = (request.args.get("name") or "").strip()
    row = db().execute("SELECT id FROM profile WHERE name = ?", (name,)).fetchone()
    return jsonify({"exists": row is not None})


@app.route("/api/session", methods=["POST"])
def api_session():
    """Start or switch to the (learn, know) session."""
    b = body()
    prof = get_profile(b.get("profile"))
    if prof is None:
        return jsonify({"error": "Choose a profile first."}), 409
    learn, know = b.get("learn"), b.get("know")
    if learn not in LANGUAGES or know not in LANGUAGES:
        return jsonify({"error": "unknown language"}), 400
    if learn == know:
        return jsonify({"error": "Pick two different languages."}), 400
    if not pair_pool(learn, know):
        return jsonify({"error": "No shared vocabulary for that pair."}), 400

    conn = db()
    row = conn.execute(
        "SELECT * FROM session WHERE profile_id = ? AND learn_lang = ? AND know_lang = ?",
        (prof["id"], learn, know)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO session (profile_id, learn_lang, know_lang, created_at, last_used)"
            " VALUES (?, ?, ?, ?, ?)", (prof["id"], learn, know, now(), now()))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM session WHERE profile_id = ? AND learn_lang = ? AND know_lang = ?",
            (prof["id"], learn, know)).fetchone()
    touch_session(row)
    return jsonify(build_state(prof, get_session(prof, row["id"])))


@app.route("/api/new_deck", methods=["POST"])
def api_new_deck():
    prof, sess = current(body())
    if sess is None:
        return jsonify({"error": "no session"}), 409
    state = build_state(prof, sess)
    if not state["deck"]["can_new_deck"]:
        return jsonify({"error": state["deck"]["reason"]}), 409

    conn = db()
    pool = pair_pool(sess["learn_lang"], sess["know_lang"])
    have = {r[0] for r in conn.execute(
        "SELECT concept_id FROM card WHERE session_id = ?", (sess["id"],)).fetchall()}
    # Walk forward from the start point, skipping anything this session has
    # already seen. Placement and a manual level change both move the start
    # point, possibly backwards, so a plain slice could hand out duplicates
    # and silently produce a short deck.
    start = sess["next_index"]
    chunk, i = [], start
    while i < len(pool) and len(chunk) < DECK_SIZE:
        if pool[i] not in have:
            chunk.append(pool[i])
        i += 1
    if not chunk:
        return jsonify({"error": "no words left"}), 409
    deck_no = sess["deck_count"] + 1
    conn.executemany(
        "INSERT OR IGNORE INTO card (session_id, concept_id, box, deck) VALUES (?, ?, 1, ?)",
        [(sess["id"], cid, deck_no) for cid in chunk])
    conn.execute("UPDATE session SET next_index = ?, deck_count = ?, last_used = ?"
                 " WHERE id = ?", (i, deck_no, now(), sess["id"]))
    conn.commit()
    return jsonify(build_state(prof, get_session(prof, sess["id"])))


@app.route("/api/learn")
def api_learn():
    prof, sess = current()
    if sess is None:
        return jsonify({"error": "no session"}), 409
    rows = [r for r in card_rows(sess["id"]) if r["box"] == 1]
    return jsonify({"cards": [card_json(r, sess, reveal=True) for r in rows]})


@app.route("/api/learn/complete", methods=["POST"])
def api_learn_complete():
    b = body()
    prof, sess = current(b)
    if sess is None:
        return jsonify({"error": "no session"}), 409
    ids = b.get("concept_ids") or []
    db().executemany(
        "UPDATE card SET seen_in_learn = 1 WHERE session_id = ? AND concept_id = ?",
        [(sess["id"], int(i)) for i in ids])
    db().commit()
    return jsonify(build_state(prof, get_session(prof, sess["id"])))


@app.route("/api/test/<int:box>")
def api_test(box):
    prof, sess = current()
    if sess is None:
        return jsonify({"error": "no session"}), 409
    if not 1 <= box <= BOXES:
        return jsonify({"error": "bad box"}), 400
    state = build_state(prof, sess)
    if box == 1 and not state["can_test_box1"]:
        return jsonify({"error": "Run Learn first - %d new card(s) not walked through yet."
                        % state["needs_learn"]}), 409
    cards = [card_json(r, sess) for r in card_rows(sess["id"]) if r["box"] == box]
    random.shuffle(cards)
    return jsonify({"box": box, "cards": cards})


@app.route("/api/answer", methods=["POST"])
def api_answer():
    b = body()
    prof, sess = current(b)
    if sess is None:
        return jsonify({"error": "no session"}), 409
    try:
        cid = int(b["concept_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "concept_id required"}), 400

    conn = db()
    row = conn.execute("SELECT * FROM card WHERE session_id = ? AND concept_id = ?",
                       (sess["id"], cid)).fetchone()
    if row is None:
        return jsonify({"error": "no such card"}), 404

    concept = CONCEPT_BY_ID[cid]
    know, learn = sess["know_lang"], sess["learn_lang"]
    correct = check_answer(concept, know, str(b.get("answer", "")))

    if correct:
        new_box = min(row["box"] + 1, BOXES)
        learned_at = row["learned_at"]
        if new_box >= LEARNED_BOX and learned_at is None:
            learned_at = now()
        conn.execute(
            "UPDATE card SET box = ?, n_correct = n_correct + 1, learned_at = ?"
            " WHERE session_id = ? AND concept_id = ?",
            (new_box, learned_at, sess["id"], cid))
    else:
        # Only the card that was missed drops - the rest of the box stays put.
        new_box = 1
        conn.execute(
            "UPDATE card SET box = 1, n_wrong = n_wrong + 1"
            " WHERE session_id = ? AND concept_id = ?", (sess["id"], cid))
    conn.commit()

    return jsonify({
        "correct": correct,
        "concept_id": cid,
        "prompt": ", ".join(concept[learn]),
        "speak": concept[learn][0] if concept[learn] else "",
        "answer": ", ".join(concept[know]),
        "answer_pinyin": concept.get("pinyin", "") if know == "zh" else "",
        "pinyin": concept.get("pinyin", "") if learn == "zh" else "",
        "from_box": row["box"],
        "to_box": new_box,
        "learned": correct and new_box >= LEARNED_BOX,
        "state": build_state(prof, get_session(prof, sess["id"])),
    })


@app.route("/api/progress")
def api_progress():
    """Milestones and proficiency for every language in this profile."""
    prof, _ = current()
    if prof is None:
        return jsonify({"error": "no profile"}), 409
    conn = db()
    rows = conn.execute(
        "SELECT * FROM session WHERE profile_id = ? ORDER BY last_used DESC",
        (prof["id"],)).fetchall()

    langs, total = [], 0
    for r in rows:
        # Dates come from the cards themselves: the nth word to reach box 6
        # carries the date that milestone was hit.
        dates = [x[0] for x in conn.execute(
            "SELECT learned_at FROM card WHERE session_id = ? AND learned_at IS NOT NULL"
            " ORDER BY learned_at", (r["id"],)).fetchall()]
        learned = len(dates)
        total += learned
        placement = r["placement"] if "placement" in r.keys() else 0
        pool = len(pair_pool(r["learn_lang"], r["know_lang"]))
        langs.append({
            "placement": placement,
            "placed_at": (r["placed_at"] or "")[:10] if "placed_at" in r.keys() else "",
            "vocab": placement + learned,
            "id": r["id"],
            "learn": r["learn_lang"],
            "know": r["know_lang"],
            "learn_name": LANGUAGES[r["learn_lang"]]["name"],
            "know_name": LANGUAGES[r["know_lang"]]["name"],
            "learned": learned,
            "pool": pool,
            "pool_pct": int(round(100.0 * (placement + learned) / pool)) if pool else 0,
            "level": level_for(placement + learned),
            "milestones": [
                {"n": m,
                 "done": learned >= m,
                 "at": dates[m - 1][:10] if learned >= m else None,
                 "reachable": m <= pool}
                for m in MILESTONES
            ],
        })

    langs.sort(key=lambda x: -x["learned"])
    return jsonify({
        "profile": profile_json(prof),
        "total_learned": total,
        "languages": langs,
        "milestones": list(MILESTONES),
        "levels": [{"at": a, "name": n, "blurb": b} for a, n, b in LEVELS],
    })


@app.route("/api/placement/start")
def api_placement_start():
    """20 words sampled across the frequency range, commonest to rarest."""
    prof, sess = current()
    if sess is None:
        return jsonify({"error": "no session"}), 409
    learn, know = sess["learn_lang"], sess["know_lang"]
    pool = pair_pool(learn, know)
    if len(pool) < PLACEMENT_QUESTIONS:
        return jsonify({"error": "not enough words for a placement test"}), 409

    edges = band_edges(len(pool))
    quotas = band_quotas(len(edges) - 1)
    picks = []
    for b in range(len(edges) - 1):
        lo, hi = edges[b], edges[b + 1]
        n = min(quotas[b], hi - lo)
        for ix in sorted(random.sample(range(lo, hi), n)):
            c = CONCEPT_BY_ID[pool[ix]]
            picks.append({
                "concept_id": c["id"],
                "band": b,
                "prompt": ", ".join(c[learn]),
                "speak": c[learn][0] if c[learn] else "",
                "pinyin": c.get("pinyin", "") if learn == "zh" else "",
                "pos": c.get("pos", ""),
            })
    return jsonify({
        "cards": picks,
        "pool": len(pool),
        "bands": len(edges) - 1,
        "edges": edges,
        "know_name": LANGUAGES[know]["name"],
        "learn_name": LANGUAGES[learn]["name"],
    })


@app.route("/api/placement/finish", methods=["POST"])
def api_placement_finish():
    """Grade the sample, estimate vocabulary, and move the deck start point."""
    b = body()
    prof, sess = current(b)
    if sess is None:
        return jsonify({"error": "no session"}), 409
    learn, know = sess["learn_lang"], sess["know_lang"]
    pool, idx = pair_pool(learn, know), pool_index(learn, know)
    edges = band_edges(len(pool))

    graded, detail = [], []
    for item in (b.get("answers") or []):
        try:
            cid = int(item.get("concept_id"))
        except (TypeError, ValueError):
            continue
        if cid not in idx or cid not in CONCEPT_BY_ID:
            continue
        concept = CONCEPT_BY_ID[cid]
        ok = check_answer(concept, know, str(item.get("answer", "")))
        graded.append((idx[cid], ok))
        detail.append({"concept_id": cid, "correct": ok,
                       "prompt": ", ".join(concept[learn]),
                       "answer": ", ".join(concept[know])})
    if not graded:
        return jsonify({"error": "no answers"}), 400

    estimate, per_band = estimate_vocabulary(graded, edges)
    estimate = max(0, min(estimate, len(pool)))

    conn = db()
    conn.execute("UPDATE session SET placement = ?, placed_at = ?, next_index = ?"
                 " WHERE id = ?", (estimate, now(), estimate, sess["id"]))
    conn.commit()

    return jsonify({
        "estimate": estimate,
        "pool": len(pool),
        "right": sum(1 for _, ok in graded if ok),
        "asked": len(graded),
        "level": level_for(estimate + session_learned(sess["id"])),
        "bands": per_band,
        "detail": detail,
        "state": build_state(prof, get_session(prof, sess["id"])),
    })


@app.route("/api/level", methods=["POST"])
def api_level():
    """Set the level by hand, or clear a placement."""
    b = body()
    prof, sess = current(b)
    if sess is None:
        return jsonify({"error": "no session"}), 409
    pool = pair_pool(sess["learn_lang"], sess["know_lang"])
    try:
        words = int(b.get("words"))
    except (TypeError, ValueError):
        return jsonify({"error": "words required"}), 400
    words = max(0, min(words, len(pool)))

    conn = db()
    conn.execute("UPDATE session SET placement = ?, placed_at = ?, next_index = ?"
                 " WHERE id = ?",
                 (words, now() if words else None, words, sess["id"]))
    conn.commit()
    return jsonify(build_state(prof, get_session(prof, sess["id"])))


@app.route("/api/retire", methods=["POST"])
def api_retire():
    b = body()
    prof, sess = current(b)
    if sess is None:
        return jsonify({"error": "no session"}), 409
    try:
        cid = int(b["concept_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "concept_id required"}), 400
    db().execute("UPDATE card SET retired = 1 WHERE session_id = ? AND concept_id = ?",
                 (sess["id"], cid))
    db().commit()
    return jsonify(build_state(prof, get_session(prof, sess["id"])))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Wipe one learning session back to zero. Other sessions are untouched."""
    b = body()
    prof, sess = current(b)
    if sess is None:
        return jsonify({"error": "no session"}), 409
    db().execute("DELETE FROM card WHERE session_id = ?", (sess["id"],))
    db().execute("UPDATE session SET next_index = 0, deck_count = 0,"
                 " placement = 0, placed_at = NULL WHERE id = ?", (sess["id"],))
    db().commit()
    return jsonify(build_state(prof, get_session(prof, sess["id"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    init_db()
    print("Leitner - %d concepts" % len(CONCEPTS))
    for (a, b), ids in sorted(PAIRS.items()):
        if a < b:
            print("  %s <-> %s: %d words" % (a, b, len(ids)))
    print("http://localhost:%d" % args.port)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
