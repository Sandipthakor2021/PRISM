"""Local embedding substitute for the semantic cache (documented, dependency-free).

A prompt is turned into a sparse vector of weighted *concepts*:
  * lowercase, strip punctuation, drop stopwords ('how do I', 'what is the', 'my' ...)
  * crude stemming (plans -> plan, resetting -> reset)
  * phrase + synonym canonicalisation ('money back' -> refund, 'bought' -> purchase)
  * 'filler' words that carry little intent ('steps', 'policy', 'way') get a small weight
    instead of full weight, so 'reset password' ~ 'what are the steps to reset password'
Similarity is cosine over these vectors. Different intent shares few concepts
('reset password' vs 'reset two-factor authentication' scores ~0.5) so it stays a miss.

Limits (documented in the README): this matches shared concepts, not deep meaning. A neural
embedding model can be dropped in by implementing the same `embed(text) -> dict` interface.
"""
import math
import re

STOPWORDS = set("""a an the i me my we our you your it its is are was were be been am do does did
how what which who whom whose when where why can could would should will shall may might must
to of in on at for from by with about as into than then so if or and but not no yes please
this that these those there here also just really very some any there s t""".split())

# low-information words: kept, but with a small weight
FILLER = set("""step steps way ways process procedure policy info information detail details
explain tell describe give show want need get got let make purchase""".split())

PHRASES = [
    (r"\bmoney back\b", "refund"), (r"\bget my money\b", "refund"), (r"\bpaid back\b", "refund"),
    (r"\btwo[- ]factor\b|\b2fa\b|\bmulti[- ]factor\b", "twofactor"),
    (r"\blog ?in\b|\bsign ?in\b", "login"), (r"\blog ?out\b|\bsign ?out\b", "logout"),
]
SYNONYMS = {
    "refunds": "refund", "reimburse": "refund", "reimbursement": "refund",
    "bought": "purchase", "buy": "purchase", "purchased": "purchase", "buying": "purchase",
    "cancel": "cancel", "terminate": "cancel", "unsubscribe": "cancel",
    "cost": "price", "pricing": "price", "fee": "price", "charge": "price",
    "remove": "delete", "erase": "delete",
    "passcode": "password", "passwd": "password",
    "yearly": "annual", "annually": "annual",
    "plans": "plan", "subscription": "plan", "subscriptions": "plan",
}


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokenize(text):
    t = text.lower()
    for pattern, repl in PHRASES:
        t = re.sub(pattern, f" {repl} ", t)
    words = re.findall(r"[a-z0-9]+", t)
    out = []
    for w in words:
        if w in STOPWORDS:
            continue
        w = SYNONYMS.get(w, w)
        w = SYNONYMS.get(_stem(w), _stem(w))
        out.append(w)
    return out


def embed(text):
    vec = {}
    for w in tokenize(text):
        weight = 0.25 if w in FILLER else 1.0
        vec[w] = max(vec.get(w, 0.0), weight)
    return vec


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0
