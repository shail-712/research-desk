"""
Gemini embeddings: gemini-embedding-001 at 768 dims, L2-normalised, with a content cache.

One service for both sides of retrieval, so queries and documents always share a vector space:
`embed_query` uses task type RETRIEVAL_QUERY, `embed_documents` uses RETRIEVAL_DOCUMENT.

Embeddings are paid for once (DECISIONS D11): every vector is cached in SQLite
(`data/cache/embeddings.sqlite`, override with EMBED_CACHE_PATH) under
sha256(model|dim|task_type|text). Only cache misses reach the API. Changing the model or the
dimension changes the key, so a stale vector can never be returned. Counters `cache_hits` and
`api_texts` are kept per service and logged per call.

Free tier (measured 2026-11-02): 30,000 tokens/minute and 1,000 requests/day, where every text
in a batch counts as one request.

Adapted from v1 core/embeddings.py (73b6fda): cache added; embed_documents accepts any number of
texts and batches the misses (≤100 per call).
"""

import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
from google.genai import Client
from google.genai.types import EmbedContentConfig, HttpOptions

from core import config
from core.config import env, env_int
from core.log import log_tool

logger = logging.getLogger(__name__)

MODEL = env("EMBEDDING_MODEL", "gemini-embedding-001")
DIMENSION = env_int("EMBEDDING_DIM", 768)
TIMEOUT_MS = 60_000
MAX_ITEMS_PER_CALL = 100  # API maximum per embed call
QUERY, DOCUMENT = "RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"


class EmbeddingError(RuntimeError):
    """The embedding API failed. The message is safe to show; details are logged."""


def normalise(vectors: np.ndarray) -> np.ndarray:
    """L2-normalises each row (gemini-embedding-001 only normalises at 3072 dims)."""
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype("float32")


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~1.4 tokens per English word, numbers count more)."""
    return int(len(text.split()) * 1.4) + 8


def cache_key(model: str, dim: int, task_type: str, text: str) -> str:
    return hashlib.sha256(f"{model}|{dim}|{task_type}|{text}".encode("utf-8")).hexdigest()


class EmbeddingCache:
    """SQLite key → float32 vector. Thread-safe; one connection per call (cheap for SQLite)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS emb (key TEXT PRIMARY KEY, dim INTEGER, vec BLOB)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def get_many(self, keys: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        with self._lock, self._connect() as db:
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                rows = db.execute(f"SELECT key, dim, vec FROM emb WHERE key IN ({','.join('?' * len(part))})", part)
                for key, dim, blob in rows:
                    vec = np.frombuffer(blob, dtype="float32")
                    if vec.shape == (dim,):
                        out[key] = vec
        return out

    def put_many(self, items: dict[str, np.ndarray]) -> None:
        with self._lock, self._connect() as db:
            db.executemany("INSERT OR REPLACE INTO emb (key, dim, vec) VALUES (?, ?, ?)",
                           [(k, int(v.shape[0]), np.asarray(v, dtype="float32").tobytes()) for k, v in items.items()])

    def __len__(self) -> int:
        with self._lock, self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM emb").fetchone()[0]


class EmbeddingService:
    def __init__(self, api_key: str | None = None, cache: EmbeddingCache | None = None,
                 model: str = MODEL, dimension: int = DIMENSION):
        self.api_key = api_key or env("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is not set. Check your .env file or app secrets.")
        self._client = None
        self.model = model
        self.dimension = dimension
        self.cache = cache
        self.cache_hits = 0
        self.api_texts = 0
        self.api_calls = 0

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = Client(api_key=self.api_key, http_options=HttpOptions(timeout=TIMEOUT_MS))
        return self._client

    def _call(self, texts: list[str], task_type: str) -> np.ndarray:
        started = time.monotonic()
        try:
            response = self.client.models.embed_content(
                model=self.model,
                contents=texts,
                config=EmbedContentConfig(output_dimensionality=self.dimension, task_type=task_type),
            )
        except Exception:
            log_tool("gemini.embed", time.monotonic() - started, False, texts=len(texts), model=self.model)
            raise
        self.api_calls += 1
        self.api_texts += len(texts)
        log_tool("gemini.embed", time.monotonic() - started, True, texts=len(texts), model=self.model)
        vectors = np.array([e.values for e in response.embeddings], dtype="float32")
        if vectors.shape != (len(texts), self.dimension):
            raise EmbeddingError(
                f"Unexpected embedding shape {vectors.shape}, expected ({len(texts)}, {self.dimension})"
            )
        return normalise(vectors)

    def _embed(self, texts: list[str], task_type: str) -> np.ndarray:
        """Cache first; misses (deduplicated) go to the API in batches of ≤100."""
        keys = [cache_key(self.model, self.dimension, task_type, t) for t in texts]
        found = self.cache.get_many(list(dict.fromkeys(keys))) if self.cache is not None else {}
        missing: dict[str, str] = {}
        for k, t in zip(keys, texts):
            if k not in found:
                missing.setdefault(k, t)
        hits = len(texts) - len(missing)  # in-batch duplicates are free too
        self.cache_hits += hits

        miss_keys = list(missing)
        for i in range(0, len(miss_keys), MAX_ITEMS_PER_CALL):
            batch = miss_keys[i:i + MAX_ITEMS_PER_CALL]
            vectors = self._call([missing[k] for k in batch], task_type)
            new = dict(zip(batch, vectors))
            if self.cache is not None:
                self.cache.put_many(new)  # saved per batch, so a 429 later loses nothing
            found.update(new)
        logger.info("Embedded %d texts (%s): cache_hits=%d api_texts=%d", len(texts), task_type, hits,
                    len(miss_keys), extra={"cache_hits": hits, "api_texts": len(miss_keys)})
        if not texts:
            return np.zeros((0, self.dimension), dtype="float32")
        return np.vstack([found[k] for k in keys]).astype("float32")

    def cached(self, texts: list[str], task_type: str = DOCUMENT) -> list[bool]:
        """Which texts are already cached (for dry runs: what an ingest would cost)."""
        if self.cache is None:
            return [False] * len(texts)
        keys = [cache_key(self.model, self.dimension, task_type, t) for t in texts]
        found = self.cache.get_many(list(dict.fromkeys(keys)))
        return [k in found for k in keys]

    def embed_query(self, text: str) -> np.ndarray:
        """One search query -> a (DIMENSION,) unit vector. Raises ValueError / EmbeddingError."""
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text.")
        try:
            return self._embed([text], QUERY)[0]
        except EmbeddingError:
            raise
        except Exception as e:
            logger.error("Gemini embedding call failed: %s", type(e).__name__)
            raise EmbeddingError("Embedding service unavailable") from e

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """Any number of documents. API errors propagate so callers (ingest) can retry 429s;
        everything embedded before the error is already cached."""
        return self._embed(list(texts), DOCUMENT)


_service: EmbeddingService | None = None
_lock = threading.Lock()


def _default_cache() -> EmbeddingCache | None:
    try:
        return EmbeddingCache(config.EMBED_CACHE_PATH)
    except (OSError, sqlite3.Error) as e:  # read-only filesystem etc.: work uncached
        logger.warning("Embedding cache unavailable (%s); embedding without a cache", type(e).__name__)
        return None


def get_service() -> EmbeddingService:
    """Shared instance (with the disk cache), created on first use."""
    global _service
    with _lock:
        if _service is None:
            _service = EmbeddingService(cache=_default_cache())
        return _service


def reset_service() -> None:
    global _service
    with _lock:
        _service = None
