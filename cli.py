"""
Command-line access to the tools.

    python cli.py market NVDA                       # one quote (source: yfinance or finnhub)
    python cli.py quotes NVDA AMD INTC QQQ SPY      # batched quotes
    python cli.py news NVDA
    python cli.py resolve "advanced micro devices"
    python cli.py ingest NVDA AMD INTC [--dry-run]  # build / extend the knowledge base
    python cli.py retriever "Compare NVIDIA and AMD data center revenue"
    python cli.py agent "What is AMD's market cap and P/E ratio, and what are the latest headlines?"

Adapted from v1 cli.py (73b6fda).
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
    p = sub.add_parser("retriever")
    p.add_argument("query", nargs="?", default="Compare NVIDIA and AMD data center revenue")
    p.add_argument("--top-k", type=int, default=5)
    p = sub.add_parser("agent")
    p.add_argument("query", nargs="?", default="What is NVIDIA's stock price and latest news?")
    args = parser.parse_args(argv)
    setup_logging(env("LOG_LEVEL", "WARNING"))  # JSON logs go to stderr; results to stdout

    from core import agent, log, market, news, retriever, tickers

    log.new_request_id()

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
        elif args.cmd == "retriever":
            chunks = retriever.search(args.query, args.top_k)
            _print([{**c, "text": c["text"][:200] + "..."} for c in chunks])
        elif args.cmd == "agent":
            result = agent.answer(args.query)
            result.pop("context", None)
            _print(result)
            return 0 if result["answer"] else 2
    except (market.MarketDataError, news.NewsError, retriever.RetrieverError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
