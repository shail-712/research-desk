"""scripts/bootstrap.py against moto: secrets never printed or overwritten by accident, create-only
index upload with an ETag-checked replace, watchlist seeded once. Adapted from v1
tests/unit/test_lambda.py bootstrap tests (73b6fda)."""

import numpy as np
import pytest

from core import embeddings
from core import vectors as V
from scripts import bootstrap

BUCKET = "research-desk-data"


@pytest.fixture
def aws(aws_env):
    import boto3
    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        yield {"s3": s3, "ssm": boto3.client("ssm", region_name="ap-south-1"),
               "ddb": boto3.resource("dynamodb", region_name="ap-south-1")}


def _index(path, rows=2, model=embeddings.MODEL):
    vecs = np.eye(rows, embeddings.DIMENSION, dtype="float32")
    meta = [{"chunk_hash": f"h{i}", "ticker": "NVDA", "text": f"t{i}"} for i in range(rows)]
    path.write_bytes(V.IndexData(vecs, meta, model, embeddings.DIMENSION).to_npz())
    return path


def test_secrets_are_created_once_and_never_returned(aws, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-secret-1")
    monkeypatch.setenv("APP_ACCESS_CODE", "code-secret")
    first = bootstrap.put_secrets(aws["ssm"], "/research-desk/")
    monkeypatch.setenv("GEMINI_API_KEY", "g-secret-2")
    second = bootstrap.put_secrets(aws["ssm"], "/research-desk")
    stored = aws["ssm"].get_parameter(Name="/research-desk/GEMINI_API_KEY", WithDecryption=True)["Parameter"]
    assert first["GEMINI_API_KEY"] == "created" and first["TELEGRAM_BOT_TOKEN"] == "missing in .env"
    assert second["GEMINI_API_KEY"].startswith("kept") and stored["Value"] == "g-secret-1"
    assert stored["Type"] == "SecureString"
    assert not {"g-secret-1", "g-secret-2", "code-secret"} & (set(first.values()) | set(second.values()))
    assert bootstrap.put_secrets(aws["ssm"], "/research-desk", overwrite=True)["GEMINI_API_KEY"] == "updated"
    assert aws["ssm"].get_parameter(Name="/research-desk/GEMINI_API_KEY",
                                    WithDecryption=True)["Parameter"]["Value"] == "g-secret-2"


def test_secret_names_match_what_the_lambdas_load():
    from aws_lambda import runtime

    assert set(bootstrap.SECRET_NAMES) == set(runtime.SECRET_NAMES)


def test_index_upload_is_create_only_and_replace_needs_the_current_etag(aws, tmp_path):
    path = _index(tmp_path / "index.npz", rows=2)
    first = bootstrap.upload_index(aws["s3"], BUCKET, path=path)
    assert first.startswith("uploaded 2 chunks to s3://research-desk-data/vectors/index.npz (ETag ")
    etag = aws["s3"].head_object(Bucket=BUCKET, Key="vectors/index.npz")["ETag"]
    assert bootstrap.upload_index(aws["s3"], BUCKET, path=path).startswith("already exists (kept")
    assert aws["s3"].head_object(Bucket=BUCKET, Key="vectors/index.npz")["ETag"] == etag  # untouched
    _index(path, rows=3)
    assert bootstrap.upload_index(aws["s3"], BUCKET, replace=True, path=path).startswith("replaced 3 chunks")


def test_replace_on_an_empty_bucket_creates_the_index(aws, tmp_path):
    path = _index(tmp_path / "index.npz")
    assert bootstrap.upload_index(aws["s3"], BUCKET, replace=True, path=path).startswith("uploaded 2 chunks")


def test_index_from_another_model_or_missing_file_is_not_uploaded(aws, tmp_path):
    with pytest.raises(V.VectorIndexError, match="rebuild"):
        bootstrap.upload_index(aws["s3"], BUCKET, path=_index(tmp_path / "x.npz", model="text-embedding-004"))
    assert bootstrap.upload_index(aws["s3"], BUCKET, path=tmp_path / "none.npz").startswith("no local index")
    assert aws["s3"].list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


def test_watchlist_is_seeded_only_once(aws):
    from core.store_dynamo import create_table

    create_table(aws["ddb"], "ResearchDesk")
    assert bootstrap.seed_watchlist("ResearchDesk", "ap-south-1", resource=aws["ddb"]) == "seeded NVDA, AMD, INTC"
    assert bootstrap.seed_watchlist("ResearchDesk", "ap-south-1", resource=aws["ddb"]).startswith("kept the existing")


def test_secrets_only_run_needs_no_stack(aws, monkeypatch, capsys):
    """Secrets go to SSM before the first `sam deploy`, so the run must not read stack outputs."""
    monkeypatch.setenv("APP_ACCESS_CODE", "code-secret")
    monkeypatch.setattr(bootstrap, "stack_outputs", lambda *a: pytest.fail("read the stack"))
    assert bootstrap.main(["--region", "ap-south-1", "--skip", "index", "watchlist"]) == 0
    out = capsys.readouterr().out
    assert "secret APP_ACCESS_CODE: created" in out and "code-secret" not in out
    assert aws["ssm"].get_parameter(Name="/research-desk/APP_ACCESS_CODE")["Parameter"]["Type"] == "SecureString"
