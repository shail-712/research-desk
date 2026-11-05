"""
Vector index for retrieval (RAG): FAISS over SEC filing chunks.

    VECTOR_BACKEND = local   one file, data/index/index.npz (laptop; ingest writes here)
                   = s3      the same .npz as ONE S3 object (AWS; Lambda reads it)

Both backends store the same `.npz`: `vectors` (float32, unit length), `meta` (JSON rows) and
`header` (JSON: model, dim, version). Loading an index built with a different embedding model or
dimension is refused (D11): mixed vector spaces silently break retrieval (a v1 gotcha).

Every row carries `chunk_hash` (sha256 of the chunk text + its filing identity), so ingest can
skip chunks that are already indexed and never pays to embed them again.

S3: each Lambda container downloads the object once and keeps it in memory (the warm copy;
not a /tmp file, which would live no longer than the container), and re-checks the ETag at
most every REFRESH_SECONDS. The first upload is create-only (`If-None-Match: *`); replacing it
needs the current ETag (`If-Match`), so two writers can't silently overwrite each other.

Search candidates: {company, ticker, source, text, score, url, form, doc_date, section, period}
with score = cosine similarity.

Adapted from v1 core/vectors.py (73b6fda): pgvector dropped, one file format, header check,
chunk_hash dedupe, S3 writes reduced to create-only / replace (D13).
"""

import io
import json
import logging
import threading
import time
from pathlib import Path

import numpy as np

from core import config
from core.config import env

logger = logging.getLogger(__name__)

OVERFETCH_K = 50
REFRESH_SECONDS = 300
FORMAT_VERSION = 2
INDEX_FILE = "index.npz"


class VectorIndexError(RuntimeError):
    """The vector index can't be read or written. The message is safe to show."""


def _candidate(m: dict, score: float) -> dict:
    return {"company": m.get("company") or m.get("ticker") or "UNKNOWN", "ticker": m.get("ticker"),
            "source": m.get("source", ""), "text": m.get("text", ""), "score": round(score, 4),
            "url": m.get("url"), "form": m.get("form"), "doc_date": m.get("filed"),
            "section": m.get("section"), "period": m.get("period"), "event_date": m.get("event_date")}


class IndexData:
    """Vectors + metadata + header, searchable with FAISS."""

    def __init__(self, vectors: np.ndarray, meta: list[dict], model: str, dim: int):
        import faiss

        vectors = np.asarray(vectors, dtype="float32").reshape(-1, dim)
        if len(vectors) != len(meta):
            raise VectorIndexError("Document index and metadata are out of sync")
        self.vectors = np.ascontiguousarray(vectors)
        self.meta = meta
        self.model, self.dim = model, dim
        self.index = faiss.IndexFlatL2(dim)
        if len(self.vectors):
            self.index.add(self.vectors)
        self.hashes = {m.get("chunk_hash") for m in meta}

    @classmethod
    def empty(cls, model: str, dim: int) -> "IndexData":
        return cls(np.zeros((0, dim), dtype="float32"), [], model, dim)

    def __len__(self) -> int:
        return len(self.meta)

    def check(self, model: str, dim: int) -> None:
        if (self.model, self.dim) != (model, dim):
            raise VectorIndexError(
                f"Document index was built with {self.model}/{self.dim}, but the app embeds with {model}/{dim}; "
                "rebuild the index")

    def search(self, vec: np.ndarray, k: int = OVERFETCH_K, ticker: str | None = None) -> list[dict]:
        vec = np.asarray(vec, dtype="float32").reshape(-1)
        if vec.shape[0] != self.dim:
            raise VectorIndexError("Document index does not match the embedding model")
        if not self.meta:
            return []
        distances, ids = self.index.search(vec.reshape(1, -1), min(k, len(self.meta)))
        # IndexFlatL2 returns squared L2; for unit vectors cosine = 1 - d²/2.
        out = [(self.meta[i], 1 - float(d) / 2) for d, i in zip(distances[0], ids[0]) if 0 <= i < len(self.meta)]
        if ticker:
            out = [(m, s) for m, s in out if m.get("ticker") == ticker]
        return [_candidate(m, s) for m, s in out]

    def with_rows(self, rows: list[dict], vectors: np.ndarray) -> "IndexData":
        """A new IndexData with the rows whose chunk_hash isn't indexed yet appended."""
        keep = [i for i, r in enumerate(rows) if r["chunk_hash"] not in self.hashes]
        seen: set[str] = set()
        keep = [i for i in keep if not (rows[i]["chunk_hash"] in seen or seen.add(rows[i]["chunk_hash"]))]
        if not keep:
            return self
        new_vecs = np.asarray(vectors, dtype="float32")[keep]
        return IndexData(np.vstack([self.vectors, new_vecs]), self.meta + [rows[i] for i in keep], self.model, self.dim)

    def with_changes(self, updates: dict[str, dict] | None = None, drop: set[str] | None = None) -> "IndexData":
        """A new IndexData with metadata fields merged into rows (by chunk_hash) and rows dropped.
        Vectors are kept as they are: no re-embedding."""
        updates, drop = updates or {}, drop or set()
        keep = [i for i, m in enumerate(self.meta) if m.get("chunk_hash") not in drop]
        meta = [{**self.meta[i], **updates.get(self.meta[i].get("chunk_hash"), {})} for i in keep]
        return IndexData(self.vectors[keep], meta, self.model, self.dim)

    def to_npz(self) -> bytes:
        buf = io.BytesIO()
        enc = lambda obj: np.frombuffer(json.dumps(obj).encode("utf-8"), dtype=np.uint8)  # noqa: E731
        np.savez_compressed(buf, vectors=self.vectors, meta=enc(self.meta),
                            header=enc({"model": self.model, "dim": self.dim, "version": FORMAT_VERSION,
                                        "rows": len(self.meta)}))
        return buf.getvalue()

    @staticmethod
    def from_npz(data: bytes) -> "IndexData":
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            if "header" not in z.files:
                raise VectorIndexError("Document index has no model header (built by v1?); rebuild it")
            header = json.loads(bytes(z["header"]).decode("utf-8"))
            meta = json.loads(bytes(z["meta"]).decode("utf-8"))
            return IndexData(z["vectors"], meta, header["model"], int(header["dim"]))


def _expected() -> tuple[str, int]:
    from core import embeddings

    return embeddings.MODEL, embeddings.DIMENSION


class LocalIndex:
    """data/index/index.npz; `save()` writes atomically (temp file + rename)."""
    name = "FAISS (local)"

    def __init__(self, directory: Path):
        self.path = Path(directory) / INDEX_FILE
        self._lock = threading.Lock()
        self._data: IndexData | None = None

    def load(self, create: bool = False) -> IndexData:
        with self._lock:
            if self._data is None:
                model, dim = _expected()
                if self.path.exists():
                    data = IndexData.from_npz(self.path.read_bytes())
                    data.check(model, dim)
                    logger.info("Local FAISS index loaded: %d vectors (%s/%d)", len(data), data.model, data.dim)
                elif create:
                    data = IndexData.empty(model, dim)
                else:
                    raise VectorIndexError("Document index is unavailable (run `python cli.py ingest NVDA AMD INTC`)")
                self._data = data
            return self._data

    def search(self, vec, k=OVERFETCH_K, ticker=None):
        return self.load().search(vec, k, ticker)

    def existing_hashes(self) -> set[str]:
        return set(self.load(create=True).hashes)

    def rows_by_hash(self) -> dict[str, dict]:
        return {m.get("chunk_hash"): m for m in self.load(create=True).meta}

    def _save(self, new: IndexData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(new.to_npz())
        tmp.replace(self.path)
        with self._lock:
            self._data = new

    def add(self, rows: list[dict], vectors: np.ndarray) -> int:
        """Appends rows not indexed yet and saves. Returns how many were added."""
        data = self.load(create=True)
        new = data.with_rows(rows, vectors)
        added = len(new) - len(data)
        if added:
            self._save(new)
        return added

    def rewrite(self, updates: dict[str, dict] | None = None, drop: set[str] | None = None) -> tuple[int, int]:
        """Merges metadata `updates` {chunk_hash: fields} into rows and removes the `drop` hashes,
        then saves. Returns (rows updated, rows dropped). Vectors are untouched."""
        data = self.load(create=True)
        updated = sum(1 for m in data.meta if m.get("chunk_hash") in (updates or {})
                      and m.get("chunk_hash") not in (drop or set()))
        new = data.with_changes(updates, drop)
        dropped = len(data) - len(new)
        if updated or dropped:
            self._save(new)
        return updated, dropped


class S3Index:
    """One .npz object on S3, kept in memory per Lambda container (warm start)."""
    name = "FAISS (S3)"

    def __init__(self, bucket: str, key: str = "vectors/index.npz", client=None):
        import boto3

        if not bucket:
            raise VectorIndexError("VECTOR_BUCKET is not configured")
        self.bucket, self.key = bucket, key
        self.s3 = client or boto3.client("s3")
        self._lock = threading.Lock()
        self._data: IndexData | None = None
        self._etag: str | None = None
        self._checked = 0.0
        self.cache_hits = 0     # warm-start reuse (EMF IndexCacheHit / IndexCacheMiss in Phase C)
        self.cache_misses = 0

    def _download(self) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        except (ClientError, BotoCoreError) as e:
            raise VectorIndexError("Document index is unavailable") from e
        data = IndexData.from_npz(obj["Body"].read())
        data.check(*_expected())
        self._data, self._etag = data, obj["ETag"]
        self._checked = time.monotonic()
        self.cache_misses += 1
        logger.info("S3 FAISS index loaded: %d vectors (etag %s)", len(data), self._etag)

    def load(self) -> IndexData:
        """Warm start: reuse the in-memory copy; re-check the ETag every REFRESH_SECONDS."""
        with self._lock:
            if self._data is None:
                self._download()
            elif time.monotonic() - self._checked > REFRESH_SECONDS:
                head = self._head()
                self._checked = time.monotonic()
                if head["ETag"] != self._etag:
                    self._download()
                else:
                    self.cache_hits += 1
            else:
                self.cache_hits += 1
            return self._data

    def _head(self) -> dict:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return self.s3.head_object(Bucket=self.bucket, Key=self.key)
        except (ClientError, BotoCoreError) as e:
            raise VectorIndexError("Document index is unavailable") from e

    def search(self, vec, k=OVERFETCH_K, ticker=None):
        return self.load().search(vec, k, ticker)

    def upload(self, npz: bytes, replace_etag: str | None = None) -> str:
        """Create-only upload (If-None-Match: *). With `replace_etag`, replaces exactly that version
        (If-Match). Raises VectorIndexError if the object exists / changed. Returns the new ETag."""
        from botocore.exceptions import ClientError

        IndexData.from_npz(npz).check(*_expected())  # never upload an index the app would refuse
        cond = {"IfMatch": replace_etag} if replace_etag else {"IfNoneMatch": "*"}
        try:
            resp = self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=npz,
                                      ContentType="application/octet-stream", **cond)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                raise VectorIndexError("The index on S3 already exists or changed; not overwritten") from e
            raise VectorIndexError("Could not upload the document index") from e
        return resp["ETag"]


_index = None
_index_lock = threading.Lock()


def get_vector_index():
    global _index
    with _index_lock:
        if _index is None:
            if config.vector_backend() == "s3":
                _index = S3Index(env("VECTOR_BUCKET"), env("VECTOR_KEY", "vectors/index.npz"))
            else:
                _index = LocalIndex(config.INDEX_DIR)
            logger.info("Vector index: %s", _index.name)
        return _index


def reset_vector_index() -> None:
    global _index
    with _index_lock:
        _index = None
