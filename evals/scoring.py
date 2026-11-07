"""
Eval scoring: every metric is computed by code (no LLM judge).

FROZEN — DECISIONS D8. Ported verbatim from v1 (investment-research-assistant @ 73b6fda),
evals/run.py: QUICK_TOOL_TO_AGENT, CITATION, score, pct, quantile, summarise. Function
bodies are byte-identical; the only change is the import of the stage-1 checker
(`from core import numbers as checker`). tests/unit/test_eval_scoring.py re-scores v1's
saved 2026-11-03 results and must reproduce v1's summary exactly. Do not edit without a
new DECISIONS entry and a re-score of both v1 and v2 results.

Metrics
- unmatched-number rate: numbers in the answer that the fact-checker's code stage
  can't trace to the evidence that mode collected (lower is better). Derived
  numbers (e.g. a growth rate the model computed) also count as unmatched here,
  equally for both modes.
- routing: expected companies and agents found (Quick tools map market->market,
  news->news, rag->filings).
- citation coverage: share of answer lines with a number that carry a citation
  ([E#] in Deep, [Doc N] in Quick).
- latency p50/p90, Gemini calls per question, follow-up rounds, checker flags.
"""

import re
import statistics

from core import numbers as checker

QUICK_TOOL_TO_AGENT = {"market": "market", "news": "news", "rag": "filings"}
CITATION = re.compile(r"\[(?:E\d+|Doc \d+)")


def score(item: dict, mode: str, r: dict) -> dict:
    answer = (r.get("answer") or "").replace(checker.UNVERIFIED_MARK, "")
    ok = bool(r.get("answer"))
    supported, unmatched = checker.verify_numbers(answer, r.get("context") or "") if ok else ([], [])
    numbers = len(supported) + len(unmatched)

    if mode == "deep" and r.get("mode") == "deep":
        agents_used = set(r.get("used_tools", []))
    else:
        agents_used = {QUICK_TOOL_TO_AGENT.get(t, t) for t in r.get("used_tools", [])}
    companies = {c["ticker"] for c in r.get("companies", [])}
    lines = [l for l in re.split(r"\n+|(?<=[.!?])\s+", answer) if re.search(r"\d", l) and len(l) > 20]
    cited = [l for l in lines if CITATION.search(l)]
    trace = r.get("trace") or {}
    return {
        "ok": ok,
        "mode_used": r.get("mode", mode),
        "numbers": numbers,
        "unmatched": len(unmatched),
        "unmatched_examples": [n.raw for n in unmatched][:5],
        "companies_ok": set(item["companies"]) <= companies,
        "agents_ok": set(item["agents"]) <= agents_used,
        "extra_agents": sorted(agents_used - set(item["agents"])),
        "cited_lines": len(cited),
        "numeric_lines": len(lines),
        "seconds": r.get("_seconds"),
        "llm_calls": r.get("_llm_calls"),
        "rounds": len(trace.get("rounds", [])),
        "flagged": len((trace.get("checker") or {}).get("unsupported", [])),
        "fallback": trace.get("fallback"),
        "error": r.get("error"),
    }


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def quantile(values: list[float], q: float) -> float:
    values = sorted(values)
    if not values:
        return float("nan")
    idx = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return values[idx]


def summarise(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["ok"]]
    nums = sum(r["numbers"] for r in ok)
    secs = [r["seconds"] for r in ok]
    return {
        "answered": f"{len(ok)}/{len(rows)}",
        "unmatched": f"{sum(r['unmatched'] for r in ok)}/{nums} ({pct(sum(r['unmatched'] for r in ok), nums)})",
        "answers_all_matched": pct(sum(1 for r in ok if r["unmatched"] == 0), len(ok)),
        "companies": pct(sum(r["companies_ok"] for r in ok), len(ok)),
        "agents": pct(sum(r["agents_ok"] for r in ok), len(ok)),
        "citations": pct(sum(r["cited_lines"] for r in ok), sum(r["numeric_lines"] for r in ok)),
        "p50": f"{statistics.median(secs):.1f} s" if secs else "n/a",
        "p90": f"{quantile(secs, 0.9):.1f} s" if secs else "n/a",
        "calls": f"{statistics.mean(r['llm_calls'] for r in ok):.1f}" if ok else "n/a",
        "follow_ups": sum(1 for r in ok if r["rounds"] > 1),
        "flagged": sum(r["flagged"] for r in ok),
        "fallbacks": sum(1 for r in ok if r["fallback"]),
    }
