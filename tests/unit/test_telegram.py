"""Telegram sender: retries only what is worth retrying, never raises, never logs the token."""

from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from core import telegram

TOKEN = "123456:SECRET-bot-token-value"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)


def resp(status, body=None):
    return SimpleNamespace(status_code=status, text=str(body or ""), json=lambda: body or {})


def test_not_configured_sends_nothing():
    with mock.patch.object(telegram.requests, "post") as post:
        assert telegram.send("hi") is False
    post.assert_not_called()


def test_success_posts_html_to_the_chat(configured):
    with mock.patch.object(telegram.requests, "post", return_value=resp(200)) as post:
        assert telegram.send("<b>hi</b>") is True
    url, payload = post.call_args.args[0], post.call_args.kwargs["json"]
    assert url.endswith(f"bot{TOKEN}/sendMessage") and post.call_args.kwargs["timeout"] == telegram.TIMEOUT_SECONDS
    assert payload == {"chat_id": "4242", "text": "<b>hi</b>", "parse_mode": "HTML", "disable_web_page_preview": True}


def test_rate_limit_and_server_errors_are_retried(configured):
    replies = [resp(429, {"parameters": {"retry_after": 1}}), resp(502), resp(200)]
    with mock.patch.object(telegram.requests, "post", side_effect=replies) as post:
        assert telegram.send("hi") is True
    assert post.call_count == 3


def test_client_error_is_not_retried_and_the_token_is_never_logged(configured, caplog):
    with mock.patch.object(telegram.requests, "post", return_value=resp(400, "Bad Request: chat not found")) as post:
        assert telegram.send("hi") is False
    assert post.call_count == 1
    assert "chat not found" in caplog.text and TOKEN not in caplog.text


def test_network_errors_give_up_after_the_attempts_without_raising(configured, caplog):
    error = requests.ConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage unreachable")
    with mock.patch.object(telegram.requests, "post", side_effect=error) as post:
        assert telegram.send("hi") is False
    assert post.call_count == telegram.MAX_ATTEMPTS
    assert TOKEN not in caplog.text


def test_format_escapes_html_and_picks_the_icon():
    text = telegram.format_event({"kind": "price_move", "title": "INTC -7.5% on Nov 03 <b>",
                                  "body": "Why: “A & B”", "url": "https://x/?a=1&b=\"2\""})
    assert text.startswith("📉 <b>INTC -7.5% on Nov 03 &lt;b&gt;</b>")
    assert "Why: “A &amp; B”" in text and 'href="https://x/?a=1&amp;b=&quot;2&quot;"' in text
    assert telegram.format_event({"kind": "price_move", "title": "NVDA +5.0%"}).startswith("📈")
    assert len(telegram.format_event({"kind": "x", "title": "t", "body": "y" * 5000})) == telegram.MAX_LEN
