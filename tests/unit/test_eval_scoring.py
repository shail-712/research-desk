"""Golden test for the frozen eval scorer (DECISIONS D8).

Re-scores v1's saved 2026-11-03 results (tests/fixtures/v1_eval_2026-11-03.json, a byte copy of
v1's evals/results/latest.json) with evals/scoring.py. The expected summaries were produced by v1's
own evals/run.py at commit 73b6fda; if this test fails, the ruler changed and v1/v2 numbers are
no longer comparable.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals import scoring

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "v1_eval_2026-11-03.json"
QUESTIONS = ROOT / "evals" / "questions.json"

GOLDEN = {
    "quick": {
        "answered": "20/20", "unmatched": "1/109 (1%)", "answers_all_matched": "95%", "companies": "100%",
        "agents": "75%", "citations": "51%", "p50": "4.7 s", "p90": "29.5 s", "calls": "1.0",
        "follow_ups": 0, "flagged": 0, "fallbacks": 0,
    },
    "deep": {
        "answered": "20/20", "unmatched": "0/98 (0%)", "answers_all_matched": "100%", "companies": "100%",
        "agents": "95%", "citations": "90%", "p50": "23.4 s", "p90": "49.3 s", "calls": "6.5",
        "follow_ups": 1, "flagged": 0, "fallbacks": 2,
    },
}


def _rows(mode: str) -> list[dict]:
    items = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    results = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [scoring.score(i, mode, results[f"{i['id']}:{mode}"]) for i in items if f"{i['id']}:{mode}" in results]


@pytest.mark.parametrize("mode", ["quick", "deep"])
def test_summarise_reproduces_v1_2026_11_03_exactly(mode):
    rows = _rows(mode)
    assert len(rows) == 20
    assert scoring.summarise(rows) == GOLDEN[mode]


def test_quick_tools_map_to_agents_and_citation_pattern():
    assert scoring.QUICK_TOOL_TO_AGENT == {"market": "market", "news": "news", "rag": "filings"}
    assert scoring.CITATION.search("Revenue $57.0B [E4].")
    assert scoring.CITATION.search("Revenue $57.0B [Doc 2].")
    assert not scoring.CITATION.search("Revenue $57.0B (source: 10-Q).")


def test_summarise_handles_no_answers():
    row = {"ok": False, "numbers": 0, "unmatched": 0, "companies_ok": False, "agents_ok": False,
           "cited_lines": 0, "numeric_lines": 0, "seconds": None, "llm_calls": None, "rounds": 0,
           "flagged": 0, "fallback": None}
    s = scoring.summarise([row])
    assert s["answered"] == "0/1" and s["p50"] == "n/a" and s["calls"] == "n/a" and s["citations"] == "n/a"


def test_scorer_imports_without_google_genai():
    """core.numbers and evals.scoring must not need the Gemini SDK (or any LLM code)."""
    code = (
        "import sys; sys.modules['google'] = None; sys.modules['google.genai'] = None\n"
        "import core.numbers, evals.scoring\n"
        "assert 'core.llm' not in sys.modules\n"
        "print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
