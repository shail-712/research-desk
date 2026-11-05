"""
The Supervisor agent: plans the question, decides on one follow-up round, and
writes the final answer from the specialists' findings, citing evidence IDs.
Planning and follow-up use Gemini JSON-schema output, so parsing is reliable.
"""

import json
import logging
import time
from datetime import datetime, timezone

from google.genai import types

from core import llm
from core.agents.evidence import EvidenceBoard
from core.agents.specialists import SPECIALISTS

logger = logging.getLogger(__name__)

AGENTS = list(SPECIALISTS)
MAX_TASKS = 3

S = types.Schema
_TASKS = S(type="ARRAY", items=S(type="OBJECT", properties={
    "agent": S(type="STRING", enum=AGENTS),
    "instruction": S(type="STRING"),
}, required=["agent", "instruction"]))
PLAN_SCHEMA = S(type="OBJECT", properties={
    "companies": S(type="ARRAY", items=S(type="STRING"), description="US tickers"),
    "tasks": _TASKS,
}, required=["companies", "tasks"])
FOLLOW_UP_SCHEMA = S(type="OBJECT", properties={"tasks": _TASKS}, required=["tasks"])

PLAN_SYSTEM = f"""You are the Supervisor of an investment research desk. Plan how to answer the
user's question using these specialist agents (each runs its own tools):
- market: live prices, day moves, market cap, P/E, side-by-side price comparisons
- news: latest headlines and the desk's recent alerts; what may be driving a stock
- filings: SEC filings (10-K/10-Q/8-K): revenue, segments, margins, guidance, risks

Return the companies (US tickers) and 1-{MAX_TASKS} tasks. Give each chosen specialist ONE
precise instruction naming the companies and exactly what to find. Only pick
specialists the question needs."""

FOLLOW_UP_SYSTEM = """You are the Supervisor of an investment research desk. The specialists reported
back. Decide whether ONE more round would materially improve the answer: only for
gaps that a DIFFERENT approach could fill (another specialist, or a clearly different
search than the ones already tried). Never repeat a search that already came back
without the answer: if the data simply isn't available, return an empty list.
Return at most 2 tasks, or an empty list if the answer can be written now."""

WRITE_SYSTEM = """You are the Supervisor of an investment research desk, writing the final answer.
Rules:
- Use ONLY the findings and evidence given. Every sentence with a fact ends with its
  evidence IDs, e.g. "... $57.0B in Q3 FY26 [E4]."
- Copy numbers exactly as they appear in the evidence; do not compute new ones unless
  you show the inputs (e.g. "up from $35.1B [E5] to $57.0B [E4]").
- Say how current the data is (quote time, session date, filing period).
- If something was asked but not found, say so briefly.
- Concise markdown: a one-line direct answer, then short sections or bullets.
- No investment advice."""


def _json_call(system: str, prompt: str, schema, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    config = types.GenerateContentConfig(system_instruction=system, temperature=0.1, max_output_tokens=800,
                                         response_mime_type="application/json", response_schema=schema)
    response = llm.call(prompt, config, budget_seconds=max(5.0, min(remaining, 40)))
    return json.loads(response.text)


def _clean_tasks(tasks: list[dict]) -> list[dict]:
    """One task per specialist (instructions merged), known agents only, at most MAX_TASKS."""
    merged: dict[str, str] = {}
    for t in tasks or []:
        agent, instruction = t.get("agent"), (t.get("instruction") or "").strip()
        if agent in SPECIALISTS and instruction:
            merged[agent] = f"{merged[agent]} Also: {instruction}" if agent in merged else instruction
    return [{"agent": a, "instruction": i} for a, i in merged.items()][:MAX_TASKS]


def plan(question: str, hints: list[str], deadline: float) -> dict:
    prompt = f"Question: {question}"
    if hints:
        prompt += f"\nCompanies recognised in the question: {', '.join(hints)}"
    data = _json_call(PLAN_SYSTEM, prompt, PLAN_SCHEMA, deadline)
    companies = [str(c).upper().strip() for c in data.get("companies", []) if str(c).strip()][:3]
    tasks = _clean_tasks(data.get("tasks", []))
    if not tasks:  # a plan with no work is no plan: ask the filings agent by default
        tasks = [{"agent": "filings", "instruction": question}]
    return {"companies": companies, "tasks": tasks}


def follow_up(question: str, reports: list[dict], deadline: float) -> list[dict]:
    lines = [f"Question: {question}", "", "Reports:"]
    for r in reports:
        tried = [f"{s['tool']}({json.dumps(s.get('args', {}))})" for s in r["steps"] if s.get("action") == "tool"]
        lines.append(f"- {r['agent']}: {len(r['findings'])} findings; gaps: {r['gaps'] or 'none'}; "
                     f"already tried: {', '.join(tried) or 'nothing'}")
    data = _json_call(FOLLOW_UP_SYSTEM, "\n".join(lines), FOLLOW_UP_SCHEMA, deadline)
    return _clean_tasks(data.get("tasks", []))[:2]


def write(question: str, reports: list[dict], board: EvidenceBoard, deadline: float) -> str:
    findings = []
    for r in reports:
        title = SPECIALISTS[r["agent"]]["title"]
        for f in r["findings"]:
            findings.append(f"- ({title}) {f['claim']} [{', '.join(f['evidence'])}]")
        for g in r["gaps"]:
            findings.append(f"- ({title}) NOT FOUND: {g}")
    cited = {i for r in reports for f in r["findings"] for i in f["evidence"]}
    prompt = (f"Today is {datetime.now(timezone.utc):%Y-%m-%d} (UTC).\n\nQUESTION:\n{question}\n\n"
              f"FINDINGS FROM SPECIALISTS:\n" + "\n".join(findings or ["- (none)"]) +
              f"\n\nEVIDENCE:\n{board.for_prompt(sorted(cited, key=lambda x: int(x[1:])) or None, 700)}")
    remaining = deadline - time.monotonic()
    config = types.GenerateContentConfig(system_instruction=WRITE_SYSTEM, temperature=0.2, max_output_tokens=1400)
    return llm.call(prompt, config, budget_seconds=max(5.0, min(remaining, 45))).text.strip()
