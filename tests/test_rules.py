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
s = j(c.get("/api/state"))
check("no session until one is chosen", s["session"] is None)
check("all four languages offered", len(s["languages"]) == 4, list(s["languages"]))
check("new deck blocked with no session", c.post("/api/new_deck", json={}).status_code == 409)

check("same language both sides refused",
      c.post("/api/session", json={"learn": "es", "know": "es"}).status_code == 400)
check("unknown language refused",
      c.post("/api/session", json={"learn": "es", "know": "xx"}).status_code == 400)

s = j(c.post("/api/session", json={"learn": "es", "know": "en"}))
es_id = s["session"]["id"]
check("session created", s["session"]["learn"] == "es" and s["session"]["know"] == "en")
check("session reports its own pool", s["session"]["pool"] > 5000, s["session"]["pool"])
check("spanish voice tag", s["session"]["voice"] == "es-ES")

s = j(c.post("/api/session", json={"learn": "zh", "know": "vi"}))
zh_id = s["session"]["id"]
check("second session is separate", zh_id != es_id)
check("second session starts empty", s["total_cards"] == 0)
check("mandarin voice tag", s["session"]["voice"] == "zh-CN")
check("both sessions listed", len(s["sessions"]) == 2, len(s["sessions"]))

s = j(c.post("/api/session", json={"learn": "es", "know": "en"}))
check("re-picking a pair reuses it", s["session"]["id"] == es_id)
check("still two sessions", len(s["sessions"]) == 2)

# ------------------------------------------------------------- one session
print("\n== deck 1 lifecycle (Mandarin from Vietnamese) ==")
S = "?session=%d" % zh_id
def post(path, b=None):
    b = dict(b or {}); b["session"] = zh_id
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

print("\n== new-deck gate and the 100-card cap ==")
def answer_right(cid):
    post("/api/answer", {"concept_id": cid, "answer": A.CONCEPT_BY_ID[cid]["vi"][0]})

def cards_in(box):
    r = c.get("/api/test/%d%s" % (box, S))
    return r.get_json().get("cards", []) if r.status_code == 200 else []

def promote_to(box):
    for _ in range(40):
        moved = False
        for b in range(1, box):
            for card in cards_in(b):
                answer_right(card["concept_id"])
                moved = True
        if not moved:
            break
    return j(c.get("/api/state" + S))

s = j(c.get("/api/state" + S))
check("blocked while deck 1 below box 4", s["deck"]["can_new_deck"] is False, s["deck"]["reason"])
s = promote_to(4)
check("deck 1 all at box 4", s["boxes"][3]["count"] == 20, [b["count"] for b in s["boxes"]])
check("new deck allowed", s["deck"]["can_new_deck"] is True, s["deck"]["reason"])

for _ in range(4):
    post("/api/new_deck")
    cs = j(c.get("/api/learn" + S))["cards"]
    post("/api/learn/complete", {"concept_ids": [x["concept_id"] for x in cs]})
    s = promote_to(4)
check("100 cards active", s["active_count"] == 100, s["active_count"])
check("cap blocks a new deck", s["deck"]["can_new_deck"] is False, s["deck"]["reason"])
check("reason points at box 5", "box 5" in s["deck"]["reason"], s["deck"]["reason"])
check("POST new_deck refused", c.post("/api/new_deck", json={"session": zh_id}).status_code == 409)

s = promote_to(5)
check("boxes 1-4 empty", s["active_count"] == 0, s["active_count"])
check("new deck allowed again", s["deck"]["can_new_deck"] is True)

print("\n== learned counter ==")
s = promote_to(6)
check("learned == 100", s["learned"] == 100, s["learned"])
cid = cards_in(6)[0]["concept_id"]
r = post("/api/answer", {"concept_id": cid, "answer": "nope"})
check("box 6 miss -> box 1", r["to_box"] == 1)
check("learned tally is 'ever reached'", r["state"]["learned"] == 100, r["state"]["learned"])

print("\n== retire, isolation, persistence ==")
s = post("/api/retire", {"concept_id": cid})
check("retired card leaves the boxes", s["total_cards"] == 99, s["total_cards"])

s = j(c.get("/api/state?session=%d" % es_id))
check("the other session is untouched", s["total_cards"] == 0, s["total_cards"])
check("and keeps its own languages", s["session"]["learn"] == "es")

s = post("/api/reset")
check("reset clears this session", s["total_cards"] == 0 and s["deck"]["number"] == 0)
s = j(c.get("/api/state?session=%d" % es_id))
check("reset did not touch the other session", s["session"]["learn"] == "es")

print("\n== profiles ==")
s = j(c.post("/api/profile", json={"name": "second"}))
check("new profile has no sessions", s["session"] is None and s["sessions"] == [])
s = j(c.get("/api/state?profile=default&session=%d" % zh_id))
check("original profile intact", len(s["sessions"]) == 2)

print("\n" + ("ALL PASS" if not fails else "FAILURES (%d): %r" % (len(fails), fails)))
sys.exit(1 if fails else 0)
