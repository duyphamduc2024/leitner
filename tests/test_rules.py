import importlib.util, os, sys, tempfile, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("lapp", os.path.join(ROOT, "app.py"))
A = importlib.util.module_from_spec(spec); spec.loader.exec_module(A)

tmp = tempfile.mkdtemp()
A.DB_PATH = os.path.join(tmp, "t.db")
A.init_db()
A.app.config["TESTING"] = True
c = A.app.test_client()

def j(r):
    assert r.status_code in (200, 409), (r.status_code, r.get_data(as_text=True))
    return r.get_json()

fails = []
def check(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL") + "  " + name + ("   " + str(extra) if extra and not cond else ""))
    if not cond: fails.append(name)

print("\n== grading ==")
for word, ans, want in [
    ({"en": ["to be", "exist"]}, "To Be", True),
    ({"en": ["to be", "exist"]}, "be", True),
    ({"en": ["to be (state, location)"]}, "to be", True),
    ({"en": ["thank you", "thanks"]}, "  THANKS  ", True),
    ({"en": ["autumn", "fall"]}, "fall", True),
    ({"en": ["dog"]}, "the dog", True),
    ({"en": ["dog"]}, "cat", False),
    ({"en": ["dog"]}, "", False),
    ({"en": ["sir, mister, gentleman"]}, "mister", True),
    ({"en": ["I am sorry"]}, "i am sorry", True),
    ({"en": ["how are you?"]}, "how are you", True),
]:
    got = A.normalize(ans) in A.accepted_answers(word) and A.normalize(ans) != ""
    check("%-28s <- %-12r" % (word["en"][0], ans), got == want, "got %s" % got)

print("\n== deck 1 lifecycle ==")
s = j(c.get("/api/state"))
check("starts empty", s["total_cards"] == 0 and s["learned"] == 0)
check("first deck offered", s["deck"]["can_new_deck"] is True)

s = j(c.post("/api/new_deck", json={}))
check("20 cards in box 1", s["boxes"][0]["count"] == 20, s["boxes"][0]["count"])
check("box 1 test locked before Learn", s["can_test_box1"] is False)
check("needs_learn == 20", s["needs_learn"] == 20)
r = c.get("/api/test/1")
check("GET /api/test/1 refused (409)", r.status_code == 409)

cards = j(c.get("/api/learn"))["cards"]
check("learn returns 20 with meanings", len(cards) == 20 and "en" in cards[0])
s = j(c.post("/api/learn/complete", json={"word_ids": [x["word_id"] for x in cards]}))
check("box 1 test unlocked after Learn", s["can_test_box1"] is True)
check("GET /api/test/1 now allowed", c.get("/api/test/1").status_code == 200)

print("\n== promote / demote ==")
w0 = cards[0]
res = j(c.post("/api/answer", json={"word_id": w0["word_id"], "answer": w0["en"][0]}))
check("correct -> box 2", res["correct"] and res["to_box"] == 2, res)
res = j(c.post("/api/answer", json={"word_id": w0["word_id"], "answer": "zzzz"}))
check("wrong -> box 1", (not res["correct"]) and res["to_box"] == 1, res)

# only the missed card drops: promote three, miss one of them
ids = [x["word_id"] for x in cards[:3]]
for wid in ids:
    for _ in range(2):
        w = A.WORD_BY_ID[wid]
        c.post("/api/answer", json={"word_id": wid, "answer": w["en"][0]})
s = j(c.get("/api/state"))
check("three cards now in box 3", s["boxes"][2]["count"] == 3, s["boxes"][2]["count"])
j(c.post("/api/answer", json={"word_id": ids[0], "answer": "nope"}))
s = j(c.get("/api/state"))
check("one missed -> box3 has 2 left (box siblings untouched)",
      s["boxes"][2]["count"] == 2, s["boxes"][2]["count"])
check("missed card is in box 1", s["boxes"][0]["count"] == 18, s["boxes"][0]["count"])

print("\n== new-deck gating ==")
def promote_all_to(box, deck_no=None):
    st = j(c.get("/api/state"))
    for _ in range(40):
        rows = []
        for b in range(1, 7):
            for es in st["boxes"][b - 1]["words"]:
                rows.append((es, b))
        moved = False
        for es, b in rows:
            if b >= box: continue
            wid = next(w["id"] for w in A.WORDS if w["es"] == es)
            c.post("/api/answer", json={"word_id": wid, "answer": A.WORD_BY_ID[wid]["en"][0]})
            moved = True
        st = j(c.get("/api/state"))
        if not moved: break
    return st

s = j(c.get("/api/state"))
check("new deck blocked while deck 1 below box 4", s["deck"]["can_new_deck"] is False, s["deck"]["reason"])
s = promote_all_to(4)
check("deck 1 all at box 4", s["boxes"][3]["count"] == 20, [b["count"] for b in s["boxes"]])
check("new deck now allowed", s["deck"]["can_new_deck"] is True, s["deck"]["reason"])

print("\n== 100-card cap ==")
for d in range(2, 6):
    s = j(c.post("/api/new_deck", json={}))
    cs = j(c.get("/api/learn"))["cards"]
    c.post("/api/learn/complete", json={"word_ids": [x["word_id"] for x in cs]})
    s = promote_all_to(4)
check("5 decks loaded, 100 active", s["active_count"] == 100, s["active_count"])
check("all 100 at box 4", s["boxes"][3]["count"] == 100, [b["count"] for b in s["boxes"]])
check("new deck blocked by the cap", s["deck"]["can_new_deck"] is False, s["deck"]["reason"])
check("reason mentions box 5 or 6", "box 5" in s["deck"]["reason"], s["deck"]["reason"])
r = c.post("/api/new_deck", json={})
check("POST /api/new_deck refused (409)", r.status_code == 409)

print("\n== promoting into box 5 makes room ==")
s = promote_all_to(5)
check("100 cards now in box 5", s["boxes"][4]["count"] == 100, [b["count"] for b in s["boxes"]])
check("boxes 1-4 empty", s["active_count"] == 0, s["active_count"])
check("new deck allowed again", s["deck"]["can_new_deck"] is True, s["deck"]["reason"])

print("\n== learned counter ==")
s = promote_all_to(6)
check("learned == 100", s["learned"] == 100, s["learned"])
check("box 6 holds 100", s["boxes"][5]["count"] == 100)
# a miss in box 6 drops the card but the learned tally is 'ever reached box 6'
wid = next(w["id"] for w in A.WORDS if w["es"] == s["boxes"][5]["words"][0])
res = j(c.post("/api/answer", json={"word_id": wid, "answer": "nope"}))
check("box 6 miss -> box 1", res["to_box"] == 1, res["to_box"])
check("learned tally stays 100", res["state"]["learned"] == 100, res["state"]["learned"])

print("\n== retire + persistence ==")
s = j(c.post("/api/retire", json={"word_id": wid}))
check("retired card leaves the boxes", s["total_cards"] == 99, s["total_cards"])
s2 = j(c.get("/api/state"))
check("state identical on reread", s2["total_cards"] == 99 and s2["learned"] == 100)

print("\n== profiles are separate ==")
s = j(c.post("/api/profile", json={"name": "second"}))
check("new profile starts empty", s["total_cards"] == 0 and s["profile"] == "second", s["profile"])
s = j(c.get("/api/state?profile=default"))
check("original profile intact", s["total_cards"] == 99 and s["learned"] == 100)

print("\n" + ("ALL PASS" if not fails else "FAILURES: %r" % fails))
sys.exit(1 if fails else 0)
