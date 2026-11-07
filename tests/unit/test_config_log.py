import io
import json
import logging
import threading

import pytest

from core import config, log


def test_env_helpers(monkeypatch):
    monkeypatch.setenv("X_BLANK", "   ")
    monkeypatch.setenv("X_NUM", "2.5")
    monkeypatch.setenv("X_BAD", "abc")
    assert config.env("X_BLANK", "d") == "d"
    assert config.env_float("X_NUM", 1.0) == 2.5
    assert config.env_int("X_BAD", 7) == 7


def test_backends_default_local_and_validate(monkeypatch):
    assert config.store_backend() == "local" and config.vector_backend() == "local"
    monkeypatch.setenv("STORE_BACKEND", "DynamoDB")
    monkeypatch.setenv("VECTOR_BACKEND", "s3")
    assert config.store_backend() == "dynamodb" and config.vector_backend() == "s3"
    monkeypatch.setenv("STORE_BACKEND", "supabase")
    with pytest.raises(config.ConfigError, match="STORE_BACKEND"):
        config.store_backend()


@pytest.fixture
def captured():
    buf = io.StringIO()
    log.setup("INFO", stream=buf)
    yield lambda: [json.loads(line) for line in buf.getvalue().splitlines()]
    log.setup("WARNING")


def test_json_logs_carry_request_id_and_extra_fields(captured):
    rid = log.new_request_id("req-123")
    logging.getLogger("t").info("hello %s", "world", extra={"ticker": "NVDA"})
    (rec,) = captured()
    assert rid == "req-123"
    assert rec["msg"] == "hello world" and rec["request_id"] == "req-123" and rec["ticker"] == "NVDA"
    assert rec["level"] == "INFO" and rec["ts"].endswith("+00:00")


def test_request_id_follows_into_threads(captured):
    log.new_request_id("req-thread")
    t = threading.Thread(target=log.run_in_context(lambda: logging.getLogger("t").info("in thread")))
    t.start()
    t.join()
    assert captured()[0]["request_id"] == "req-thread"


def test_one_wrapper_serves_many_concurrent_threads():
    """A thread pool reuses one wrapper for every task; concurrent calls must not collide."""
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(4)
    log.new_request_id("req-pool")

    def task(i):
        barrier.wait(timeout=5)  # all four inside the wrapped call at the same time
        return log.get_request_id()

    wrapped = log.run_in_context(task)
    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(wrapped, range(4))) == ["req-pool"] * 4


def test_log_tool_records_name_latency_success_tokens(captured):
    log.log_tool("get_quote", 0.4567, True, tokens=120, ticker="AMD")
    rec = captured()[0]
    assert (rec["tool"], rec["seconds"], rec["ok"], rec["tokens"], rec["ticker"]) == ("get_quote", 0.457, True, 120,
                                                                                         "AMD")


def test_secrets_are_redacted(captured, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-secret-value-123")
    logging.getLogger("t").warning("GET https://finnhub.io/api/v1/quote?token=fh-secret-value-123 failed")
    try:
        raise RuntimeError("boom fh-secret-value-123")
    except RuntimeError:
        logging.getLogger("t").exception("call failed")
    out = json.dumps(captured())
    assert "fh-secret-value-123" not in out and log.REDACTED in out


def test_log_module_does_not_shadow_stdlib():
    assert logging.getLogger is not None and log.__name__ == "core.log"
