"""
Semantic search over company filings (RAG retrieval).

The index backend comes from core/vectors.py (VECTOR_BACKEND = local | s3).

`ticker` / `tickers` restrict the search to those companies. A company with no indexed filings gets
nothing: there is no fallback to other companies' documents (I5: an Apple question used to get NVIDIA's
filings). `not_covered()` names the companies the answer must say aren't covered.
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


def indexed_tickers() -> frozenset[str] | None:
    """Companies with filings in the index, or None if the index can't be read (then nothing is
    claimed as "not covered"; search itself reports the error)."""
    try:
        return vectors.get_vector_index().tickers()
    except Exception as e:
        logger.warning("Couldn't read the index's companies: %s", type(e).__name__)
        return None


def not_covered(companies) -> list:
    """The companies (objects with .ticker) whose filings aren't in the index."""
    indexed = indexed_tickers() if companies else None
    return [c for c in companies if c.ticker not in indexed] if indexed else []


def not_covered_message(companies) -> str | None:
    """User-facing line for companies without indexed filings, e.g. "SEC filings for Apple (AAPL) aren't
    covered: the filings index has AMD, INTC and NVDA only." None when every company is covered."""
    missing = not_covered(companies)
    if not missing:
        return None
    names = ", ".join(f"{c.name} ({c.ticker})" if c.name and c.name != c.ticker else c.ticker for c in missing)
    have = sorted(indexed_tickers() or [])
    have_text = ", ".join(have[:-1]) + " and " + have[-1] if len(have) > 1 else "".join(have)
    return f"SEC filings for {names} aren't covered: the filings index has {have_text} only."


def search(query: str, top_k=DEFAULT_TOP_K, ticker: str | None = None, tickers=None) -> list[dict]:
    """
    Returns up to top_k chunks: [{company, ticker, source, text, score, url, form, doc_date, section, period}].
    `ticker` (one company) or `tickers` (several): only those companies' documents; companies with none
    indexed are skipped, so the result can be empty. Neither: all documents.
    Raises ValueError for an empty query, RetrieverError when search is unavailable.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("Missing query")
    top_k = safe_top_k(top_k)
    wanted = [ticker] if ticker else [t for t in (tickers or []) if t]

    try:
        index = vectors.get_vector_index()
        if wanted:
            indexed = index.tickers()
            wanted = list(dict.fromkeys(t for t in wanted if t in indexed))
            if not wanted:  # none of the named companies has filings: no embedding call, no fallback
                logger.info("No indexed filings for the named companies; returning none")
                return []
    except vectors.VectorIndexError as e:
        raise RetrieverError(str(e)) from e
    except Exception as e:
        logger.error("Document search failed: %s", type(e).__name__)
        raise RetrieverError("Document search failed") from e

    try:
        vec = embeddings.get_service().embed_query(query)
    except ValueError as e:  # missing API key
        logger.error("Embedding service misconfigured: %s", e)
        raise RetrieverError("Embedding service is not configured") from e
    except embeddings.EmbeddingError as e:
        raise RetrieverError("Embedding service unavailable, please retry") from e

    try:
        if wanted:
            candidates = [c for t in wanted for c in index.search(vec, OVERFETCH_K, t)]
            candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
        else:
            candidates = index.search(vec, OVERFETCH_K, None)
    except vectors.VectorIndexError as e:
        raise RetrieverError(str(e)) from e
    except Exception as e:
        logger.error("Document search failed: %s", type(e).__name__)
        raise RetrieverError("Document search failed") from e

    results = balance(candidates, top_k)
    logger.info("Retrieved %d chunks for %r", len(results), query[:80])
    return results
