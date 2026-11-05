"""
Company name / ticker resolver for any US-listed stock.
Ported from v1 core/tickers.py (73b6fda); COMMON_WORDS extended (see the v2 comment there).

Source: SEC's free company_tickers.json (~10k companies, ticker + name + CIK),
cached on disk for 24 h. If SEC can't be reached (or SEC_USER_AGENT isn't set),
a small built-in map keeps NVIDIA, AMD and Intel working offline.

    resolve("apple")      -> Company(ticker="AAPL", name="Apple Inc.", cik=320193)
    find_companies("Compare NVIDIA and AMD revenue") -> [NVDA, AMD]
"""

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass

from core import sec
from core.config import CACHE_DIR
from core.market import normalise_ticker

logger = logging.getLogger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_FILE = CACHE_DIR / "company_tickers.json"
CACHE_TTL_SECONDS = 24 * 3600
MAX_COMPANIES_PER_QUERY = 3


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    cik: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# Offline fallback (and nicer display names than SEC's upper-case titles).
FALLBACK = {
    "NVDA": Company("NVDA", "NVIDIA", 1045810),
    "AMD": Company("AMD", "AMD", 2488),
    "INTC": Company("INTC", "Intel", 50863),
}
ALIASES = {
    "nvidia": "NVDA", "amd": "AMD", "advanced micro devices": "AMD", "intel": "INTC",
    "google": "GOOGL", "facebook": "META", "berkshire": "BRK-B",
}

# Upper-case words that look like tickers but are usually just words in a question.
NOT_TICKERS = {
    "A", "I", "AI", "AN", "AND", "ARE", "AS", "AT", "BE", "BY", "CEO", "CFO", "DO", "EPS", "ETF",
    "EV", "FOR", "GDP", "HAS", "HOW", "IN", "IPO", "IS", "IT", "ITS", "ME", "MY", "NEW", "NOW",
    "OF", "ON", "OR", "PE", "Q", "SEC", "SO", "THE", "TO", "UP", "US", "USA", "VS", "WHAT", "WHO",
    "WHY", "YOY", "K", "IR", "OUT", "ALL", "ANY", "CAN", "GO", "AM", "PM", "EOD",
}
# First words of company names that are also everyday English (never used alone).
COMMON_WORDS = {
    "first", "general", "american", "united", "national", "international", "global", "new",
    "bank", "energy", "capital", "financial", "the", "best", "big", "real", "royal", "standard",
    "southern", "northern", "western", "eastern", "public", "applied", "advanced", "digital",
    "data", "life", "health", "home", "world", "great", "news", "market", "stock", "price",
    "company", "trust", "value", "target", "intuitive", "booking", "visa", "match", "block",
    "compare", "latest", "recent", "what", "which", "about", "revenue", "earnings", "share",
    "shares", "news", "today", "week", "year", "quarter", "guidance", "analysis", "profit",
    "growth", "sales", "outlook", "results", "report", "ratio", "dividend", "cash", "debt",
    "income", "margin", "chip", "chips", "semiconductor", "cloud", "data", "center", "how",
    "does", "with", "and", "for", "from", "will", "this", "that", "they", "their", "them",
    # v2: everyday words that matched real tickers in the eval questions (2026-11-05 run:
    # "right" -> RTON, "total" -> SWZ, "main" -> MAIN, "business" -> BFST, "research" -> RACD).
    "right", "total", "main", "business", "research", "development", "according", "filings",
    "filing", "segment", "segments", "perform", "spend", "much", "driving", "describe", "competition",
    "highlight", "risk", "risks", "session", "last", "next", "move", "moves", "drop", "fall", "rise",
    "open", "close", "high", "higher", "lower", "most", "more", "good", "major", "customers",
    "customer", "threats", "sentiment", "factors", "identify", "assess", "evaluate", "analyze",
    "compare", "selloff", "related", "driven", "drove", "summary", "overview",
}
_NAME_NOISE = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc",
    "holdings", "holding", "group", "the", "class", "sa", "nv", "ag", "se", "lp", "llc", "com",
    "de", "del", "new",
}

_lock = threading.Lock()
_by_ticker: dict[str, Company] | None = None
_by_name: dict[str, str] | None = None


def clean_name(name: str) -> str:
    """'NVIDIA CORP' -> 'nvidia', 'Amazon.com, Inc.' -> 'amazon'."""
    words = re.findall(r"[a-z0-9&]+", name.lower())
    return " ".join(w for w in words if w not in _NAME_NOISE)


def _fetch_sec_list() -> list[dict]:
    """company_tickers.json rows, from the disk cache when fresh, else from SEC."""
    try:
        if CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < CACHE_TTL_SECONDS:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rows = list(sec.get(SEC_TICKERS_URL).json().values())
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(rows), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not cache the SEC ticker list: %s", e)
    return rows


def _load() -> tuple[dict[str, Company], dict[str, str]]:
    """Builds the ticker and name indexes once per process."""
    global _by_ticker, _by_name
    with _lock:
        if _by_ticker is not None:
            return _by_ticker, _by_name
        by_ticker: dict[str, Company] = {}
        by_name: dict[str, str] = {}
        try:
            rows = _fetch_sec_list()
        except Exception as e:  # SecError, bad JSON, no network
            logger.warning("SEC ticker list unavailable (%s); using the built-in fallback", e)
            rows = []
        # SEC lists the largest companies first, so the first match for a name wins.
        for row in rows:
            ticker = normalise_ticker(str(row.get("ticker", "")))
            if not ticker or ticker in by_ticker:
                continue
            company = Company(ticker, str(row.get("title", ticker)), int(row["cik_str"]))
            by_ticker[ticker] = company
            name = clean_name(company.name)
            if name:
                by_name.setdefault(name, ticker)
                first = name.split()[0]
                if len(first) >= 4 and first not in COMMON_WORDS:
                    by_name.setdefault(first, ticker)
        for ticker, company in FALLBACK.items():
            by_ticker[ticker] = company  # nicer names, and works offline
        for alias, ticker in ALIASES.items():
            by_name[alias] = ticker
        _by_ticker, _by_name = by_ticker, by_name
        logger.info("Ticker resolver ready: %d tickers (%s)", len(by_ticker),
                    "SEC" if rows else "fallback only")
        return _by_ticker, _by_name


def reset_cache() -> None:
    """Forget the in-memory indexes (tests)."""
    global _by_ticker, _by_name
    with _lock:
        _by_ticker, _by_name = None, None


def lookup_ticker(ticker: str) -> Company | None:
    by_ticker, _ = _load()
    return by_ticker.get(normalise_ticker(ticker))


def resolve(text: str) -> Company | None:
    """A ticker ('aapl', '$NVDA') or a company name ('Apple', 'advanced micro devices')."""
    raw = (text or "").strip().lstrip("$")
    if not raw:
        return None
    by_ticker, by_name = _load()
    company = by_ticker.get(normalise_ticker(raw))
    if company:
        return company
    ticker = by_name.get(clean_name(raw)) or by_name.get(raw.lower())
    return by_ticker.get(ticker) if ticker else None


_TICKER_SHAPE = re.compile(r"^\$?[A-Za-z]{1,5}(?:[.\-][A-Za-z]{1,2})?$")


def resolve_or_ticker(text: str) -> Company | None:
    """resolve(), or, for input shaped like a ticker ("AAPL", "brk.b"), a bare Company that
    the caller must validate with a live quote. Lets the watchlist accept any symbol even
    when SEC's company list is unavailable (no SEC_USER_AGENT)."""
    company = resolve(text)
    if company is None and _TICKER_SHAPE.match((text or "").strip()):
        symbol = normalise_ticker(text.strip().lstrip("$"))
        company = Company(symbol, symbol, None)
    return company


def find_companies(query: str, limit: int = MAX_COMPANIES_PER_QUERY) -> list[Company]:
    """
    Companies mentioned in a free-text question, in the order they appear.
    Matches $TICKER or UPPER-CASE tickers, then company names (longest phrase first).
    """
    by_ticker, by_name = _load()
    found: list[tuple[int, Company]] = []
    seen: set[str] = set()

    def add(pos: int, company: Company | None):
        if company and company.ticker not in seen:
            seen.add(company.ticker)
            found.append((pos, company))

    # 1. Tickers: "$aapl" anywhere, or an all-caps word like "NVDA".
    for m in re.finditer(r"\$([A-Za-z][A-Za-z.\-]{0,5})\b|\b([A-Z][A-Z.\-]{0,5})\b", query):
        symbol = m.group(1) or m.group(2)
        if m.group(2) and (symbol in NOT_TICKERS or len(symbol) < 2):
            continue
        add(m.start(), by_ticker.get(normalise_ticker(symbol)))

    # 2. Names: try 3-, 2- then 1-word phrases ("advanced micro devices", "apple").
    words = [(m.start(), m.group(0).lower()) for m in re.finditer(r"[A-Za-z0-9&]+(?:'s)?", query)]
    words = [(pos, w[:-2] if w.endswith("'s") else w) for pos, w in words]
    used: set[int] = set()
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            span = range(i, i + size)
            if any(j in used for j in span):
                continue
            phrase = " ".join(words[j][1] for j in span)
            if size == 1 and (phrase in COMMON_WORDS or len(phrase) < 3):
                continue
            ticker = by_name.get(phrase)
            if ticker:
                used.update(span)
                add(words[i][0], by_ticker.get(ticker))

    found.sort(key=lambda item: item[0])
    return [company for _, company in found[:limit]]
