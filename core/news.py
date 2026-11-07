"""
Latest headlines for any ticker from Yahoo Finance RSS (no API key).

Yahoo's per-symbol feed also carries loosely related market stories, so we keep
headlines that mention the ticker or the company name. If none do, we fall back
to the feed's top stories rather than showing nothing.

Ported from v1 core/news.py (73b6fda). One change: the feed is fetched with `requests` and its
own timeout, then parsed from bytes. v1 set `socket.setdefaulttimeout()`, which is process-wide
and not thread-safe while Deep mode runs tools in parallel.
"""

import logging
import re
import time

import feedparser
import requests

from core.log import log_tool
from core.market import normalise_ticker

logger = logging.getLogger(__name__)

FEED_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
DEFAULT_COUNT = 5
MAX_COUNT = 20
FEED_TIMEOUT_SECONDS = 8
# Yahoo rejects requests' default User-Agent on some edges.
HEADERS = {"User-Agent": "Mozilla/5.0 (research-desk news reader)"}

# Words that say nothing about which company a headline is about.
_NAME_NOISE = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc",
    "holdings", "holding", "group", "the", "class", "sa", "nv", "ag", "se", "lp", "llc", "com",
}


class NewsError(RuntimeError):
    """The news feed could not be fetched. The message is safe to show."""


def safe_count(raw) -> int:
    """Parses a caller-supplied count; falls back to the default on bad input, caps at 20."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_COUNT
    return max(1, min(value, MAX_COUNT))


def name_keywords(ticker: str, company: str | None) -> set[str]:
    """Lower-case words a relevant headline should contain: the ticker and the core name."""
    keywords = {ticker.lower()}
    if company:
        words = [w for w in re.findall(r"[a-z0-9&]+", company.lower()) if w not in _NAME_NOISE]
        if words:
            keywords.add(" ".join(words))   # "advanced micro devices"
            # First word alone only for short names ("meta platforms" -> "meta"),
            # never for long ones ("advanced micro devices" -> not "advanced").
            if len(words) <= 2 and len(words[0]) >= 4:
                keywords.add(words[0])
    return keywords


def _mentions(title: str, keywords: set[str]) -> bool:
    t = title.lower()
    return any(re.search(r"\b" + re.escape(k) + r"\b", t) for k in keywords)


def get_news(ticker: str, company: str | None = None, count=DEFAULT_COUNT) -> list[dict]:
    """
    Returns up to `count` articles: [{title, link, published, summary}].
    Raises NewsError if the feed is unreachable.
    """
    symbol = normalise_ticker(ticker)
    if not symbol:
        raise ValueError("Missing ticker")
    count = safe_count(count)

    started = time.monotonic()
    try:
        response = requests.get(FEED_URL.format(ticker=symbol), headers=HEADERS, timeout=FEED_TIMEOUT_SECONDS)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as e:
        log_tool("news.rss", time.monotonic() - started, False, ticker=symbol)
        logger.error("RSS fetch failed for %s: %s", symbol, type(e).__name__)
        raise NewsError("News feed is unavailable, please retry") from e
    log_tool("news.rss", time.monotonic() - started, True, ticker=symbol, entries=len(feed.entries))

    if getattr(feed, "bozo", False) and not feed.entries:
        logger.error("RSS feed unreachable or malformed for %s", symbol)
        raise NewsError("News feed is unavailable, please retry")

    articles = [
        {
            "title": e.get("title", "No title"),
            "link": e.get("link", ""),
            "published": e.get("published", ""),
            "summary": (e.get("summary") or e.get("description") or "").strip() or None,
        }
        for e in feed.entries[:MAX_COUNT]
    ]
    keywords = name_keywords(symbol, company)
    relevant = [a for a in articles if _mentions(a["title"], keywords)]
    logger.info("News %s: %d/%d headlines mention %s", symbol, len(relevant), len(articles), keywords)
    return (relevant or articles)[:count]
