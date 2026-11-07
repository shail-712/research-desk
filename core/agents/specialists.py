"""
The three specialist agents. Each has its own instructions and ONLY its own tools,
runs a function-calling loop, and returns a Report:

    {agent, findings: [{claim, evidence: [E#...]}], gaps: [...], steps, llm_calls, seconds, error}
"""

import re
import time
from dataclasses import asdict, dataclass, field

from core.agents.evidence import EvidenceBoard
from core.agents.loop import run_tool_loop

OUTPUT_FORMAT = """
Finish with exactly this format and nothing else:
FINDINGS:
- <one factual claim with its numbers> [E#]
- ...
GAPS:
- <something you were asked for but could not find>   (or "- none")

Rules: every finding cites the evidence IDs it comes from, e.g. [E2] or [E2, E5].
Never state a number that is not in the evidence. No investment advice."""

SPECIALISTS = {
    "market": {
        "title": "Market Analyst",
        "tools": ["get_quote", "get_quotes"],
        "system": ("You are the Market Analyst on an investment research desk. You report live "
                   "prices, day moves, valuation (market cap, P/E) and side-by-side numbers for US "
                   "stocks. Use get_quotes for several tickers, get_quote for one ticker in detail."
                   + OUTPUT_FORMAT),
    },
    "news": {
        "title": "News Analyst",
        "tools": ["get_news", "recent_alerts"],
        "system": ("You are the News Analyst on an investment research desk. You find what is new "
                   "about a company and what may be driving its stock, from recent headlines and "
                   "the desk's own alerts. Report what the headlines say, with their times; do not "
                   "invent causes the headlines don't state." + OUTPUT_FORMAT),
    },
    "filings": {
        "title": "Filings Analyst",
        "tools": ["search_filings", "latest_filing"],
        "system": ("You are the Filings Analyst on an investment research desk. You answer from "
                   "what companies reported to the SEC: revenue, segments, margins, guidance, "
                   "risks. Use search_filings with focused queries (one company per query when "
                   "comparing). Quote figures exactly as the passages state them, with their "
                   "period (quarter / fiscal year)." + OUTPUT_FORMAT),
    },
}

_EVIDENCE_REF = re.compile(r"\[(E\d+(?:\s*,\s*E\d+)*)\]")


@dataclass
class Report:
    agent: str
    findings: list[dict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    seconds: float = 0.0
    error: str | None = None
    raw: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_report(agent: str, text: str | None) -> tuple[list[dict], list[str]]:
    """'FINDINGS: - claim [E1]' / 'GAPS: - ...' -> (findings, gaps). Tolerant of format drift."""
    findings, gaps, section = [], [], "findings"
    for raw in (text or "").splitlines():
        line = raw.strip()
        heading = line.strip("*#: ").upper()  # "**FINDINGS:**", "## Gaps", "GAPS:"
        if heading == "FINDINGS":
            section = "findings"
            continue
        if heading == "GAPS":
            section = "gaps"
            continue
        if not line.startswith(("-", "*", "•")):
            continue
        item = line.lstrip("-*• ").strip()
        if not item:
            continue
        if section == "gaps":
            if item.lower().rstrip(".") not in ("none", "n/a", "no gaps"):
                gaps.append(item)
            continue
        ids = [i.strip() for m in _EVIDENCE_REF.findall(item) for i in m.split(",")]
        findings.append({"claim": _EVIDENCE_REF.sub("", item).strip(), "evidence": ids})
    return findings, gaps


def run_specialist(agent: str, instruction: str, board: EvidenceBoard, deadline: float,
                   companies: list[str] | None = None) -> Report:
    spec = SPECIALISTS[agent]
    started = time.monotonic()
    prompt = instruction
    if companies:
        prompt += f"\n\nCompanies (tickers): {', '.join(companies)}"
        if agent == "filings" and len(companies) > 1:
            prompt += each_company_instruction(companies)
    loop = run_tool_loop(agent, spec["system"], prompt, spec["tools"], board, deadline)
    findings, gaps = parse_report(agent, loop.text)
    report = Report(agent=agent, findings=findings, gaps=gaps, steps=loop.steps, llm_calls=loop.llm_calls,
                    seconds=round(time.monotonic() - started, 2), error=loop.error, raw=loop.text)
    if not findings:
        # Out of time or unparseable: fall back to the raw evidence this agent gathered.
        mine = [e for e in board.items() if e.agent == agent]
        report.findings = [{"claim": e.summary[:300], "evidence": [e.id]} for e in mine[:6]]
        if loop.error:
            # The detail (model name, HTTP status) stays in report.error and the trace; the writer
            # only sees a neutral gap, so it can't leak into the answer (review A, S1).
            report.gaps.append(incomplete_gap(agent))
    return report


def incomplete_gap(agent: str) -> str:
    return f"{SPECIALISTS[agent]['title']} could not complete its search"


def each_company_instruction(companies: list[str]) -> str:
    """Review A, S2: with several companies, one search per company, so no company's figures are
    left to the writer's memory. latest_filing has dates only."""
    return (f"\nSearch EACH company's filings separately: call search_filings once per ticker "
            f"({', '.join(companies)}) with that ticker set, before writing. latest_filing gives filing "
            "dates only, never financial figures; if a company's figures are not in the passages, list it "
            "under GAPS.")
