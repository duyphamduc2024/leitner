#!/usr/bin/env python3
"""Build data/words_es.json - a frequency-ordered Spanish -> English word list.

Two tiers, concatenated in order:

  1. CORE  - ~320 hand-curated entries. Clean single-sense glosses, nouns carry
             their definite article so you learn gender with the word. These are
             the words you actually meet first, so they are worth doing by hand.

  2. AUTO  - the rest, out to --limit (default 5000). Built by joining:
               * OpenSubtitles 2018 Spanish frequency list (hermitdave/FrequencyWords)
               * FreeDict eng-spa (Wiktionary-derived, ~64k headwords), inverted
             English glosses are ranked by English corpus frequency, so the most
             ordinary meaning comes first ("verde" -> green, not -> fescennine).
             AUTO nouns have no article: the source has no gender field, and
             guessing from the ending would teach wrong genders.

Run it only when you want to rebuild or extend the list. The committed
data/words_es.json is the output and the app needs nothing else.

    python3 build_wordlist.py              # 5000 words
    python3 build_wordlist.py --limit 8000 # go deeper into the frequency list
    python3 build_wordlist.py --core-only  # skip the network, 320 curated words
"""
import argparse, collections, gzip, json, os, re, sys, unicodedata, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".wordlist-cache")
OUT = os.path.join(HERE, "data", "words_es.json")

ES_FREQ = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/es/es_50k.txt"
EN_FREQ = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/en/en_50k.txt"
DICT = "https://download.freedict.org/dictionaries/eng-spa/2025.11.23/freedict-eng-spa-2025.11.23.dictd.tar.xz"

# ---------------------------------------------------------------- curated core
# "spanish | english glosses (comma separated) | part of speech"
# Ordered most-useful-first: each run of 20 is one deck in the app.
CORE = """
hola | hello, hi | interj
sí | yes | adv
no | no, not | adv
gracias | thank you, thanks | interj
por favor | please | phrase
adiós | goodbye, bye | interj
perdón | sorry, excuse me | interj
yo | I | pron
tú | you | pron
él | he, him | pron
ella | she, her | pron
nosotros | we, us | pron
ellos | they, them | pron
usted | you (formal) | pron
ser | to be | verb
estar | to be (state, location) | verb
tener | to have | verb
hacer | to do, to make | verb
ir | to go | verb
querer | to want, to love | verb

poder | can, to be able | verb
decir | to say, to tell | verb
ver | to see | verb
dar | to give | verb
saber | to know (a fact) | verb
conocer | to know (a person) | verb
venir | to come | verb
hablar | to speak, to talk | verb
comer | to eat | verb
beber | to drink | verb
vivir | to live | verb
trabajar | to work | verb
de | of, from | prep
a | to, at | prep
en | in, on, at | prep
con | with | prep
para | for, in order to | prep
por | for, by, through | prep
y | and | conj
o | or | conj

pero | but | conj
porque | because | conj
que | that, which | conj
qué | what | pron
quién | who | pron
cuándo | when | adv
dónde | where | adv
cómo | how | adv
cuánto | how much, how many | adv
por qué | why | adv
muy | very | adv
más | more, most | adv
menos | less, fewer | adv
mucho | a lot, much | adv
poco | little, few | adv
todo | all, everything | adj
nada | nothing | pron
algo | something | pron
alguien | someone | pron
nadie | nobody | pron

siempre | always | adv
nunca | never | adv
ahora | now | adv
hoy | today | adv
ayer | yesterday | adv
bien | well, fine | adv
mal | badly, poorly | adv
también | also, too | adv
tampoco | neither, not either | adv
ya | already | adv
todavía | still, yet | adv
aquí | here | adv
allí | there | adv
mi | my | adj
tu | your | adj
su | his, her, their | adj
este | this | adj
ese | that | adj
otro | other, another | adj
cada | each, every | adj

grande | big, large | adj
pequeño | small, little | adj
bueno | good | adj
malo | bad | adj
nuevo | new | adj
viejo | old | adj
joven | young | adj
el agua | water | noun
la comida | food, meal | noun
la casa | house, home | noun
el hombre | man | noun
la mujer | woman | noun
el niño | boy, child | noun
la niña | girl | noun
el amigo | friend | noun
la familia | family | noun
el padre | father | noun
la madre | mother | noun
el hijo | son | noun
la hija | daughter | noun

el hermano | brother | noun
la hermana | sister | noun
el día | day | noun
la noche | night | noun
la mañana | morning, tomorrow | noun
la tarde | afternoon, evening | noun
el año | year | noun
el mes | month | noun
la semana | week | noun
la hora | hour, time (of day) | noun
el tiempo | time, weather | noun
el trabajo | work, job | noun
la ciudad | city | noun
el país | country | noun
la calle | street | noun
el coche | car | noun
el tren | train | noun
el avión | airplane, plane | noun
la puerta | door | noun
la ventana | window | noun

la mesa | table | noun
la silla | chair | noun
la cama | bed | noun
el libro | book | noun
el teléfono | telephone, phone | noun
el dinero | money | noun
la tienda | shop, store | noun
el mercado | market | noun
el restaurante | restaurant | noun
el hotel | hotel | noun
el baño | bathroom | noun
la cocina | kitchen | noun
el cuarto | room | noun
la escuela | school | noun
el médico | doctor | noun
el hospital | hospital | noun
el nombre | name | noun
la vida | life | noun
la mano | hand | noun
la cabeza | head | noun

el ojo | eye | noun
la boca | mouth | noun
el pie | foot | noun
el corazón | heart | noun
el pan | bread | noun
el café | coffee | noun
el té | tea | noun
la leche | milk | noun
el vino | wine | noun
la cerveza | beer | noun
la carne | meat | noun
el pescado | fish (as food) | noun
la fruta | fruit | noun
la verdura | vegetable | noun
el huevo | egg | noun
el queso | cheese | noun
el arroz | rice | noun
la sal | salt | noun
el azúcar | sugar | noun
el perro | dog | noun

el gato | cat | noun
necesitar | to need | verb
gustar | to like, to please | verb
pensar | to think | verb
creer | to believe | verb
sentir | to feel | verb
dormir | to sleep | verb
despertar | to wake up | verb
empezar | to begin, to start | verb
terminar | to finish, to end | verb
comprar | to buy | verb
vender | to sell | verb
pagar | to pay | verb
costar | to cost | verb
buscar | to look for, to search | verb
encontrar | to find | verb
llegar | to arrive | verb
salir | to leave, to go out | verb
entrar | to enter, to go in | verb
abrir | to open | verb

cerrar | to close, to shut | verb
poner | to put, to place | verb
llevar | to carry, to wear | verb
traer | to bring | verb
dejar | to leave, to let | verb
pedir | to ask for, to order | verb
preguntar | to ask (a question) | verb
responder | to answer, to reply | verb
escuchar | to listen | verb
oír | to hear | verb
mirar | to look at, to watch | verb
leer | to read | verb
escribir | to write | verb
aprender | to learn | verb
enseñar | to teach, to show | verb
estudiar | to study | verb
entender | to understand | verb
recordar | to remember | verb
olvidar | to forget | verb
ayudar | to help | verb

esperar | to wait, to hope | verb
usar | to use | verb
jugar | to play | verb
cantar | to sing | verb
bailar | to dance | verb
correr | to run | verb
caminar | to walk | verb
viajar | to travel | verb
volver | to return, to come back | verb
quedar | to stay, to remain | verb
cambiar | to change | verb
seguir | to follow, to continue | verb
parar | to stop | verb
caer | to fall | verb
subir | to go up, to climb | verb
bajar | to go down, to lower | verb
mover | to move | verb
tomar | to take, to drink | verb
nacer | to be born | verb
morir | to die | verb

uno | one | num
dos | two | num
tres | three | num
cuatro | four | num
cinco | five | num
seis | six | num
siete | seven | num
ocho | eight | num
nueve | nine | num
diez | ten | num
once | eleven | num
doce | twelve | num
veinte | twenty | num
treinta | thirty | num
cien | one hundred | num
mil | one thousand | num
primero | first | adj
segundo | second | adj
último | last | adj
medio | half | adj

el lunes | Monday | noun
el martes | Tuesday | noun
el miércoles | Wednesday | noun
el jueves | Thursday | noun
el viernes | Friday | noun
el sábado | Saturday | noun
el domingo | Sunday | noun
enero | January | noun
febrero | February | noun
marzo | March | noun
abril | April | noun
mayo | May | noun
junio | June | noun
julio | July | noun
agosto | August | noun
septiembre | September | noun
octubre | October | noun
noviembre | November | noun
diciembre | December | noun
el fin de semana | weekend | noun

la primavera | spring | noun
el verano | summer | noun
el otoño | autumn, fall | noun
el invierno | winter | noun
el sol | sun | noun
la luna | moon | noun
el cielo | sky, heaven | noun
la lluvia | rain | noun
la nieve | snow | noun
el viento | wind | noun
el mar | sea | noun
la montaña | mountain | noun
el río | river | noun
el árbol | tree | noun
la flor | flower | noun
la playa | beach | noun
el campo | countryside, field | noun
la tierra | earth, land | noun
el fuego | fire | noun
el aire | air | noun

blanco | white | adj
negro | black | adj
rojo | red | adj
azul | blue | adj
verde | green | adj
amarillo | yellow | adj
gris | grey, gray | adj
rosa | pink | adj
marrón | brown | adj
largo | long | adj
corto | short (in length) | adj
alto | tall, high | adj
bajo | short, low | adj
gordo | fat | adj
delgado | thin, slim | adj
fuerte | strong, loud | adj
débil | weak | adj
rápido | fast, quick | adj
lento | slow | adj
mismo | same | adj

caliente | hot | adj
frío | cold | adj
fácil | easy | adj
difícil | difficult, hard | adj
feliz | happy | adj
triste | sad | adj
cansado | tired | adj
enfermo | sick, ill | adj
bonito | pretty, nice | adj
feo | ugly | adj
limpio | clean | adj
sucio | dirty | adj
lleno | full | adj
vacío | empty | adj
caro | expensive | adj
barato | cheap | adj
rico | rich, tasty | adj
pobre | poor | adj
listo | ready, clever | adj
guapo | good-looking, handsome | adj

casi | almost, nearly | adv
solo | only, alone | adv
juntos | together | adv
pronto | soon | adv
tarde | late | adv
temprano | early | adv
antes | before | adv
después | after, afterwards | adv
durante | during | prep
hasta | until, up to | prep
desde | since, from | prep
sin | without | prep
sobre | on, about | prep
entre | between, among | prep
contra | against | prep
según | according to | prep
aunque | although, even though | conj
mientras | while | conj
entonces | then, so | adv
quizás | maybe, perhaps | adv

si | if, whether | conj
como | as, like | conj
el señor | sir, mister, gentleman | noun
la señora | madam, lady, Mrs. | noun
la señorita | miss, young lady | noun
la gente | people | noun
la cosa | thing | noun
la vez | time, occasion | noun
la verdad | truth | noun
el mundo | world | noun
por supuesto | of course | phrase
de acuerdo | agreed, all right | phrase
lo siento | I am sorry | phrase
buenos días | good morning | phrase
buenas tardes | good afternoon | phrase
buenas noches | good night, good evening | phrase
¿cómo estás? | how are you? | phrase
me llamo | my name is | phrase
mucho gusto | nice to meet you | phrase
hasta luego | see you later | phrase
"""

# AUTO entries whose machine-derived gloss is wrong or misleading, corrected by
# hand. Spotted by reading the generated list; add to it as you find more.
OVERRIDE = {
    "chica": ["girl", "young woman"],
    "chico": ["boy", "guy"],
    "esposa": ["wife"],
    "dios": ["god"],
    "pasar": ["to pass", "to happen", "to spend (time)"],
    "cuerpo": ["body"],
    "escucha": ["listening"],
    "juego": ["game", "play"],
    "lugar": ["place", "spot"],
    "labios": ["lips"],
    "chaqueta": ["jacket"],
    "whisky": ["whisky"],
    "cola": ["tail", "queue", "glue"],
    "taza": ["cup", "mug"],
    "justo": ["fair", "just", "exactly"],
    "muerte": ["death"],
    "parte": ["part", "portion"],
    "razón": ["reason"],
    "problema": ["problem"],
    "idea": ["idea"],
    "cierto": ["certain", "true"],
    "claro": ["clear", "of course"],
    "seguro": ["sure", "safe", "insurance"],
    "estado": ["state", "status"],
    "espera": ["wait"],
    "socio": ["partner", "member"],
    "ruego": ["plea", "request"],
    "tirar": ["to throw", "to pull"],
    "andar": ["to walk", "to go"],
    "carro": ["car", "cart"],
    "venta": ["sale"],
    "huir": ["to flee", "to run away"],
    "compromiso": ["commitment", "engagement"],
    "desgracia": ["misfortune"],
    "mono": ["monkey", "cute"],
    "planta": ["plant", "floor"],
    "corriente": ["current", "ordinary"],
    "caído": ["fallen"],
    "hecho": ["fact", "done"],
    "dicho": ["said", "saying"],
    "pasado": ["past", "last"],
    "supuesto": ["supposed", "assumption"],
    "pues": ["well", "then"],
    "vaya": ["wow", "what a"],
    "tanto": ["so much", "as much"],
    "fuera": ["outside", "out"],
    "acuerdo": ["agreement"],
    "favor": ["favour", "favor"],
    "la": ["the (feminine)"],
    "lo": ["it", "the (neuter)"],
    "al": ["to the"],
    "se": ["oneself", "himself", "herself"],
    "le": ["to him", "to her"],
    "me": ["me", "myself"],
    "mis": ["my (plural)"],
    "sus": ["his", "her", "their"],
    "ti": ["you (after a preposition)"],
    "mío": ["mine"],
    "un": ["a", "an", "one"],
    "eso": ["that"],
    "esto": ["this"],
    "así": ["like this", "so"],
    "tan": ["so", "such"],
    "mejor": ["better", "best"],
}

# ------------------------------------------------------------------- fetching
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


def unaccent(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def bare(es):
    """Drop a leading definite article: 'la tarde' -> 'tarde'."""
    return re.sub(r"^(el|la|los|las) ", "", es.lower())


def variants(word):
    """Spellings this word would be a redundant duplicate of.

    Only ever maps a marked form back to its base (feminine -> masculine,
    plural -> singular, accented -> plain), never the other way round, so
    genuinely distinct pairs like cuenta/cuento both survive.
    """
    u = unaccent(word)
    out = {u}
    for suf in ("es", "s"):
        if u.endswith(suf) and len(u) - len(suf) >= 3:
            out.add(u[:-len(suf)])
    if u.endswith("a"):
        out.update({u[:-1] + "o", u[:-1] + "e"})
    if u.endswith(("as", "os")):
        out.add(u[:-2] + "o")
    return out


B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def b64int(s):
    n = 0
    for c in s:
        n = n * 64 + B64.index(c)
    return n


POS_MAP = {
    "n": "noun", "v": "verb", "adj": "adj", "adv": "adv", "prep": "prep",
    "pron": "pron", "conj": "conj", "interj": "interj", "num": "num",
    "art": "art", "det": "det", "phrase": "phrase",
}
POS_SKIP = {"suffix", "prefix", "name", "pn", "proverb", "abbrev", "initialism",
            "symbol", "punct", "character"}


def invert_dictionary(dict_dz, index_path):
    """spanish headword -> {english headword: best position in its sense list}.

    The position matters for ranking. If the English entry "girl" lists
    "chica" as its FIRST Spanish translation, then "girl" is very likely the
    primary meaning of "chica" - whereas "bird" merely mentions "chica" far
    down its own list. Position beats raw English frequency, which on its own
    would rank "bird" above "girl" because "bird" is the commoner word.
    """
    raw = gzip.open(dict_dz, "rb").read()
    inv = collections.defaultdict(dict)
    pos_of = {}
    with open(index_path, encoding="utf-8") as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) < 3:
                continue
            eng, off, ln = p[0], b64int(p[1]), b64int(p[2])
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z '\-]*", eng):
                continue
            body = raw[off:off + ln].decode("utf-8", "replace").split("\n")
            if not body:
                continue
            m = re.search(r"<([a-z]+)>\s*$", body[0])
            tag = m.group(1) if m else ""
            if tag in POS_SKIP:
                continue
            pos = POS_MAP.get(tag, tag)
            for line2 in body[1:]:
                t = line2.strip()
                if not t:
                    continue
                t = re.sub(r"^\d+\.\s*", "", t)
                t = re.sub(r"\s*\d+\.\s*$", "", t).strip()
                if not t or t.startswith("(") or len(t) > 90:
                    break
                for i, cand in enumerate(t.split(",")):
                    cand = cand.strip().lower()
                    if not re.fullmatch(r"[a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1]+", cand):
                        continue
                    e = eng.lower()
                    prev = inv[cand].get(e)
                    if prev is None or i < prev:
                        inv[cand][e] = i
                    pos_of.setdefault(cand, pos)
                break
    return inv, pos_of


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--core-only", action="store_true")
    args = ap.parse_args()

    words = []
    seen_exact = set()   # exact core spellings, so "la tarde" and "tarde" coexist
    seen_norm = set()    # accent-stripped bare words, for the AUTO dedupe

    for raw in CORE.strip().split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        es, en, pos = [x.strip() for x in raw.split("|")]
        if es in seen_exact:
            sys.stderr.write("duplicate in CORE: %s\n" % es)
            continue
        seen_exact.add(es)
        seen_norm.add(unaccent(bare(es)))
        words.append({"es": es, "en": [g.strip() for g in en.split(",")],
                      "pos": pos, "src": "core"})
    sys.stderr.write("core: %d words\n" % len(words))

    if not args.core_only and len(words) < args.limit:
        es_rank = freq_rank(fetch(ES_FREQ, "es_50k.txt"))
        en_rank = freq_rank(fetch(EN_FREQ, "en_50k.txt"))

        tar = fetch(DICT, "eng-spa.dictd.tar.xz")
        ddir = os.path.join(CACHE, "eng-spa")
        if not os.path.exists(os.path.join(ddir, "eng-spa.index")):
            import tarfile
            with tarfile.open(tar) as tf:
                tf.extractall(CACHE)
        inv, pos_of = invert_dictionary(os.path.join(ddir, "eng-spa.dict.dz"),
                                       os.path.join(ddir, "eng-spa.index"))
        sys.stderr.write("dictionary: %d spanish headwords\n" % len(inv))

        # archaic, dialectal or junk glosses that would mislead a learner
        BAD_GLOSS = {
            "thee", "thy", "thine", "ye", "doth", "hast", "whoa", "eh", "huh",
            "aye", "lest", "dont", "yea", "nay", "prithee", "tee", "vee",
            "mush", "mira", "basta", "ido", "ho", "dibs", "chez", "yonder",
            "ahoy", "groovy", "sooth", "o", "id", "ante", "giggitygiggity",
            "labia", "hand job", "copacetic", "abode", "stead", "slap",
            "shade", "vend", "fray", "bane", "pointer", "hot water", "gee",
            "overripe", "cheese", "backlash", "corps", "handcuff", "lass",
            "bird", "shoot", "mush", "tad", "whoa",
        }
        PROFANITY = {
            "mierda", "puta", "puto", "joder", "cojones", "coño", "polla",
            "cabrón", "gilipollas", "follar", "hostia", "pendejo", "chingar",
            "verga", "culo", "teta", "tetas", "zorra", "maricón", "coger",
        }
        # conjugated verb forms and particles that land on a wrong homograph
        # gloss ("era" -> threshing floor, "son" -> sound, "pienso" -> fodder)
        BLOCK = {
            "era", "eran", "son", "ha", "han", "hay", "hace", "haya", "vamos",
            "pasa", "dije", "dijo", "ve", "va", "van", "digo", "dice", "toma",
            "ido", "esperando", "pienso", "dado", "mira", "diablos", "voy",
            "fui", "fue", "soy", "eres", "sea", "ser\u00e1", "vas", "das",
            "quiero", "quieres", "quiere", "puedo", "puedes", "puede",
            "tengo", "tienes", "tiene", "s\u00e9", "veo", "vi", "oye", "oh",
            "ah", "eh", "ey", "hey", "uh", "um", "ok", "okay", "vale", "guau",
            "cosas", "d\u00f3lares", "a\u00f1os", "d\u00edas", "veces",
        }
        def ok_gloss(g):
            if g in BAD_GLOSS or len(g) > 28:
                return False
            # the source index drops hyphens, so "run-of-the-mill" arrives as
            # one long unreadable token
            return not (" " not in g and len(g) > 14)

        for es in sorted(es_rank, key=lambda w: es_rank[w]):
            if len(words) >= args.limit:
                break
            if len(es) < 2 or not re.fullmatch(r"[a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1]+", es):
                continue
            if es in BLOCK or es in PROFANITY:
                continue
            if variants(es) & seen_norm:
                continue  # plural / gender / accent variant of a word we have
            senses = inv.get(es, {})
            cands = [c for c in senses if ok_gloss(c)]
            # primary-sense first, then the more ordinary English word
            cands.sort(key=lambda c: (min(senses[c], 2), en_rank.get(c, 10 ** 6)))
            if not cands or en_rank.get(cands[0], 10 ** 6) > 40000:
                continue  # no recognisable English gloss -> not worth a card
            seen_norm.add(unaccent(es))
            words.append({"es": es, "en": OVERRIDE.get(es, cands[:3]),
                          "pos": pos_of.get(es, ""), "src": "auto"})

    for i, w in enumerate(words):
        w["id"] = i
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(words, fh, ensure_ascii=False, indent=0)
    sys.stderr.write("wrote %s  (%d words, %d decks of 20)\n"
                     % (OUT, len(words), len(words) // 20))


if __name__ == "__main__":
    main()
