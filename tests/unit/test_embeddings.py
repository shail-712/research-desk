"""Embedding service + cache (D11). The Gemini client is a fake that records every text it embeds."""

import hashlib
from types import SimpleNamespace

import numpy as np
import pytest

from core import embeddings as E


class FakeModels:
    def __init__(self, dim, fail_after=None):
        self.dim, self.calls, self.fail_after = dim, [], fail_after

    def embed_content(self, model, contents, config):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        self.calls.append({"model": model, "texts": list(contents), "task": config.task_type})
        vecs = []
        for t in contents:  # deterministic, text-dependent, not unit length (tests normalisation)
            seed = int(hashlib.sha256(f"{model}|{t}".encode()).hexdigest()[:8], 16)
            vecs.append(np.random.default_rng(seed).normal(size=self.dim) * 3)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=list(v)) for v in vecs])


def _service(tmp_path, model="gemini-embedding-001", dim=8, fail_after=None, cache=True):
    svc = E.EmbeddingService(api_key="k", model=model, dimension=dim,
                             cache=E.EmbeddingCache(tmp_path / "c.sqlite") if cache else None)
    svc._client = SimpleNamespace(models=FakeModels(dim, fail_after))
    return svc


def _texts(svc):
    return [t for c in svc._client.models.calls for t in c["texts"]]


def test_same_text_twice_costs_one_api_text(tmp_path):
    svc = _service(tmp_path)
    a = svc.embed_documents(["alpha", "beta", "alpha"])
    b = svc.embed_documents(["beta", "alpha"])
    assert _texts(svc) == ["alpha", "beta"]
    assert (svc.api_texts, svc.cache_hits) == (2, 3)
    assert np.allclose(a[0], a[2]) and np.allclose(a[0], b[1])
    assert np.allclose(np.linalg.norm(a, axis=1), 1.0)


def test_cache_survives_a_new_service(tmp_path):
    _service(tmp_path).embed_documents(["alpha"])
    svc2 = _service(tmp_path)
    svc2.embed_documents(["alpha"])
    assert svc2.api_texts == 0 and svc2.cache_hits == 1


def test_model_or_dim_change_is_a_cache_miss(tmp_path):
    _service(tmp_path).embed_documents(["alpha"])
    other_model = _service(tmp_path, model="gemini-embedding-2")
    other_model.embed_documents(["alpha"])
    other_dim = _service(tmp_path, dim=16)
    other_dim.embed_documents(["alpha"])
    assert other_model.api_texts == 1 and other_dim.api_texts == 1


def test_query_and_document_task_types_are_cached_separately(tmp_path):
    svc = _service(tmp_path)
    svc.embed_documents(["alpha"])
    svc.embed_query("alpha")
    svc.embed_query("alpha")
    assert [c["task"] for c in svc._client.models.calls] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


def test_misses_are_batched_at_the_api_limit(tmp_path):
    svc = _service(tmp_path)
    svc.embed_documents([f"t{i}" for i in range(250)])
    assert [len(c["texts"]) for c in svc._client.models.calls] == [100, 100, 50]


def test_batches_before_an_error_are_kept(tmp_path):
    svc = _service(tmp_path, fail_after=1)
    with pytest.raises(RuntimeError, match="429"):
        svc.embed_documents([f"t{i}" for i in range(150)])
    assert svc.cached([f"t{i}" for i in range(150)]) == [True] * 100 + [False] * 50


def test_works_without_a_cache(tmp_path):
    svc = _service(tmp_path, cache=False)
    svc.embed_documents(["a"])
    svc.embed_documents(["a"])
    assert svc.api_texts == 2


def test_empty_inputs(tmp_path):
    svc = _service(tmp_path)
    assert svc.embed_documents([]).shape == (0, 8)
    with pytest.raises(ValueError):
        svc.embed_query("  ")


def test_query_api_failure_is_a_safe_error(tmp_path):
    svc = _service(tmp_path, fail_after=0)
    with pytest.raises(E.EmbeddingError, match="unavailable"):
        svc.embed_query("q")


def test_cache_key_covers_model_dim_task_text():
    keys = {E.cache_key("m", 768, "D", "t"), E.cache_key("m2", 768, "D", "t"), E.cache_key("m", 3072, "D", "t"),
            E.cache_key("m", 768, "Q", "t"), E.cache_key("m", 768, "D", "t2")}
    assert len(keys) == 5


def test_shared_service_uses_the_configured_cache_path(tmp_path):
    from core import config

    svc = E.get_service()
    assert svc.cache is not None and svc.cache.path == config.EMBED_CACHE_PATH
    assert str(tmp_path) in str(svc.cache.path)  # conftest isolation: never data/cache
