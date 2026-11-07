"""
Live market data: Yahoo Finance (yfinance, no key) first, Finnhub as the fallback (DECISIONS D2).

- get_quotes(tickers): ONE batched Yahoo download for the whole watchlist (price, previous close,
  day % change, trading date). Tickers Yahoo can't serve (blocked, throttled, empty) are fetched
  from Finnhub one by one. Used by the alert scan and GET /quotes.
- get_quote(ticker): richer detail for one ticker (open/high/low, volume, market cap, P/E). Used by
  the agents. Same fallback.
- get_benchmarks(): QQQ and SPY, for "moved with the market" and market-conditions questions.

Every quote carries `source` ("yfinance" or "finnhub") and `trading_date`: the New York date of the
session the price belongs to. The scan skips price rules when it isn't today (D12). A ticker nobody
can serve comes back as {ticker, error} instead of failing the whole batch.

Finnhub's free tier has current quotes only (no candles), so there is no Finnhub history here.
Adapted from v1 core/market.py (73b6fda): Finnhub fallback, `source`, `trading_date` (was
`market_date`), benchmarks; `get_weekly_changes` dropped with the digest (D3).
"""

import logging
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

from core.config import env
from core.log import log_tool

logger = logging.getLogger(__name__)

RETRIES = 2                 # Yahoo attempts before falling back (Yahoo blocks are usually not transient)
YAHOO_TIMEOUT = 10
FINNHUB_URL = "https://finnhub.io/api/v1"
FINNHUB_TIMEOUT = 8
FINNHUB_RETRIES = 2
BENCHMARKS = ("QQQ", "SPY")
NEW_YORK = ZoneInfo("America/New_York")


class MarketDataError(RuntimeError):
    """Market data could not be fetched. The message is safe to show."""


# ─── Caller's deadline (review A, S3) ───────────────────────────────────────
# Deep mode's tools run under a time budget. The deadline travels in a contextvar (set per tool
# call by core.agents.tools.run_tool), so optional calls (Finnhub market cap / P/E) are skipped
# when there isn't time for them. Without a deadline everything runs as before.

_deadline: ContextVar[float | None] = ContextVar("market_deadline", default=None)
OPTIONAL_CALL_MIN_SECONDS = FINNHUB_TIMEOUT + 2   # one Finnhub request plus a little slack


@contextmanager
def deadline(at: float | None):
    """Within this block, seconds_left() counts down to `at` (time.monotonic())."""
    token = _deadline.set(at)
    try:
        yield
    finally:
        _deadline.reset(token)


def seconds_left() -> float | None:
    at = _deadline.get()
    return None if at is None else at - time.monotonic()


def _time_for_optional_call() -> bool:
    left = seconds_left()
    return left is None or left >= OPTIONAL_CALL_MIN_SECONDS


def _num(value):
    """float(value), or None for missing/NaN values."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _with_retry(fn, what: str, retries: int = RETRIES):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                logger.warning("%s failed after %d tries: %s", what, retries, type(e).__name__)
                raise MarketDataError(f"Market data is unavailable for {what}, please retry") from e
            wait = 2 ** attempt
            logger.warning("%s failed (%s), retrying in %ss", what, type(e).__name__, wait)
            time.sleep(wait)


def normalise_ticker(ticker: str) -> str:
    """'brk.b ' -> 'BRK-B' (Yahoo uses '-' for share classes)."""
    return (ticker or "").strip().upper().replace(".", "-")


def _round(x, n=2):
    return round(x, n) if x is not None else None


# ─── Finnhub (fallback) ─────────────────────────────────────────────────────

def finnhub_configured() -> bool:
    return bool(env("FINNHUB_API_KEY"))


def _finnhub_get(path: str, **params) -> dict:
    """GET a Finnhub endpoint with a timeout; retries 429/5xx/connection errors with backoff."""
    key = env("FINNHUB_API_KEY")
    if not key:
        raise MarketDataError("Finnhub is not configured (FINNHUB_API_KEY)")

    def fetch():
        # The key goes in a header, so it never appears in a URL or a logged exception.
        r = requests.get(f"{FINNHUB_URL}/{path}", params=params, headers={"X-Finnhub-Token": key},
                         timeout=FINNHUB_TIMEOUT)
        if r.status_code in (401, 403):
            raise PermissionError(f"Finnhub HTTP {r.status_code}")
        r.raise_for_status()
        return r.json() or {}

    for attempt in range(1, FINNHUB_RETRIES + 1):
        try:
            return fetch()
        except PermissionError as e:  # bad key or a symbol the free plan doesn't cover: no retry
            raise MarketDataError(f"Finnhub refused the request ({e})") from e
        except Exception as e:
            if attempt == FINNHUB_RETRIES:
                raise MarketDataError(f"Finnhub is unavailable ({type(e).__name__})") from e
            time.sleep(2 ** attempt)


def _ny_date(unix_seconds) -> str | None:
    ts = _num(unix_seconds)
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(NEW_YORK).date().isoformat()


def _finnhub_quote(symbol: str) -> dict:
    """{ticker, price, prev_close, change, change_pct, open, high, low, trading_date, source, fetched_at}."""
    started = time.monotonic()
    try:
        q = _finnhub_get("quote", symbol=symbol)
        price = _num(q.get("c"))
        if not price:  # Finnhub answers unknown symbols with zeros
            raise MarketDataError(f"No price data for {symbol} (unknown or delisted ticker?)")
    except MarketDataError:
        log_tool("finnhub.quote", time.monotonic() - started, False, ticker=symbol)
        raise
    log_tool("finnhub.quote", time.monotonic() - started, True, ticker=symbol)
    prev = _num(q.get("pc"))
    change = price - prev if prev else None
    return {
        "ticker": symbol,
        "price": _round(price),
        "prev_close": _round(prev),
        "change": _round(change),
        "change_pct": _round(change / prev * 100) if change is not None else None,
        "open": _num(q.get("o")) or None,
        "high": _num(q.get("h")) or None,
        "low": _num(q.get("l")) or None,
        "trading_date": _ny_date(q.get("t")),
        "source": "finnhub",
        "fetched_at": _now_iso(),
    }


def _finnhub_detail(symbol: str) -> dict:
    """Finnhub quote + market cap + P/E. Cap and P/E are optional (ETFs have neither)."""
    quote = _finnhub_quote(symbol)
    market_cap = pe_ratio = None
    if _time_for_optional_call():
        try:
            cap_millions = _num(_finnhub_get("stock/profile2", symbol=symbol).get("marketCapitalization"))
            market_cap = cap_millions * 1e6 if cap_millions else None
        except MarketDataError as e:
            logger.info("Finnhub market cap unavailable for %s: %s", symbol, e)
    else:
        logger.info("Finnhub market cap skipped for %s: little time left", symbol)
    if _time_for_optional_call():
        try:
            metrics = _finnhub_get("stock/metric", symbol=symbol, metric="all").get("metric") or {}
            pe_ratio = _num(metrics.get("peTTM")) or _num(metrics.get("peBasicExclExtraTTM"))
        except MarketDataError as e:
            logger.info("Finnhub P/E unavailable for %s: %s", symbol, e)
    else:
        logger.info("Finnhub P/E skipped for %s: little time left", symbol)
    return {**quote, "volume": None, "market_cap": market_cap, "pe_ratio": pe_ratio}


def _fallback(symbol: str, reason: str, detail: bool = False) -> dict:
    """Finnhub for one symbol after Yahoo failed; {ticker, error} if that fails too."""
    if not finnhub_configured():
        return {"ticker": symbol, "error": f"{reason}; no fallback configured"}
    logger.warning("Quote fallback to Finnhub for %s: %s", symbol, reason, extra={"quote_fallback": True})
    try:
        return _finnhub_detail(symbol) if detail else _finnhub_quote(symbol)
    except MarketDataError as e:
        return {"ticker": symbol, "error": f"{reason}; Finnhub: {e}"}


# ─── Public API ─────────────────────────────────────────────────────────────

def get_quotes(tickers: list[str]) -> dict[str, dict]:
    """
    Returns {ticker: quote} for every requested ticker.
    quote = {ticker, price, prev_close, change, change_pct, trading_date, source, fetched_at}
    or {ticker, error} when neither Yahoo nor Finnhub had data for it.
    """
    symbols = list(dict.fromkeys(normalise_ticker(t) for t in tickers if t and t.strip()))
    if not symbols:
        return {}

    def download():
        return yf.download(
            symbols, period="5d", interval="1d", group_by="ticker",
            auto_adjust=False, progress=False, threads=True, timeout=YAHOO_TIMEOUT,
        )

    started = time.monotonic()
    try:
        frame = _with_retry(download, ", ".join(symbols))
        log_tool("yfinance.download", time.monotonic() - started, True, tickers=len(symbols))
    except MarketDataError as e:
        log_tool("yfinance.download", time.monotonic() - started, False, tickers=len(symbols))
        if not finnhub_configured():
            raise
        return {s: _fallback(s, str(e)) for s in symbols}

    fetched_at = _now_iso()
    quotes: dict[str, dict] = {}
    for symbol in symbols:
        try:
            closes = frame[symbol]["Close"].dropna()
        except (KeyError, TypeError):
            closes = []
        if len(closes) == 0:
            quotes[symbol] = _fallback(symbol, "No price data from Yahoo (unknown or delisted ticker?)")
            continue

        price = _num(closes.iloc[-1])
        prev = _num(closes.iloc[-2]) if len(closes) > 1 else None
        change = price - prev if price is not None and prev else None
        quotes[symbol] = {
            "ticker": symbol,
            "price": _round(price),
            "prev_close": _round(prev),
            "change": _round(change),
            "change_pct": _round(change / prev * 100) if change is not None else None,
            "trading_date": closes.index[-1].strftime("%Y-%m-%d"),
            "source": "yfinance",
            "fetched_at": fetched_at,
        }
    return quotes


def _yahoo_trading_date(tk) -> str | None:
    """Date of the latest daily bar (Yahoo's index is the exchange-local date). Non-fatal."""
    try:
        hist = tk.history(period="5d", interval="1d", auto_adjust=False, timeout=YAHOO_TIMEOUT)
        return hist.index[-1].strftime("%Y-%m-%d") if len(hist.index) else None
    except Exception as e:
        logger.info("Trading date lookup failed (non-fatal): %s", type(e).__name__)
        return None


def get_quote(ticker: str) -> dict:
    """Detailed quote for one ticker. Raises MarketDataError if neither provider has it."""
    symbol = normalise_ticker(ticker)
    if not symbol:
        raise ValueError("Missing ticker")

    def fetch():
        tk = yf.Ticker(symbol)
        fi = tk.fast_info
        price = _num(fi.get("lastPrice"))
        if price is None:
            raise MarketDataError(f"No price data for {symbol}")
        return tk, {
            "price": price,
            # previousClose can include after-hours trading; the regular-session
            # close matches get_quotes() and what Yahoo shows as the day change.
            "prev_close": _num(fi.get("regularMarketPreviousClose")) or _num(fi.get("previousClose")),
            "open": _num(fi.get("open")),
            "high": _num(fi.get("dayHigh")),
            "low": _num(fi.get("dayLow")),
            "volume": _num(fi.get("lastVolume")),
            "market_cap": _num(fi.get("marketCap")),
        }

    started = time.monotonic()
    try:
        tk, data = _with_retry(fetch, symbol)
        log_tool("yfinance.quote", time.monotonic() - started, True, ticker=symbol)
    except MarketDataError as e:
        log_tool("yfinance.quote", time.monotonic() - started, False, ticker=symbol)
        q = _fallback(symbol, str(e), detail=True)
        if "error" in q:
            raise MarketDataError(f"Market data is unavailable for {symbol}, please retry") from e
        return q

    # P/E is only in the slower .info call; a failure here must not lose the quote.
    pe_ratio = None
    try:
        pe_ratio = _num(tk.info.get("trailingPE"))
    except Exception as e:
        logger.warning("P/E lookup failed for %s (non-fatal): %s", symbol, type(e).__name__)

    prev = data["prev_close"]
    change_pct = round((data["price"] - prev) / prev * 100, 2) if prev else None
    # Same precision as get_quotes() and the Finnhub path (Yahoo returns 691.9500122070312).
    data = {**data, **{k: _round(data[k]) for k in ("price", "prev_close", "open", "high", "low")},
            "volume": _round(data["volume"], 0), "market_cap": _round(data["market_cap"], 0)}
    return {"ticker": symbol, **data, "pe_ratio": _round(pe_ratio), "change_pct": change_pct,
            "trading_date": _yahoo_trading_date(tk), "source": "yfinance", "fetched_at": _now_iso()}


def get_benchmarks() -> dict[str, dict]:
    """QQQ and SPY quotes (market conditions, "moved with the market")."""
    return get_quotes(list(BENCHMARKS))
