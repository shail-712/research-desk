"""
Command-line access to the tools.

    python cli.py market NVDA                       # one quote (source: yfinance or finnhub)
    python cli.py quotes NVDA AMD INTC QQQ SPY      # batched quotes
    python cli.py news NVDA
    python cli.py resolve "advanced micro devices"
    python cli.py ingest NVDA AMD INTC [--dry-run]  # build / extend the knowledge base

Adapted from v1 cli.py (73b6fda). `retriever` and `agent [--deep]` are added with their modules
(PLAN A.11, A.12).
"""

import argparse
import json
import sys

from core.config import env, setup_logging


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles / redirects

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("quotes").add_argument("tickers", nargs="+")
    sub.add_parser("market").add_argument("ticker", nargs="?", default="NVDA")
    p = sub.add_parser("news")
    p.add_argument("ticker", nargs="?", default="NVDA")
    p.add_argument("--count", type=int, default=5)
    sub.add_parser("resolve").add_argument("text")
    p = sub.add_parser("ingest")
    p.add_argument("tickers", nargs="+")
    p.add_argument("--dry-run", action="store_true", help="show filings, sections and embedding cost only")
    args = parser.parse_args(argv)
    setup_logging(env("LOG_LEVEL", "WARNING"))  # JSON logs go to stderr; results to stdout

    from core import market, news, tickers

    try:
        if args.cmd == "quotes":
            _print(market.get_quotes(args.tickers))
        elif args.cmd == "market":
            _print(market.get_quote(args.ticker))
        elif args.cmd == "news":
            company = tickers.lookup_ticker(args.ticker)
            _print(news.get_news(args.ticker, company.name if company else None, args.count))
        elif args.cmd == "resolve":
            company = tickers.resolve(args.text)
            if company is None:
                print(f"Not found: no US-listed company or ticker matches {args.text!r}. "
                      "Try the ticker (e.g. AMD) or the company's name (e.g. Advanced Micro Devices).")
                return 1
            _print({"resolve": company.to_dict(),
                    "find_companies": [c.to_dict() for c in tickers.find_companies(args.text)]})
        elif args.cmd == "ingest":
            from core import ingest

            stats = ingest.ingest(args.tickers, dry_run=args.dry_run)
            ingest.print_report(stats, args.dry_run)
            return 1 if stats["stopped"] else 0
    except (market.MarketDataError, news.NewsError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
