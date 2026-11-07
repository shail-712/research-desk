"""LLM fallback chain (core/llm.py). Ported from v1 tests/unit/test_agent.py (llm section) at 73b6fda,
plus token-usage logging. Gemini is always mocked."""

import io
import json
from unittest import mock

import pytest
from google.genai import errors as genai_errors

from core import llm, log


# ─── llm.generate fallback chain ─────────────────────────────────────────────

def _api_error(code):
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": {"code": code, "message": "x", "status": "X"}})


@pytest.fixture
def fake_client():
    client = mock.MagicMock()
    with mock.patch.object(llm, "get_client", return_value=client), \
         mock.patch.object(llm.time, "sleep"), \
         mock.patch.object(llm, "GEMINI_MODEL", "primary"), \
         mock.patch.object(llm, "GEMINI_FALLBACK_MODELS", ["backup1", "backup2"]):
        yield client


def test_fallback_moves_to_next_model_on_503(fake_client):
    fake_client.models.generate_content.side_effect = [_api_error(503), mock.MagicMock(text="the answer")]
    assert llm.generate("q") == "the answer"
    models = [c.kwargs["model"] for c in fake_client.models.generate_content.call_args_list]
    assert models == ["primary", "backup1"]


def test_fallback_handles_429_and_timeouts(fake_client):
    fake_client.models.generate_content.side_effect = [
        _api_error(429), TimeoutError("timed out"), mock.MagicMock(text="ok")]
    assert llm.generate("q") == "ok"


def test_fallback_raises_when_all_models_fail(fake_client):
    fake_client.models.generate_content.side_effect = [_api_error(503)] * 3
    with pytest.raises(llm.LLMUnavailableError):
        llm.generate("q")


def test_fallback_stops_on_a_bad_api_key(fake_client):
    fake_client.models.generate_content.side_effect = [_api_error(403)]
    with pytest.raises(llm.LLMUnavailableError):
        llm.generate("q")
    assert fake_client.models.generate_content.call_count == 1


def test_model_specific_client_errors_move_to_the_next_model(fake_client):
    llm.reset_cooldowns()
    fake_client.models.generate_content.side_effect = [_api_error(400), mock.MagicMock(text="ok")]
    assert llm.generate("q") == "ok"
    assert llm._model_order()[0] == "primary"  # a 400 doesn't put the model in cooldown


def test_fallback_skips_empty_answers(fake_client):
    fake_client.models.generate_content.side_effect = [mock.MagicMock(text=""), mock.MagicMock(text="real")]
    assert llm.generate("q") == "real"


def test_missing_api_key_is_an_unavailable_llm(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(llm, "_client", None)
    with pytest.raises(llm.LLMUnavailableError):
        llm.get_client()
def test_failed_model_cools_down_and_is_tried_last(fake_client):
    llm.reset_cooldowns()
    fake_client.models.generate_content.side_effect = [_api_error(503), mock.MagicMock(text="a"),
                                                       mock.MagicMock(text="b")]
    assert llm.generate("q") == "a"
    assert llm.generate("q") == "b"
    models = [c.kwargs["model"] for c in fake_client.models.generate_content.call_args_list]
    assert models == ["primary", "backup1", "backup1"]  # primary skipped while cooling down
    llm.reset_cooldowns()


def test_each_attempt_timeout_fits_the_remaining_budget(fake_client):
    llm.reset_cooldowns()
    fake_client.models.generate_content.return_value = mock.MagicMock(text="ok")
    llm.call("q", llm.GenerateContentConfig(), budget_seconds=7)
    timeout_ms = fake_client.models.generate_content.call_args.kwargs["config"].http_options.timeout
    assert 3000 <= timeout_ms <= 7000




def test_default_chain_matches_spec(monkeypatch):
    import importlib
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    fresh = importlib.reload(llm)
    try:
        assert [fresh.GEMINI_MODEL] + fresh.GEMINI_FALLBACK_MODELS == [
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash"]
    finally:
        importlib.reload(llm)


def test_each_attempt_is_logged_with_tokens(fake_client):
    llm.reset_cooldowns()
    ok = mock.MagicMock(text="ok")
    ok.usage_metadata.total_token_count = 321
    fake_client.models.generate_content.side_effect = [_api_error(429), ok]
    buf = io.StringIO()
    log.setup("INFO", stream=buf)
    try:
        llm.generate("q")
    finally:
        log.setup("WARNING")
    tools = [r for r in map(json.loads, buf.getvalue().splitlines()) if "tool" in r]
    assert [(r["tool"], r["ok"], r["tokens"]) for r in tools] == [("gemini:primary", False, None),
                                                                  ("gemini:backup1", True, 321)]
    assert tools[0]["error"] == "HTTP 429"
