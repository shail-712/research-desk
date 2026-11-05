"""
The Fact-Checker agent: every number in the answer must trace to the evidence.

Stage 1 (code, no LLM): extract the answer's numbers ($, %, B/M/K, plain figures),
normalise them, and match each against the numbers in the evidence within its
display precision (so "$57.0B" matches "57,006 million", "-1.3%" matches "-1.26%").
Stage 2 (one LLM call, only if something failed stage 1): decide whether each
unmatched number is correctly derived from the evidence (e.g. a growth rate) or
unsupported. Unsupported numbers are flagged in the text, never silently kept.

Ported from v1 core/agents/checker.py (73b6fda); stage 1 moved to core/numbers.py.
"""

import json
import logging
import time

from google.genai import types

from core import llm

logger = logging.getLogger(__name__)

# Stage 1 (pure code) is core/numbers.py, shared with the frozen eval scorer (D8).
from core.numbers import UNVERIFIED_MARK, Number, Verdict, extract_numbers, verify_numbers  # noqa: E402,F401


CHECK_SCHEMA = types.Schema(type="OBJECT", properties={"verdicts": types.Schema(
    type="ARRAY", items=types.Schema(type="OBJECT", properties={
        "number": types.Schema(type="STRING"),
        "verdict": types.Schema(type="STRING", enum=["derived", "unsupported"]),
    }, required=["number", "verdict"]))}, required=["verdicts"])

CHECK_SYSTEM = """You are the Fact-Checker of an investment research desk. For each listed number
from a draft answer, decide using ONLY the evidence:
- "derived": correctly calculated from evidence values (a difference, ratio, growth rate,
  or a rounding/unit conversion of an evidence value);
- "unsupported": anything else.
Be strict."""


def check(answer: str, evidence_text: str, deadline: float, use_llm: bool = True) -> Verdict:
    supported, unmatched = verify_numbers(answer, evidence_text)
    verdict = Verdict(numbers_checked=len(supported) + len(unmatched), supported=len(supported), answer=answer)
    if not unmatched:
        return verdict

    flagged = unmatched
    remaining = deadline - time.monotonic()
    if use_llm and remaining > 6:
        lines = "\n".join(f"- {n.raw!r} in: …{answer[max(0, n.start - 80):n.end + 40]}…" for n in unmatched)
        prompt = f"NUMBERS TO CHECK:\n{lines}\n\nEVIDENCE:\n{evidence_text[:12000]}"
        config = types.GenerateContentConfig(system_instruction=CHECK_SYSTEM, temperature=0,
                                             max_output_tokens=600, response_mime_type="application/json",
                                             response_schema=CHECK_SCHEMA)
        try:
            verdict.llm_calls = 1
            data = json.loads(llm.call(prompt, config, budget_seconds=min(remaining, 30)).text)
            derived = {v["number"].strip() for v in data.get("verdicts", []) if v.get("verdict") == "derived"}
            flagged = [n for n in unmatched if n.raw.strip() not in derived]
            verdict.derived = [n.raw for n in unmatched if n.raw.strip() in derived]
        except (llm.LLMUnavailableError, ValueError, KeyError) as e:
            logger.warning("Fact-check LLM stage skipped: %s", e)

    verdict.unsupported = [n.raw for n in flagged]
    text = answer
    for n in sorted(flagged, key=lambda n: n.start, reverse=True):  # edit from the end so offsets hold
        text = text[:n.end] + UNVERIFIED_MARK + text[n.end:]
    verdict.answer = text
    return verdict
