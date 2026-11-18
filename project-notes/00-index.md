# Research Desk: Study Notes (Index)

## Summary

Research Desk is an AI investment research assistant. You ask a question about a US stock in plain English, for example "What's AMD's P/E and what's in the news?". It answers in seconds, and every number in the answer cites its source: a live quote, a news headline, or a passage from an SEC filing. It has two answer modes. **Quick** uses one Gemini call. **Deep** uses a team of AI "specialist" agents plus a code-based fact-checker. A third part, the **Watchdog**, checks a watchlist of stocks every 15 minutes during US market hours. When a stock moves 3% or more, it sends a Telegram alert with a one-line "why". The backend runs serverless on AWS (Lambda, API Gateway, DynamoDB, S3, EventBridge Scheduler, SQS, SNS) and is designed to cost about $0/month. The frontend is a Streamlit web app. It was built for an AI Engineer take-home assessment (`docs/ASSESSMENT.md`).

## Read in this order

| # | File | What you get from it |
|---|---|---|
| 1 | [01-overview.md](01-overview.md) | What it is, who it's for, the features, a 60-second pitch, and a "My Role" section for you to fill in |
| 2 | [02-tech-stack.md](02-tech-stack.md) | Every technology, why it was picked, and an alternative you can mention |
| 3 | [03-system-design.md](03-system-design.md) | Architecture diagram, what each part does, one request step by step, folder map, deployment |
| 4 | [04-data-and-features.md](04-data-and-features.md) | DynamoDB table design, API endpoints, the 3 main features end to end, auth, error handling |
| 5 | [05-challenges-and-interview-prep.md](05-challenges-and-interview-prep.md) | Hard parts, trade-offs, weaknesses, scaling to 10x and 100x, security, 20 interview Q&As |

## Tips

- Read 01, then 03, before any interview. Those two let you explain the project out loud.
- Read 05 the night before. Practise the Q&As by saying the answers aloud.
- The project's own deeper docs: `docs/DESIGN.md` (decisions and limits), `docs/DECISIONS.md` (D1–D18), `docs/DEMO-SCRIPT.md` (the demo run sheet).
