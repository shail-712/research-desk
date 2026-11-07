"""News: any-ticker RSS URL, relevance filter, safe errors (feedparser mocked)."""

from types import SimpleNamespace
from unittest import mock

import pytest

from core import news


@pytest.fixture(autouse=True)
def http_ok(monkeypatch):
    """The feed download succeeds; each test scripts feedparser.parse (as in v1)."""
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append({"url": url, "timeout": timeout})
        return SimpleNamespace(content=b"<rss/>", raise_for_status=lambda: None)

    monkeypatch.setattr(news.requests, "get", get)
    return calls


def _feed(titles):
    entries = [{"title": t, "link": "https://x", "published": "today", "summary": "s"} for t in titles]
    return SimpleNamespace(bozo=False, entries=entries, bozo_exception=None)


@pytest.mark.parametrize("raw, expected", [
    (3, 3), ("4", 4), ("abc", 5), (None, 5), (0, 1), (-2, 1), (500, 20), ([1], 5),
])
def test_safe_count(raw, expected):
    assert news.safe_count(raw) == expected


def test_any_ticker_builds_its_own_feed_url(http_ok):
    with mock.patch.object(news.feedparser, "parse", return_value=_feed(["Apple ships"])):
        news.get_news("aapl", "Apple Inc.")
    assert "s=AAPL&" in http_ok[0]["url"] and http_ok[0]["timeout"] == news.FEED_TIMEOUT_SECONDS


def test_keeps_only_headlines_about_the_company():
    titles = ["Nvidia beats", "Unrelated market story", "NVDA options surge", "Nvidiaesque thing"]
    with mock.patch.object(news.feedparser, "parse", return_value=_feed(titles)):
        articles = news.get_news("NVDA", "NVIDIA CORP")
    assert [a["title"] for a in articles] == ["Nvidia beats", "NVDA options surge"]


def test_falls_back_to_top_stories_when_nothing_matches():
    with mock.patch.object(news.feedparser, "parse", return_value=_feed(["Market wrap", "Fed day"])):
        articles = news.get_news("XYZ", "Obscure Holdings Inc", count=1)
    assert [a["title"] for a in articles] == ["Market wrap"]


def test_name_keywords_drop_legal_suffixes():
    assert news.name_keywords("AMD", "ADVANCED MICRO DEVICES INC") == {"amd", "advanced micro devices"}
    assert news.name_keywords("META", "Meta Platforms, Inc.") == {"meta", "meta platforms"}


def test_feed_failure_raises_safe_error(monkeypatch):
    monkeypatch.setattr(news.requests, "get", mock.Mock(side_effect=news.requests.Timeout("socket secret")))
    with mock.patch.object(news.feedparser, "parse", return_value=_feed(["x"])):
        with pytest.raises(news.NewsError) as err:
            news.get_news("NVDA")
    assert "secret" not in str(err.value)


def test_unreachable_feed_raises():
    feed = SimpleNamespace(bozo=True, entries=[], bozo_exception=Exception("x"))
    with mock.patch.object(news.feedparser, "parse", return_value=feed):
        with pytest.raises(news.NewsError):
            news.get_news("NVDA")


def test_missing_ticker_is_rejected():
    with pytest.raises(ValueError):
        news.get_news("  ")


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Yahoo</title>
<item><title>Nvidia beats estimates</title><link>https://example.com/a</link><pubDate>Wed, 04 Nov 2026 21:05:00 +0000</pubDate></item>
<item><title>Market wrap</title><link>https://example.com/b</link><pubDate>Wed, 04 Nov 2026 20:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_real_rss_bytes_are_parsed_into_dated_linked_headlines(monkeypatch):
    monkeypatch.setattr(news.requests, "get",
                        lambda *a, **k: SimpleNamespace(content=RSS, raise_for_status=lambda: None))
    (a,) = news.get_news("NVDA", "NVIDIA CORP")
    assert a["title"] == "Nvidia beats estimates" and a["link"] == "https://example.com/a"
    assert a["published"].startswith("Wed, 04 Nov 2026")


def test_http_error_is_a_safe_news_error(monkeypatch):
    def bad():
        raise news.requests.HTTPError("HTTP 503")
    monkeypatch.setattr(news.requests, "get", lambda *a, **k: SimpleNamespace(content=b"", raise_for_status=bad))
    with pytest.raises(news.NewsError, match="unavailable"):
        news.get_news("NVDA")


def test_global_socket_timeout_is_never_touched(monkeypatch):
    import socket
    before = socket.getdefaulttimeout()
    with mock.patch.object(news.feedparser, "parse", return_value=_feed(["Nvidia"])):
        news.get_news("NVDA")
    assert socket.getdefaulttimeout() == before
