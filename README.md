# Prism: LLM Gateway and Semantic Cache

An OpenAI-compatible gateway that sits between applications and model providers. It authenticates virtual keys,
enforces per-key allowlists / rate limits / monthly budgets, routes by alias with retries and failover, streams
without buffering, routes `model: "auto"` by prompt difficulty, serves paraphrased repeats from a per-key semantic
cache, and meters every request. Built with **Python 3.10+, FastAPI, SQLite, httpx**.

The original capstone brief is in `PRISM_PROBLEM_STATEMENT.md` and `docs/`; the provided pack README is kept at
`docs/CAPSTONE_PACK_README.md`. Latest verification results: `docs/VERIFICATION_REPORT.md`.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

./scripts/dev_up.sh          # starts mock providers alpha:9001, beta:9002 and the gateway on :8080
# or in three terminals:
python3 scripts/mock_provider.py --port 9001 --name alpha
python3 scripts/mock_provider.py --port 9002 --name beta
python3 -m uvicorn prism.app:app --port 8080
```

Open the ops console at <http://localhost:8080/console> (admin token `prism-admin-demo`).

| Env var | Default | Meaning |
|---|---|---|
| `PRISM_DB` | `./prism.sqlite3` | SQLite file (keys, usage, logs, cache survive restarts) |
| `PRISM_ADMIN_TOKEN` | `prism-admin-demo` | demo token for `/admin/*` (header `X-Admin-Token`) |
| `PRISM_UPSTREAM_TIMEOUT` | `2.5` | seconds before an upstream call is abandoned |

**Seeding / provider config:** on startup the gateway loads `data/seed_keys.json` (upserts tenants; re-seeding refreshes
limits but never resets usage), `data/model_pricing.json`, and `data/gateway_config.sample.json` (providers, aliases,
fallback chains, retry policy). A model belongs to the provider whose name prefixes it (`alpha-small` -> `alpha`).
Provider `api_key`s stay server-side; they never appear in responses, logs or errors (tested).

## Try it

```bash
KEY=prism-sk-search-1a2b3c
curl -si localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"What is a message queue?"}]}'
curl -sN localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"fast","stream":true,"messages":[{"role":"user","content":"Explain SSE briefly"}]}'
```

Failure injection on the mocks (no restart needed):
`curl -X POST localhost:9001/admin/config -d '{"mode":"down"}'` (also `{"latency_ms":3000}`, `{"fail_rate":0.3}`, `{"mode":"ok","fail_rate":0,"latency_ms":0}`).

### Seeded demo keys

| Team | Key | Allowlist | RPM | Budget | Cache (threshold) |
|---|---|---|---|---|---|
| search | `prism-sk-search-1a2b3c` | fast | 60 | $50 | on (0.92) |
| research | `prism-sk-research-4d5e6f` | fast, smart, auto | 300 | $500 | off |
| free-tier | `prism-sk-free-7g8h9i` | fast | 10 | $5 | on (0.85) |
| budget-demo | `prism-sk-budget-demo-0j1k2l` | fast | 60 | $0.00001 | off |

## API

### Data plane: `POST /v1/chat/completions` (OpenAI-compatible)
`Authorization: Bearer <virtual key>`; body `{"model": "fast|smart|auto|<model>", "messages": [...], "stream": false}`.
Every response (including errors) carries `x-prism-provider`, `x-prism-cache` (`hit`/`miss`), `x-prism-fallback`
(`true` only if a fallback served it), `x-prism-cost-usd` (`0.000000000` for hits; omitted on streams, whose cost is
recorded in the log and usage API). Cache hits also send `x-prism-cache-similarity`.
Also: `GET /v1/models`, `GET /health`.

### Errors (OpenAI-style body `{"error": {"message", "type", "code"}}`)

| Case | HTTP | `type` | Log `status` |
|---|---|---|---|
| Missing/invalid virtual key | 401 | `authentication_error` | `rejected_auth` |
| Malformed body / empty messages | 400 | `invalid_request_error` | `rejected_invalid` |
| Unknown model or alias | 404 | `not_found_error` | `rejected_unknown_model` |
| Model not on the key's allowlist | 403 | `model_not_allowed` | `rejected_allowlist` |
| Rate limit exceeded (`Retry-After` sent) | 429 | `rate_limit_exceeded` | `rejected_rate_limit` |
| Monthly budget exhausted | 402 | `budget_exceeded` | `rejected_budget` |
| Every provider failed | 502 | `upstream_error` | `upstream_error` |

**Check order** (cheapest first, upstream last): auth -> body -> known model -> allowlist -> rate limit -> budget ->
`auto` classification -> cache -> upstream. Every request, including every rejection, writes one request-log row.

### Admin plane (header `X-Admin-Token: prism-admin-demo`)
- `GET /admin/usage[?key=&from=&to=]` spend, tokens, cache hits, rejections per key (aggregated from the request log, so it reconciles with client totals; includes budget and month-to-date spend)
- `GET /admin/logs?key=&limit=` newest first; fields: request_id, virtual_key, requested_model, resolved_provider/model, status, tokens, cost_usd, cache, fallback, route_reason, retries, latency_ms, upstream_ms, stream, error
- `GET /admin/cache/stats` hits, misses, hit rate overall and per key
- `GET /admin/routing/eval` runs the routing eval and returns the report
- `GET /console` the ops console (usage by key, cache hit rate, recent requests, routing eval)

## Architecture

```
client -> FastAPI app (prism/app.py)
            -> Gateway.chat (prism/gateway.py)  the request pipeline
                 limits.py      race-safe sliding-window rate limiter
                 db.py          SQLite: keys, usage, request logs, cache entries
                 classifier.py  `auto` difficulty routing
                 cache.py + embedder.py   per-key semantic cache
                 upstream.py    retries, backoff, ordered failover
                 providers/     adapter interface; OpenAI-compatible HTTP adapter; in-process fake for tests
                 metering.py    cost from provider-reported usage (integer nano-USD)
```
The core only talks to the `ProviderAdapter` interface (`chat`, `open_stream`), so mocks, real providers (Ollama, hosted)
or the in-process `FakeProvider` used by the tests are interchangeable.

## Design decisions

- **Rate limiting:** in-memory sliding window; check-and-record happens under one lock, so concurrent requests cannot both
  see "room left" (no read-then-write race). Verified by a threaded unit test (exactly the limit admitted), an HTTP concurrency test and `load_test.py`. State is per process and resets on restart.
- **Budgets:** budget check is one indexed read of the key's month row (reset-on-read calendar month, UTC). Usage is added
  with a single atomic `INSERT ... ON CONFLICT DO UPDATE`. **Overshoot (documented):** a request admitted while others are in flight can push spend past the budget by up to its own cost (and streams by their final cost); there is no pre-reservation. Money is integer nano-USD so sums are exact.
- **Metering:** cost = `prompt_tokens * input_per_1m + completion_tokens * output_per_1m` from the provider's `usage` object, never from client-declared numbers. Streams capture usage from the final upstream chunk.
- **Streaming:** each upstream SSE line is forwarded as it arrives; the gateway only observes lines to capture usage/content. Ends with `data: [DONE]`. **Mid-stream failure policy:** an SSE `error` event followed by `[DONE]`; the log row is `stream_error`; the gateway never restarts on another provider or splices outputs. Failover for streams happens only *before* the first token. Client disconnects still write a log row and usage.
- **Retries and failover:** retryable errors (429, 5xx, connection errors) are retried on the same provider up to `max_attempts` (3) with exponential backoff (200 ms x2); a **timeout fails over immediately** (retrying a slow provider multiplies the client's wait). Non-retryable upstream 4xx skips to the next target. `x-prism-fallback` and the log record failover. There is no circuit breaker, so a down primary is retried on every request (about 0.6 s extra) and traffic returns to it the moment it recovers.
- **`auto` routing:** `classifier.py` scores the *task*, not the length: reasoning/expertise cues (+) and lookup/transform cues (-), applied to the first + last sentence of long prompts (where instructions live, not pasted data). Threshold 2.0 -> `smart`, else `fast`. The decision and reason (score and matched signals) are stored in `route_reason`.
- **Semantic cache:** local substitute for neural embeddings. Prompt -> weighted concept vector (stopwords removed, stemming, synonym/phrase canonicalisation, low weight for filler words like "steps"/"policy"), cosine similarity vs the key's threshold from `seed_keys.json`.
  - *Scoping:* lookups are `WHERE virtual_key = caller AND model = alias`; one tenant can never see another's entries (tested). The `research` and `budget-demo` keys have the cache disabled per seed data.
  - *Multi-turn:* similarity is on the last user message; earlier turns must match exactly (hash), so identical follow-ups in different conversations never hit.
  - *Not cached:* errors, empty responses, time-sensitive prompts ("current", "status", "today", "now" ...). No eviction or TTL (entries accumulate; see limitations).
  - *Streaming:* a stream can hit the cache (replayed as one content chunk + `[DONE]`) and a completed stream fills the cache.
  - Budget is checked before the cache, so an exhausted key is rejected even for would-be hits.

## Evaluation flow

```bash
python3 -m pytest -q                                        # 40 automated tests (unit + integration with fake providers)
python3 scripts/smoke_test.py --url http://localhost:8080 --key prism-sk-search-1a2b3c --model fast
python3 scripts/load_test.py  --url http://localhost:8080 --key prism-sk-free-7g8h9i --model fast --requests 30 --concurrency 10 --rpm-limit 10
python3 scripts/routing_eval.py                             # one-command routing eval (add --json for the full report)
python3 scripts/verify.py                                   # runs everything above plus drills and writes docs/VERIFICATION_REPORT.md
```
`verify.py` needs a running gateway + mocks and is best run on a fresh DB (`RESET_DB=1 ./scripts/dev_up.sh`), since the
budget-demo key can only be exhausted once per month. It waits about a minute so the free key's rate-limit window is fresh.

**Demo flow** (see the problem statement's 10 steps): headers on a normal request -> stream tokens -> repeat + paraphrase hit ->
`auto` with `route_011` (smart) and `route_006` (fast) + routing eval -> burst the free key -> budget-demo key twice ->
kill `alpha` live -> open `/console` -> run smoke and load tests.

## Known limitations

- Single gateway instance: the rate limiter and its state are in memory (resets on restart); no distributed limiting.
- `tokens_per_minute` from the seed data is stored but **not enforced** (requests-per-minute is).
- Budget overshoot by in-flight requests (see above); no budget reset job beyond reset-on-read per calendar month.
- SQLite is accessed synchronously on the event loop. Fine at this scale, not for high throughput.
- The embedder matches shared concepts, not deep meaning ("forgot password" vs "reset password" is a miss). Synonym tables are small, and thresholds were tuned on the provided pairs. A neural embedding model can replace `embedder.py` behind the same `embed()`/`cosine()` interface.
- The `auto` classifier is heuristic and was tuned while looking at the 20 provided cases (100% there); `tests/test_units.py` checks held-out prompts, but real-world accuracy will be lower. Misrouting is possible on unusual prompts.
- No cache eviction/TTL; no circuit breaker or health-based skipping; no key-management API; provider health endpoint not implemented.
- Streaming cache hits are replayed as a single chunk rather than a paced stream.
