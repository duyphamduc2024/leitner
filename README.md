# Spanish Vocab — Leitner ES

A local web app for learning Spanish vocabulary with a 6-box
[Leitner](https://en.wikipedia.org/wiki/Leitner_system) system. Interface in English,
target language Spanish. Flask + SQLite, no accounts, no internet needed once it's running.

![The six boxes, with a deck spread across boxes 1-4](docs/screenshot.jpg)

---

## Run it

```bash
cd ~/Desktop/SpanishVocab && python3 app.py
```

Then open <http://localhost:5100>.

The only dependency is Flask. If `python3 -c "import flask"` fails:

```bash
python3 -m pip install flask
```

Change the port with `python3 app.py --port 8000`.

---

## How it works

**Six boxes.** Every new word starts in box 1. Answer a card correctly and it moves up one
box; miss it and it drops straight back to box 1. A word that reaches box 6 counts as
learned, and the counter at the top of the page tracks that.

**Box 1 has Learn and Test. Boxes 2–6 have Test only.**

- **Learn** walks you through every card sitting in box 1 — Spanish word, part of speech,
  English meaning, and audio. Arrow keys move, space replays the audio. When you reach the
  end, box 1's Test button unlocks.
- **Test** shows a Spanish word and asks you to type the English meaning. Correct → up a
  box. Wrong → back to box 1.

Test on box 1 stays locked until you have walked every box-1 card through Learn at least
once. Cards that drop back down from a higher box don't re-lock it — you have already seen
them.

**One wrong answer moves one card.** Missing a card in box 4 sends *that card* to box 1;
everything else in box 4 stays where it is.

**New Deck** loads the next 20 words. It unlocks when two things are true:

1. every card of your current deck has reached **box 4 or higher**, and
2. boxes 1–4 together hold **no more than 100 cards**.

The second rule is the brake. Five decks of 20 fill the 100-card allowance, and after that
the only way to make room is to promote cards into boxes 5 and 6 — that is, to actually
learn them. The New Deck bar always spells out which condition is holding you up.

**Answers are graded leniently.** Case, accents and punctuation are ignored, a leading
*a / an / the* is stripped, and `to` is optional on verbs — `Ser` → "to be", "be" and "To Be"
all pass. Every listed meaning counts, so `el otoño` accepts both "autumn" and "fall", and a
gloss like "to be (state, location)" also accepts the bare "to be".

**Retire this card** appears under each test verdict. Use it when a translation is wrong or
useless: the card leaves your profile permanently and stops appearing in tests. The word list
file itself is untouched.

---

## Audio

Audio comes from your Mac's own speech synthesis through the browser's Web Speech API.

**Why this and not audio files:** it's free, there is nothing to download or generate, it
works offline, it covers all 5,000 words plus anything you add later, and it costs no API
keys. The alternatives were pre-generating ~10,000 MP3s (hundreds of megabytes, a paid TTS
bill up front, and a regeneration step every time the word list changes) or calling a cloud
TTS per word (better prosody, but a per-word cost and a network round trip). For single-word
vocabulary drilling, the quality difference does not justify either.

Open **Audio & settings** at the bottom of the page to pick a voice, set the speed, or turn
off automatic playback. macOS ships ~18 Spanish voices; the app defaults to **Mónica**
(es-ES) and lists **Paulina** (es-MX) next, ahead of the novelty voices.

If the voice list is empty: System Settings → Accessibility → Spoken Content → System Voice →
Manage Voices, add a Spanish voice, then reload the page.

---

## The word list

`data/words_es.json` holds **5,000 Spanish words ordered from most to least common** — 250
decks of 20. It is built in two tiers, and `build_wordlist.py` regenerates the whole thing.

**Words 1–360 are hand-written.** Clean single-sense glosses, nouns carry their definite
article so you learn the gender with the word (`el perro`, `la mano`), and the order is
pedagogical: greetings and pronouns, then the top verbs, then function words, everyday nouns,
numbers, days, months, colours, adjectives and common phrases. These are the first 18 decks —
realistically months of study under the 100-card cap.

**Words 361–5,000 are generated** by joining two public sources:

| Source | What it gives |
|---|---|
| [hermitdave/FrequencyWords](https://github.com/hermitdave/FrequencyWords) (OpenSubtitles 2018) | Spanish and English words in frequency order |
| [FreeDict `eng-spa`](https://freedict.org) (Wiktionary-derived, ~64k headwords) | English → Spanish translations, inverted |

The join is where the quality comes from. English glosses are ranked by *reverse
consistency* first — if the entry for "girl" lists `chica` as its own primary translation,
"girl" wins over "bird", even though "bird" is the commoner English word — and by English
corpus frequency second. On top of that the builder drops proper nouns, profanity, archaic
glosses ("thee", "prithee"), conjugated forms that land on a wrong homograph (`era` →
"threshing floor", `son` → "sound", `pienso` → "fodder"), and plural/feminine/accent
duplicates of words already in the list. Roughly 65 remaining bad glosses are corrected by
hand in the `OVERRIDE` table.

**Be aware of the tail.** Generated entries are good but not hand-checked, and the further
down the frequency list you go the more you'll meet an odd gloss or a British-leaning sense.
Generated nouns also carry **no article**, because the source has no gender field and guessing
from the ending would teach you wrong genders. When a card is bad, hit **Retire this card**.

### Rebuilding or extending

```bash
python3 build_wordlist.py                # the default 5,000
python3 build_wordlist.py --limit 8000   # deeper into the frequency list
python3 build_wordlist.py --core-only    # no network, just the 360 curated words
```

Sources are cached in `.wordlist-cache/` (~12 MB) on first run. To improve a translation,
edit `CORE` or `OVERRIDE` in `build_wordlist.py` and rebuild. Cards already in a profile keep
their box — they're stored by word id, so rebuilding with the same ordering is safe. Changing
the *order* of existing words would reshuffle what's in your boxes, so if you reorder, reset
the profile.

---

## Profiles and saved state

Everything lives in `data/leitner.db` (SQLite): which box each card is in, deck progress,
right/wrong counts, retired cards, and how far into the word list you've drawn. It survives
reloads, restarts and switching browsers.

Use **+ Profile** for a second learner; each profile has its own independent boxes and
progress. **Reset this profile** under Audio & settings wipes one profile's cards and starts
again from word 1 — the word list is not touched.

To back up your progress, copy `data/leitner.db`.

---

## Files

| File | What |
|---|---|
| `app.py` | Flask server, SQLite schema, promotion/demotion rules, answer grading, 9 JSON endpoints |
| `build_wordlist.py` | Builds `data/words_es.json` — curated core plus the generated tail |
| `data/words_es.json` | 5,000 words: `{id, es, en[], pos, src}` |
| `data/leitner.db` | Your progress (created on first run) |
| `templates/index.html` | The whole UI — six boxes plus the Learn/Test overlay |
| `static/app.js` | Rendering, Learn walkthrough, Test round, speech synthesis |
| `static/style.css` | Hand-written, CSS variables at the top, light and dark |

Settings worth knowing, at the top of `app.py`:

```python
DECK_SIZE = 20          # cards drawn per deck
ACTIVE_CAP = 100        # hard cap on cards in boxes 1-4
NEW_DECK_MIN_BOX = 4    # every deck card must reach this box
```

---

## Tests

`tests/test_rules.py` drives the API through a throwaway database and checks the grading
rules, the Learn gate, promotion and demotion, the new-deck gate, the 100-card cap, the
learned counter, retiring, and profile isolation.

```bash
python3 tests/test_rules.py
```
