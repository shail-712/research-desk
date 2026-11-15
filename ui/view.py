"""
Display helpers for the Streamlit app (ui/app.py): plain functions over the API's JSON, no Streamlit
calls, so they are unit-tested directly (tests/unit/test_ui.py).

They read only fields the API already returns: a job's `notice`, `sources[].kind`, `trace` and
`timings`; a feed event's `kind`, `title`, `dedupe_key` and `created_at`.
"""

import re
from datetime import datetime

# The retriever's coverage sentence (core/retriever.not_covered_message). Deep mode may join it to
# other notices ("Partial answer: ...", "Deep mode wasn't available ..."), so it is cut out here.
_NOT_COVERED = re.compile(r"SEC filings for .+? aren't covered: the filings index has .+? only\.")

TYPICAL_SECONDS = {"quick": 10.0, "deep": 25.0}   # for the progress bar only (an estimate)
SOURCE_GROUPS = [("market", "Market data"), ("news", "News"), ("document", "SEC filings")]
TOOL_NAMES = {"market": "Live quote", "news": "News", "rag": "SEC filings", "llm": "Gemini"}
MODE_NAMES = {"quick": "Quick", "deep": "Deep", "quick-fallback": "Quick (Deep fell back)"}


def md(text: str | None) -> str:
    """Streamlit reads $...$ as maths; money amounts must stay text."""
    return (text or "").replace("$", r"\$")


def split_notice(notice: str | None) -> tuple[str | None, str | None]:
    """(the "not covered" sentence, any other notice text), each None when absent."""
    if not notice:
        return None, None
    m = _NOT_COVERED.search(notice)
    if not m:
        return None, notice.strip() or None
    rest = (notice[:m.start()] + notice[m.end():]).strip()
    return m.group(0), re.sub(r"\s{2,}", " ", rest) or None


def is_replay(event: dict) -> bool:
    """A demo replay alert (jobs/scan.py): `replay:` dedupe key, "(replay)" title."""
    return (str(event.get("dedupe_key") or "").startswith("replay:")
            or str(event.get("title") or "").rstrip().endswith("(replay)"))


def is_down(event: dict) -> bool:
    """Same rule as core/telegram.py: price_below, or a price_move whose title has a minus."""
    kind = event.get("kind")
    return kind == "price_below" or (kind == "price_move" and " -" in (event.get("title") or ""))


def fmt_time(iso: str | None) -> str:
    """'2026-11-13T18:00:21' -> '13 Nov 2026 18:00 UTC' (the API stores UTC)."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return iso[:16].replace("T", " ")


def progress_fraction(waited: float, mode: str) -> float:
    """How full the progress bar is: an estimate from the mode's typical time, never 100% until done."""
    return max(0.0, min(0.95, waited / TYPICAL_SECONDS.get(mode, 25.0)))


def answer_meta(r: dict) -> str:
    timings = r.get("timings") or {}
    calls = timings.get("llm_calls") or (1 if r.get("mode") == "quick" else None)
    parts = [MODE_NAMES.get(r.get("mode"), r.get("mode") or "?"), f"as of {fmt_time(r.get('as_of'))}"]
    if timings.get("total") is not None:
        parts.append(f"{timings['total']} s")
    if calls:
        parts.append(f"{calls} Gemini call{'s' if calls != 1 else ''}")
    return " · ".join(parts)


def group_sources(sources: list[dict]) -> list[tuple[str, list[dict]]]:
    """[(heading, sources)] in a fixed order (market, news, filings, other); empty groups dropped."""
    known = {k for k, _ in SOURCE_GROUPS}
    groups = [(title, [s for s in sources if s.get("kind") == kind]) for kind, title in SOURCE_GROUPS]
    groups.append(("Other", [s for s in sources if s.get("kind") not in known]))
    return [(title, items) for title, items in groups if items]


def tool_timings(r: dict) -> list[dict]:
    """Quick mode's per-tool seconds as table rows, e.g. {"Step": "Live quote (NVDA)", "Seconds": 1.2}."""
    rows = []
    for key, seconds in (r.get("timings") or {}).items():
        if key in ("total", "llm_calls") or not isinstance(seconds, (int, float)):
            continue
        tool, _, ticker = key.partition(":")
        name = TOOL_NAMES.get(tool, tool)
        rows.append({"Step": f"{name} ({ticker})" if ticker else name, "Seconds": seconds})
    return rows


def trace_lines(r: dict) -> list[str]:
    """A short, readable account of how the answer was made (no raw JSON)."""
    trace = r.get("trace")
    if not trace:   # Quick mode
        tools = [TOOL_NAMES.get(t, t) for t in r.get("used_tools") or []]
        return ["One Gemini call over the data it fetched.", f"Tools used: {', '.join(tools) or 'none'}."]
    lines = []
    if trace.get("fallback"):
        lines.append(f"Deep mode wasn't available ({trace['fallback']}), so this is a Quick answer.")
    if trace.get("fast_path"):
        lines.append("Route: fast path (one specialist, no planner).")
    elif trace.get("plan"):
        agents = [t.get("agent") for t in trace["plan"].get("tasks", [])]
        lines.append(f"Route: the planner chose {', '.join(a for a in agents if a) or 'no agents'}.")
    for i, round_ in enumerate(trace.get("rounds") or [], start=1):
        for rep in round_:
            tools = [s.get("tool") for s in rep.get("steps", []) if s.get("action") == "tool"]
            n_f, n_g = len(rep.get("findings", [])), len(rep.get("gaps", []))
            lines.append(f"Round {i}: **{rep.get('agent')}** agent, {rep.get('seconds')} s, "
                         f"{n_f} finding{'s' if n_f != 1 else ''}, {n_g} gap{'s' if n_g != 1 else ''}, "
                         f"tools: {', '.join(t for t in tools if t) or 'none'}.")
    checker = trace.get("checker") or {}
    if checker:
        lines.append(f"Fact-checker: {checker.get('supported')}/{checker.get('numbers_checked')} numbers matched "
                     f"the evidence ({len(checker.get('derived') or [])} derived, "
                     f"{len(checker.get('unsupported') or [])} unsupported).")
    return lines
