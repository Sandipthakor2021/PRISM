"""Per-key semantic response cache.

Scoping (security): every lookup is `WHERE virtual_key = <caller's key> AND model = <alias>`,
so one tenant can never be served another tenant's response.
Multi-turn: similarity is computed on the LAST user message; the earlier conversation
(system prompt + previous turns) must match exactly (via a hash), so a follow-up like
'what happens when it is exhausted?' never matches a different conversation.
Only successful, non-empty responses are stored. Time-sensitive prompts are never cached.
"""
import hashlib
import json
import re

from . import embedder

TIME_SENSITIVE = re.compile(
    r"\b(current|currently|right now|today|tonight|latest|now|status|live|breaking|this (week|month|year)|"
    r"yesterday|tomorrow|price of|weather|score)\b", re.I)


def split_prompt(messages):
    """-> (last_user_text, context_hash)."""
    last_idx = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
    if last_idx is None:
        return "", ""
    last = str(messages[last_idx].get("content", ""))
    ctx = [(m.get("role"), str(m.get("content", ""))) for m in messages[:last_idx]]
    return last, hashlib.sha256(json.dumps(ctx).encode()).hexdigest()


def is_cacheable_prompt(text):
    return bool(text.strip()) and not TIME_SENSITIVE.search(text)


class SemanticCache:
    def __init__(self, db, scan_limit=500):
        self.db = db
        self.scan_limit = scan_limit

    def lookup(self, virtual_key, model, messages, threshold):
        text, ctx = split_prompt(messages)
        if not is_cacheable_prompt(text):
            return None
        query = embedder.embed(text)
        best, best_score = None, 0.0
        for row in self.db.cache_candidates(virtual_key, model, ctx, self.scan_limit):
            score = embedder.cosine(query, json.loads(row["embedding"]))
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= threshold:
            self.db.cache_touch(best["id"])
            return json.loads(best["response"]), best_score
        return None

    def store(self, virtual_key, model, messages, response):
        text, ctx = split_prompt(messages)
        if not is_cacheable_prompt(text):
            return False
        content = (response.get("choices") or [{}])[0].get("message", {}).get("content")
        if not content:
            return False
        self.db.cache_store(virtual_key, model, ctx, text, embedder.embed(text), response)
        return True
