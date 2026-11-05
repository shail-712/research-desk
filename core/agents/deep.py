"""
Deep mode: the multi-agent answer path.

    fast path?  -> one specialist                       (1 intent, at most 1 company)
    otherwise   -> Supervisor plan -> specialists in parallel
                -> at most one follow-up round on gaps
    then        -> Supervisor writes, citing [E#] -> Fact-Checker

Returns the same shape as core.agent.answer() plus "mode" and "trace", so the UI and
CLI just switch functions. Any model outage, the time budget or the daily cap fall
back to the Quick path, so the user always gets an answer.

Ported from v1 core/agents/deep.py (73b6fda); the request ID follows the specialist threads.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core import agent as quick_agent
from core import llm, tickers
from core.agents import checker, supervisor
from core.agents.evidence import EvidenceBoard
from core.agents.specialists import SPECIALISTS, run_specialist
from core.config import env_float, env_int
from core.log import run_in_context
from core.store import StoreError, get_store

logger = logging.getLogger(__name__)

DEEP_BUDGET_SECONDS = env_float("DEEP_BUDGET_SECONDS", 75)
DEEP_MODE_DAILY_CAP = env_int("DEEP_MODE_DAILY_CAP", 50)
FOLLOW_UP_MIN_SECONDS = 30   # only start a second round with this much budget left
WRITE_RESERVE_SECONDS = 20   # specialists must leave this much time for writing + checking
TOOL_TO_AGENT = {"market": "market", "news": "news", "rag": "filings"}
TIME_BUDGET_ERROR = "time budget used up"  # core.agents.loop's error when the deadline passes
# Questions asking for causes or links need several angles, so never take the fast path.
NEEDS_PLANNING = re.compile(r"\b(?:why|driv\w*|reasons?|caus\w*|explain\w*|behind|relat\w*|impact\w*|affect\w*)\b", re.I)


def _cap_key() -> str:
    return "deep:count:" + datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _take_daily_slot() -> bool:
    """Counts Deep questions per Pacific day (Gemini's quota day). Fails open if the store is down."""
    try:
        store = get_store()
        used = int(store.get_state(_cap_key(), 0))
        if used >= DEEP_MODE_DAILY_CAP:
            return False
        store.set_state(_cap_key(), used + 1)
    except StoreError:
        pass
    return True


def _quick(question: str, reason: str, trace: dict) -> dict:
    result = quick_agent.answer(question)
    trace["fallback"] = reason
    result.update(mode="quick-fallback", trace=trace,
                  notice=f"Deep mode wasn't available ({reason}), so this is a Quick answer.")
    return result


def _dispatch(tasks: list[dict], board: EvidenceBoard, deadline: float, companies: list[str]) -> list[dict]:
    """Runs the round's specialists in parallel."""
    agent_deadline = deadline - WRITE_RESERVE_SECONDS
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = [pool.submit(run_in_context(run_specialist), t["agent"], t["instruction"], board, agent_deadline, companies)
                   for t in tasks]
        return [f.result().to_dict() for f in futures]


def answer_deep(question: str) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("Missing question")
    started = time.monotonic()
    deadline = started + DEEP_BUDGET_SECONDS
    calls_before = llm.calls_made()
    trace: dict = {"fast_path": False, "plan": None, "rounds": [], "checker": None, "fallback": None}

    if not _take_daily_slot():
        return _quick(question, f"daily limit of {DEEP_MODE_DAILY_CAP} Deep questions reached", trace)

    hints = [c.ticker for c in tickers.find_companies(question)]
    tools = quick_agent.classify_query(question)
    board = EvidenceBoard()

    # ── plan ──
    if len(tools) == 1 and len(hints) <= 1 and not NEEDS_PLANNING.search(question):
        trace["fast_path"] = True
        plan = {"companies": hints, "tasks": [{"agent": TOOL_TO_AGENT[tools[0]], "instruction": question}]}
    else:
        try:
            plan = supervisor.plan(question, hints, deadline)
        except (llm.LLMUnavailableError, ValueError) as e:
            return _quick(question, f"planner unavailable: {e}", trace)
    trace["plan"] = plan

    # ── round 1, then at most one follow-up round ──
    reports = _dispatch(plan["tasks"], board, deadline, plan["companies"])
    trace["rounds"].append(reports)
    if (not trace["fast_path"] and any(r["gaps"] for r in reports)
            and deadline - time.monotonic() > FOLLOW_UP_MIN_SECONDS):
        try:
            extra = supervisor.follow_up(question, reports, deadline)
        except (llm.LLMUnavailableError, ValueError):
            extra = []
        if extra:
            more = _dispatch(extra, board, deadline, plan["companies"])
            trace["rounds"].append(more)
            reports = reports + more  # new list: round 1 in the trace must stay as it was

    # ── write + fact-check ──
    if not len(board):
        return _quick(question, "the specialists found no evidence", trace)
    try:
        draft = supervisor.write(question, reports, board, deadline)
    except llm.LLMUnavailableError as e:
        return _quick(question, f"writer unavailable: {e}", trace)
    verdict = checker.check(draft, board.text(), deadline)
    trace["checker"] = {k: v for k, v in verdict.to_dict().items() if k != "answer"}

    cited = set(re.findall(r"\bE\d+\b", verdict.answer))
    companies = [(tickers.lookup_ticker(t) or tickers.Company(t, t)).to_dict() for t in plan["companies"]]
    total = round(time.monotonic() - started, 2)
    # SPEC §6: when the budget cut specialists short, the answer is built from what's on the board
    # and says so.
    cut_short = sorted({r["agent"] for r in reports if r.get("error") == TIME_BUDGET_ERROR})
    trace["partial"] = cut_short or None
    logger.info("Deep answer: %.1fs, %d LLM calls, %d evidence, checker %s/%s supported",
                total, llm.calls_made() - calls_before, len(board), verdict.supported, verdict.numbers_checked,
                extra={"mode": "deep", "seconds": total, "partial": bool(cut_short)})
    notice = (f"Partial answer: the {DEEP_BUDGET_SECONDS:.0f} s time budget ran out before the "
              f"{', '.join(cut_short)} specialist(s) finished, so this uses the evidence gathered so far."
              if cut_short else None)
    return {
        "notice": notice,
        "answer": verdict.answer,
        "error": None,
        "used_tools": [t["agent"] for t in plan["tasks"]],
        "companies": companies,
        "sources": board.sources(cited),
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timings": {"total": total, "llm_calls": llm.calls_made() - calls_before,
                    **{f"agent:{r['agent']}": r["seconds"] for r in reports}},
        "mode": "deep",
        "trace": trace,
        "context": board.text(),
    }
