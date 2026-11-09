#!/usr/bin/env python3
"""Drive the API through a throwaway database and check the learning rules."""
import importlib.util, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("lapp", os.path.join(ROOT, "app.py"))
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

A.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
A.init_db()
A.app.config["TESTING"] = True
c = A.app.test_client()

fails = []
def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name +
          ("   " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)

def j(r):
    assert r.status_code in (200, 400, 409), (r.status_code, r.get_data(as_text=True))
    return r.get_json()

def concept(en_first):
    for x in A.CONCEPTS:
        if x["en"] and x["en"][0] == en_first:
            return x
    raise AssertionError("no concept " + en_first)

# ------------------------------------------------------------------ data
print("\n== profiles ==")
st = c.get("/api/state").get_json()
check("no profile is invented on first run", st["profile"] is None, st["profile"])
check("and none are listed", st["profiles"] == [], st["profiles"])
check("20 profile pictures are offered", len(st["avatars"]) == 20, len(st["avatars"]))
check("each picture has a glyph and a colour",
      all(a["glyph"] and a["colour"].startswith("#") for a in st["avatars"]))
check("picture ids are 0..19",
      [a["id"] for a in st["avatars"]] == list(range(20)))
check("pictures are all different",
      len({a["glyph"] for a in st["avatars"]}) == 20)
check("a session cannot start without a profile",
      c.post("/api/session", json={"learn": "es", "know": "en"}).status_code == 409)
check("progress needs a profile too", c.get("/api/progress").status_code == 409)

check("a blank name is refused",
      c.post("/api/profile", json={"name": "   "}).status_code == 400)
check("an over-long name is refused",
      c.post("/api/profile", json={"name": "x" * 41}).status_code == 400)

st = j(c.post("/api/profile", json={"name": "Duy", "avatar": 11}))
check("profile created with its picture",
      st["profile"]["name"] == "Duy" and st["profile"]["avatar"]["id"] == 11,
      st["profile"])
check("a new profile has no session", st["session"] is None)
st = j(c.post("/api/profile", json={"name": "Mai", "avatar": 3}))
check("a second profile is separate", len(st["profiles"]) == 2)
check("an out-of-range picture falls back to the first",
      j(c.post("/api/profile", json={"name": "Linh", "avatar": 99}))
      ["profile"]["avatar"]["id"] == 0)
check("re-posting an existing name just switches to it",
      j(c.post("/api/profile", json={"name": "Duy"}))["profile"]["avatar"]["id"] == 11)
check("an unknown profile name is not silently swapped",
      c.get("/api/state?profile=ghost").get_json()["profile"] is None)
check("profile names round-trip",
      sorted(p["name"] for p in st["profiles"]) == ["Duy", "Mai"])

PROF = "Duy"

print("\n== concept table ==")
check("concepts loaded", len(A.CONCEPTS) > 5000, len(A.CONCEPTS))
for a, b in [("es", "en"), ("en", "es"), ("vi", "en"), ("zh", "en"),
             ("zh", "vi"), ("es", "vi"), ("vi", "zh"), ("en", "zh")]:
    check("pair %s<-%s has a pool" % (a, b), len(A.pair_pool(a, b)) > 1000,
          len(A.pair_pool(a, b)))
core = [x for x in A.CONCEPTS if x["src"] == "core"]
check("core concepts present", len(core) >= 365, len(core))
check("every core concept has English and at least one other language",
      all(x["en"] and (x["es"] or x["vi"] or x["zh"]) for x in core))
check("the shipped core is complete in all 4 languages",
      sum(1 for x in core if x["en"] and x["es"] and x["vi"] and x["zh"]) >= 365)
check("every core Mandarin concept has pinyin",
      all(x["pinyin"] for x in core if x["zh"]))

# --------------------------------------------------------------- grading
print("\n== grading, per language ==")
hello = concept("hello")
water = concept("water")
for lang, ans, want, label in [
    ("en", "hello",       True,  "en exact"),
    ("en", "  HI  ",      True,  "en case + spaces"),
    ("en", "goodbye",     False, "en wrong"),
    ("es", "hola",        True,  "es exact"),
    ("vi", "xin chào",    True,  "vi with tone marks"),
    ("vi", "xin chao",    True,  "vi without tone marks"),
    ("zh", "你好",         True,  "zh hanzi"),
    ("zh", "nǐ hǎo",      True,  "zh pinyin with tones"),
    ("zh", "ni hao",      True,  "zh pinyin plain"),
    ("zh", "nihao",       True,  "zh pinyin no spaces"),
    ("zh", "再见",         False, "zh wrong hanzi"),
    ("en", "",            False, "empty is never right"),
]:
    check(label, A.check_answer(hello, lang, ans) == want)

check("es strips the article", A.check_answer(water, "es", "agua"))
check("es accepts the article", A.check_answer(water, "es", "el agua"))
check("en 'to be' accepts 'be'", A.check_answer(concept("to be"), "en", "be"))
autumn = concept("autumn")
check("en multi-gloss accepts either",
      A.check_answer(autumn, "en", "fall") and A.check_answer(autumn, "en", "autumn"))

# --------------------------------------------------------------- sessions
print("\n== sessions ==")
s = j(c.get("/api/state?profile=Duy"))
check("no session until one is chosen", s["session"] is None)
check("all four languages offered", len(s["languages"]) == 4, list(s["languages"]))
check("new deck blocked with no session",
      c.post("/api/new_deck", json={"profile": PROF}).status_code == 409)

check("same language both sides refused",
      c.post("/api/session", json={"profile": PROF, "learn": "es", "know": "es"}).status_code == 400)
check("unknown language refused",
      c.post("/api/session", json={"profile": PROF, "learn": "es", "know": "xx"}).status_code == 400)

s = j(c.post("/api/session", json={"profile": PROF, "learn": "es", "know": "en"}))
es_id = s["session"]["id"]
check("session created", s["session"]["learn"] == "es" and s["session"]["know"] == "en")
check("session reports its own pool", s["session"]["pool"] > 5000, s["session"]["pool"])
check("spanish voice tag", s["session"]["voice"] == "es-ES")

s = j(c.post("/api/session", json={"profile": PROF, "learn": "zh", "know": "vi"}))
zh_id = s["session"]["id"]
check("second session is separate", zh_id != es_id)
check("second session starts empty", s["total_cards"] == 0)
check("mandarin voice tag", s["session"]["voice"] == "zh-CN")
check("both sessions listed", len(s["sessions"]) == 2, len(s["sessions"]))

s = j(c.post("/api/session", json={"profile": PROF, "learn": "es", "know": "en"}))
check("re-picking a pair reuses it", s["session"]["id"] == es_id)
check("still two sessions", len(s["sessions"]) == 2, len(s["sessions"]))

# ------------------------------------------------------------- one session
print("\n== deck 1 lifecycle (Mandarin from Vietnamese) ==")
S = "?profile=Duy&session=%d" % zh_id
def post(path, b=None):
    b = dict(b or {}); b["session"] = zh_id; b["profile"] = PROF
    return j(c.post(path, json=b))

s = post("/api/new_deck")
check("20 cards in box 1", s["boxes"][0]["count"] == 20, s["boxes"][0]["count"])
check("box 1 test locked before Learn", s["can_test_box1"] is False)
check("GET test/1 refused", c.get("/api/test/1" + S).status_code == 409)

cards = j(c.get("/api/learn" + S))["cards"]
check("learn returns 20 with answers", len(cards) == 20 and "answer" in cards[0])
check("mandarin prompt carries pinyin", all(x["pinyin"] for x in cards), cards[0])
s = post("/api/learn/complete", {"concept_ids": [x["concept_id"] for x in cards]})
check("box 1 test unlocked", s["can_test_box1"] is True)

print("\n== audio text ==")
# Some concepts carry several accepted words in a language ("hello, hi",
# "vâng, dạ, có"). Which land in deck 1 depends on the pair, so assert
# against the table. Spanish and Mandarin have none: the generated tier
# stores one word per language, and the curated core happens to give those
# two a single word each.
multi = {L: sum(1 for x in A.CONCEPTS if len(x[L]) > 1) for L in ("en", "es", "vi", "zh")}
check("multi-variant concepts exist to exercise this",
      multi["en"] > 50 and multi["vi"] > 10, multi)
sample = next(x for x in A.CONCEPTS if len(x["en"]) > 1)
check("a multi-variant prompt would read as a list",
      ", ".join(sample["en"]) != sample["en"][0], sample["en"])
check("audio says one word, not the whole list",
      all(x["speak"] == x["prompt"].split(", ")[0] for x in cards),
      [(x["prompt"], x["speak"]) for x in cards[:4]])
check("audio text never contains a comma",
      all("," not in x["speak"] for x in cards),
      [x["speak"] for x in cards if "," in x["speak"]][:3])
check("audio text is never empty", all(x["speak"] for x in cards))
_t = j(c.get("/api/test/1" + S))["cards"]
check("test cards carry audio text too", all(x["speak"] for x in _t))
_r = post("/api/answer", {"concept_id": cards[0]["concept_id"], "answer": "__wrong__"})
check("the answer response carries audio text",
      _r["speak"] == _r["prompt"].split(", ")[0], (_r["prompt"], _r["speak"]))

print("\n== promote / demote ==")
w = cards[0]
r = post("/api/answer", {"concept_id": w["concept_id"], "answer": w["answer"].split(",")[0]})
check("correct -> box 2", r["correct"] and r["to_box"] == 2, r)
r = post("/api/answer", {"concept_id": w["concept_id"], "answer": "zzzz"})
check("wrong -> box 1", (not r["correct"]) and r["to_box"] == 1, r)

ids = [x["concept_id"] for x in cards[:3]]
for cid in ids:
    for _ in range(2):
        post("/api/answer", {"concept_id": cid,
                             "answer": A.CONCEPT_BY_ID[cid]["vi"][0]})
s = j(c.get("/api/state" + S))
check("three cards in box 3", s["boxes"][2]["count"] == 3, s["boxes"][2]["count"])
post("/api/answer", {"concept_id": ids[0], "answer": "nope"})
s = j(c.get("/api/state" + S))
check("only the missed card drops", s["boxes"][2]["count"] == 2, s["boxes"][2]["count"])

def answer_right(cid):
    post("/api/answer", {"concept_id": cid, "answer": A.CONCEPT_BY_ID[cid]["vi"][0]})

def cards_in(box):
    r = c.get("/api/test/%d%s" % (box, S))
    return r.get_json().get("cards", []) if r.status_code == 200 else []

def promote_to(box):
    """Answer everything below `box` correctly until nothing moves."""
    for _ in range(40):
        moved = False
        for b in range(1, box):
            for card in cards_in(b):
                answer_right(card["concept_id"])
                moved = True
        if not moved:
            break
    return j(c.get("/api/state" + S))


print("\n== new-deck gate: a 40-card limit per box, boxes 1-4 ==")
# its own session, so the counts are exact and the rule is readable
cap = j(c.post("/api/session", json={"profile": PROF, "learn": "es", "know": "vi"}))
cid_s = cap["session"]["id"]
CS = "?profile=Duy&session=%d" % cid_s

def cpost(path, b=None):
    b = dict(b or {}); b["session"] = cid_s; b["profile"] = PROF
    return j(c.post(path, json=b))

def cbox():
    return [b["count"] for b in j(c.get("/api/state" + CS))["boxes"]]

def ccards(box):
    r = c.get("/api/test/%d%s" % (box, CS))
    return r.get_json()["cards"] if r.status_code == 200 else []

def cright(concept_id):
    cpost("/api/answer", {"concept_id": concept_id,
                          "answer": A.CONCEPT_BY_ID[concept_id]["vi"][0]})

st0 = j(c.get("/api/state" + CS))
check("boxes 1-4 report their cap",
      [b["cap"] for b in st0["boxes"]] == [40, 40, 40, 40, None, None],
      [b["cap"] for b in st0["boxes"]])
check("boxes 5 and 6 have no cap",
      st0["boxes"][4]["cap"] is None and st0["boxes"][5]["cap"] is None)
check("the only limit reported is per box",
      st0["config"]["box_cap"] == 40 and st0["config"].get("active_cap") is None,
      st0["config"])

cpost("/api/new_deck")
st = j(c.get("/api/state" + CS))
check("one deck leaves room for another",
      st["boxes"][0]["count"] == 20 and st["deck"]["can_new_deck"] is True,
      (st["boxes"][0]["count"], st["deck"]["reason"]))

cpost("/api/new_deck")
st = j(c.get("/api/state" + CS))
check("box 1 reaches its 40 limit", st["boxes"][0]["count"] == 40, cbox())
check("box 1 reports itself full", st["boxes"][0]["full"] is True)
check("a full box 1 blocks a new deck", st["deck"]["can_new_deck"] is False,
      st["deck"]["reason"])
check("the reason names box 1 and the limit",
      "Box 1" in st["deck"]["reason"] and "40" in st["deck"]["reason"],
      st["deck"]["reason"])
check("POST new_deck refused",
      c.post("/api/new_deck", json={"profile": PROF, "session": cid_s}).status_code == 409)

cs = j(c.get("/api/learn" + CS))["cards"]
cpost("/api/learn/complete", {"concept_ids": [x["concept_id"] for x in cs]})
for card in ccards(1)[:4]:
    cright(card["concept_id"])
st = j(c.get("/api/state" + CS))
check("moving four up leaves box 1 at 36", st["boxes"][0]["count"] == 36, cbox())
check("36 + 20 would be 56, so still blocked",
      st["deck"]["can_new_deck"] is False, st["deck"]["reason"])
check("a deck is never part-loaded to squeeze in",
      c.post("/api/new_deck", json={"profile": PROF, "session": cid_s}).status_code == 409)

for card in ccards(1)[:16]:
    cright(card["concept_id"])
st = j(c.get("/api/state" + CS))
check("box 1 down to 20 unblocks it",
      st["boxes"][0]["count"] == 20 and st["deck"]["can_new_deck"] is True,
      (st["boxes"][0]["count"], st["deck"]["reason"]))
check("box 2 took the cards and is not full", st["boxes"][1]["count"] == 20, cbox())

cpost("/api/new_deck")
st = j(c.get("/api/state" + CS))
check("boxes 1-4 may hold more than 100 between them",
      st["active_count"] == 60 and st["deck"]["can_new_deck"] is False,
      (st["active_count"], st["deck"]["reason"]))
check("only box 1 being full is what stops it",
      st["boxes"][0]["count"] == 40 and all(b["count"] <= 40 for b in st["boxes"][:4]),
      cbox())

print("\n== learned counter ==")
# Drill five decks all the way to box 6. Box 1 caps at 40, so this is load,
# clear, load again - which is the loop the cap is meant to produce.
for _ in range(12):
    s = j(c.get("/api/state" + S))
    if s["learned"] >= 100:
        break
    if s["deck"]["can_new_deck"]:
        post("/api/new_deck")
        cs = j(c.get("/api/learn" + S))["cards"]
        post("/api/learn/complete", {"concept_ids": [x["concept_id"] for x in cs]})
    s = promote_to(6)
check("learned == 100", s["learned"] == 100, s["learned"])
check("boxes 1-4 emptied on the way", s["active_count"] == 0,
      [b["count"] for b in s["boxes"]])
cid = cards_in(6)[0]["concept_id"]
r = post("/api/answer", {"concept_id": cid, "answer": "nope"})
check("box 6 miss -> box 1", r["to_box"] == 1)
check("learned tally is 'ever reached'", r["state"]["learned"] == 100, r["state"]["learned"])

print("\n== proficiency levels ==")
for learned, want in [(0, "Starting out"), (99, "Starting out"), (100, "Survival"),
                      (499, "Survival"), (500, "A1 · Breakthrough"),
                      (1000, "A2 · Waystage"), (2000, "B1 · Threshold"),
                      (4000, "B2 · Vantage"), (7000, "C1 · Advanced"),
                      (10000, "C2 · Mastery"), (99999, "C2 · Mastery")]:
    check("%6d -> %s" % (learned, want), A.level_for(learned)["name"] == want,
          A.level_for(learned)["name"])
check("top level reports no next", A.level_for(12000)["top"] is True)
check("top level sits at 100%", A.level_for(12000)["pct"] == 100)
lv = A.level_for(300)
check("mid-level counts down to the next", lv["to_next"] == 200, lv)
check("mid-level percentage", lv["pct"] == 50, lv)
check("level 0 is not 'top'", A.level_for(0)["top"] is False)

print("\n== milestones ==")
p = j(c.get("/api/progress?profile=Duy"))
check("progress lists every session", len(p["languages"]) == 3, len(p["languages"]))
check("milestones start at one deck", p["milestones"][0] == 20, p["milestones"])
check("profile total is the sum",
      p["total_learned"] == sum(L["learned"] for L in p["languages"]))
zh = [L for L in p["languages"] if L["learn"] == "zh"][0]
check("the worked session has 100 learned", zh["learned"] == 100, zh["learned"])
done = [m["n"] for m in zh["milestones"] if m["done"]]
check("20/50/100 unlocked", done == [20, 50, 100], done)
check("200 still locked", not [m for m in zh["milestones"] if m["n"] == 200][0]["done"])
check("unlocked milestones carry a date",
      all(m["at"] for m in zh["milestones"] if m["done"]),
      [(m["n"], m["at"]) for m in zh["milestones"]])
check("locked milestones carry none",
      all(m["at"] is None for m in zh["milestones"] if not m["done"]))
check("milestone date looks like a date",
      len([m for m in zh["milestones"] if m["n"] == 100][0]["at"]) == 10)
check("reachability reflects the pair's pool",
      all(m["reachable"] == (m["n"] <= zh["pool"]) for m in zh["milestones"]))
check("session level at 100 words is Survival", zh["level"]["name"] == "Survival", zh["level"])
es = [L for L in p["languages"] if L["learn"] == "es"][0]
check("untouched session has nothing unlocked",
      es["learned"] == 0 and not any(m["done"] for m in es["milestones"]))
check("languages sorted by words learned",
      [L["learned"] for L in p["languages"]] ==
      sorted([L["learned"] for L in p["languages"]], reverse=True))

print("\n== placement test ==")
pool_es = A.pair_pool("es", "en")
edges_es = A.band_edges(len(pool_es))
check("bands line up with the level thresholds",
      edges_es[:4] == [0, 50, 100, 250], edges_es[:4])
check("last band ends at the pool", edges_es[-1] == len(pool_es))
check("exactly 20 questions", sum(A.band_quotas(len(edges_es) - 1)) == 20,
      A.band_quotas(len(edges_es) - 1))
for pr in [("vi", "zh"), ("en", "vi"), ("zh", "en")]:
    e = A.band_edges(len(A.pair_pool(*pr)))
    check("20 questions for %s<-%s too" % pr,
          sum(A.band_quotas(len(e) - 1)) == 20)

# one sample inside every band, so no band is left uncredited
every_band = [edges_es[b] for b in range(len(edges_es) - 1)]
check("knowing nothing estimates 0",
      A.estimate_vocabulary([(p, False) for p in every_band], edges_es)[0] == 0)
check("knowing everything estimates the whole pool",
      A.estimate_vocabulary([(p, True) for p in every_band], edges_es)[0]
      == len(pool_es))
check("a band nobody was asked about is credited nothing",
      A.estimate_vocabulary([(0, True)], edges_es)[0] == edges_es[1],
      A.estimate_vocabulary([(0, True)], edges_es)[0])

def simulate(knows, pair=("es", "en")):
    """Answer correctly for words above the learner's ceiling only."""
    pool = A.pair_pool(*pair)
    e = A.band_edges(len(pool))
    q = A.band_quotas(len(e) - 1)
    graded = []
    for b in range(len(e) - 1):
        lo, hi = e[b], e[b + 1]
        step = max(1, (hi - lo) // max(1, q[b]))
        for k in range(min(q[b], hi - lo)):
            pos = min(hi - 1, lo + k * step)
            graded.append((pos, pos < knows))
    return A.estimate_vocabulary(graded, e)[0]

for knows, lo, hi in [(0, 0, 0), (500, 250, 900), (2000, 1200, 3000),
                      (8000, 6000, 11000)]:
    got = simulate(knows)
    check("knows %5d -> estimate %5d, within one band" % (knows, got),
          lo <= got <= hi, got)

print("\n== placement through the API ==")
ps = j(c.post("/api/session", json={"profile": PROF, "learn": "es", "know": "en"}))
psid = ps["session"]["id"]
check("a fresh session has no placement", ps["session"]["placement"] == 0)
check("vocabulary starts at zero", ps["session"]["vocab"] == 0)

start = j(c.get("/api/placement/start?profile=Duy&session=%d" % psid))
check("placement offers 20 words", len(start["cards"]) == 20, len(start["cards"]))
check("placement words are spread across bands",
      len({x["band"] for x in start["cards"]}) >= 9,
      sorted({x["band"] for x in start["cards"]}))
check("placement never leaks the answer",
      all("answer" not in x for x in start["cards"]))
check("placement carries audio text", all(x["speak"] for x in start["cards"]))

# answer correctly only for words inside the first 2000 of the pool
idx_es = A.pool_index("es", "en")
answers = []
for card in start["cards"]:
    pcid = card["concept_id"]
    right = A.CONCEPT_BY_ID[pcid]["en"][0]
    answers.append({"concept_id": pcid,
                    "answer": right if idx_es[pcid] < 2000 else ""})
res = j(c.post("/api/placement/finish", json={"profile": PROF, "session": psid, "answers": answers}))
check("estimate lands near the simulated 2000",
      1000 <= res["estimate"] <= 3500, res["estimate"])
check("result reports the score", res["asked"] == 20 and res["right"] > 0, res["right"])
check("result breaks down by band", len(res["bands"]) >= 9)
st = j(c.get("/api/state?profile=Duy&session=%d" % psid))
check("placement is stored", st["session"]["placement"] == res["estimate"])
check("new decks now start from the estimate",
      st["session"]["used"] == res["estimate"], st["session"]["used"])
check("vocabulary counts the placement", st["vocab"] == res["estimate"])
check("level reflects the placement, not zero",
      st["level"]["name"] != "Starting out", st["level"])
check("milestones still count only drilled words",
      st["learned"] == 0, st["learned"])

print("\n== deck drawing after the start point moves ==")
PS = "?profile=Duy&session=%d" % psid
d1 = j(c.post("/api/new_deck", json={"profile": PROF, "session": psid}))
check("deck drawn from the placement point", d1["boxes"][0]["count"] == 20)
deck1 = set(d1["boxes"][0]["words"])

lv = j(c.post("/api/level", json={"profile": PROF, "session": psid, "words": 0}))
check("manual level moves the start point back", lv["session"]["placement"] == 0)
check("and resets where decks are drawn", lv["session"]["used"] == 0)

# the second deck no longer needs deck 1 promoted; walk Learn anyway so the
# session is in a realistic state
cs = j(c.get("/api/learn" + PS))["cards"]
c.post("/api/learn/complete", json={"profile": PROF, "session": psid,
                                    "concept_ids": [x["concept_id"] for x in cs]})
for _ in range(6):
    moved = False
    for b in range(1, 4):
        r = c.get("/api/test/%d%s" % (b, psid and psid or 0) if False else
                  "/api/test/%d%s" % (b, PS))
        if r.status_code != 200:
            continue
        for card in r.get_json()["cards"]:
            cid = card["concept_id"]
            c.post("/api/answer", json={"profile": PROF, "session": psid, "concept_id": cid,
                                        "answer": A.CONCEPT_BY_ID[cid]["en"][0]})
            moved = True
    if not moved:
        break
st = j(c.get("/api/state" + PS))
check("deck 1 cleared to box 4", st["boxes"][3]["count"] == 20,
      [b["count"] for b in st["boxes"]])

d2 = j(c.post("/api/new_deck", json={"profile": PROF, "session": psid}))
check("a second full deck is drawn after moving back",
      d2["total_cards"] == 40, d2["total_cards"])
allw = [w for bx in d2["boxes"] for w in bx["words"]]
check("no card is handed out twice", len(set(allw)) == len(allw),
      len(allw) - len(set(allw)))
check("the new deck really is different words",
      not (set(d2["boxes"][0]["words"]) & deck1),
      sorted(set(d2["boxes"][0]["words"]) & deck1)[:4])

print("\n== manual level ==")
lv = j(c.post("/api/level", json={"profile": PROF, "session": psid, "words": 2000}))
check("manual level sets vocabulary", lv["session"]["placement"] == 2000)
check("manual level sets the band", lv["level"]["name"] == "B1 · Threshold", lv["level"])
check("a silly level is clamped to the pool",
      j(c.post("/api/level", json={"profile": PROF, "session": psid, "words": 999999}))["session"]["placement"]
      == len(pool_es))
check("a negative level is clamped to zero",
      j(c.post("/api/level", json={"profile": PROF, "session": psid, "words": -5}))["session"]["placement"] == 0)
check("a non-numeric level is refused",
      c.post("/api/level", json={"profile": PROF, "session": psid, "words": "lots"}).status_code == 400)
j(c.post("/api/level", json={"profile": PROF, "session": psid, "words": 2000}))
check("reset clears the placement too",
      j(c.post("/api/reset", json={"profile": PROF, "session": psid}))["session"]["placement"] == 0)

print("\n== retire, isolation, persistence ==")
before = j(c.get("/api/state" + S))["total_cards"]
retire_cid = cards_in(1)[0]["concept_id"]
s = post("/api/retire", {"concept_id": retire_cid})
check("retired card leaves the boxes", s["total_cards"] == before - 1,
      (before, s["total_cards"]))
check("retiring is idempotent",
      post("/api/retire", {"concept_id": retire_cid})["total_cards"] == before - 1)

s = j(c.get("/api/state?profile=Duy&session=%d" % es_id))
check("the other session is untouched", s["total_cards"] == 0, s["total_cards"])
check("and keeps its own languages", s["session"]["learn"] == "es")

s = post("/api/reset")
check("reset clears this session", s["total_cards"] == 0 and s["deck"]["number"] == 0)
s = j(c.get("/api/state?profile=Duy&session=%d" % es_id))
check("reset did not touch the other session", s["session"]["learn"] == "es")

print("\n== profile isolation ==")
s = j(c.post("/api/profile", json={"name": "second"}))
check("new profile has no sessions", s["session"] is None and s["sessions"] == [])
s = j(c.get("/api/state?profile=Duy&session=%d" % zh_id))
check("original profile intact", len(s["sessions"]) == 3, len(s["sessions"]))
check("sessions do not leak between profiles",
      j(c.get("/api/state?profile=Mai"))["sessions"] == [])

print("\n" + ("ALL PASS" if not fails else "FAILURES (%d): %r" % (len(fails), fails)))
sys.exit(1 if fails else 0)
