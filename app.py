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
SCHEMA_VERSION = 2

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


def pair_pool(learn, know):
    return PAIRS.get((learn, know), [])


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
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    learn_lang TEXT NOT NULL,
    know_lang  TEXT NOT NULL,
    next_index INTEGER NOT NULL DEFAULT 0,
    deck_count INTEGER NOT NULL DEFAULT 0,
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
    conn = db()
    if name:
        row = conn.execute("SELECT * FROM profile WHERE name = ?", (name,)).fetchone()
        if row:
            return row
    row = conn.execute("SELECT * FROM profile ORDER BY id LIMIT 1").fetchone()
    if row:
        return row
    conn.execute("INSERT INTO profile (name, created_at) VALUES (?, ?)",
                 ("default", now()))
    conn.commit()
    return conn.execute("SELECT * FROM profile ORDER BY id LIMIT 1").fetchone()


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


def session_json(row):
    return {
        "id": row["id"],
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
    sessions = [session_json(r) for r in conn.execute(
        "SELECT * FROM session WHERE profile_id = ? ORDER BY last_used DESC",
        (prof["id"],)).fetchall()]
    profiles = [r["name"] for r in
                conn.execute("SELECT name FROM profile ORDER BY id").fetchall()]

    base = {
        "profile": prof["name"],
        "profiles": profiles,
        "sessions": sessions,
        "languages": {k: dict(v, code=k) for k, v in LANGUAGES.items()},
        "pair_counts": {"%s|%s" % (a, b): len(ids) for (a, b), ids in PAIRS.items()},
        "config": {
            "deck_size": DECK_SIZE,
            "active_cap": ACTIVE_CAP,
            "new_deck_min_box": NEW_DECK_MIN_BOX,
            "boxes": BOXES,
        },
    }
    if sess is None:
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

    learned = conn.execute(
        "SELECT COUNT(*) FROM card WHERE session_id = ? AND learned_at IS NOT NULL",
        (sid,)).fetchone()[0]

    base.update({
        "session": session_json(sess),
        "boxes": boxes,
        "deck": {
            "number": current_deck,
            "size": DECK_SIZE,
            "can_new_deck": can_new_deck,
            "reason": reason,
        },
        "learned": learned,
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
    """(profile, session) for this request."""
    src = payload if payload is not None else request.args
    prof = get_profile(src.get("profile"))
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
    name = body().get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = db()
    try:
        conn.execute("INSERT INTO profile (name, created_at) VALUES (?, ?)",
                     (name, now()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already exists - just switch to it
    prof = get_profile(name)
    return jsonify(build_state(prof, get_session(prof)))


@app.route("/api/session", methods=["POST"])
def api_session():
    """Start or switch to the (learn, know) session."""
    b = body()
    prof = get_profile(b.get("profile"))
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
    start = sess["next_index"]
    chunk = pool[start:start + DECK_SIZE]
    if not chunk:
        return jsonify({"error": "no words left"}), 409
    deck_no = sess["deck_count"] + 1
    conn.executemany(
        "INSERT OR IGNORE INTO card (session_id, concept_id, box, deck) VALUES (?, ?, 1, ?)",
        [(sess["id"], cid, deck_no) for cid in chunk])
    conn.execute("UPDATE session SET next_index = ?, deck_count = ?, last_used = ?"
                 " WHERE id = ?", (start + len(chunk), deck_no, now(), sess["id"]))
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
        "answer": ", ".join(concept[know]),
        "answer_pinyin": concept.get("pinyin", "") if know == "zh" else "",
        "pinyin": concept.get("pinyin", "") if learn == "zh" else "",
        "from_box": row["box"],
        "to_box": new_box,
        "learned": correct and new_box >= LEARNED_BOX,
        "state": build_state(prof, get_session(prof, sess["id"])),
    })


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
    db().execute("UPDATE session SET next_index = 0, deck_count = 0 WHERE id = ?",
                 (sess["id"],))
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
