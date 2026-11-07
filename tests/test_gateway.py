"""Integration tests through the real HTTP app with in-process fake providers."""
import json
from concurrent.futures import ThreadPoolExecutor

from conftest import ADMIN, BUDGET, FREE, RESEARCH, SEARCH, auth, body

URL = "/v1/chat/completions"
# long enough that one request costs more than the budget-demo key's whole $0.00001 budget
LONG = "Explain in plain words how caching works in a distributed web application and why it helps latency"


def post(env, key, payload):
    return env.client.post(URL, json=payload, headers=auth(key))


def logs(env, key=None, limit=50):
    return env.client.get("/admin/logs", params={"key": key, "limit": limit} if key else {"limit": limit}, headers=ADMIN).json()["logs"]


# ---- header contract + metering -------------------------------------------------------
def test_response_carries_header_contract_and_cost(env):
    r = post(env, SEARCH, body("What is a load balancer?"))
    assert r.status_code == 200
    assert r.headers["x-prism-provider"] == "alpha/alpha-small"
    assert r.headers["x-prism-cache"] == "miss"
    assert r.headers["x-prism-fallback"] == "false"
    assert float(r.headers["x-prism-cost-usd"]) > 0
    assert r.json()["usage"]["prompt_tokens"] == 10   # cost uses the PROVIDER's usage


def test_client_declared_tokens_are_ignored(env):
    r = post(env, SEARCH, body("hello there", usage={"prompt_tokens": 999999}, prompt_tokens=999999))
    # fake provider reports 10 prompt tokens; cost must follow that, not client claims
    assert float(r.headers["x-prism-cost-usd"]) < 0.001


# ---- four distinct rejection types (+ unknown model) ----------------------------------
def test_invalid_key_is_401(env):
    r = post(env, "nope", body("hi"))
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    assert "x-prism-provider" in r.headers  # header contract also on errors


def test_missing_key_is_401(env):
    assert env.client.post(URL, json=body("hi")).status_code == 401


def test_model_not_on_allowlist_is_403(env):
    r = post(env, SEARCH, body("hi", model="smart"))
    assert r.status_code == 403 and r.json()["error"]["type"] == "model_not_allowed"
    assert post(env, SEARCH, body("hi", model="auto")).status_code == 403   # search key only has fast


def test_unknown_model_is_404(env):
    r = post(env, SEARCH, body("hi", model="no-such-model-xyz"))
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"


def test_malformed_requests_are_400(env):
    assert post(env, SEARCH, {"model": "fast", "messages": []}).status_code == 400
    assert post(env, SEARCH, {"messages": [{"role": "user", "content": "x"}]}).status_code == 400


def test_rate_limit_is_429_with_exact_admission_count(env):
    codes = [post(env, FREE, body(f"unique prompt number {i} about topic{i}")).status_code for i in range(15)]
    assert codes.count(200) == 10 and codes.count(429) == 5      # free tier = 10 rpm
    r = post(env, FREE, body("another"))
    assert r.json()["error"]["type"] == "rate_limit_exceeded" and "retry-after" in r.headers


def test_budget_exhausted_after_one_request(env):
    assert post(env, BUDGET, body(LONG)).status_code == 200
    r = post(env, BUDGET, body(LONG))
    assert r.status_code == 402 and r.json()["error"]["type"] == "budget_exceeded"


def test_every_request_including_rejections_is_logged(env):
    post(env, "bad-key", body("x"))
    post(env, SEARCH, body("x", model="smart"))
    post(env, BUDGET, body(LONG))
    post(env, BUDGET, body(LONG + " again"))
    statuses = {l["status"] for l in logs(env)}
    assert {"rejected_auth", "rejected_allowlist", "ok", "rejected_budget"} <= statuses


# ---- routing / failover ---------------------------------------------------------------
def test_alias_resolves_to_provider_models(env):
    assert post(env, RESEARCH, body("hi", model="smart")).headers["x-prism-provider"] == "alpha/alpha-large"


def test_auto_routes_by_difficulty_and_logs_reason(env):
    hard = post(env, RESEARCH, body("Prove that the square root of 2 is irrational.", model="auto"))
    easy = post(env, RESEARCH, body("What is the capital of France?", model="auto"))
    assert hard.headers["x-prism-provider"].endswith("alpha-large")
    assert easy.headers["x-prism-provider"].endswith("alpha-small")
    entry = logs(env, RESEARCH, 2)
    assert all(e["requested_model"] == "auto" and "auto ->" in e["route_reason"] for e in entry)


def test_failover_when_primary_down(env):
    env.alpha.mode = "down"
    r = post(env, SEARCH, body("failover please"))
    assert r.status_code == 200
    assert r.headers["x-prism-provider"] == "beta/beta-small" and r.headers["x-prism-fallback"] == "true"
    e = logs(env, SEARCH, 1)[0]
    assert e["fallback"] is True and e["retries"] >= 1 and e["resolved_provider"] == "beta"


def test_failover_when_primary_rate_limited_or_slow(env):
    for mode in ("rate_limited", "slow"):
        env.alpha.mode = mode
        r = post(env, SEARCH, body(f"failover {mode}"))
        assert r.status_code == 200 and r.headers["x-prism-fallback"] == "true"


def test_retry_absorbs_transient_errors_without_failover(env):
    env.alpha.fail_first = 2   # fails twice then succeeds; max_attempts is 3
    r = post(env, SEARCH, body("flaky provider"))
    assert r.status_code == 200 and r.headers["x-prism-fallback"] == "false"
    assert logs(env, SEARCH, 1)[0]["retries"] == 2


def test_502_when_every_provider_fails_and_nothing_is_cached(env):
    env.alpha.mode = env.beta.mode = "down"
    r = post(env, SEARCH, body("everything is down"))
    assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
    env.alpha.mode = env.beta.mode = "ok"
    assert post(env, SEARCH, body("everything is down")).headers["x-prism-cache"] == "miss"  # errors never cached


# ---- streaming --------------------------------------------------------------------------
def sse_lines(resp):
    return [l for l in resp.text.splitlines() if l.startswith("data: ")]


def test_stream_terminates_with_done_and_usage_is_recorded(env):
    r = post(env, SEARCH, body("explain sse briefly", stream=True))
    lines = sse_lines(r)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert len(lines) >= 3 and lines[-1] == "data: [DONE]"
    assert r.headers["x-prism-provider"] == "alpha/alpha-small" and r.headers["x-prism-cache"] == "miss"
    e = logs(env, SEARCH, 1)[0]
    assert e["stream"] is True and e["status"] == "ok" and e["completion_tokens"] > 0 and e["cost_usd"] > 0
    usage = env.client.get("/admin/usage", params={"key": SEARCH}, headers=ADMIN).json()
    assert usage["completion_tokens"] == e["completion_tokens"]


def test_stream_failover_before_first_token(env):
    env.alpha.mode = "down"
    r = post(env, SEARCH, body("stream failover", stream=True))
    assert r.headers["x-prism-fallback"] == "true" and sse_lines(r)[-1] == "data: [DONE]"


def test_mid_stream_failure_emits_error_event_and_never_splices(env):
    env.alpha.mode = "stream_dies"
    r = post(env, SEARCH, body("this stream will die", stream=True))
    lines = sse_lines(r)
    assert any('"upstream_error"' in l for l in lines) and lines[-1] == "data: [DONE]"
    assert env.beta.calls == 0                       # did not silently restart on another provider
    assert logs(env, SEARCH, 1)[0]["status"] == "stream_error"


# ---- semantic cache ------------------------------------------------------------------------
def test_paraphrase_hits_cache_for_free(env):
    first = post(env, SEARCH, body("How do I reset my password on the dashboard?"))
    para = post(env, SEARCH, body("What are the steps to reset my dashboard password?"))
    assert first.headers["x-prism-cache"] == "miss" and para.headers["x-prism-cache"] == "hit"
    assert para.headers["x-prism-cost-usd"] == "0.000000000"
    assert env.alpha.calls == 1                      # the provider was only called once


def test_near_miss_is_not_a_hit(env):
    post(env, SEARCH, body("How do I reset my password on the dashboard?"))
    r = post(env, SEARCH, body("How do I reset my two-factor authentication on the dashboard?"))
    assert r.headers["x-prism-cache"] == "miss"


def test_cache_is_isolated_between_tenants(env):
    p = body("What is the secret internal launch plan for the new dashboard?")
    assert post(env, SEARCH, p).headers["x-prism-cache"] == "miss"
    assert post(env, SEARCH, p).headers["x-prism-cache"] == "hit"
    other = post(env, FREE, p)                        # same prompt, different tenant
    assert other.status_code == 200 and other.headers["x-prism-cache"] == "miss"


def test_cache_respects_per_key_settings_and_time_sensitive_prompts(env):
    p = body("Explain how DNS resolution works")
    post(env, RESEARCH, p)                            # research key: cache disabled
    assert post(env, RESEARCH, p).headers["x-prism-cache"] == "miss"
    t = body("What is the current status of the payments service?")
    post(env, SEARCH, t)
    assert post(env, SEARCH, t).headers["x-prism-cache"] == "miss"   # time-sensitive: never cached


def test_multi_turn_context_must_match(env):
    a = {"model": "fast", "messages": [{"role": "user", "content": "What is connection pooling?"},
                                       {"role": "assistant", "content": "It reuses connections."},
                                       {"role": "user", "content": "What happens when the pool is exhausted?"}]}
    b = {"model": "fast", "messages": [{"role": "user", "content": "What is thread pooling?"},
                                       {"role": "assistant", "content": "It reuses threads."},
                                       {"role": "user", "content": "What happens when the pool is exhausted?"}]}
    post(env, SEARCH, a)
    assert post(env, SEARCH, a).headers["x-prism-cache"] == "hit"
    assert post(env, SEARCH, b).headers["x-prism-cache"] == "miss"   # different conversation, same last turn


def test_streaming_request_can_hit_cache(env):
    post(env, SEARCH, body("How do I reset my password on the dashboard?"))
    r = post(env, SEARCH, body("What are the steps to reset my dashboard password?", stream=True))
    assert r.headers["x-prism-cache"] == "hit" and sse_lines(r)[-1] == "data: [DONE]"


# ---- secrets + admin + concurrency ------------------------------------------------------------
def test_provider_keys_never_leak(env):
    env.alpha.mode = env.beta.mode = "down"
    texts = [post(env, SEARCH, body("leak check")).text, json.dumps(logs(env)),
             json.dumps(env.client.get("/v1/models").json())]
    assert all("mock-key" not in t for t in texts)


def test_admin_endpoints_need_the_token(env):
    assert env.client.get("/admin/usage").status_code == 401
    assert env.client.get("/admin/logs", headers=ADMIN).status_code == 200


def test_concurrent_requests_are_all_counted_and_reconcile(env):
    def one(i):
        r = post(env, RESEARCH, body(f"concurrent request {i} about subject{i}"))
        return r.status_code, float(r.headers["x-prism-cost-usd"]), r.json()["usage"]
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(one, range(60)))
    assert all(c == 200 for c, _, _ in results)
    u = env.client.get("/admin/usage", params={"key": RESEARCH}, headers=ADMIN).json()
    assert u["requests"] == 60
    assert u["prompt_tokens"] == sum(x["prompt_tokens"] for _, _, x in results)
    assert u["completion_tokens"] == sum(x["completion_tokens"] for _, _, x in results)
    assert abs(u["cost_usd"] - sum(c for _, c, _ in results)) < 1e-8


def test_no_over_admission_through_http_under_concurrency(env):
    with ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda i: post(env, FREE, body(f"burst {i} subject{i}")).status_code, range(40)))
    assert codes.count(200) == 10        # free tier: 10 requests per minute, exactly


def test_routing_eval_endpoint(env):
    rep = env.client.get("/admin/routing/eval", headers=ADMIN).json()
    assert rep["total"] == 20 and rep["accuracy"] >= 0.8
