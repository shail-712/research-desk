"""Ticker resolver: names and tickers from a fake SEC list, plus offline fallback."""

import pytest

from core import sec, tickers


@pytest.mark.parametrize("text, expected", [
    ("Apple", "AAPL"), ("apple", "AAPL"), ("aapl", "AAPL"), ("$NVDA", "NVDA"), ("nvidia", "NVDA"),
    ("Advanced Micro Devices", "AMD"), ("microsoft", "MSFT"), ("Amazon", "AMZN"), ("brk.b", "BRK-B"),
    ("berkshire", "BRK-B"),
])
def test_resolve_names_and_tickers(sec_tickers, text, expected):
    assert sec_tickers.resolve(text).ticker == expected


def test_resolve_unknown_returns_none(sec_tickers):
    assert sec_tickers.resolve("not a company at all") is None
    assert sec_tickers.resolve("") is None


def test_fallback_names_are_friendlier_than_sec_titles(sec_tickers):
    assert sec_tickers.resolve("NVDA").name == "NVIDIA"
    assert sec_tickers.resolve("AAPL").name == "Apple Inc."
    assert sec_tickers.resolve("AAPL").cik == 320193


@pytest.mark.parametrize("query, expected", [
    ("What is Apple's stock price and latest news?", ["AAPL"]),
    ("Compare NVIDIA and AMD revenue", ["NVDA", "AMD"]),
    ("How does AMD compete with Intel?", ["AMD", "INTC"]),
    ("Is MSFT a buy vs $amzn?", ["MSFT", "AMZN"]),
    ("What is the stock price?", []),
    ("What is the target price for the first quarter?", []),   # common words, not TGT / FRST
    ("What did the CEO say about EPS in the US?", []),          # upper-case non-tickers
    ("Tell me about advanced micro devices", ["AMD"]),
])
def test_find_companies(sec_tickers, query, expected):
    assert [c.ticker for c in sec_tickers.find_companies(query)] == expected


def test_find_companies_caps_at_three(sec_tickers):
    found = sec_tickers.find_companies("nvidia apple microsoft amazon")
    assert [c.ticker for c in found] == ["NVDA", "AAPL", "MSFT"]


def test_offline_fallback_keeps_original_three(monkeypatch):
    def offline():
        raise sec.SecError("no network")
    monkeypatch.setattr(tickers, "_fetch_sec_list", offline)
    tickers.reset_cache()
    assert [c.ticker for c in tickers.find_companies("Compare nvidia, AMD and Intel")] == ["NVDA", "AMD", "INTC"]
    assert tickers.resolve("Apple") is None


def test_sec_list_is_cached_on_disk(monkeypatch, tmp_path):
    calls = []

    class Response:
        def json(self):
            return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

    def fake_get(url):
        calls.append(url)
        return Response()

    monkeypatch.setattr(sec, "get", fake_get)
    assert tickers._fetch_sec_list()[0]["ticker"] == "AAPL"
    assert tickers._fetch_sec_list()[0]["ticker"] == "AAPL"
    assert len(calls) == 1
    assert tickers.CACHE_FILE.exists()


def test_sec_client_refuses_without_contact_email(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "no email here")
    with pytest.raises(sec.SecError, match="SEC_USER_AGENT"):
        sec.get("https://www.sec.gov/files/company_tickers.json")


@pytest.mark.parametrize("query, expected", [
    ("What is NVIDIA's stock price right now?", ["NVDA"]),            # not RTON ("Right On Brands")
    ("How much did AMD spend on research and development?", ["AMD"]),
])
def test_everyday_words_are_not_companies(sec_tickers, query, expected):
    assert [c.ticker for c in sec_tickers.find_companies(query)] == expected
