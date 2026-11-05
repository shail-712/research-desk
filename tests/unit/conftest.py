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
    from core import config

    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "local.db"))
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    for name in ("STORE_BACKEND", "VECTOR_BACKEND", "VECTOR_BUCKET", "DDB_TABLE"):
        monkeypatch.delenv(name, raising=False)
    yield
