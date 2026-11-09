#!/usr/bin/env python3
"""Leitner ES - a 6-box spaced-repetition trainer for Spanish vocabulary.

Flask + SQLite, single machine, no auth. State lives in data/leitner.db so a
profile survives reloads, restarts and a switch of browser.

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
WORDS_PATH = os.path.join(HERE, "data", "words_es.json")

DECK_SIZE = 20          # cards drawn per deck
ACTIVE_CAP = 100        # hard cap on cards sitting in boxes 1-4
NEW_DECK_MIN_BOX = 4    # every card of the current deck must reach this box
BOXES = 6
LEARNED_BOX = 6

app = Flask(__name__)
# local app, edited in place - never serve a stale stylesheet or script
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# --------------------------------------------------------------- word list

with open(WORDS_PATH, encoding="utf-8") as fh:
    WORDS = json.load(fh)
WORD_BY_ID = {w["id"]: w for w in WORDS}


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def normalize(s):
    """Fold an answer down to what we actually compare.

    Case, accents, punctuation and a leading English article or infinitive
    "to" all get thrown away, so "To Be", "be" and "ser" -> "be" all match.
    """
    s = strip_accents(s.lower().strip())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(to|a|an|the) ", "", s)
    return s


def accepted_answers(word):
    """Every spelling we will accept for this card."""
    out = set()
    for gloss in word["en"]:
        # "to be (state, location)" -> also accept "to be"
        bare = re.sub(r"\([^)]*\)", " ", gloss)
        for piece in [gloss, bare] + bare.split(","):
            n = normalize(piece)
            if n:
                out.add(n)
                # "to look for" -> also accept "look for" (normalize already
                # drops a leading "to", this catches "to go out" style glosses
                # where the user types the bare verb)
                out.add(normalize(re.sub(r"^to ", "", piece)))
    return {a for a in out if a}


# ----------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    next_word_index INTEGER NOT NULL DEFAULT 0,
    deck_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card (
    profile_id    INTEGER NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    word_id       INTEGER NOT NULL,
    box           INTEGER NOT NULL DEFAULT 1,
    deck          INTEGER NOT NULL,
    seen_in_learn INTEGER NOT NULL DEFAULT 0,
    retired       INTEGER NOT NULL DEFAULT 0,
    n_correct     INTEGER NOT NULL DEFAULT 0,
    n_wrong       INTEGER NOT NULL DEFAULT 0,
    learned_at    TEXT,
    PRIMARY KEY (profile_id, word_id)
);
CREATE INDEX IF NOT EXISTS card_box ON card(profile_id, box, retired);
"""


def db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_profile(name=None):
    """Return the named profile, or the first one, creating a default if empty."""
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


# -------------------------------------------------------------- state model

def card_rows(pid):
    return db().execute(
        "SELECT * FROM card WHERE profile_id = ? AND retired = 0 ORDER BY deck, word_id",
        (pid,)).fetchall()


def card_json(row, reveal=False):
    w = WORD_BY_ID[row["word_id"]]
    out = {
        "word_id": row["word_id"],
        "es": w["es"],
        "pos": w["pos"],
        "box": row["box"],
        "deck": row["deck"],
        "n_correct": row["n_correct"],
        "n_wrong": row["n_wrong"],
    }
    if reveal:
        out["en"] = w["en"]
    return out


def build_state(prof):
    pid = prof["id"]
    conn = db()
    rows = card_rows(pid)

    boxes = []
    for b in range(1, BOXES + 1):
        in_box = [r for r in rows if r["box"] == b]
        boxes.append({
            "box": b,
            "count": len(in_box),
            "words": [WORD_BY_ID[r["word_id"]]["es"] for r in in_box],
        })

    current_deck = prof["deck_count"]
    deck_rows = [r for r in rows if r["deck"] == current_deck]
    active = [r for r in rows if r["box"] <= NEW_DECK_MIN_BOX]

    unlearned_box1 = [r for r in rows if r["box"] == 1 and not r["seen_in_learn"]]

    deck_ready = bool(deck_rows) and all(r["box"] >= NEW_DECK_MIN_BOX for r in deck_rows)
    room = len(active) + DECK_SIZE <= ACTIVE_CAP
    words_left = prof["next_word_index"] < len(WORDS)

    if current_deck == 0:
        reason = "Load your first deck of %d words to begin." % DECK_SIZE
        can_new_deck = words_left
    elif not words_left:
        reason = "You have reached the end of the %d-word list. Nothing left to load." % len(WORDS)
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
        reason = "Deck %d is at box %d or better. Ready for 20 more." % (
            current_deck, NEW_DECK_MIN_BOX)
        can_new_deck = True

    learned = conn.execute(
        "SELECT COUNT(*) FROM card WHERE profile_id = ? AND learned_at IS NOT NULL",
        (pid,)).fetchone()[0]

    profiles = [r["name"] for r in
                conn.execute("SELECT name FROM profile ORDER BY id").fetchall()]

    return {
        "profile": prof["name"],
        "profiles": profiles,
        "boxes": boxes,
        "deck": {
            "number": current_deck,
            "size": DECK_SIZE,
            "cards": [card_json(r) for r in deck_rows],
            "can_new_deck": can_new_deck,
            "reason": reason,
        },
        "learned": learned,
        "active_count": len(active),
        "active_cap": ACTIVE_CAP,
        "total_cards": len(rows),
        "words_available": len(WORDS),
        "words_used": prof["next_word_index"],
        "needs_learn": len(unlearned_box1),
        "can_test_box1": len(unlearned_box1) == 0 and
                         any(r["box"] == 1 for r in rows),
        "config": {
            "deck_size": DECK_SIZE,
            "active_cap": ACTIVE_CAP,
            "new_deck_min_box": NEW_DECK_MIN_BOX,
            "boxes": BOXES,
        },
    }


# ------------------------------------------------------------------- routes

@app.context_processor
def inject_static_version():
    """Stamp static URLs with the file mtime.

    Without it a browser that cached style.css once keeps the stale copy
    across edits and restarts, no matter what Cache-Control says.
    """
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
    prof = get_profile(request.args.get("profile"))
    return jsonify(build_state(prof))


@app.route("/api/profile", methods=["POST"])
def api_profile():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = db()
    try:
        conn.execute("INSERT INTO profile (name, created_at) VALUES (?, ?)",
                     (name, now()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already exists - just switch to it
    return jsonify(build_state(get_profile(name)))


@app.route("/api/new_deck", methods=["POST"])
def api_new_deck():
    prof = get_profile((request.json or {}).get("profile"))
    state = build_state(prof)
    if not state["deck"]["can_new_deck"]:
        return jsonify({"error": state["deck"]["reason"]}), 409

    conn = db()
    start = prof["next_word_index"]
    chunk = WORDS[start:start + DECK_SIZE]
    if not chunk:
        return jsonify({"error": "no words left"}), 409
    deck_no = prof["deck_count"] + 1
    conn.executemany(
        "INSERT OR IGNORE INTO card (profile_id, word_id, box, deck) VALUES (?, ?, 1, ?)",
        [(prof["id"], w["id"], deck_no) for w in chunk])
    conn.execute("UPDATE profile SET next_word_index = ?, deck_count = ? WHERE id = ?",
                 (start + len(chunk), deck_no, prof["id"]))
    conn.commit()
    return jsonify(build_state(get_profile(prof["name"])))


@app.route("/api/learn")
def api_learn():
    """Every box-1 card, with meanings - the walkthrough before a box-1 test."""
    prof = get_profile(request.args.get("profile"))
    rows = [r for r in card_rows(prof["id"]) if r["box"] == 1]
    return jsonify({"cards": [card_json(r, reveal=True) for r in rows]})


@app.route("/api/learn/complete", methods=["POST"])
def api_learn_complete():
    body = request.json or {}
    prof = get_profile(body.get("profile"))
    ids = body.get("word_ids") or []
    conn = db()
    conn.executemany(
        "UPDATE card SET seen_in_learn = 1 WHERE profile_id = ? AND word_id = ?",
        [(prof["id"], int(i)) for i in ids])
    conn.commit()
    return jsonify(build_state(get_profile(prof["name"])))


@app.route("/api/test/<int:box>")
def api_test(box):
    prof = get_profile(request.args.get("profile"))
    if not 1 <= box <= BOXES:
        return jsonify({"error": "bad box"}), 400
    state = build_state(prof)
    if box == 1 and not state["can_test_box1"]:
        return jsonify({"error": "Run Learn first - %d new card(s) not walked through yet."
                        % state["needs_learn"]}), 409
    rows = [r for r in card_rows(prof["id"]) if r["box"] == box]
    cards = [card_json(r) for r in rows]
    random.shuffle(cards)
    return jsonify({"box": box, "cards": cards})


@app.route("/api/answer", methods=["POST"])
def api_answer():
    body = request.json or {}
    prof = get_profile(body.get("profile"))
    try:
        word_id = int(body["word_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "word_id required"}), 400
    answer = str(body.get("answer", ""))

    conn = db()
    row = conn.execute(
        "SELECT * FROM card WHERE profile_id = ? AND word_id = ?",
        (prof["id"], word_id)).fetchone()
    if row is None:
        return jsonify({"error": "no such card"}), 404

    word = WORD_BY_ID[word_id]
    correct = normalize(answer) in accepted_answers(word) and normalize(answer) != ""

    if correct:
        new_box = min(row["box"] + 1, BOXES)
        learned_at = row["learned_at"]
        if new_box >= LEARNED_BOX and learned_at is None:
            learned_at = now()
        conn.execute(
            "UPDATE card SET box = ?, n_correct = n_correct + 1, learned_at = ? "
            "WHERE profile_id = ? AND word_id = ?",
            (new_box, learned_at, prof["id"], word_id))
    else:
        # Only the card that was missed drops - the rest of the box stays put.
        new_box = 1
        conn.execute(
            "UPDATE card SET box = 1, n_wrong = n_wrong + 1 "
            "WHERE profile_id = ? AND word_id = ?", (prof["id"], word_id))
    conn.commit()

    return jsonify({
        "correct": correct,
        "word_id": word_id,
        "es": word["es"],
        "en": word["en"],
        "from_box": row["box"],
        "to_box": new_box,
        "learned": correct and new_box >= LEARNED_BOX,
        "state": build_state(get_profile(prof["name"])),
    })


@app.route("/api/retire", methods=["POST"])
def api_retire():
    """Pull a card out of rotation - for a bad auto-generated translation."""
    body = request.json or {}
    prof = get_profile(body.get("profile"))
    try:
        word_id = int(body["word_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "word_id required"}), 400
    conn = db()
    conn.execute("UPDATE card SET retired = 1 WHERE profile_id = ? AND word_id = ?",
                 (prof["id"], word_id))
    conn.commit()
    return jsonify(build_state(get_profile(prof["name"])))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    body = request.json or {}
    prof = get_profile(body.get("profile"))
    conn = db()
    conn.execute("DELETE FROM card WHERE profile_id = ?", (prof["id"],))
    conn.execute("UPDATE profile SET next_word_index = 0, deck_count = 0 WHERE id = ?",
                 (prof["id"],))
    conn.commit()
    return jsonify(build_state(get_profile(prof["name"])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    init_db()
    print("Leitner ES - %d words, %d decks of %d" %
          (len(WORDS), len(WORDS) // DECK_SIZE, DECK_SIZE))
    print("http://localhost:%d" % args.port)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
