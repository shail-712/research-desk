# 01 · Overview

## What it is (to a friend)

It's a chatbot for stock research. You ask "How did NVIDIA do last quarter, and what's in the news?" and it pulls the live price, recent headlines and NVIDIA's SEC filings, then writes a short answer. Every number in the answer points to where it came from. It can also text your phone (Telegram) when a stock you watch jumps or drops sharply.

## The problem it solves

- **The problem:** research managers spend days per company combining market data, news and filings by hand. The assessment brief (`docs/ASSESSMENT.md`) asks to cut that to under 5 minutes.
- **The other problem:** AI models make up numbers ("hallucinate"). In finance, a number with no source is a liability. This project's main idea is that **every number must be traceable** to evidence, and code checks this.
- **Who uses it:** one user (a research analyst or investor) via the Streamlit web UI, or a developer via Postman. There are no user accounts. Access is one shared code.

## Main features

- **Quick mode:** keyword rules choose the tools, the tools run in parallel, one Gemini call writes a cited answer (`core/agent.py`).
- **Deep mode:** a supervisor AI plans, then market, news and filings specialist agents run in parallel (`core/agents/`).
- **Evidence board:** every tool result gets an ID (`[E1]`, `[E2]`…), and the answer may only cite those IDs (`core/agents/evidence.py`).
- **Fact-checker:** code pulls every number out of the answer and matches it against the evidence. Numbers it can't match are flagged ⚠️ (`core/agents/checker.py`, `core/numbers.py`).
- **Market data tool:** live quotes from Yahoo Finance (`yfinance`), with Finnhub as the fallback (`core/market.py`).
- **News tool:** Yahoo Finance RSS headlines per company, no API key needed (`core/news.py`).
- **SEC filings search (RAG):** a FAISS vector index of 934 chunks from NVIDIA, AMD and Intel 10-K / 10-Q / 8-K filings (`core/ingest.py`, `core/retriever.py`).
- **Company name lookup:** "advanced micro devices" → AMD, using SEC's `company_tickers.json` (`core/tickers.py`).
- **Watchdog price alerts:** a scan every 15 min, a 3% move rule (or price above/below), one alert per rule per day (`jobs/scan.py`, `core/alerts.py`).
- **"Why did it move":** the alert explains the move from the market (QQQ/SPY) or a same-session headline, or says "no clear catalyst" (`core/explain.py`).
- **Telegram delivery with a dead-letter queue:** failed alerts land in SQS and trigger an email alarm (`aws_lambda/notify.py`).
- **Async ask API:** `POST /ask` returns a job ID right away, and the client polls (`aws_lambda/api.py`).
- **Streamlit UI:** tabs for Ask, Watchlist, Alerts and How it works (`ui/app.py`).
- **Eval set:** 20 questions, both modes, scored only by code with no AI judge (`evals/`).

> **Terms:**
> **RAG** (Retrieval-Augmented Generation) means you search your own documents first, then give the matching passages to the AI so it answers from them.
> **Vector index** means text turned into lists of numbers ("embeddings") so similar meanings sit close together and can be searched.

## 60-second answer: "Tell me about your project"

> "I built Research Desk, an AI investment research assistant on AWS. You ask a question about a stock and get a short answer where every number cites its source: a live quote, a headline, or a passage from an SEC filing.
>
> It has two modes. Quick mode uses keyword rules to pick tools, runs them in parallel, and makes one Gemini call. That takes about 6 seconds. Deep mode is multi-agent. A supervisor plans the work, and market, news and filings specialists run in parallel. They post everything to a shared evidence board. A fact-checker then matches every number in the answer against that evidence in code, and flags anything it can't trace.
>
> It's fully serverless: API Gateway, four Lambda functions, DynamoDB and S3, defined in AWS SAM. Answers take longer than API Gateway's 30-second limit, so the API returns a job ID and the client polls. There's also a Watchdog that scans prices every 15 minutes and sends Telegram alerts through a DynamoDB Stream, with a dead-letter queue and alarms.
>
> I measured it on a 20-question eval set scored by code. On the deployed stack, only about 1% of numbers couldn't be traced to evidence. It's designed to cost about zero dollars a month."

## My Role

- **Was this solo or a team project?** TODO
- **What I designed myself:** TODO (e.g. the async job API, the evidence board, the DynamoDB single-table design)
- **What I coded myself vs. with AI help:** TODO (the repo has `AGENT.md` with "Builder" and "Reviewer" AI-agent prompts, so be ready to explain how you used AI coding agents and what you decided yourself)
- **Hardest problem I personally solved:** TODO
- **What I'd point to as my best decision:** TODO
- **Time spent:** TODO (git history shows commits from 2026-11-04 to 2026-11-15; the brief allowed 5–7 days)
- **Outcome:** TODO (was it submitted? how did the interview go?)
