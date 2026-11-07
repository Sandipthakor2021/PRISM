"""SQLite persistence: virtual keys, usage aggregates, request logs, cache entries.

All access happens on the event-loop thread with no `await` between a read and
the write that depends on it, and increments are single atomic UPDATE statements,
so concurrent requests are both counted.
"""
import json
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS virtual_keys (
  virtual_key TEXT PRIMARY KEY, team TEXT NOT NULL, monthly_budget_nano INTEGER NOT NULL,
  rpm INTEGER NOT NULL, tpm INTEGER, model_allowlist TEXT NOT NULL,
  cache_enabled INTEGER NOT NULL, cache_threshold REAL, status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
  virtual_key TEXT NOT NULL, month TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_nano INTEGER NOT NULL DEFAULT 0, cache_hits INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (virtual_key, month));
CREATE TABLE IF NOT EXISTS request_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, virtual_key TEXT,
  requested_model TEXT, resolved_provider TEXT, resolved_model TEXT, status TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost_nano INTEGER NOT NULL DEFAULT 0, cache TEXT NOT NULL DEFAULT 'miss',
  fallback INTEGER NOT NULL DEFAULT 0, route_reason TEXT, retries INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0, upstream_ms INTEGER NOT NULL DEFAULT 0,
  stream INTEGER NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_logs_key ON request_logs(virtual_key, id);
CREATE TABLE IF NOT EXISTS cache_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, virtual_key TEXT NOT NULL, model TEXT NOT NULL,
  context_hash TEXT NOT NULL, prompt_text TEXT NOT NULL, embedding TEXT NOT NULL,
  response TEXT NOT NULL, created_at TEXT NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_cache_scope ON cache_entries(virtual_key, model, id);
"""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def current_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    # ---- virtual keys -------------------------------------------------
    def seed_keys(self, tenants):
        """Idempotent: re-seeding refreshes limits but never resets usage."""
        for t in tenants:
            sc = t.get("semantic_cache", {})
            self.conn.execute(
                """INSERT INTO virtual_keys(virtual_key, team, monthly_budget_nano, rpm, tpm,
                   model_allowlist, cache_enabled, cache_threshold, status, created_at)
                   VALUES(?,?,?,?,?,?,?,?,'active',?)
                   ON CONFLICT(virtual_key) DO UPDATE SET team=excluded.team,
                   monthly_budget_nano=excluded.monthly_budget_nano, rpm=excluded.rpm,
                   tpm=excluded.tpm, model_allowlist=excluded.model_allowlist,
                   cache_enabled=excluded.cache_enabled, cache_threshold=excluded.cache_threshold""",
                (t["virtual_key"], t["team"], round(t["monthly_budget_usd"] * 1e9),
                 t["rate_limit"]["requests_per_minute"], t["rate_limit"].get("tokens_per_minute"),
                 json.dumps(t["model_allowlist"]), 1 if sc.get("enabled") else 0,
                 sc.get("similarity_threshold"), utcnow()))

    def get_key(self, virtual_key):
        row = self.conn.execute("SELECT * FROM virtual_keys WHERE virtual_key=?", (virtual_key,)).fetchone()
        if not row or row["status"] != "active":
            return None
        d = dict(row)
        d["model_allowlist"] = json.loads(d["model_allowlist"])
        return d

    def list_keys(self):
        return [dict(r) for r in self.conn.execute("SELECT virtual_key, team, monthly_budget_nano FROM virtual_keys")]

    # ---- usage / budget ----------------------------------------------
    def spent_nano(self, virtual_key):
        """Cheap budget read: one indexed row for the current calendar month (reset-on-read)."""
        row = self.conn.execute("SELECT cost_nano FROM usage WHERE virtual_key=? AND month=?",
                                (virtual_key, current_month())).fetchone()
        return row["cost_nano"] if row else 0

    def add_usage(self, virtual_key, prompt_tokens, completion_tokens, cost_nano, cache_hit=False):
        # one atomic upsert: concurrent requests are all counted
        self.conn.execute(
            """INSERT INTO usage(virtual_key, month, requests, prompt_tokens, completion_tokens, cost_nano, cache_hits)
               VALUES(?,?,1,?,?,?,?)
               ON CONFLICT(virtual_key, month) DO UPDATE SET requests=requests+1,
               prompt_tokens=prompt_tokens+excluded.prompt_tokens,
               completion_tokens=completion_tokens+excluded.completion_tokens,
               cost_nano=cost_nano+excluded.cost_nano, cache_hits=cache_hits+excluded.cache_hits""",
            (virtual_key, current_month(), prompt_tokens, completion_tokens, cost_nano, 1 if cache_hit else 0))

    # ---- request logs -------------------------------------------------
    def log_request(self, **f):
        cols = ["request_id", "virtual_key", "requested_model", "resolved_provider", "resolved_model",
                "status", "prompt_tokens", "completion_tokens", "cost_nano", "cache", "fallback",
                "route_reason", "retries", "latency_ms", "upstream_ms", "stream", "error"]
        vals = [f.get(c) for c in cols]
        defaults = {"prompt_tokens": 0, "completion_tokens": 0, "cost_nano": 0, "cache": "miss",
                    "fallback": 0, "retries": 0, "latency_ms": 0, "upstream_ms": 0, "stream": 0}
        vals = [defaults.get(c, v) if v is None and c in defaults else v for c, v in zip(cols, vals)]
        self.conn.execute(
            f"INSERT INTO request_logs({','.join(cols)}, created_at) VALUES({','.join('?' * len(cols))},?)",
            vals + [utcnow()])

    def recent_logs(self, key=None, limit=50):
        q, args = "SELECT * FROM request_logs", []
        if key:
            q += " WHERE virtual_key=?"
            args.append(key)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.conn.execute(q, args):
            d = dict(r)
            d["cost_usd"] = d.pop("cost_nano") / 1e9
            d["fallback"] = bool(d["fallback"])
            d["stream"] = bool(d["stream"])
            out.append(d)
        return out

    def usage_summary(self, key, date_from=None, date_to=None):
        """Aggregated from the request log over a window, so it reconciles with client-side totals."""
        q = """SELECT COUNT(*) AS requests,
                      COALESCE(SUM(prompt_tokens),0) AS pt, COALESCE(SUM(completion_tokens),0) AS ct,
                      COALESCE(SUM(cost_nano),0) AS cost,
                      COALESCE(SUM(status='cache_hit'),0) AS hits
               FROM request_logs WHERE virtual_key=? AND status IN ('ok','cache_hit')"""
        args = [key]
        if date_from:
            q += " AND created_at >= ?"
            args.append(date_from)
        if date_to:
            q += " AND created_at < ?"
            args.append(date_to + "T99")  # inclusive: 'YYYY-MM-DD' covers the whole day
        r = self.conn.execute(q, args).fetchone()
        rej = self.conn.execute(
            "SELECT COUNT(*) AS n FROM request_logs WHERE virtual_key=? AND status LIKE 'rejected%'", (key,)
        ).fetchone()["n"]
        return {"key": key, "from": date_from, "to": date_to, "requests": r["requests"],
                "prompt_tokens": r["pt"], "completion_tokens": r["ct"],
                "cost_usd": round(r["cost"] / 1e9, 9), "cache_hits": r["hits"], "rejected": rej}

    # ---- cache --------------------------------------------------------
    def cache_candidates(self, virtual_key, model, context_hash, limit):
        return self.conn.execute(
            """SELECT id, embedding, response FROM cache_entries
               WHERE virtual_key=? AND model=? AND context_hash=? ORDER BY id DESC LIMIT ?""",
            (virtual_key, model, context_hash, limit)).fetchall()

    def cache_store(self, virtual_key, model, context_hash, prompt_text, embedding, response):
        self.conn.execute(
            """INSERT INTO cache_entries(virtual_key, model, context_hash, prompt_text, embedding, response, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (virtual_key, model, context_hash, prompt_text, json.dumps(embedding), json.dumps(response), utcnow()))

    def cache_touch(self, entry_id):
        self.conn.execute("UPDATE cache_entries SET hit_count=hit_count+1 WHERE id=?", (entry_id,))

    def cache_stats(self):
        rows = self.conn.execute(
            """SELECT virtual_key,
                      SUM(cache='hit') AS hits, SUM(cache='miss' AND status='ok') AS misses
               FROM request_logs WHERE status IN ('ok','cache_hit') GROUP BY virtual_key""").fetchall()
        per_key, th, tm = {}, 0, 0
        for r in rows:
            h, m = r["hits"] or 0, r["misses"] or 0
            per_key[r["virtual_key"]] = {"hits": h, "misses": m, "hit_rate": round(h / (h + m), 4) if h + m else 0.0}
            th, tm = th + h, tm + m
        entries = self.conn.execute("SELECT COUNT(*) AS n FROM cache_entries").fetchone()["n"]
        return {"hits": th, "misses": tm, "hit_rate": round(th / (th + tm), 4) if th + tm else 0.0,
                "entries": entries, "per_key": per_key}
