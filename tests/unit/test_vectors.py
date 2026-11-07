"""Vector index: header checks, chunk_hash dedupe, local save/load, S3 read + create-only upload (moto)."""

import io
import json

import numpy as np
import pytest

from core import embeddings, vectors as V

DIM = embeddings.DIMENSION
MODEL = embeddings.MODEL


def _unit(seed):
    v = np.random.default_rng(seed).normal(size=DIM).astype("float32")
    return v / np.linalg.norm(v)


def _row(h, ticker="NVDA", text=None):
    return {"chunk_hash": h, "ticker": ticker, "company": ticker, "source": f"{ticker} 10-Q", "text": text or h,
            "url": f"https://sec.gov/{h}", "form": "10-Q", "filed": "2026-08-27", "section": "MD&A",
            "period": "2026-07-27"}


def test_local_add_skips_known_hashes_and_round_trips(tmp_path):
    idx = V.LocalIndex(tmp_path)
    assert idx.add([_row("a"), _row("b"), _row("a")], np.vstack([_unit(1), _unit(2), _unit(1)])) == 2
    assert idx.add([_row("b"), _row("c")], np.vstack([_unit(2), _unit(3)])) == 1
    fresh = V.LocalIndex(tmp_path)
    assert fresh.existing_hashes() == {"a", "b", "c"}
    top = fresh.search(_unit(2), k=1)[0]
    assert top["text"] == "b" and top["score"] == pytest.approx(1.0, abs=1e-4)
    assert {"form", "doc_date", "section", "url", "ticker", "period"} <= set(top)


def test_search_filters_by_ticker(tmp_path):
    idx = V.LocalIndex(tmp_path)
    idx.add([_row("n", "NVDA"), _row("a", "AMD")], np.vstack([_unit(1), _unit(2)]))
    assert [c["ticker"] for c in idx.search(_unit(1), ticker="AMD")] == ["AMD"]


def test_missing_index_is_a_friendly_error(tmp_path):
    with pytest.raises(V.VectorIndexError, match="ingest"):
        V.LocalIndex(tmp_path).search(_unit(1))


def test_index_from_another_model_is_refused(tmp_path):
    other = V.IndexData(np.vstack([_unit(1)]), [_row("a")], "text-embedding-004", DIM)
    (tmp_path / V.INDEX_FILE).write_bytes(other.to_npz())
    with pytest.raises(V.VectorIndexError, match="rebuild"):
        V.LocalIndex(tmp_path).search(_unit(1))


def test_index_without_header_is_refused():
    buf = io.BytesIO()
    np.savez_compressed(buf, vectors=np.zeros((0, DIM), "float32"),
                        meta=np.frombuffer(json.dumps([]).encode(), dtype=np.uint8))
    with pytest.raises(V.VectorIndexError, match="header"):
        V.IndexData.from_npz(buf.getvalue())


def test_query_dimension_mismatch_is_refused():
    data = V.IndexData(np.vstack([_unit(1)]), [_row("a")], MODEL, DIM)
    with pytest.raises(V.VectorIndexError):
        data.search(np.ones(DIM + 1, dtype="float32"))


@pytest.fixture
def s3(aws_env):
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("s3", region_name="ap-south-1")
        client.create_bucket(Bucket="research-desk-test", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        yield client


def test_s3_upload_is_create_only_and_search_uses_the_warm_copy(s3, tmp_path):
    npz = V.IndexData(np.vstack([_unit(1), _unit(2)]), [_row("a"), _row("b")], MODEL, DIM).to_npz()
    idx = V.S3Index("research-desk-test", client=s3)
    etag = idx.upload(npz)
    with pytest.raises(V.VectorIndexError, match="already exists"):
        idx.upload(npz)
    assert idx.search(_unit(2), k=1)[0]["text"] == "b"
    idx.search(_unit(1))
    assert (idx.cache_misses, idx.cache_hits) == (1, 1)
    # Replacing needs the current ETag.
    assert idx.upload(npz, replace_etag=etag)
    with pytest.raises(V.VectorIndexError):
        idx.upload(npz, replace_etag='"stale"')


def test_s3_refuses_to_upload_an_index_from_another_model(s3, tmp_path):
    npz = V.IndexData(np.vstack([_unit(1)]), [_row("a")], "other-model", DIM).to_npz()
    with pytest.raises(V.VectorIndexError, match="rebuild"):
        V.S3Index("research-desk-test", client=s3).upload(npz)


def test_s3_missing_object_is_a_friendly_error(s3, tmp_path):
    with pytest.raises(V.VectorIndexError, match="unavailable"):
        V.S3Index("research-desk-test", client=s3).search(_unit(1))


def test_backend_selection(monkeypatch):
    assert isinstance(V.get_vector_index(), V.LocalIndex)
    V.reset_vector_index()
    monkeypatch.setenv("VECTOR_BACKEND", "s3")
    monkeypatch.setenv("VECTOR_BUCKET", "")
    with pytest.raises(V.VectorIndexError, match="VECTOR_BUCKET"):
        V.get_vector_index()


def test_s3_etag_recheck_failure_is_a_friendly_error(s3, monkeypatch):
    """Review A NIT: head_object errors become VectorIndexError like get_object's."""
    idx = V.S3Index("research-desk-test", client=s3)
    idx.upload(V.IndexData(np.vstack([_unit(1)]), [_row("a")], MODEL, DIM).to_npz())
    idx.search(_unit(1))
    s3.delete_object(Bucket="research-desk-test", Key=idx.key)
    monkeypatch.setattr(V, "REFRESH_SECONDS", -1)  # force the ETag re-check
    with pytest.raises(V.VectorIndexError, match="unavailable"):
        idx.search(_unit(1))
