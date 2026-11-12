"""
Quick vs Deep evaluation runner (SPEC F14). Every metric is computed by code, with the scorer ported
verbatim from v1 and frozen (evals/scoring.py, DECISIONS D8).

    python -m evals.run --target local                       # both modes, all questions, this laptop
    python -m evals.run --target api --url $URL              # the deployed stack (POST /ask + poll)
    python -m evals.run --target local --only c1 w1          # a subset
    python -m evals.run --target local --report-only         # re-render the report from saved results

Both modes answer each question back to back, so they see the same live data. Raw results are saved
after every answer (evals/results/v2-<target>.json, git-ignored) and a rerun skips what is already
done, so a run interrupted by a rate limit resumes.

The report (docs/eval/v2-results.md) has one section per target ("Local", "Deployed"); rendering one
leaves the other as it is. Each section compares v2 with v1's same-day baseline (D14:
docs/eval/v1-rerun-2026-11-05.json), overall and by question type, on the same frozen scorer.

Adapted from v1 evals/run.py (73b6fda): scoring moved to evals/scoring.py; targets; report sections.
"""

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import ROOT, env, setup_logging
from evals import scoring

QUESTIONS = ROOT / "evals" / "questions.json"
RESULTS_DIR = ROOT / "evals" / "results"
REPORT = ROOT / "docs" / "eval" / "v2-results.md"
V1_BASELINE = ROOT / "docs" / "eval" / "v1-rerun-2026-11-05.json"
V1_BASELINE_DATE = "2026-11-05"
SECTIONS = {"local": "Local", "api": "Deployed (full)"}
API_POLL_SECONDS = 2.0
API_TIMEOUT_SECONDS = 150
# Every key the frozen scorer reads from a result (tested by test_eval_run.py's contract test).
RESULT_KEYS = ("answer", "context", "used_tools", "companies", "mode", "error")
DEEP_TRACE_KEYS = ("rounds", "checker", "fallback")


def results_path(target: str) -> Path:
    return RESULTS_DIR / f"v2-{target}.json"


QUOTA_SIGNS = ("HTTP 429", "RESOURCE_EXHAUSTED")


def quota_hit(result: dict) -> bool:
    """True if Gemini's quota ran out during this answer (Deep falls back to Quick or loses a
    specialist instead of failing, so the answer exists but isn't a fair Deep answer)."""
    blob = json.dumps({k: result.get(k) for k in ("error", "notice", "trace")}, ensure_ascii=False, default=str)
    return any(sign in blob for sign in QUOTA_SIGNS)


def is_complete(result: dict) -> bool:
    """Saved and kept on a rerun: answered, and not cut short by the quota (those are retried)."""
    return bool(result.get("answer")) and not quota_hit(result)


def load_results(target: str) -> dict:
    path = results_path(target)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_results(target: str, results: dict) -> None:
    path = results_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")


# ─── running one answer ─────────────────────────────────────────────────────

def run_local(mode: str, question: str) -> dict:
    from core import agent, llm, log
    from core.agents.deep import answer_deep

    log.new_request_id()
    calls_before, started = llm.calls_made(), time.monotonic()
    try:
        result = answer_deep(question) if mode == "deep" else agent.answer(question)
    except Exception as e:  # recorded, never fatal for the run
        result = {"answer": None, "error": f"{type(e).__name__}: {e}"}
    result["_seconds"] = round(time.monotonic() - started, 2)
    result["_llm_calls"] = llm.calls_made() - calls_before
    result["_models"] = " → ".join([llm.GEMINI_MODEL] + llm.GEMINI_FALLBACK_MODELS)
    return result


def run_api(mode: str, question: str, url: str, access_code: str, session=None, sleep=time.sleep) -> dict:
    """POST /ask with include_context (D10), then poll GET /ask/{id} until done or error."""
    import requests

    http = session or requests.Session()
    headers = {"x-access-code": access_code, "content-type": "application/json"}
    started = time.monotonic()
    try:
        r = http.post(f"{url.rstrip('/')}/ask", json={"question": question, "mode": mode, "include_context": True},
                      headers=headers, timeout=30)
        r.raise_for_status()
        job = r.json()
        poll = job.get("poll") or f"/ask/{job['job_id']}"
        poll_url = poll if poll.startswith("http") else f"{url.rstrip('/')}{poll}"
        while True:
            if time.monotonic() - started > API_TIMEOUT_SECONDS:
                raise TimeoutError(f"no result after {API_TIMEOUT_SECONDS} s")
            sleep(API_POLL_SECONDS)
            p = http.get(poll_url, headers=headers, timeout=30)
            p.raise_for_status()
            body = p.json()
            if body.get("status") in ("done", "error"):
                break
        result = dict(body.get("result") or body)
        result.setdefault("answer", None)
        if body.get("status") == "error" and not result.get("error"):
            result["error"] = "job failed"
    except Exception as e:
        result = {"answer": None, "error": f"{type(e).__name__}: {e}"}
    result["_seconds"] = round(time.monotonic() - started, 2)  # client-side, includes polling
    timings = result.get("timings") or {}
    result["_llm_calls"] = timings.get("llm_calls", 1 if result.get("mode") == "quick" else 0)
    return result


# ─── report ─────────────────────────────────────────────────────────────────

def _rows(items: list[dict], results: dict, mode: str) -> list[dict]:
    return [scoring.score(i, mode, results[f"{i['id']}:{mode}"]) for i in items if f"{i['id']}:{mode}" in results]


def render_section(target: str, items: list[dict], results: dict, baseline: dict, meta: dict) -> str:
    v2 = {m: scoring.summarise(_rows(items, results, m)) for m in ("quick", "deep")}
    v1 = {m: scoring.summarise(_rows(items, baseline, m)) for m in ("quick", "deep")}
    out = [f"**{meta['note']}**", ""] if meta.get("note") else []
    out += [
        f"> Generated by `python -m evals.run --target {target}` on {meta['date']}. Answers dated {meta['answer_dates']}. "
        f"Model chain: `{meta['models']}`. Knowledge base: {meta['kb']}.",
        "",
        "| Metric | v1 Quick | **v2 Quick** | v1 Deep | **v2 Deep** |",
        "|---|---|---|---|---|",
    ]
    for key, label in [("answered", "Answered"), ("unmatched", "**Numbers not traceable to evidence** (lower is better)"),
                       ("answers_all_matched", "Answers where every number traces"), ("companies", "Right companies found"),
                       ("agents", "Right tools/agents used (routing)"), ("citations", "Lines with numbers that carry a citation"),
                       ("p50", "Latency p50"), ("p90", "Latency p90"), ("calls", "Gemini calls per question (mean)"),
                       ("follow_ups", "Follow-up rounds used"), ("flagged", "Numbers flagged ⚠️unverified"),
                       ("fallbacks", "Fell back to Quick")]:
        out.append(f"| {label} | {v1['quick'][key]} | **{v2['quick'][key]}** | {v1['deep'][key]} | **{v2['deep'][key]}** |")
    out += ["", f"v1 = same-config rerun on {V1_BASELINE_DATE} (`docs/eval/v1-baseline.md`, D14). Same frozen scorer.",
            "", "**By question type**", "",
            "| Type | Mode | Untraceable v1 → v2 | Routing v1 → v2 | Citations v1 → v2 | p50 v1 → v2 |",
            "|---|---|---|---|---|---|"]
    types: dict[str, list[dict]] = {}
    for i in items:
        types.setdefault(i["type"], []).append(i)
    for t, its in types.items():
        for mode in ("quick", "deep"):
            a = scoring.summarise(_rows(its, baseline, mode))
            b = scoring.summarise(_rows(its, results, mode))
            out.append(f"| {t} ({len(its)}) | {mode} | {a['unmatched']} → {b['unmatched']} | {a['agents']} → {b['agents']} "
                       f"| {a['citations']} → {b['citations']} | {a['p50']} → {b['p50']} |")
    out += ["", "**Per question (v2)**", "",
            "| # | Type | Question | Quick: untraceable/numbers, s | Deep: untraceable/numbers, s, agents |",
            "|---|---|---|---|---|"]
    for i in items:
        cells = []
        for mode in ("quick", "deep"):
            r = results.get(f"{i['id']}:{mode}")
            if not r:
                cells.append("—")
                continue
            s = scoring.score(i, mode, r)
            if not s["ok"]:
                cells.append(f"no answer ({(s['error'] or '')[:40]})")
                continue
            extra = ""
            if mode == "deep":
                extra = f", {'ok' if s['agents_ok'] else 'missed agent'}" + (" (fallback)" if s["fallback"] else "")
            examples = f" `{', '.join(s['unmatched_examples'])}`" if s["unmatched"] else ""
            cells.append(f"{s['unmatched']}/{s['numbers']}{examples}, {s['seconds']} s{extra}")
        out.append(f"| {i['id']} | {i['type']} | {i['q']} | {cells[0]} | {cells[1]} |")
    return "\n".join(out)


SCORER_QUIRKS = (
    "**Known quirks of the frozen scorer (D8).** They apply equally to v1 and v2, so the comparison stays fair, "
    "but they move the absolute numbers: (1) a Markdown heading that contains a digit (\"Q3 2026\") counts as a "
    "numeric line that needs a citation; (2) scale words are only parsed in lower case, so \"$4,832.5 Billion\" "
    "reads as 4,832.5 and a correct figure counts as not traceable (Quick c5's two on 2026-11-05). The live "
    "fact-checker normalises scale words before matching (review A, S5); the scorer does not.")


def write_report(target: str, section_md: str, path: Path = REPORT) -> None:
    """Replaces this target's section in the report, keeping the other one."""
    name = SECTIONS[target]
    begin, end = f"<!-- section:{target} -->", f"<!-- /section:{target} -->"
    block = f"{begin}\n## {name}\n\n{section_md}\n{end}"
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = ("# v2 evaluation: Quick vs Deep, local and deployed\n\n"
                "Scored by the frozen v1 scorer (`evals/scoring.py`, D8). Every metric is computed by code; no LLM judge. "
                "\"Not traceable\" counts numbers the fact-checker's code stage can't find in that answer's evidence "
                "(derived numbers such as growth rates count too, in both versions).\n\n"
                + SCORER_QUIRKS + "\n\n"
                "<!-- section:local -->\n## Local\n\n_Not run yet._\n<!-- /section:local -->\n\n"
                "<!-- section:api -->\n## Deployed (full)\n\n_Not run yet (Phase C)._\n<!-- /section:api -->\n")
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    text = pattern.sub(lambda _: block, text) if pattern.search(text) else text.rstrip() + "\n\n" + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _kb_description(target: str) -> str:
    if target == "api":
        return "the deployed S3 index"
    try:
        from core import config, vectors

        data = vectors.LocalIndex(config.INDEX_DIR).load()
        by_ticker: dict[str, int] = {}
        for m in data.meta:
            by_ticker[m.get("ticker")] = by_ticker.get(m.get("ticker"), 0) + 1
        return f"{len(data)} chunks ({', '.join(f'{t} {n}' for t, n in sorted(by_ticker.items()))}), latest filed 10-K/10-Q/8-K"
    except Exception as e:  # report still renders without an index
        return f"unavailable ({type(e).__name__})"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=("local", "api"), default="local")
    parser.add_argument("--url", default=env("DESK_API_URL"))
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--modes", nargs="*", default=["quick", "deep"])
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--fresh", action="store_true", help="ignore saved results and rerun everything")
    parser.add_argument("--force", action="store_true", help="rerun the selected items even if saved")
    parser.add_argument("--note", default=None, help="a bold line at the top of the section, e.g. 'provisional'")
    args = parser.parse_args(argv)
    setup_logging(env("LOG_LEVEL", "WARNING"))
    if args.target == "api" and not args.url:
        raise SystemExit("--target api needs --url (or DESK_API_URL)")

    all_items = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    items = [i for i in all_items if not args.only or i["id"] in args.only]
    results = {} if args.fresh else load_results(args.target)

    if not args.report_only:
        for item in items:
            for mode in args.modes:
                key = f"{item['id']}:{mode}"
                if key in results and is_complete(results[key]) and not args.force:
                    continue
                print(f"[{key}] {item['q']}", flush=True)
                if args.target == "api":
                    results[key] = run_api(mode, item["q"], args.url, env("APP_ACCESS_CODE"))
                else:
                    results[key] = run_local(mode, item["q"])
                results[key]["_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                save_results(args.target, results)
                r = results[key]
                print(f"   -> {r.get('mode', mode)}, {r['_seconds']} s, {r['_llm_calls']} calls"
                      f"{'' if r.get('answer') else ', NO ANSWER: ' + str(r.get('error'))[:80]}", flush=True)

    baseline = json.loads(V1_BASELINE.read_text(encoding="utf-8"))
    meta = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "answer_dates": ", ".join(sorted({r.get("_at", "")[:10] for r in results.values() if r.get("_at")})) or "n/a",
            "models": " | ".join(sorted({r.get("_models") for r in results.values() if r.get("_models")})) or "n/a",
            "kb": _kb_description(args.target), "note": args.note}
    write_report(args.target, render_section(args.target, all_items, results, baseline, meta))
    print(f"Report: {REPORT}")


if __name__ == "__main__":
    main()
