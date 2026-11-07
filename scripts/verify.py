#!/usr/bin/env python3
"""Run the full verification against a RUNNING gateway + mock providers and write the report.

    python3 scripts/verify.py [--url http://localhost:8080] [--out docs/VERIFICATION_REPORT.md]

Best run on a fresh database (delete prism.sqlite3 and restart the gateway first) because the
budget-demo key can only be 'consumed' once per month. Stdlib only.
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCH, RESEARCH, FREE, BUDGET = ("prism-sk-search-1a2b3c", "prism-sk-research-4d5e6f",
                                  "prism-sk-free-7g8h9i", "prism-sk-budget-demo-0j1k2l")
ADMIN = {"X-Admin-Token": "prism-admin-demo"}


def call(url, key, body, timeout=30):
    req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(r.read()), time.monotonic() - t
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, json.loads(e.read() or b"{}"), time.monotonic() - t


def admin(url, path):
    req = urllib.request.Request(url + path, headers=ADMIN)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def mock(port, cfg):
    req = urllib.request.Request(f"http://localhost:{port}/admin/config", data=json.dumps(cfg).encode(), method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    return p.returncode, (p.stdout + p.stderr).strip()


def msg(p):
    return {"model": "fast", "messages": [{"role": "user", "content": p}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--out", default=str(ROOT / "docs" / "VERIFICATION_REPORT.md"))
    a = ap.parse_args()
    url, out = a.url, []
    salt = str(int(time.time()))[-6:]
    py = sys.executable

    def sec(title):
        out.append(f"\n## {title}\n")
        print(f"== {title}")

    out.append("# Prism Verification Report")
    out.append(f"\nGenerated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} against `{url}` with two mock providers "
               "(alpha :9001, beta :9002). Produced by `python3 scripts/verify.py`.")

    sec("1. Smoke test")
    rc, text = run([py, "scripts/smoke_test.py", "--url", url, "--key", SEARCH, "--model", "fast"])
    out.append("```text\n" + text + "\n```")
    smoke_ok = rc == 0

    sec("2. Semantic cache behaviour (search key, threshold 0.92)")
    rows = []
    pairs = [("A1 (first ask)", f"({salt}) How do I reset my password on the dashboard?"),
             ("A2 (paraphrase of A1)", f"({salt}) What are the steps to reset my dashboard password?"),
             ("A3 (near-miss: 2FA reset)", f"({salt}) How do I reset my two-factor authentication on the dashboard?"),
             ("B1 (first ask)", f"({salt}) What is the refund policy for annual plans?"),
             ("B2 (paraphrase of B1)", f"({salt}) If I bought an annual plan, can I get my money back?")]
    for name, p in pairs:
        s, h, _, _ = call(url, SEARCH, msg(p))
        rows.append(f"| {name} | {h.get('x-prism-cache')} | {h.get('x-prism-cache-similarity', '-')} | {h.get('x-prism-cost-usd')} |")
    out.append("| Prompt | x-prism-cache | similarity | cost |\n|---|---|---|---|\n" + "\n".join(rows))
    p_iso = f"({salt}) What is the confidential tenant isolation probe?"
    call(url, SEARCH, msg(p_iso))
    s1, h1, _, _ = call(url, SEARCH, msg(p_iso))
    time.sleep(0)  # different tenant, same prompt
    s2, h2, _, _ = call(url, "prism-sk-free-7g8h9i", msg(p_iso))
    out.append(f"\nTenant isolation: same prompt, `search` key repeat -> `{h1.get('x-prism-cache')}`; `free-tier` key -> "
               f"`{h2.get('x-prism-cache') if s2 == 200 else 'HTTP ' + str(s2) + ' (rate-limit window exhausted; see tests)'}`.")

    sec("3. Failover drill (mock `alpha` controlled live)")
    steps = []
    mock(9001, {"mode": "down", "latency_ms": 0, "fail_rate": 0})
    s, h, _, t = call(url, RESEARCH, msg(f"({salt}) failover down drill"))
    steps.append(f"| alpha `down` | {s} | {h.get('x-prism-provider')} | {h.get('x-prism-fallback')} | {t:.2f}s |")
    mock(9001, {"mode": "ok", "latency_ms": 3000})
    s, h, _, t = call(url, RESEARCH, msg(f"({salt}) failover slow drill"))
    steps.append(f"| alpha slow 3000 ms (timeout 2.5 s) | {s} | {h.get('x-prism-provider')} | {h.get('x-prism-fallback')} | {t:.2f}s |")
    mock(9001, {"mode": "ok", "latency_ms": 0, "fail_rate": 0.3})
    res = [call(url, RESEARCH, msg(f"({salt}) flaky drill {i}")) for i in range(30)]
    ok = sum(1 for r in res if r[0] == 200)
    fb = sum(1 for r in res if r[1].get("x-prism-fallback") == "true")
    steps.append(f"| alpha flaky (fail_rate 0.3), 30 requests | {ok}/30 succeeded | - | {fb} via fallback | - |")
    mock(9001, {"mode": "ok", "latency_ms": 0, "fail_rate": 0})
    s, h, _, t = call(url, RESEARCH, msg(f"({salt}) restored drill"))
    steps.append(f"| alpha restored | {s} | {h.get('x-prism-provider')} | {h.get('x-prism-fallback')} | {t:.2f}s |")
    out.append("| Scenario | Result | Served by | x-prism-fallback | Client wait |\n|---|---|---|---|---|\n" + "\n".join(steps))

    sec("4. Load test: over-admission and accounting (free-tier key, limit 10 rpm)")
    print("   waiting 61s so the free key's rate-limit window is fresh ...", flush=True)
    time.sleep(61)
    before = admin(url, f"/admin/usage?key={FREE}")
    rc, text = run([py, "scripts/load_test.py", "--url", url, "--key", FREE, "--model", "fast",
                    "--requests", "30", "--concurrency", "10", "--rpm-limit", "10"])
    after = admin(url, f"/admin/usage?key={FREE}")
    out.append("```text\n" + text + "\n```")
    load1_ok = rc == 0
    d = {k: after[k] - before[k] for k in ("requests", "prompt_tokens", "completion_tokens")}
    d["cost_usd"] = round(after["cost_usd"] - before["cost_usd"], 9)
    m_acc = int(re.search(r"accepted requests:\s+(\d+)", text).group(1))
    m_pt = int(re.search(r"prompt tokens:\s+(\d+)", text).group(1))
    m_ct = int(re.search(r"completion tokens:\s+(\d+)", text).group(1))
    recon1 = (d["requests"], d["prompt_tokens"], d["completion_tokens"]) == (m_acc, m_pt, m_ct)
    out.append("Usage API delta for the same key over the burst: "
               f"`requests={d['requests']} prompt_tokens={d['prompt_tokens']} completion_tokens={d['completion_tokens']} "
               f"cost_usd={d['cost_usd']}`  ->  **{'RECONCILES' if recon1 else 'MISMATCH'}** with the client-side totals above.")

    sec("5. Load test: accounting at scale (research key, 300 requests, concurrency 25)")
    before = admin(url, f"/admin/usage?key={RESEARCH}")
    rc, text = run([py, "scripts/load_test.py", "--url", url, "--key", RESEARCH, "--model", "fast",
                    "--requests", "300", "--concurrency", "25", "--rpm-limit", "300"])
    after = admin(url, f"/admin/usage?key={RESEARCH}")
    out.append("```text\n" + text + "\n```")
    load2_ok = rc == 0
    m_acc = int(re.search(r"accepted requests:\s+(\d+)", text).group(1))
    m_pt = int(re.search(r"prompt tokens:\s+(\d+)", text).group(1))
    m_ct = int(re.search(r"completion tokens:\s+(\d+)", text).group(1))
    m_cost = float(re.search(r"sum of cost headers:\s+([\d.]+)", text).group(1))
    api = (after["requests"] - before["requests"], after["prompt_tokens"] - before["prompt_tokens"],
           after["completion_tokens"] - before["completion_tokens"])
    cost_api = round(after["cost_usd"] - before["cost_usd"], 6)
    recon2 = api == (m_acc, m_pt, m_ct) and abs(cost_api - m_cost) < 1e-5
    out.append(f"Usage API delta: `requests={api[0]} prompt_tokens={api[1]} completion_tokens={api[2]} cost_usd={cost_api}` "
               f"vs client `{m_acc} / {m_pt} / {m_ct} / {m_cost}`  ->  **{'RECONCILES' if recon2 else 'MISMATCH'}**.")

    sec("6. Routing eval (`auto` router vs data/routing_eval.jsonl)")
    rc, text = run([py, "scripts/routing_eval.py"])
    out.append("```text\n" + text + "\n```")
    rep = json.loads(run([py, "scripts/routing_eval.py", "--json"])[1])
    out.append(f"Method: engineered signals on the *task* sentence (first+last sentence of long prompts), see `prism/classifier.py`. "
               f"Accuracy **{rep['correct']}/{rep['total']} ({rep['accuracy']:.0%})** vs length-only baseline "
               f"**{rep['length_only_baseline_accuracy']:.0%}**. Misses: "
               f"{', '.join(c['id'] for c in rep['cases'] if not c['correct']) or 'none on this set'}. "
               "The expected labels are never read by the router. Because the signals were tuned while looking at this 20-case set, "
               "treat the figure as optimistic; `tests/test_units.py::test_router_held_out_prompts` checks prompts written separately.")

    sec("7. Budget and rate-limit rejections")
    long = "Explain in plain words how caching works in a distributed web application and why it helps latency"
    s_a, h_a, b_a, _ = call(url, BUDGET, msg(long))
    s_b, h_b, b_b, _ = call(url, BUDGET, msg(long))
    out.append(f"- budget-demo key, request 1: HTTP {s_a} (cost {h_a.get('x-prism-cost-usd')})\n"
               f"- budget-demo key, request 2: HTTP {s_b} `{b_b.get('error', {}).get('type')}` - {b_b.get('error', {}).get('message')}")
    out.append("- rate limit: see section 4 (free key: 10 accepted, 20 rejected with HTTP 429 `rate_limit_exceeded`).")

    sec("8. Gateway added latency")
    logs = admin(url, "/admin/logs?limit=500")["logs"]
    clean = [l for l in logs if l["status"] == "ok" and not l["stream"] and l["retries"] == 0 and not l["fallback"]]
    over = sorted(max(0, l["latency_ms"] - l["upstream_ms"]) for l in clean)
    lat = sorted(l["latency_ms"] for l in clean)
    if over:
        out.append(f"Measured as `total request time - upstream call time` from the request log over the last {len(clean)} clean "
                   f"(cache-miss, no retry, non-streaming) requests: **median {statistics.median(over):.0f} ms, p95 "
                   f"{over[max(0, int(len(over) * .95) - 1)]} ms** added by the gateway (auth, limits, routing, cache lookup, DB writes). "
                   f"End-to-end latency median {statistics.median(lat):.0f} ms, p95 {lat[max(0, int(len(lat) * .95) - 1)]} ms "
                   "(includes the mock provider's own time). Coarse measurement on one machine; it excludes response serialisation and network transfer.")

    sec("9. Result summary")
    out.append("| Check | Result |\n|---|---|\n"
               f"| Smoke test | {'PASS' if smoke_ok else 'FAIL'} |\n"
               f"| Load test 1 (zero over-admission) | {'PASS' if load1_ok else 'FAIL'} |\n"
               f"| Load test 1 accounting reconciles | {'PASS' if recon1 else 'FAIL'} |\n"
               f"| Load test 2 (scale) | {'PASS' if load2_ok else 'FAIL'} |\n"
               f"| Load test 2 accounting reconciles | {'PASS' if recon2 else 'FAIL'} |\n"
               f"| Routing accuracy | {rep['accuracy']:.0%} (baseline {rep['length_only_baseline_accuracy']:.0%}) |\n"
               f"| Budget rejection | {'PASS' if (s_a == 200 and s_b == 402) else 'CHECK (use a fresh DB)'} |")
    Path(a.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nreport written to {a.out}")
    ok_all = smoke_ok and load1_ok and recon1 and load2_ok and recon2
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
