"""
Knowledge-base ingest: SEC filing → sections → ~300-token chunks → cached embeddings → FAISS index.

    python -m core.ingest NVDA AMD INTC --dry-run     # what would be indexed, and what it would cost
    python -m core.ingest NVDA AMD INTC               # build / extend data/index/index.npz

For each ticker it takes the **latest filed** 10-K, 10-Q and earnings 8-K (Item 2.02; the EX-99
press release) from SEC's submissions feed (SPEC F4; "NVIDIA's Q3" = latest filed quarter).
Only the sections research questions need are indexed:
  10-K  Item 1 Business (incl. competition, customers), Item 1A Risk Factors, Item 7 MD&A
  10-Q  Item 2 MD&A, Part II Item 1A Risk Factors
  both  the notes on segment information and concentration of revenue / customers
  8-K   the earnings press release (EX-99.1)

Embeddings are paid for once (D11): every chunk has a `chunk_hash` (accession + section + text);
hashes already in the index are skipped, and the rest go through the embedding cache, so only
text never embedded before costs API quota. API texts are capped per Pacific day
(FILING_EMBED_DAILY_CAP, the Gemini quota day) and paced under 30k tokens/minute; the index is
saved after every batch, so an interrupted build resumes where it stopped. This is also the
manual ingest for new watchlist tickers or new filings (auto-ingest is cut, D13).

Rewritten for v2. Reused from v1 (73b6fda): core/filings.py `html_to_text` (_TextExtractor),
`chunk_words`, the EX-99 exhibit lookup, and the token-based batching and pacing of `index_pending`.
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from core import config, embeddings, sec, tickers, vectors
from core.config import env_int

logger = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{doc}"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index.html"

# ~300 tokens with 50 overlap (SPEC F4), at the ~1.4 tokens/word of embeddings.estimate_tokens.
CHUNK_WORDS, CHUNK_OVERLAP = 215, 36
MAX_DOC_CHARS = 2_000_000
NOTE_WINDOW_CHARS = 4_000        # text kept after a note heading
NOTE_MAX_CHARS = 16_000          # per note type, after merging windows
FILING_EMBED_DAILY_CAP = env_int("FILING_EMBED_DAILY_CAP", 400)
EMBED_TOKENS_PER_BATCH = env_int("EMBED_TOKENS_PER_BATCH", 12_000)   # free tier: 30k tokens/minute
SECONDS_BETWEEN_BATCHES = 62
RATE_LIMIT_RETRIES = 3


@dataclass
class Filing:
    ticker: str
    company: str
    cik: int
    form: str
    accession: str
    filed: str
    period: str
    primary_doc: str
    items: str = ""

    @property
    def folder(self) -> str:
        return self.accession.replace("-", "")

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(cik=self.cik, folder=self.folder, doc=self.primary_doc)

    @property
    def label(self) -> str:
        return f"{self.ticker} {self.form} (period {self.period}, filed {self.filed})"


# ─── SEC feed ───────────────────────────────────────────────────────────────

def is_earnings_8k(form: str, items: str) -> bool:
    return form == "8-K" and "2.02" in (items or "")


def latest_filings(ticker: str, cik: int, company: str) -> list[Filing]:
    """The latest filed 10-K, 10-Q and earnings 8-K (any that exist), from SEC submissions."""
    recent = sec.get(SUBMISSIONS_URL.format(cik=int(cik))).json().get("filings", {}).get("recent", {})
    picked: dict[str, Filing] = {}
    forms, items = recent.get("form", []), recent.get("items") or []
    for i, form in enumerate(forms):  # newest first
        item = items[i] if i < len(items) else ""
        if form in ("10-K", "10-Q"):
            kind = form
        elif is_earnings_8k(form, item):
            kind = "8-K"
        else:
            continue
        if kind in picked:
            continue
        picked[kind] = Filing(ticker=ticker, company=company, cik=int(cik), form=form,
                              accession=recent["accessionNumber"][i], filed=recent["filingDate"][i],
                              period=(recent.get("reportDate") or [""] * len(forms))[i] or recent["filingDate"][i],
                              primary_doc=recent["primaryDocument"][i], items=item or "")
        if len(picked) == 3:
            break
    return [picked[k] for k in ("10-K", "10-Q", "8-K") if k in picked]


# ─── Documents (from v1 core/filings.py) ────────────────────────────────────

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title", "ix:header"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
    CELL = {"td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in self.CELL:
            self.parts.append(" ")  # v2: keep table cells apart ("Data Center" "41,096")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def press_release_text(f: Filing) -> str:
    """An earnings 8-K's EX-99.1 press release (the 8-K body itself is boilerplate). Exhibit file
    names vary (q2fy27pr.htm, q32026991.htm), so the type comes from the filing index page."""
    index_html = sec.get(FILING_INDEX_URL.format(cik=f.cik, folder=f.folder, accession=f.accession)).text
    docs = re.findall(r'href="([^"]+\.html?)"[^<]*</a>\s*</td>\s*<td[^>]*>\s*(EX-99[.\d]*)\s*</td>', index_html, re.I)
    docs.sort(key=lambda d: (d[1].upper() != "EX-99.1", d[1]))
    if not docs:
        return ""
    href = docs[0][0]
    url = href if href.startswith("http") else "https://www.sec.gov" + href
    return html_to_text(sec.get(url.replace("/ix?doc=", "")).text)


def document_text(f: Filing) -> str:
    if f.form == "8-K":
        text = press_release_text(f)
        if text:
            return text[:MAX_DOC_CHARS]
        logger.warning("No EX-99 press release found for %s; using the 8-K body", f.accession)
    return html_to_text(sec.get(f.url).text)[:MAX_DOC_CHARS]


# ─── Sections ───────────────────────────────────────────────────────────────
# Headings are matched as whole lines, so in-text references ("see Item 7, Management's
# Discussion…") never start a section. A heading with no end heading after it is ignored.
# The longest span wins, which skips table-of-contents entries. Intel's filings don't use
# "Item N." headings in the body (only a cross-reference index), so each section also has
# title-heading alternatives.

_APOS = r"[’'`]?"
_MDA_TITLE = rf"management{_APOS}s\s+discussion\s+and\s+analysis"


def _line(pat: str) -> re.Pattern:
    return re.compile(rf"^[ \t]*(?:{pat})[^\n]{{0,120}}$", re.I | re.M)


def _item(num: str, title: str = "") -> str:
    return rf"item\s*{num}\b\s*[.:\-–—]?\s*{title}"


# form → [(section name, [(start, end), …alternatives])]
ITEM_SECTIONS = {
    "10-K": [
        ("Item 1. Business", [(_item("1", "business"), _item("1a")),
                              (r"overview\s+and\s+our\s+strategy$|our\s+business$", _MDA_TITLE + r"(?:\s*\(md&a\))?$")]),
        ("Item 1A. Risk Factors", [(_item("1a", r"risk\s+factors"), rf"{_item('1b')}|{_item('1c')}|{_item('2')}"),
                                   (r"risk\s+factors(?:\s+and\s+other\s+key\s+information)?$",
                                    r"other\s+key\s+information$|financial\s+statements\s+and\s+supplement")]),
        ("Item 7. MD&A", [(_item("7", _MDA_TITLE), _item("7a")),
                          (_MDA_TITLE + r"(?:\s*\(md&a\))?$", r"risk\s+factors(?:\s+and\s+other\s+key\s+information)?$")]),
    ],
    "10-Q": [
        ("Part I, Item 2. MD&A", [(_item("2", _MDA_TITLE), _item("3")),
                                 (_MDA_TITLE + r"(?:\s*\(md&a\))?$",
                                  r"risk\s+factors(?:\s+and\s+other\s+key\s+information)?$|quantitative\s+and\s+qualitative")]),
        ("Part II, Item 1A. Risk Factors", [(_item("1a", r"risk\s+factors"), rf"{_item('2')}|{_item('5')}|{_item('6')}"),
                                            (r"risk\s+factors$", r"quantitative\s+and\s+qualitative|controls\s+and\s+procedures$")]),
    ],
}
NOTE_PATTERNS = [
    ("Note: Segment Information", _line(r"(?:note\s*\d+\s*[.:\-–—]?\s*)?segment\s+information$")),
    ("Note: Concentration of Revenue and Customers", re.compile(
        r"^[ \t]*concentration\s+of\s+(?:revenue|credit\s+risk|customers?)[^\n]{0,60}$|"
        r"customers?\s+(?:that|which|who)\s+(?:each\s+)?(?:accounted|represented)\s+(?:for\s+)?(?:more\s+than\s+)?10\s*%|"
        r"\b(?:direct|indirect)\s+customer\s+[A-Z]\b", re.I | re.M)),
]
MIN_SECTION_CHARS = 1500
MAX_SECTION_CHARS = 300_000


def _longest_span(text: str, start_pat: str, end_pat: str) -> tuple[int, int]:
    best = (0, 0)
    end_re = _line(end_pat)
    for m in _line(start_pat).finditer(text):
        end = end_re.search(text, m.end())
        if not end:
            continue
        stop = min(end.start(), m.start() + MAX_SECTION_CHARS)
        if stop - m.start() > best[1] - best[0]:
            best = (m.start(), stop)
    return best


def _note_windows(text: str, pattern: re.Pattern, skip_before: int) -> str:
    """Merged windows after each match of a note pattern (past the table of contents)."""
    spans: list[list[int]] = []
    for m in pattern.finditer(text, skip_before):
        s, e = max(0, m.start() - 200), min(len(text), m.start() + NOTE_WINDOW_CHARS)
        if spans and s <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([s, e])
    return "\n".join(text[s:e] for s, e in spans)[:NOTE_MAX_CHARS]


def sections(form: str, text: str) -> list[tuple[str, str]]:
    """[(section name, text)] worth indexing for this form."""
    if form == "8-K":
        return [("Earnings press release (EX-99.1)", text)] if text.strip() else []
    out: list[tuple[str, str]] = []
    for name, alternatives in ITEM_SECTIONS.get(form, []):
        for start, end in alternatives:
            s, e = _longest_span(text, start, end)
            if e - s >= MIN_SECTION_CHARS:
                out.append((name, text[s:e]))
                break
    # Notes are labelled sections of their own, even when an item above also contains them
    # (NVIDIA's "Concentration of Revenue" sits inside its 10-Q MD&A): retrieval can then
    # cite the note by name. The extra cost is a few chunks per filing.
    toc_end = min(len(text) // 20, 20_000)
    for name, pattern in NOTE_PATTERNS:
        body = _note_windows(text, pattern, toc_end)
        if len(body) >= 300:
            out.append((name, body))
    if not out:  # unusual layout: index the whole document rather than nothing
        out.append(("Full document", text[:MAX_SECTION_CHARS]))
    return out


def chunk_words(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fixed-size word windows with overlap, so context isn't cut at chunk edges (from v1)."""
    words = text.split()
    if not words:
        return []
    step = max(1, size - overlap)
    return [" ".join(words[i:i + size]) for i in range(0, max(1, len(words) - overlap), step)]


# Embedding order when the daily cap can't cover everything: what demo questions need first
# (latest results, customer concentration, segments, competition), repeated risk factors last.
SECTION_PRIORITY = [("Earnings press release", 0), ("Part I, Item 2. MD&A", 1), ("Note:", 1),
                    ("Item 1. Business", 2), ("Item 7. MD&A", 3), ("Item 1A. Risk Factors", 4),
                    ("Part II, Item 1A. Risk Factors", 5)]


def priority(section: str) -> int:
    for prefix, rank in SECTION_PRIORITY:
        if section.startswith(prefix):
            return rank
    return 6


def order_key(row: dict, focus: list[str] | None = None) -> tuple[int, int]:
    """Embedding order: the focus tickers' chunks first (all their sections), then everyone
    else's; each group by section priority. `--focus NVDA` makes the demo company complete
    before the others' risk factors when the day's quota may not cover everything."""
    focused = {t.upper() for t in focus or []}
    return (0 if row["ticker"] in focused else 1, priority(row["section"]))


def chunk_hash(f: Filing, section: str, text: str) -> str:
    return hashlib.sha256(f"{f.accession}|{section}|{text}".encode("utf-8")).hexdigest()


def filing_rows(f: Filing, text: str) -> tuple[list[dict], dict[str, int]]:
    """Index rows for one filing, and chunk counts per section."""
    rows, counts = [], {}
    for section, body in sections(f.form, text):
        chunks = chunk_words(body)
        counts[section] = len(chunks)
        for c in chunks:
            rows.append({"chunk_hash": chunk_hash(f, section, c), "ticker": f.ticker, "company": f.company,
                         "form": f.form, "accession": f.accession, "filed": f.filed, "period": f.period,
                         "url": f.url, "section": section,
                         "source": f"{f.ticker} {f.form} (period {f.period}) — {section}", "text": c})
    return rows, counts


# ─── Embedding budget ───────────────────────────────────────────────────────

def _quota_file():
    return config.EMBED_CACHE_PATH.parent / "embed_quota.json"


def _quota_day() -> str:
    # Gemini's daily quota resets at midnight Pacific time.
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def used_today() -> int:
    try:
        data = json.loads(_quota_file().read_text(encoding="utf-8"))
        return int(data.get(_quota_day(), 0))
    except (OSError, ValueError):
        return 0


def _add_used(n: int) -> None:
    path = _quota_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({_quota_day(): used_today() + n}), encoding="utf-8")


def batches(rows: list[dict]) -> list[list[dict]]:
    """Batches under EMBED_TOKENS_PER_BATCH estimated tokens and the API's 100 items."""
    out, current, tokens = [], [], 0
    for row in rows:
        t = embeddings.estimate_tokens(row["text"])
        if current and (tokens + t > EMBED_TOKENS_PER_BATCH or len(current) >= embeddings.MAX_ITEMS_PER_CALL):
            out.append(current)
            current, tokens = [], 0
        current.append(row)
        tokens += t
    if current:
        out.append(current)
    return out


def _is_rate_limit(e: Exception) -> bool:
    return "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e).upper()


# ─── Ingest ─────────────────────────────────────────────────────────────────

def resolve_tickers(symbols: list[str]) -> list[tickers.Company]:
    out = []
    for s in symbols:
        c = tickers.resolve(s)
        if c is None or not c.cik:
            raise SystemExit(f"Unknown ticker or no SEC CIK: {s}")
        out.append(c)
    return out


def ingest(symbols: list[str], dry_run: bool = False, index=None, service=None, sleep=time.sleep,
           focus: list[str] | None = None) -> dict:
    """Indexes the latest filings of each ticker. Returns stats (also printed by the CLI)."""
    index = index or vectors.LocalIndex(config.INDEX_DIR)
    service = service or embeddings.get_service()
    known = index.existing_hashes()
    stats = {"filings": [], "chunks": 0, "new_chunks": 0, "need_api": 0, "api_texts": 0, "cache_hits": 0,
             "indexed": 0, "stopped": None, "used_today": used_today(), "daily_cap": FILING_EMBED_DAILY_CAP}
    todo: list[dict] = []
    for company in resolve_tickers(symbols):
        for f in latest_filings(company.ticker, company.cik, company.name):
            rows, counts = filing_rows(f, document_text(f))
            new = [r for r in rows if r["chunk_hash"] not in known]
            need = sum(not c for c in service.cached([r["text"] for r in new])) if new else 0
            stats["filings"].append({**asdict(f), "url": f.url, "sections": counts, "chunks": len(rows),
                                     "new_chunks": len(new), "need_api": need})
            stats["chunks"] += len(rows)
            stats["new_chunks"] += len(new)
            stats["need_api"] += need
            todo += new
    if dry_run or not todo:
        return stats

    todo.sort(key=lambda r: order_key(r, focus))  # stable: keeps ticker and chunk order within a rank

    first = True
    for batch in batches(todo):
        texts = [r["text"] for r in batch]
        misses = sum(not c for c in service.cached(texts))
        if misses and used_today() + misses > FILING_EMBED_DAILY_CAP:
            stats["stopped"] = (f"daily embedding cap reached ({used_today()}/{FILING_EMBED_DAILY_CAP}); "
                                "run again after midnight Pacific to continue")
            break
        if misses and not first:
            sleep(SECONDS_BETWEEN_BATCHES)  # stay under 30k tokens/minute
        before_api, before_hits = service.api_texts, service.cache_hits
        for attempt in range(1, RATE_LIMIT_RETRIES + 1):
            try:
                vecs = service.embed_documents(texts)
                break
            except Exception as e:
                if _is_rate_limit(e) and attempt < RATE_LIMIT_RETRIES:
                    logger.warning("Embedding rate limited; waiting %ss (attempt %d)", SECONDS_BETWEEN_BATCHES, attempt)
                    sleep(SECONDS_BETWEEN_BATCHES)
                    continue
                stats["stopped"] = f"embedding failed ({type(e).__name__}); progress is saved, run again to resume"
                vecs = None
                break
        api = service.api_texts - before_api
        if api:
            _add_used(api)
            first = False
        stats["api_texts"] += api
        stats["cache_hits"] += service.cache_hits - before_hits
        if vecs is None:
            break
        stats["indexed"] += index.add(batch, vecs)
        logger.info("Indexed batch: %d chunks (api_texts=%d, total indexed %d)", len(batch), api, stats["indexed"],
                    extra={"api_texts": api, "chunks": len(batch)})
    stats["used_today"] = used_today()
    return stats


def print_report(stats: dict, dry_run: bool, out=None) -> None:
    w = (out or sys.stdout).write
    for f in stats["filings"]:
        w(f"{f['ticker']} {f['form']}  accession {f['accession']}  period {f['period']}  filed {f['filed']}\n")
        w(f"    {f['url']}\n")
        for name, n in f["sections"].items():
            w(f"    - {name}: {n} chunks\n")
        w(f"    chunks {f['chunks']}, not yet indexed {f['new_chunks']}, need embedding (not cached) {f['need_api']}\n")
    w(f"\nTotal chunks {stats['chunks']}, not yet indexed {stats['new_chunks']}, need embedding {stats['need_api']}\n")
    w(f"Embedding quota used today: {stats['used_today']}/{stats['daily_cap']} (Pacific day)\n")
    if not dry_run:
        w(f"Indexed {stats['indexed']} chunks; api_texts={stats['api_texts']} cache_hits={stats['cache_hits']}\n")
        if stats["stopped"]:
            w(f"Stopped: {stats['stopped']}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index the latest SEC filings of some tickers.")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--dry-run", action="store_true", help="show filings, sections and cost; embed nothing")
    parser.add_argument("--focus", nargs="*", default=None, help="embed these tickers' chunks first (e.g. NVDA)")
    args = parser.parse_args(argv)
    config.setup_logging(config.env("LOG_LEVEL", "WARNING"))
    stats = ingest(args.tickers, dry_run=args.dry_run, focus=args.focus)
    print_report(stats, args.dry_run)
    return 1 if stats["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
