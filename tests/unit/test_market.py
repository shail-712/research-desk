"""Market data: batched Yahoo quotes, retries, safe errors (ported from v1 tests/unit/test_market.py),
plus the Finnhub fallback, `source` and `trading_date`. No network."""

from unittest import mock

import numpy as np
import pandas as pd
import pytest

from core import market

# 2026-11-04 20:00 UTC = 15:00 New York (session of 2026-11-04);
# 2026-11-05 03:00 UTC = 22:00 New York on 2026-11-04 (still that session's date).
T_SESSION = 1793822400
T_LATE = 1793847600


def _frame(closes: dict[str, list[float]]) -> pd.DataFrame:
    index = pd.to_datetime(["2026-10-30", "2026-11-02"])
    cols = pd.MultiIndex.from_product([list(closes), ["Open", "Close"]])
    data = np.column_stack([v for vals in closes.values() for v in (vals, vals)])
    return pd.DataFrame(data, index=index, columns=cols)


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise market.requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def finnhub(monkeypatch):
    """Fake Finnhub: per-symbol quote / profile2 / metric payloads; records calls."""
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-test-key-123")
    monkeypatch.setattr(market.time, "sleep", lambda s: None)
    data = {
        ("quote", "NVDA"): {"c": 210.5, "pc": 200.0, "o": 201.0, "h": 212.0, "l": 199.0, "t": T_SESSION},
        ("quote", "QQQ"): {"c": 500.0, "pc": 505.0, "o": 504.0, "h": 506.0, "l": 499.0, "t": T_LATE},
        ("quote", "XYZ"): {"c": 0, "pc": 0, "t": 0},
        ("stock/profile2", "NVDA"): {"marketCapitalization": 5_100_000.0},
        ("stock/metric", "NVDA"): {"metric": {"peTTM": 58.3}},
        ("stock/profile2", "QQQ"): _Resp({}, 403),
        ("stock/metric", "QQQ"): _Resp({}, 403),
    }
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        path = url.split("/api/v1/")[1]
        calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        payload = data.get((path, params["symbol"]), {})
        return payload if isinstance(payload, _Resp) else _Resp(payload)

    monkeypatch.setattr(market.requests, "get", get)
    return calls


def _yahoo_down():
    return mock.patch.object(market.yf, "download", side_effect=ConnectionError("blocked"))


# ─── v1 behaviour (ported) ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [(" nvda ", "NVDA"), ("brk.b", "BRK-B"), ("", "")])
def test_normalise_ticker(raw, expected):
    assert market.normalise_ticker(raw) == expected


def test_get_quotes_computes_day_change_from_one_download():
    frame = _frame({"NVDA": [200.0, 210.0], "XYZ": [np.nan, np.nan]})
    with mock.patch.object(market.yf, "download", return_value=frame) as download:
        quotes = market.get_quotes(["nvda", "XYZ", "NVDA"])
    download.assert_called_once()
    assert download.call_args.args[0] == ["NVDA", "XYZ"]  # deduplicated, one call
    q = quotes["NVDA"]
    assert (q["price"], q["prev_close"], q["change"], q["change_pct"]) == (210.0, 200.0, 10.0, 5.0)
    assert q["trading_date"] == "2026-11-02" and q["source"] == "yfinance"
    assert "error" in quotes["XYZ"]


def test_get_quotes_retries_then_raises_safe_error_without_fallback():
    with _yahoo_down(), mock.patch.object(market.time, "sleep") as sleep:
        with pytest.raises(market.MarketDataError):
            market.get_quotes(["NVDA"])
    assert sleep.call_count == market.RETRIES - 1


def test_get_quotes_empty_input():
    assert market.get_quotes(["", "  "]) == {}


def test_get_quote_survives_pe_failure():
    ticker = mock.MagicMock()
    ticker.fast_info = {"lastPrice": 100.0, "previousClose": 97.0, "regularMarketPreviousClose": 98.0,
                        "open": 99.0, "dayHigh": 101.0, "dayLow": 97.0, "lastVolume": 5e6, "marketCap": 1e11}
    type(ticker).info = mock.PropertyMock(side_effect=ValueError("blocked"))
    ticker.history.return_value = pd.DataFrame({"Close": [99.0, 100.0]},
                                               index=pd.to_datetime(["2026-11-03", "2026-11-04"]))
    with mock.patch.object(market.yf, "Ticker", return_value=ticker):
        q = market.get_quote("aapl")
    assert q["ticker"] == "AAPL" and q["price"] == 100.0 and q["pe_ratio"] is None
    assert q["change_pct"] == 2.04
    assert q["source"] == "yfinance" and q["trading_date"] == "2026-11-04"


def test_get_quote_without_price_is_an_error():
    ticker = mock.MagicMock()
    ticker.fast_info = {"lastPrice": None}
    with mock.patch.object(market.yf, "Ticker", return_value=ticker), mock.patch.object(market.time, "sleep"):
        with pytest.raises(market.MarketDataError):
            market.get_quote("XYZ123")


# ─── Finnhub fallback (SPEC F1, DECISIONS D2) ───────────────────────────────

def test_get_quote_falls_back_to_finnhub_when_yahoo_fails(finnhub):
    with mock.patch.object(market.yf, "Ticker", side_effect=ConnectionError("blocked")):
        q = market.get_quote("nvda")
    assert q["source"] == "finnhub"
    assert (q["price"], q["prev_close"], q["change"], q["change_pct"]) == (210.5, 200.0, 10.5, 5.25)
    assert q["market_cap"] == 5.1e12 and q["pe_ratio"] == 58.3
    assert q["trading_date"] == "2026-11-04"
    # The key travels in a header, never in the URL or the query string.
    assert all("fh-test-key-123" not in c["url"] and "fh-test-key-123" not in str(c["params"]) for c in finnhub)
    assert all(c["headers"]["X-Finnhub-Token"] == "fh-test-key-123" and c["timeout"] for c in finnhub)


def test_get_quotes_falls_back_per_ticker_when_the_download_fails(finnhub):
    with _yahoo_down():
        quotes = market.get_quotes(["NVDA", "QQQ", "XYZ"])
    assert quotes["NVDA"]["source"] == "finnhub" and quotes["NVDA"]["change_pct"] == 5.25
    assert quotes["QQQ"]["source"] == "finnhub" and quotes["QQQ"]["trading_date"] == "2026-11-04"
    assert "error" in quotes["XYZ"] and "Finnhub" in quotes["XYZ"]["error"]


def test_get_quotes_falls_back_only_for_tickers_yahoo_left_empty(finnhub):
    frame = _frame({"AMD": [100.0, 101.0], "NVDA": [np.nan, np.nan]})
    with mock.patch.object(market.yf, "download", return_value=frame):
        quotes = market.get_quotes(["AMD", "NVDA"])
    assert quotes["AMD"]["source"] == "yfinance" and quotes["NVDA"]["source"] == "finnhub"
    assert {c["params"]["symbol"] for c in finnhub} == {"NVDA"}


def test_etf_quote_works_without_profile_or_metrics(finnhub):
    with mock.patch.object(market.yf, "Ticker", side_effect=ConnectionError("blocked")):
        q = market.get_quote("QQQ")
    assert q["source"] == "finnhub" and q["price"] == 500.0 and q["change_pct"] == -0.99
    assert q["market_cap"] is None and q["pe_ratio"] is None


def test_unknown_ticker_fails_on_both_providers(finnhub):
    with mock.patch.object(market.yf, "Ticker", side_effect=ConnectionError("blocked")):
        with pytest.raises(market.MarketDataError, match="unavailable"):
            market.get_quote("XYZ")


def test_finnhub_rate_limit_is_retried(finnhub, monkeypatch):
    replies = iter([_Resp({}, 429), _Resp({"c": 10.0, "pc": 9.0, "t": T_SESSION})])
    monkeypatch.setattr(market.requests, "get", lambda *a, **k: next(replies))
    with _yahoo_down():
        q = market.get_quotes(["AMD"])["AMD"]
    assert q["source"] == "finnhub" and q["price"] == 10.0


def test_finnhub_bad_key_is_not_retried(finnhub, monkeypatch):
    calls = []
    monkeypatch.setattr(market.requests, "get", lambda *a, **k: calls.append(1) or _Resp({}, 401))
    with _yahoo_down():
        q = market.get_quotes(["AMD"])["AMD"]
    assert "error" in q and len(calls) == 1


def test_benchmarks_are_qqq_and_spy():
    frame = _frame({"QQQ": [500.0, 505.0], "SPY": [600.0, 594.0]})
    with mock.patch.object(market.yf, "download", return_value=frame):
        b = market.get_benchmarks()
    assert set(b) == {"QQQ", "SPY"} and b["SPY"]["change_pct"] == -1.0


def test_ny_date_uses_new_york_time():
    assert market._ny_date(T_SESSION) == "2026-11-04"
    assert market._ny_date(T_LATE) == "2026-11-04"
    assert market._ny_date(0) is None and market._ny_date(None) is None
