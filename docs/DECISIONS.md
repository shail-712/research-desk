# Architecture decisions and trade-offs

Append-only. Format: decision · options considered · why · trade-off. D1–D7 carried over from v1.

## D1. Gemini instead of Amazon Bedrock
- **Options:** Bedrock (Claude Haiku / Nova Lite) as the brief asks; Gemini free tier; both with failover.
- **Decision:** Gemini free tier only, behind one provider interface (`core/llm.py`), with a fallback chain
  across Gemini models, per-call timeouts and a cooldown for failing models.
- **Why:** cost target ~$0/month long term. Bedrock is pay-per-token with no always-free tier.
- **Trade-off:** deviates from the brief's "Amazon Bedrock" requirement and depends on a non-AWS service with
  free-tier rate limits. Bedrock would be one more adapter (Converse API with tool use).

## D2. Finnhub as the backup market-data provider
- **Options:** yfinance only; Alpha Vantage fallback; Finnhub fallback.
- **Decision:** yfinance primary, Finnhub on error, block or empty data. Each quote records its source.
- **Why:** Yahoo often blocks AWS IPs. Finnhub free (~60 calls/min, real-time US quotes) covers a 15-minute
  scan; Alpha Vantage free (~25 requests/day) does not. *(Verify on finnhub.io/pricing.)*
- **Trade-off:** Finnhub free has no historical candles; multi-day history needs yfinance.

## D3. No weekly email digest
- **Why:** Telegram already delivers alerts; a digest adds SES sandbox setup for little value.

## D4. One stack: AWS backend, Streamlit Community Cloud as frontend only
- **Why:** v1 kept a parallel Supabase / GitHub-cron backend; it doubled the code paths to test and explain.
- **Kept for local dev:** SQLite store and local FAISS files, so the app and tests run without AWS.

## D5. Quick mode keeps keyword routing; Deep mode is the agentic path
- **Why:** Quick is fast and cheap (1 Gemini call). Deep is where the LLM decides which tools to call,
  as the brief asks, and it is what the demo shows.
- **Evidence (v1 eval, 20 questions):** routing 75% → 95%, cited lines 51% → 90% (Quick → Deep).
- **Trade-off:** Deep costs ~6.5 Gemini calls per question and p50 ~23 s.

## D6. FAISS on S3 instead of OpenSearch or Bedrock Knowledge Bases
- **Why:** the index is a few MB; OpenSearch Serverless has a monthly minimum far above the budget.
- **Trade-off:** one writer at a time — handled with S3 conditional writes (`If-Match`) and retry.

## D7. Async job API for `/ask`
- **Why:** API Gateway's 30 s integration limit is below Deep mode's 20–75 s.
- **Trade-off:** clients poll `GET /ask/{id}`; jobs stored in DynamoDB with a 1-day TTL.

## D8. Eval scorer ported verbatim and frozen
- **Options:** rewrite the eval metrics for v2; port v1's scoring unchanged.
- **Decision:** `evals/scoring.py` (v1 `evals/run.py`: `score`, `summarise`, `pct`, `quantile`, `CITATION`,
  `QUICK_TOOL_TO_AGENT`) and `core/numbers.py` (v1 `core/agents/checker.py` stage 1: `extract_numbers`,
  `_supported`, `verify_numbers`, constants) are copied with identical function bodies from v1 commit `73b6fda`,
  before any other module. A golden test re-scores v1's saved 2026-11-03 results and must reproduce v1's summary
  exactly. Stage 1 is split out of `checker.py` so the scorer has no LLM/SDK dependency.
- **Why:** v1 → v2 numbers (routing 75%/95%, cited lines 51%/90%, untraceable 1/109 and 0/98) are only comparable if the
  ruler doesn't change.
- **Trade-off:** inherits v1's quirks (derived numbers count as untraceable; sentence-split heuristic for
  "lines"). Any change needs a new decision entry and a re-score of both v1 and v2 results.

## D9. v1 baseline is re-run on the same day; corpus difference is disclosed
- **Decision:** v1's eval is re-run on 2026-11-04 from a copy outside both repos (`docs/eval/v1-baseline.md`),
  and v2 is compared against same-day runs, by question type.
- **Why:** live prices/news change daily, Gemini model names changed, and v1's local knowledge base contained only
  the three earnings press-release PDFs (1,491 chunks, no filing metadata), while v2 indexes 10-K/10-Q/8-K.
  Filing-question differences are partly the corpus, not only the code; the report says so.
- **Trade-off:** one extra eval run of free-tier quota (~150 Gemini calls).
