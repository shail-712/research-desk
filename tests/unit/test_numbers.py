"""Stage-1 fact-checker (core/numbers.py). Ported from v1 tests/unit/test_agents.py (fact-checker
section, stage-1 cases) at 73b6fda; the stage-2 (LLM) tests move with core/agents/checker.py."""

import pytest

from core import numbers

EVIDENCE = ("NVDA price $202.06, day change -1.26%; market cap $4,879.1B. Total revenue 57,006 35,082 "
            "(In millions). Net revenue was $7.4 billion, up 36%.")


@pytest.mark.parametrize("answer, unmatched", [
    ("Revenue was $57,006 million [E4]", []),            # table figure "in millions"
    ("Revenue $57.0B and $35.1 billion a year ago", []),  # unit conversion + rounding
    ("price $202 and -1.3%", []),                         # display precision
    ("market cap $4.88 trillion", []),
    ("guidance of $68 billion", ["$68 billion"]),         # invented
    ("up 62% year over year", ["62%"]),                    # not in evidence (derived or not: LLM decides)
    ("In 2025, Q3 FY26 had 3 segments; 10-Q [E12] filed 2026-11-02", []),  # not checkable numbers
])
def test_verify_numbers(answer, unmatched):
    _, bad = numbers.verify_numbers(answer, EVIDENCE)
    assert [n.raw.strip() for n in bad] == unmatched


def test_extract_numbers_skips_small_counts_and_years_in_answers_only():
    text = "In 2025 there were 3 segments and revenue of $12 and 150 units"
    assert [n.raw.strip() for n in numbers.extract_numbers(text)] == ["$12", "150"]
    assert len(numbers.extract_numbers(text, for_evidence=True)) == 4


def test_extract_numbers_scales_and_percentages():
    (b,), (p,) = numbers.extract_numbers("$1.5 billion"), numbers.extract_numbers("-2.5%")
    assert b.value == 1.5e9 and b.scaled and b.kind == "num"
    assert p.value == 2.5 and p.kind == "pct" and not p.scaled


def test_empty_inputs():
    assert numbers.extract_numbers("") == [] and numbers.extract_numbers(None) == []
    assert numbers.verify_numbers("", "") == ([], [])


def test_unverified_mark_is_unchanged():
    assert numbers.UNVERIFIED_MARK == " ⚠️unverified"
