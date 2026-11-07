"""Prompt-difficulty classifier for the `auto` alias (engineered signals, fully offline).

Why not length? The eval set has short-but-hard prompts (proofs) and long-but-trivial
ones (a roster plus 'who is on call Thursday?'). So we judge the TASK being asked, not
the amount of text around it:

  1. Split the prompt into sentences. For long prompts (pasted data/logs/emails) the
     task lives in the first sentence ('Rewrite the following...') or the last
     ('...What is the timestamp of the first ERROR line?'), so only those are scored.
  2. Score reasoning/expertise cues in the task text (+) and lookup/transform cues (-).
  3. score >= THRESHOLD -> complex (smart), otherwise simple (fast).

The answer key (expected_tier) is never used here; it is only used by the eval.
"""
import re

THRESHOLD = 2.0

# (regex, weight, label). Positive = needs deeper reasoning.
COMPLEX_CUES = [
    (r"\b(prove|disprove|proof|derive|show that|demonstrate that)\b", 3.0, "proof/derivation"),
    (r"\bshow your (reasoning|work)\b|\bstep[- ]by[- ]step\b|\bjustif(y|ication)\b", 2.0, "asks for reasoning"),
    (r"\bestimate how many\b|\bfermi\b", 2.0, "estimation"),
    (r"\bwhy (can|does|do|would|is|are|might)\b", 1.5, "causal 'why'"),
    (r"\b(trade-?offs?|pros and cons)\b", 2.0, "trade-off analysis"),
    (r"\b(compare|versus|vs\.?)\b.*\b(recommend|choose|pick|decide)\b|\brecommend one\b", 2.5, "compare+recommend"),
    (r"\bdesign (a|an|the)\b", 2.0, "design task"),
    (r"\b(architecture|schema|migration plan|rollback|roll back|zero-downtime)\b", 1.5, "systems design"),
    (r"\b(plan|strategy|roadmap)\b.*\b(stage|phase|each|including)\b", 1.0, "multi-stage planning"),
    (r"\b(algorithm|time complexity|better than o\(|o\(n|big-o)\b", 2.0, "algorithmic"),
    (r"\b(write|implement)\b.*\b(function|class|algorithm|program)\b", 1.5, "code writing"),
    (r"\b(consistency|quorum|partition|consensus|linearizab|replication|idempoten|p99|p95|tail latency|race condition|deadlock)\b",
     2.0, "deep systems concept"),
    (r"\b(most likely causes?|root cause|in order of likelihood|how (would|do) you confirm|debug|diagnos)", 2.0, "diagnosis"),
    (r"\b(give|list|name|provide)\b.{0,15}\b(two|three|four|five|\d+)\b.{0,20}\b(mechanisms?|causes?|reasons?|ways?|approaches)\b",
     1.5, "multiple explanations"),
    (r"\b(explain)\b.{0,60}\b(guarantee|how the|why|works)\b", 1.0, "explanation"),
    (r"\bconcrete (anomaly|example|scenario)\b|\bedge cases?\b|\bfailure modes?\b", 1.0, "depth markers"),
]

# Negative = lookup / transform / extraction a small model handles well.
SIMPLE_CUES = [
    (r"\bwhat is the (capital|population|currency)\b", -3.0, "fact lookup"),
    (r"\b(convert|translate)\b", -2.5, "conversion/translation"),
    (r"\bwhat does .{1,40} mean\b|\bdefine\b|\bdefinition of\b", -2.0, "definition"),
    (r"\b(extract|find|pull out)\b.{0,40}\b(email|date|name|number|address|timestamp|phone)\b", -2.5, "extraction"),
    (r"\b(rewrite|rephrase|paraphrase|proofread|fix the (typo|grammar))\b", -2.5, "rewrite"),
    (r"\bone-?line\b|\bin one sentence\b|\bcommit message\b", -2.0, "tiny output"),
    (r"^\s*is \d+ (divisible|even|odd|prime)\b|\bis \d+ (divisible|a multiple)\b", -3.0, "simple arithmetic"),
    (r"\b(who|when|where|which)\b.{0,40}\b(is|are|was|were|on call)\b.*\?", -1.5, "lookup question"),
    (r"\bwhat is the (timestamp|first|last|name|date|total)\b", -2.0, "lookup question"),
    (r"\b(summari[sz]e|tl;?dr)\b.{0,30}\b(in|into)\b.{0,10}\b(one|1|two|2)\b", -1.5, "short summary"),
]

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z'\"(])")


def _task_text(prompt):
    sentences = [s.strip() for s in _SENT_SPLIT.split(prompt.strip()) if s.strip()]
    words = len(prompt.split())
    if words <= 40 or len(sentences) < 2:
        return prompt, "whole prompt"
    # long prompt: the instruction is at the start or the end; the middle is pasted data
    return sentences[0] + " " + sentences[-1], "first+last sentence of long prompt"


def classify(prompt):
    """Return (tier, score, reason). tier is 'fast' or 'smart'."""
    text, scope = _task_text(prompt)
    low = text.lower()
    score, hits = 0.0, []
    for pattern, weight, label in COMPLEX_CUES + SIMPLE_CUES:
        if re.search(pattern, low):
            score += weight
            hits.append(f"{label}({weight:+g})")
    task_words = len(text.split())
    if task_words > 25:           # genuinely long *task description*, not pasted data
        score += 0.5
        hits.append("long task description(+0.5)")
    tier = "smart" if score >= THRESHOLD else "fast"
    reason = f"score={score:g} (threshold {THRESHOLD:g}) on {scope}; signals: " + (", ".join(hits) or "none")
    return tier, score, reason


def length_only_baseline(prompt, words=30):
    """The strawman the router must beat: long prompt => smart."""
    return "smart" if len(prompt.split()) > words else "fast"
