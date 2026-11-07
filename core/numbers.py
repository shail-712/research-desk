"""
Stage 1 of the fact-checker: pure-code number extraction and matching.

FROZEN — DECISIONS D8. Ported verbatim from v1 (investment-research-assistant @ 73b6fda),
core/agents/checker.py, from UNVERIFIED_MARK through verify_numbers. Function bodies and
constants are byte-identical; only the module boundary changed so the eval scorer
(evals/scoring.py) has no LLM/SDK dependency. Stdlib only. Do not edit without a new
DECISIONS entry and a re-score of both v1 and v2 results.
"""

import re
from dataclasses import asdict, dataclass, field

UNVERIFIED_MARK = " ⚠️unverified"
SCALES = {"trillion": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6,
          "m": 1e6, "thousand": 1e3, "k": 1e3}
REL_TOLERANCE = 0.005

_NUM = re.compile(
    r"(?<![\w.])(?P<sign>[-+−])?(?P<cur>\$)?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<unit>%|percent\b|trillion\b|billion\b|million\b|thousand\b|bn\b|mn\b|[BMKT]\b))?"
    r"(?![\w-])"
)


@dataclass
class Number:
    raw: str
    value: float          # absolute value in base units
    kind: str             # "pct" or "num"
    tolerance: float
    start: int
    end: int
    scaled: bool = False  # written with a unit (B/M/K/billion/...)


def extract_numbers(text: str, for_evidence: bool = False) -> list[Number]:
    """Numbers worth checking. In answers, plain small integers / years are skipped
    (counts, quarters, dates); evidence keeps everything."""
    out = []
    for m in _NUM.finditer(text or ""):
        raw_num, unit = m.group("num"), (m.group("unit") or "")
        unit_l = unit.lower()
        decimals = len(raw_num.split(".")[1]) if "." in raw_num else 0
        base = float(raw_num.replace(",", ""))
        if unit_l in ("%", "percent"):
            kind, scale = "pct", 1.0
        else:
            kind, scale = "num", SCALES.get(unit_l, 1.0)
        if not for_evidence and kind == "num" and not m.group("cur") and scale == 1.0:
            if decimals == 0 and (base < 100 or 1900 <= base <= 2100):
                continue  # counts, quarters, days, years
        value = abs(base) * scale
        precision = 0.5 * (10 ** -decimals) * scale
        out.append(Number(m.group(0), value, kind, max(precision, value * REL_TOLERANCE), m.start(), m.end(),
                          scaled=scale != 1.0))
    return out


# Financial tables print bare figures under "(in millions)" / "(in thousands)" headers,
# so a bare evidence number may stand for itself times one of these.
TABLE_UNITS = (1.0, 1e3, 1e6, 1e9)


def _supported(n: Number, evidence: list[Number]) -> bool:
    for e in evidence:
        if e.kind != n.kind:
            continue
        units = TABLE_UNITS if (n.kind == "num" and n.scaled and not e.scaled) else (1.0,)
        for unit in units:
            value = e.value * unit
            if abs(value - n.value) <= max(n.tolerance, value * REL_TOLERANCE):
                return True
    return False


@dataclass
class Verdict:
    numbers_checked: int = 0
    supported: int = 0
    derived: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    llm_calls: int = 0
    answer: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def verify_numbers(answer: str, evidence_text: str) -> tuple[list[Number], list[Number]]:
    """(supported, unmatched) numbers of the answer against the evidence text. Pure code."""
    evidence = extract_numbers(evidence_text, for_evidence=True)
    supported, unmatched = [], []
    for n in extract_numbers(answer):
        (supported if _supported(n, evidence) else unmatched).append(n)
    return supported, unmatched
