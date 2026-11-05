"""
Shared test setup. Unit tests never reach the network: every external call is mocked, settings
that would point at real services are removed, and local stores and caches live in a temp folder.
"""

import os

import pytest

# Must happen before core.* is imported (core.config loads .env with override=False,
# so values set here win over the developer's .env).
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
for _name in ("FINNHUB_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SEC_USER_AGENT",
              "APP_ACCESS_CODE", "GEMINI_MODEL", "GEMINI_FALLBACK_MODELS"):
    os.environ[_name] = ""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh local DB + caches per test."""
    from core import config, embeddings, tickers, vectors

    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tickers, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tickers, "CACHE_FILE", tmp_path / "company_tickers.json")
    # The real index and embedding cache in data/ must never be touched by tests.
    monkeypatch.setattr(config, "INDEX_DIR", tmp_path / "index")
    monkeypatch.setattr(config, "EMBED_CACHE_PATH", tmp_path / "cache" / "embeddings.sqlite")
    for name in ("STORE_BACKEND", "VECTOR_BACKEND", "VECTOR_BUCKET", "DDB_TABLE"):
        monkeypatch.delenv(name, raising=False)
    tickers.reset_cache()
    embeddings.reset_service()
    vectors.reset_vector_index()
    yield
    tickers.reset_cache()
    embeddings.reset_service()
    vectors.reset_vector_index()


@pytest.fixture
def local_store():
    from core import store

    store.reset_store()
    yield store.get_store()
    store.reset_store()


@pytest.fixture
def aws_env(monkeypatch):
    """Fake credentials + region so boto3 never touches a real account."""
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "ap-south-1",
                 "AWS_REGION": "ap-south-1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


SEC_ROWS = [
    {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    {"cik_str": 1018724, "ticker": "AMZN", "title": "AMAZON COM INC"},
    {"cik_str": 2488, "ticker": "AMD", "title": "ADVANCED MICRO DEVICES INC"},
    {"cik_str": 50863, "ticker": "INTC", "title": "INTEL CORP"},
    {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    {"cik_str": 27419, "ticker": "TGT", "title": "TARGET CORP"},
    {"cik_str": 99999, "ticker": "FRST", "title": "FIRST BANCORP"},
    {"cik_str": 1100000, "ticker": "RTON", "title": "Right On Brands, Inc."},
]


@pytest.fixture
def sec_tickers(monkeypatch):
    """The resolver sees a small fake SEC company list."""
    from core import tickers

    monkeypatch.setattr(tickers, "_fetch_sec_list", lambda: SEC_ROWS)
    tickers.reset_cache()
    return tickers


@pytest.fixture
def dynamo_store(aws_env):
    """DynamoStore on moto's in-memory DynamoDB, with the table from TABLE_DEFINITION."""
    import boto3
    from moto import mock_aws

    from core.store_dynamo import DynamoStore, create_table

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="ap-south-1")
        create_table(resource, "ResearchDesk")
        yield DynamoStore("ResearchDesk", resource=resource)
