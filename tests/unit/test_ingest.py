"""Ingest: filing selection, sections, chunking, hash dedupe, cache reuse, daily cap, pacing.
SEC and Gemini are fakes; nothing touches the network or data/."""

import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from core import embeddings, ingest, vectors

DIM = embeddings.DIMENSION


# ─── fakes ──────────────────────────────────────────────────────────────────

SUBMISSIONS = {"filings": {"recent": {
    "form": ["8-K", "10-Q", "8-K", "4", "10-Q", "10-K", "8-K"],
    "items": ["5.02", "", "2.02,9.01", "", "", "", "2.02"],
    "accessionNumber": ["A-8K-other", "A-10Q-new", "A-8K-earn", "A-4", "A-10Q-old", "A-10K", "A-8K-old"],
    "filingDate": ["2026-09-01", "2026-08-26", "2026-08-26", "2026-08-20", "2026-05-20", "2026-02-25", "2026-02-25"],
    "reportDate": ["2026-09-01", "2026-07-26", "2026-08-26", "", "2026-04-26", "2026-01-25", "2026-02-25"],
    "primaryDocument": ["o.htm", "q.htm", "e.htm", "f4.htm", "q0.htm", "k.htm", "e0.htm"],
}}}

FILLER = " ".join(f"word{i}" for i in range(400))  # ~400 words of body text

TEN_K = f"""<html><body>
<p>Table of Contents</p><p>Item 1.</p><p>Business</p><p>Item 1A.</p><p>Risk Factors</p>
<p>Item 1. Business</p><p>Our Company. GPUs for data centers. {FILLER}</p>
<p>Item 1A. Risk Factors</p><p>Competition could harm us. {FILLER}</p>
<p>Item 1B. Unresolved Staff Comments</p><p>None.</p>
<p>Item 7. Management's Discussion and Analysis of Financial Condition</p><p>Revenue grew. {FILLER}</p>
<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p><p>Rates.</p>
<p>Item 8. Financial Statements</p><p>See Item 7, Management's Discussion and Analysis, for more.</p>
<p>Concentration of Revenue</p><p>Direct Customer A represented 22% of total revenue. {FILLER}</p>
</body></html>"""

TEN_Q = f"""<html><body><p>Item 2. Management's Discussion and Analysis</p><p>Data Center revenue was
$41,096 million. {FILLER}</p><p>Item 3. Quantitative and Qualitative Disclosures</p><p>x</p></body></html>"""

INDEX_8K = """<table><tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/1/A8Kearn/e.htm">e.htm</a></td>
<td>8-K</td></tr><tr><td>2</td><td>EX-99.1</td><td><a href="/Archives/edgar/data/1/A8Kearn/q2pr.htm">q2pr.htm</a></td>
<td>EX-99.1</td></tr></table>"""
PRESS = f"<html><body><p>NVIDIA Announces Financial Results. Revenue of $46.7 billion. {FILLER}</p></body></html>"


@pytest.fixture
def fake_sec(monkeypatch):
    calls = []

    def get(url):
        calls.append(url)
        if "submissions" in url:
            return SimpleNamespace(json=lambda: SUBMISSIONS)
        body = {"k.htm": TEN_K, "q.htm": TEN_Q, "-index.html": INDEX_8K, "q2pr.htm": PRESS}
        for suffix, text in body.items():
            if url.endswith(suffix):
                return SimpleNamespace(text=text)
        raise AssertionError(f"unexpected SEC url {url}")

    monkeypatch.setattr(ingest.sec, "get", get)
    monkeypatch.setattr(ingest, "resolve_tickers",
                        lambda symbols: [SimpleNamespace(ticker=s, cik=1045810, name="NVIDIA") for s in symbols])
    return calls


class FakeModels:
    def __init__(self, fail_first=0):
        self.texts, self.fail_first = [], fail_first

    def embed_content(self, model, contents, config):
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        self.texts += list(contents)
        vecs = [np.random.default_rng(int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)).normal(size=DIM)
                for t in contents]
        return SimpleNamespace(embeddings=[SimpleNamespace(values=list(v)) for v in vecs])


def _service(tmp_path, fail_first=0):
    svc = embeddings.EmbeddingService(api_key="k", cache=embeddings.EmbeddingCache(tmp_path / "cache.sqlite"))
    svc._client = SimpleNamespace(models=FakeModels(fail_first))
    return svc


# ─── selection and sections ─────────────────────────────────────────────────

def test_latest_filings_picks_newest_10k_10q_and_earnings_8k(fake_sec):
    got = ingest.latest_filings("NVDA", 1045810, "NVIDIA")
    assert [(f.form, f.accession, f.period) for f in got] == [
        ("10-K", "A-10K", "2026-01-25"), ("10-Q", "A-10Q-new", "2026-07-26"), ("8-K", "A-8K-earn", "2026-08-26")]
    assert got[0].url == "https://www.sec.gov/Archives/edgar/data/1045810/A10K/k.htm"


def test_10k_sections_skip_the_table_of_contents_and_in_text_references():
    secs = dict(ingest.sections("10-K", ingest.html_to_text(TEN_K)))
    assert set(secs) == {"Item 1. Business", "Item 1A. Risk Factors", "Item 7. MD&A",
                         "Note: Concentration of Revenue and Customers"}
    assert secs["Item 1. Business"].startswith("Item 1. Business") and "Risk Factors\nCompetition" not in secs["Item 1. Business"]
    assert "Rates." not in secs["Item 7. MD&A"]          # stops at Item 7A
    assert "Direct Customer A represented 22%" in secs["Note: Concentration of Revenue and Customers"]


def test_title_headings_are_used_when_items_are_missing():
    text = ingest.html_to_text(f"""<p>Our Business</p><p>We make chips. {FILLER}</p>
        <p>Management's Discussion and Analysis</p><p>Results. {FILLER}</p>
        <p>Risk Factors</p><p>Risks. {FILLER}</p><p>Other Key Information</p><p>Officers.</p>""")
    secs = dict(ingest.sections("10-K", text))
    assert {"Item 1. Business", "Item 7. MD&A", "Item 1A. Risk Factors"} <= set(secs)
    assert "Officers." not in secs["Item 1A. Risk Factors"]


def test_heading_without_an_end_is_not_a_section_and_unknown_layouts_fall_back():
    text = ingest.html_to_text(f"<p>Item 7. Management's Discussion and Analysis</p><p>{FILLER}</p>")
    assert [n for n, _ in ingest.sections("10-K", text)] == ["Full document"]


def test_press_release_comes_from_the_ex99_document(fake_sec):
    f = ingest.latest_filings("NVDA", 1045810, "NVIDIA")[2]
    assert ingest.document_text(f).startswith("NVIDIA Announces Financial Results")


def test_table_cells_stay_apart():
    assert ingest.html_to_text("<table><tr><td>Data Center</td><td>41,096</td></tr></table>") == "Data Center 41,096"


def test_chunks_are_about_300_tokens_with_overlap():
    words = [f"w{i}" for i in range(1000)]
    chunks = ingest.chunk_words(" ".join(words))
    assert all(len(c.split()) <= ingest.CHUNK_WORDS for c in chunks)
    assert chunks[1].split()[0] == words[ingest.CHUNK_WORDS - ingest.CHUNK_OVERLAP]
    assert 250 <= embeddings.estimate_tokens(chunks[0]) <= 320


def test_priority_puts_demo_sections_first():
    order = ["Part II, Item 1A. Risk Factors", "Item 1A. Risk Factors", "Item 7. MD&A", "Item 1. Business",
             "Note: Segment Information", "Part I, Item 2. MD&A", "Earnings press release (EX-99.1)"]
    ranks = [ingest.priority(s) for s in order]
    assert ranks == sorted(ranks, reverse=True)


# ─── ingest runs ────────────────────────────────────────────────────────────

@pytest.fixture
def no_sleep():
    slept = []
    return slept, slept.append


def test_dry_run_reports_cost_and_embeds_nothing(fake_sec, tmp_path, no_sleep):
    svc = _service(tmp_path)
    stats = ingest.ingest(["NVDA"], dry_run=True, index=vectors.LocalIndex(tmp_path / "idx"), service=svc,
                          sleep=no_sleep[1])
    assert stats["chunks"] == stats["new_chunks"] == stats["need_api"] > 0
    assert svc.api_texts == 0 and not (tmp_path / "idx" / vectors.INDEX_FILE).exists()
    assert "Note: Concentration of Revenue and Customers" in stats["filings"][0]["sections"]


def test_second_ingest_makes_no_api_calls(fake_sec, tmp_path, no_sleep):
    idx_dir = tmp_path / "idx"
    first = ingest.ingest(["NVDA"], index=vectors.LocalIndex(idx_dir), service=_service(tmp_path), sleep=no_sleep[1])
    assert first["indexed"] == first["chunks"] and first["api_texts"] == first["chunks"]
    svc2 = _service(tmp_path)
    second = ingest.ingest(["NVDA"], index=vectors.LocalIndex(idx_dir), service=svc2, sleep=no_sleep[1])
    assert second["new_chunks"] == 0 and second["api_texts"] == 0 and svc2.api_texts == 0


def test_rebuilt_index_reuses_the_embedding_cache(fake_sec, tmp_path, no_sleep):
    ingest.ingest(["NVDA"], index=vectors.LocalIndex(tmp_path / "a"), service=_service(tmp_path), sleep=no_sleep[1])
    svc = _service(tmp_path)
    stats = ingest.ingest(["NVDA"], index=vectors.LocalIndex(tmp_path / "b"), service=svc, sleep=no_sleep[1])
    assert stats["indexed"] > 0 and svc.api_texts == 0 and stats["cache_hits"] == stats["indexed"]


def test_daily_cap_stops_and_the_next_run_resumes(fake_sec, tmp_path, monkeypatch, no_sleep):
    monkeypatch.setattr(ingest, "FILING_EMBED_DAILY_CAP", 5)
    monkeypatch.setattr(ingest, "EMBED_TOKENS_PER_BATCH", 300)  # one chunk per batch
    idx_dir = tmp_path / "idx"
    stats = ingest.ingest(["NVDA"], index=vectors.LocalIndex(idx_dir), service=_service(tmp_path), sleep=no_sleep[1])
    assert stats["indexed"] == 5 and "daily embedding cap" in stats["stopped"]
    assert ingest.used_today() == 5
    # The chunks indexed first are the highest-priority ones (press release, then notes / 10-Q MD&A).
    done = vectors.LocalIndex(idx_dir).load().meta
    assert done[0]["section"] == "Earnings press release (EX-99.1)"
    dry = ingest.ingest(["NVDA"], dry_run=True, index=vectors.LocalIndex(idx_dir), service=_service(tmp_path))
    left = [s for f in dry["filings"] for s in f["sections"]]
    assert max(ingest.priority(r["section"]) for r in done) <= max(ingest.priority(s) for s in left)
    monkeypatch.setattr(ingest, "FILING_EMBED_DAILY_CAP", 10_000)
    again = ingest.ingest(["NVDA"], index=vectors.LocalIndex(idx_dir), service=_service(tmp_path), sleep=no_sleep[1])
    assert again["new_chunks"] == stats["chunks"] - 5 and again["stopped"] is None


def test_rate_limit_waits_and_retries(fake_sec, tmp_path, no_sleep):
    slept, sleep = no_sleep
    stats = ingest.ingest(["NVDA"], index=vectors.LocalIndex(tmp_path / "idx"), service=_service(tmp_path, fail_first=1),
                          sleep=sleep)
    assert stats["stopped"] is None and stats["indexed"] == stats["chunks"]
    assert ingest.SECONDS_BETWEEN_BATCHES in slept


def test_persistent_failure_stops_with_progress_saved(fake_sec, tmp_path, no_sleep):
    stats = ingest.ingest(["NVDA"], index=vectors.LocalIndex(tmp_path / "idx"),
                          service=_service(tmp_path, fail_first=99), sleep=no_sleep[1])
    assert "run again to resume" in stats["stopped"] and stats["indexed"] == 0


def test_rows_carry_citation_metadata(fake_sec, tmp_path, no_sleep):
    idx = vectors.LocalIndex(tmp_path / "idx")
    ingest.ingest(["NVDA"], index=idx, service=_service(tmp_path), sleep=no_sleep[1])
    row = next(r for r in idx.load().meta if r["form"] == "10-Q")
    assert row["url"].endswith("/q.htm") and row["period"] == "2026-07-26" and row["filed"] == "2026-08-26"
    assert row["section"] == "Part I, Item 2. MD&A" and row["source"].startswith("NVDA 10-Q (period 2026-07-26)")
