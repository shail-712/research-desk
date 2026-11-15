"""Retriever: balanced results, ticker preference, safe errors (ported from v1 tests/unit/test_retriever.py;
pgvector and local-fallback tests dropped with those backends)."""

from unittest import mock

import numpy as np
import pytest

from core import embeddings, retriever, vectors


@pytest.fixture
def small_index(tmp_path, monkeypatch):
    """A local index with 6 chunks: 4 NVIDIA, 2 AMD, set as the active index."""
    dim = embeddings.DIMENSION
    base = np.zeros((6, dim), dtype="float32")
    base[:4, 0] = 1.0
    base[4:, 1] = 1.0
    base[:, 2] = np.arange(6) * 0.01
    vecs = base / np.linalg.norm(base, axis=1, keepdims=True)
    meta = [{"chunk_hash": f"n{i}", "ticker": "NVDA", "company": "NVIDIA", "source": "NVDA 10-Q", "text": f"n{i}",
             "form": "10-Q", "filed": "2026-08-26", "section": "MD&A"} for i in range(4)] + \
           [{"chunk_hash": f"a{i}", "ticker": "AMD", "company": "AMD", "source": "AMD 10-Q", "text": f"a{i}",
             "form": "10-Q", "filed": "2026-11-04", "section": "MD&A"} for i in range(2)]
    index = vectors.LocalIndex(tmp_path)
    index._data = vectors.IndexData(vecs, meta, embeddings.MODEL, dim)
    monkeypatch.setattr(vectors, "_index", index)
    yield index


def _query(*hot):
    v = np.zeros(embeddings.DIMENSION, dtype="float32")
    for i in hot:
        v[i] = 1.0
    return v / np.linalg.norm(v)


def _embedder(vec=None, error=None):
    service = mock.MagicMock()
    if error:
        service.embed_query.side_effect = error
    else:
        service.embed_query.return_value = np.asarray(vec, dtype="float32")
    return mock.patch.object(retriever.embeddings, "get_service", return_value=service)


@pytest.mark.parametrize("raw, expected", [(3, 3), ("7", 7), ("x", 5), (None, 5), (0, 1), (99, 20)])
def test_safe_top_k(raw, expected):
    assert retriever.safe_top_k(raw) == expected


def test_balance_caps_each_company():
    cands = [{"company": "A"}] * 5 + [{"company": "B"}] * 2
    out = retriever.balance(cands, 5)
    assert [c["company"] for c in out] == ["A", "A", "A", "B", "B"]


def test_results_are_balanced_scored_and_carry_citation_fields(small_index):
    with _embedder(_query(0)):
        chunks = retriever.search("nvidia", top_k=5)
    companies = [c["company"] for c in chunks]
    assert companies.count("NVIDIA") == retriever.MAX_CHUNKS_PER_COMPANY
    assert companies.count("AMD") == 2
    assert chunks[0]["score"] > chunks[-1]["score"]
    assert chunks[0]["form"] == "10-Q" and chunks[0]["doc_date"] == "2026-08-26" and chunks[0]["section"] == "MD&A"


def test_compare_query_returns_both_companies(small_index):
    with _embedder(_query(0, 1)):
        chunks = retriever.search("Compare NVIDIA and AMD data center revenue")
    assert {"NVDA", "AMD"} <= {c["ticker"] for c in chunks}


def test_ticker_preference_filters_results(small_index):
    with _embedder(_query(0)):
        chunks = retriever.search("amd", ticker="AMD")
    assert {c["company"] for c in chunks} == {"AMD"}


def test_company_without_filings_gets_nothing_not_other_companies(small_index):
    """I5 bug fix: an Apple question used to get NVIDIA/AMD filings (fallback to all documents)."""
    with _embedder(_query(0)) as get_service:
        assert retriever.search("apple revenue", ticker="AAPL") == []
        assert retriever.search("apple revenue", tickers=["AAPL", "MSFT"]) == []
    get_service.assert_not_called()  # no embedding call when nothing can match


def test_several_companies_keep_only_the_indexed_ones(small_index):
    with _embedder(_query(0, 1)):
        chunks = retriever.search("compare apple and amd", tickers=["AAPL", "AMD"])
    assert chunks and {c["ticker"] for c in chunks} == {"AMD"}


def test_ticker_search_is_not_limited_to_the_global_top_k(small_index, monkeypatch):
    """Without the fallback, a covered company whose chunks rank below the global top k must still be found."""
    monkeypatch.setattr(retriever, "OVERFETCH_K", 2)  # the global top 2 are both NVIDIA
    with _embedder(_query(0)):
        assert {c["ticker"] for c in retriever.search("nvidia", top_k=2)} == {"NVDA"}
        chunks = retriever.search("nvidia", ticker="AMD", top_k=2)
    assert [c["ticker"] for c in chunks] == ["AMD", "AMD"]


def test_not_covered_names_missing_companies(small_index):
    from core.tickers import Company

    apple, amd = Company("AAPL", "Apple Inc.", 320193), Company("AMD", "AMD", 2488)
    assert retriever.indexed_tickers() == {"NVDA", "AMD"}
    assert retriever.not_covered([apple, amd]) == [apple]
    assert retriever.not_covered_message([amd]) is None
    assert retriever.not_covered_message([apple, amd]) == (
        "SEC filings for Apple Inc. (AAPL) aren't covered: the filings index has AMD and NVDA only.")
    assert retriever.not_covered_message([Company("ZZZZ", "ZZZZ")]).startswith("SEC filings for ZZZZ aren't")


def test_not_covered_claims_nothing_when_the_index_is_unreadable(tmp_path, monkeypatch):
    from core.tickers import Company

    monkeypatch.setattr(vectors, "_index", vectors.LocalIndex(tmp_path))
    assert retriever.indexed_tickers() is None
    assert retriever.not_covered_message([Company("AAPL", "Apple Inc.")]) is None


def test_single_company_results_are_not_capped(small_index):
    with _embedder(_query(0)):
        chunks = retriever.search("nvidia", ticker="NVDA", top_k=4)
    assert [c["company"] for c in chunks] == ["NVIDIA"] * 4


def test_dimension_mismatch_is_a_safe_error(small_index):
    with _embedder(np.ones(embeddings.DIMENSION + 1)):
        with pytest.raises(retriever.RetrieverError, match="does not match"):
            retriever.search("anything")


def test_embedding_failure_is_a_safe_error(small_index):
    with _embedder(error=embeddings.EmbeddingError("HTTP 429 secret")):
        with pytest.raises(retriever.RetrieverError) as err:
            retriever.search("x")
    assert "secret" not in str(err.value)


def test_missing_index_is_a_safe_error(tmp_path, monkeypatch):
    monkeypatch.setattr(vectors, "_index", vectors.LocalIndex(tmp_path))
    with _embedder(_query(0)):
        with pytest.raises(retriever.RetrieverError, match="unavailable"):
            retriever.search("x")


def test_empty_query_is_rejected():
    with pytest.raises(ValueError):
        retriever.search("  ")
