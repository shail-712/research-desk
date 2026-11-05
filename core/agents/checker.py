"""
The Fact-Checker agent: every number in the answer must trace to the evidence.

Stage 1 (code, no LLM): extract the answer's numbers ($, %, B/M/K, plain figures),
normalise them, and match each against the numbers in the evidence within its
display precision (so "$57.0B" matches "57,006 million", "-1.3%" matches "-1.26%").
Stage 2 (one LLM call, only if something failed stage 1): decide whether each
unmatched number is correctly derived from the evidence (e.g. a growth rate) or
unsupported. Unsupported numbers are flagged in the text, never silently kept.

v2 additions around the frozen stage 1 (review A): capitalised scale words ("Billion") are
normalised before matching (S5), and a number whose citation names only metadata evidence
(latest_filing: dates, no figures) that doesn't contain it is flagged even if the figure appears
elsewhere on the board or stage 2 calls it derived (S2).

Ported from v1 core/agents/checker.py (73b6fda); stage 1 moved to core/numbers.py.
"""

import json
import logging
import re
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


# Evidence that carries filing dates, never figures (review A, S2): a number cited only to these
# items did not come from them.
METADATA_TOOLS = ("latest_filing",)
_CITATION = re.compile(r"\[(E\d+(?:\s*,\s*E\d+)*)\]")
_SCALE_WORDS = re.compile(r"\b(?:trillion|billion|million|thousand|percent|bn|mn)\b", re.I)


def normalise_scale_words(answer: str) -> str:
    """'$4,832.5 Billion' -> '$4,832.5 billion' (review A, S5). Only scale words are lower-cased,
    and they keep their length, so offsets still point into the original answer. The frozen stage 1
    (D8) knows lower-case words and upper-case B/M/K/T, so a plain answer.lower() would turn the
    common '$57.0B' into an unparsed '$57.0b'."""
    return _SCALE_WORDS.sub(lambda m: m.group(0).lower(), answer)


def _cited_ids(text: str, end: int) -> list[str]:
    """Evidence IDs of the first citation after `end` on the same line."""
    line_end = text.find("\n", end)
    m = _CITATION.search(text, end, len(text) if line_end == -1 else line_end)
    return [i.strip() for i in m.group(1).split(",")] if m else []


def miscited(text: str, numbers: list[Number], metadata: dict[str, str]) -> list[Number]:
    """Numbers whose citation names only metadata evidence ({id: text}) that doesn't contain them."""
    out = []
    for n in numbers:
        ids = _cited_ids(text, n.end)
        if ids and all(i in metadata for i in ids):
            supported, _ = verify_numbers(text[n.start:n.end], "\n".join(metadata[i] for i in ids))
            if not supported:
                out.append(n)
    return out


def check(answer: str, evidence_text: str, deadline: float, use_llm: bool = True,
          metadata: dict[str, str] | None = None) -> Verdict:
    """`metadata` = {evidence id: text} of items with no figures (METADATA_TOOLS)."""
    text = normalise_scale_words(answer)
    supported, unmatched = verify_numbers(text, evidence_text)
    bad = miscited(text, supported + unmatched, metadata or {})
    bad_at = {n.start for n in bad}
    supported = [n for n in supported if n.start not in bad_at]
    unmatched = [n for n in unmatched if n.start not in bad_at]
    verdict = Verdict(numbers_checked=len(supported) + len(unmatched) + len(bad), supported=len(supported),
                      answer=answer)
    if not unmatched and not bad:
        return verdict

    def raw(n: Number) -> str:  # as written in the answer (original casing)
        return answer[n.start:n.end]

    flagged = unmatched
    remaining = deadline - time.monotonic()
    if unmatched and use_llm and remaining > 6:
        lines = "\n".join(f"- {raw(n)!r} in: …{answer[max(0, n.start - 80):n.end + 40]}…" for n in unmatched)
        prompt = f"NUMBERS TO CHECK:\n{lines}\n\nEVIDENCE:\n{evidence_text[:12000]}"
        config = types.GenerateContentConfig(system_instruction=CHECK_SYSTEM, temperature=0,
                                             max_output_tokens=600, response_mime_type="application/json",
                                             response_schema=CHECK_SCHEMA)
        try:
            verdict.llm_calls = 1
            data = json.loads(llm.call(prompt, config, budget_seconds=min(remaining, 30)).text)
            derived = {v["number"].strip() for v in data.get("verdicts", []) if v.get("verdict") == "derived"}
            flagged = [n for n in unmatched if raw(n).strip() not in derived]
            verdict.derived = [raw(n) for n in unmatched if raw(n).strip() in derived]
        except (llm.LLMUnavailableError, ValueError, KeyError) as e:
            logger.warning("Fact-check LLM stage skipped: %s", e)

    # Mis-cited numbers are flagged whatever stage 1 or 2 said: the cited item can't hold them.
    flagged = sorted(flagged + bad, key=lambda n: n.start)
    verdict.unsupported = [raw(n) for n in flagged]
    out = answer
    for n in reversed(flagged):  # edit from the end so offsets hold
        out = out[:n.end] + UNVERIFIED_MARK + out[n.end:]
    verdict.answer = out
    return verdict
