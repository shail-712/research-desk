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
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core import agent as quick_agent
from core import llm, tickers
from core.agents import checker, supervisor
from core.agents.evidence import EvidenceBoard
from core.agents.specialists import Report, incomplete_gap, run_specialist
from core.config import env_float, env_int
from core.log import run_in_context
from core.store import StoreError, get_store

logger = logging.getLogger(__name__)

DEEP_BUDGET_SECONDS = env_float("DEEP_BUDGET_SECONDS", 75)
DEEP_MODE_DAILY_CAP = env_int("DEEP_MODE_DAILY_CAP", 50)
FOLLOW_UP_MIN_SECONDS = 30   # only start a second round with this much budget left
WRITE_RESERVE_SECONDS = 20   # specialists must leave this much time for writing + checking
SPECIALIST_GRACE_SECONDS = 3  # wait this long past a specialist's deadline before cutting it off
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


def _overrun_report(agent: str, board: EvidenceBoard) -> Report:
    """A specialist still running at its deadline: its evidence so far, marked as cut short."""
    mine = [e for e in board.items() if e.agent == agent]
    return Report(agent=agent, findings=[{"claim": e.summary[:300], "evidence": [e.id]} for e in mine[:6]],
                  gaps=[incomplete_gap(agent)], error=TIME_BUDGET_ERROR)


def _dispatch(tasks: list[dict], board: EvidenceBoard, deadline: float, companies: list[str]) -> list[dict]:
    """Runs the round's specialists in parallel. Each is awaited until its deadline (+ a small
    grace for its last call to return), never joined after that (review A, S3)."""
    agent_deadline = deadline - WRITE_RESERVE_SECONDS
    pool = ThreadPoolExecutor(max_workers=len(tasks))
    try:
        futures = [pool.submit(run_in_context(run_specialist), t["agent"], t["instruction"], board, agent_deadline, companies)
                   for t in tasks]
        reports = []
        for t, f in zip(tasks, futures):
            try:
                reports.append(f.result(timeout=max(0.0, agent_deadline + SPECIALIST_GRACE_SECONDS - time.monotonic())))
            except FuturesTimeout:
                logger.warning("Specialist %s overran its deadline", t["agent"])
                reports.append(_overrun_report(t["agent"], board))
        return [r.to_dict() for r in reports]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def answer_deep(question: str) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValueError("Missing question")
    started = time.monotonic()
    deadline = started + DEEP_BUDGET_SECONDS
    calls_before = llm.calls_made()
    # `used_tools` (scored by the frozen eval, D8) lists round-1 agents only; agents added by the
    # follow-up round appear in trace["rounds"][1] and trace["follow_up_agents"]. The UI trace
    # should show both (review A NIT).
    trace: dict = {"fast_path": False, "plan": None, "rounds": [], "follow_up_agents": [], "checker": None,
                   "fallback": None, "note": "used_tools = round-1 agents; follow-up agents are in rounds[1]"}

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
            trace["follow_up_agents"] = [r["agent"] for r in more]
            reports = reports + more  # new list: round 1 in the trace must stay as it was

    # ── write + fact-check ──
    if not len(board):
        return _quick(question, "the specialists found no evidence", trace)
    try:
        draft = supervisor.write(question, reports, board, deadline)
    except llm.LLMUnavailableError as e:
        return _quick(question, f"writer unavailable: {e}", trace)
    metadata = {e.id: e.summary for e in board.items() if e.tool in checker.METADATA_TOOLS}
    verdict = checker.check(draft, board.text(), deadline, metadata=metadata)
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
