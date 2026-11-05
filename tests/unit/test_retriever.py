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


def test_ticker_without_documents_falls_back_to_all(small_index):
    with _embedder(_query(0)):
        chunks = retriever.search("apple", ticker="AAPL")
    assert chunks and {c["ticker"] for c in chunks} <= {"NVDA", "AMD"}


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
