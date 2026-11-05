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
    from core import config, tickers

    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tickers, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tickers, "CACHE_FILE", tmp_path / "company_tickers.json")
    for name in ("STORE_BACKEND", "VECTOR_BACKEND", "VECTOR_BUCKET", "DDB_TABLE"):
        monkeypatch.delenv(name, raising=False)
    tickers.reset_cache()
    yield
    tickers.reset_cache()


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
