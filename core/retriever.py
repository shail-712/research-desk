"""
Semantic search over company filings (RAG retrieval).

The index backend comes from core/vectors.py (VECTOR_BACKEND = local | s3).

`ticker` is a preference: that company's documents if it has any, else all.
Balanced retrieval: over-fetch 50 candidates, then keep at most 3 per company, so
"Compare NVIDIA and AMD" doesn't return five chunks from one company (a v1 gotcha).

Adapted from v1 core/retriever.py (73b6fda): pgvector and the "try the local index" fallback
are gone (Lambda has no local index; locally the local index is the primary).
"""

import logging

from core import embeddings, vectors

logger = logging.getLogger(__name__)

OVERFETCH_K = 50
MAX_CHUNKS_PER_COMPANY = 3
DEFAULT_TOP_K = 5
MAX_TOP_K = 20


class RetrieverError(RuntimeError):
    """Search failed. The message is safe to show; details are logged."""


def safe_top_k(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(1, min(value, MAX_TOP_K))


def balance(candidates: list[dict], top_k: int) -> list[dict]:
    """Keeps candidate order but caps each company at MAX_CHUNKS_PER_COMPANY.
    When every candidate is from one company (a ticker-filtered search) there is
    nothing to balance, so the cap is lifted."""
    single_company = len({c.get("company") for c in candidates}) <= 1
    cap = top_k if single_company else MAX_CHUNKS_PER_COMPANY
    per_company: dict[str, int] = {}
    results = []
    for chunk in candidates:
        company = chunk.get("company") or "UNKNOWN"
        if per_company.get(company, 0) >= cap:
            continue
        per_company[company] = per_company.get(company, 0) + 1
        results.append(chunk)
        if len(results) >= top_k:
            break
    return results


def search(query: str, top_k=DEFAULT_TOP_K, ticker: str | None = None) -> list[dict]:
    """
    Returns up to top_k chunks: [{company, ticker, source, text, score, url, form, doc_date, section, period}].
    `ticker` is a preference: that company's documents if it has any, else all documents.
    Raises ValueError for an empty query, RetrieverError when search is unavailable.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("Missing query")
    top_k = safe_top_k(top_k)

    try:
        vec = embeddings.get_service().embed_query(query)
    except ValueError as e:  # missing API key
        logger.error("Embedding service misconfigured: %s", e)
        raise RetrieverError("Embedding service is not configured") from e
    except embeddings.EmbeddingError as e:
        raise RetrieverError("Embedding service unavailable, please retry") from e

    try:
        index = vectors.get_vector_index()
        candidates = index.search(vec, OVERFETCH_K, ticker) if ticker else []
        candidates = candidates or index.search(vec, OVERFETCH_K, None)
    except vectors.VectorIndexError as e:
        raise RetrieverError(str(e)) from e
    except Exception as e:
        logger.error("Document search failed: %s", type(e).__name__)
        raise RetrieverError("Document search failed") from e

    results = balance(candidates, top_k)
    logger.info("Retrieved %d chunks for %r", len(results), query[:80])
    return results
