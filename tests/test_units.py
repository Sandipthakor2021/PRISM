"""Unit tests: cost maths, rate limiter, embedder/cache decisions, routing."""
import json
import threading
from decimal import Decimal

from prism import embedder
from prism.classifier import classify, length_only_baseline
from prism.config import load_pricing, ROOT
from prism.limits import RateLimiter
from prism.metering import compute_cost_nano, format_usd
from routing_eval import run_eval


# ---- cost computation ---------------------------------------------------------
def test_cost_matches_price_table():
    pricing = load_pricing(ROOT / "data")
    # alpha-small: $0.15 in / $0.60 out per 1M tokens
    nano = compute_cost_nano(pricing, "alpha-small", 1_000_000, 1_000_000)
    assert nano == round((0.15 + 0.60) * 1e9)
    assert compute_cost_nano(pricing, "alpha-large", 5, 17) == round((5 * 3.00 + 17 * 15.00) / 1e6 * 1e9)


def test_cost_is_exact_and_formatted():
    pricing = load_pricing(ROOT / "data")
    nano = compute_cost_nano(pricing, "beta-small", 3, 7)  # (3*0.10 + 7*0.40)/1e6 USD
    assert format_usd(nano) == "0.000003100"
    assert compute_cost_nano(pricing, "unknown-model", 10, 10) == 0


# ---- rate limiter ------------------------------------------------------------
def test_rate_limiter_blocks_over_limit_and_recovers():
    now = [0.0]
    rl = RateLimiter(window_s=60, clock=lambda: now[0])
    assert all(rl.try_acquire("k", 3)[0] for _ in range(3))
    ok, retry = rl.try_acquire("k", 3)
    assert not ok and retry >= 1
    now[0] = 61
    assert rl.try_acquire("k", 3)[0]


def test_rate_limiter_has_no_over_admission_under_threads():
    rl, admitted, lock = RateLimiter(), [], threading.Lock()

    def worker():
        for _ in range(20):
            if rl.try_acquire("k", 10)[0]:
                with lock:
                    admitted.append(1)
    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(admitted) == 10  # exactly the limit, never more


def test_rate_limits_are_per_key():
    rl = RateLimiter()
    assert rl.try_acquire("a", 1)[0] and not rl.try_acquire("a", 1)[0]
    assert rl.try_acquire("b", 1)[0]


# ---- cache decision (embedding similarity) --------------------------------------------
def sim(a, b):
    return embedder.cosine(embedder.embed(a), embedder.embed(b))


def test_paraphrases_score_above_seed_thresholds():
    assert sim("How do I reset my password on the dashboard?", "What are the steps to reset my dashboard password?") >= 0.92
    assert sim("What is the refund policy for annual plans?", "If I bought an annual plan, can I get my money back?") >= 0.92


def test_near_miss_and_unrelated_stay_below_threshold():
    assert sim("How do I reset my password on the dashboard?",
               "How do I reset my two-factor authentication on the dashboard?") < 0.85
    assert sim("How do I reset my password?", "Compare TCP and UDP for game servers.") < 0.3


# ---- routing -----------------------------------------------------------------------------
def test_router_hits_target_accuracy_and_beats_length_baseline():
    rep = run_eval()
    assert rep["accuracy"] >= 0.80
    assert rep["accuracy"] > rep["length_only_baseline_accuracy"] + 0.2


def test_router_trap_cases():
    assert classify("Prove that the square root of 2 is irrational.")[0] == "smart"      # short but hard
    long_trivial = ("Here is a roster: " + ", ".join(f"day{i} - person{i}" for i in range(30)) + ". Who is on call on day 3?")
    assert classify(long_trivial)[0] == "fast"                                              # long but trivial
    assert length_only_baseline(long_trivial) == "smart"                                    # (the strawman is fooled)


def test_router_held_out_prompts():
    """Prompts written independently of the provided eval set (generalisation check)."""
    simple = ["What is the boiling point of water in Fahrenheit?", "Translate 'thank you' to German.",
              "Extract the phone number from: 'Call Sam at 555-0100 tomorrow.'",
              "Rewrite this sentence to be more formal: 'gonna be late, sorry'."]
    hard = ["Prove that there are infinitely many prime numbers.",
            "Design a rate limiter for a multi-tenant API and explain the trade-offs between algorithms.",
            "Why can retries with no jitter cause a thundering herd, and how would you diagnose it?"]
    wrong = [p for p in simple if classify(p)[0] != "fast"] + [p for p in hard if classify(p)[0] != "smart"]
    assert len(wrong) <= 1, wrong
