"""CLI wiring: commands call the core functions and print friendly errors (core mocked)."""

import json

import pytest

import cli
from core import market, news


def test_market_prints_the_quote_with_its_source(monkeypatch, capsys):
    monkeypatch.setattr(market, "get_quote", lambda t: {"ticker": t, "price": 1.0, "source": "finnhub"})
    assert cli.main(["market", "QQQ"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "finnhub"


def test_resolve_unknown_is_a_friendly_not_found(sec_tickers, capsys):
    assert cli.main(["resolve", "nonexistent co"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("Not found:") and "Traceback" not in out


def test_resolve_known_name_returns_ticker_and_cik(sec_tickers, capsys):
    assert cli.main(["resolve", "advanced micro devices"]) == 0
    assert json.loads(capsys.readouterr().out)["resolve"] == {"ticker": "AMD", "name": "AMD", "cik": 2488}


def test_market_failure_is_a_one_line_error(monkeypatch, capsys):
    def down(t):
        raise market.MarketDataError("Market data is unavailable for NVDA, please retry")
    monkeypatch.setattr(market, "get_quote", down)
    assert cli.main(["market", "NVDA"]) == 1
    assert capsys.readouterr().err.strip() == "Error: Market data is unavailable for NVDA, please retry"


def test_news_failure_is_a_one_line_error(monkeypatch, sec_tickers, capsys):
    def down(*a, **k):
        raise news.NewsError("News feed is unavailable, please retry")
    monkeypatch.setattr(news, "get_news", down)
    assert cli.main(["news", "NVDA"]) == 1
    assert "News feed is unavailable" in capsys.readouterr().err


def test_ingest_dry_run_is_wired(monkeypatch, capsys):
    from core import ingest

    seen = {}
    monkeypatch.setattr(ingest, "ingest", lambda t, dry_run, focus=None: seen.update(t=t, dry=dry_run, focus=focus) or {
        "filings": [], "chunks": 0, "new_chunks": 0, "need_api": 0, "used_today": 0, "daily_cap": 400, "stopped": None,
        "relabelled": 0, "dropped": 0})
    assert cli.main(["ingest", "NVDA", "--dry-run", "--focus", "NVDA"]) == 0
    assert seen == {"t": ["NVDA"], "dry": True, "focus": ["NVDA"]} and "Total chunks 0" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["unknown-cmd"]])
def test_bad_usage_exits_with_usage(argv):
    with pytest.raises(SystemExit):
        cli.main(argv)


def test_agent_prints_the_answer_without_context(monkeypatch, capsys):
    from core import agent

    monkeypatch.setattr(agent, "answer", lambda q: {"answer": "ok [Doc 1]", "context": "big", "mode": "quick"})
    assert cli.main(["agent", "q"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"answer": "ok [Doc 1]", "mode": "quick"}


def test_retriever_error_is_a_one_line_error(monkeypatch, capsys):
    from core import retriever

    def down(*a, **k):
        raise retriever.RetrieverError("Document index is unavailable")
    monkeypatch.setattr(retriever, "search", down)
    assert cli.main(["retriever", "x"]) == 1
    assert capsys.readouterr().err.strip() == "Error: Document index is unavailable"


def test_scan_is_wired_and_a_bad_replay_is_a_one_line_error(monkeypatch, capsys):
    from jobs import scan

    calls = []
    monkeypatch.setattr(scan, "run", lambda **kw: calls.append(kw) or {"price_events": ["INTC -7.5% (replay)"]})
    assert cli.main(["scan", "--dry-run", "--replay", "INTC:-7.5"]) == 0
    assert calls == [{"dry_run": True, "replay": "INTC:-7.5"}]
    assert "INTC -7.5% (replay)" in capsys.readouterr().out
    monkeypatch.setattr(scan, "run", lambda **kw: scan.parse_replay("INTC"))
    assert cli.main(["scan", "--replay", "INTC"]) == 1
    assert capsys.readouterr().err.startswith("Error: Replay must look like TICKER:PCT")
