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
  using v1's config exactly as it is that day (`GEMINI_MODEL=gemini-3.8-flash`, v1's default fallbacks) plus
  `SEC_USER_AGENT`; a model that returns 404 is replaced by the nearest available one and the exact models used
  are recorded. v2 is compared against same-day runs, by question type.
- **Why:** live prices/news change daily, Gemini model names changed, and v1's local knowledge base contained only
  three older 10-Q PDFs in `data/raw/` (named `*_PressRelease.pdf` but actually AMD Q1 2025, INTC Q3 2025,
  NVDA Q3 FY2026; 1,491 chunks, no 10-K, no filing metadata), while v2 indexes the latest 10-K/10-Q/8-K.
  Filing-question differences are partly the corpus, not only the code; the report says so.
- **Trade-off:** one extra eval run of free-tier quota (~150 Gemini calls).

## D10. `include_context` on `POST /ask`, for the eval runner only
- **Options:** score the deployed stack without untraceable-number counts; always return the evidence text;
  an opt-in flag.
- **Decision:** `POST /ask` accepts `include_context` (default `false`). Only then does the worker keep the
  evidence text on the job item (capped at 100 KB, `context_truncated` flag) and `GET /ask/{id}` return it.
  Only `evals/run.py --target api` sets it.
- **Why:** the frozen scorer (D8) needs the evidence text to count untraceable numbers; v1's worker dropped it,
  so the deployed stack couldn't be scored on the headline metric.
- **Trade-off:** larger job items for eval jobs (DynamoDB 400 KB item limit → the cap); one more input to validate.

## D11. Embeddings are paid for once
- **Options:** re-embed on every index rebuild/deploy; ship the index in the image; cache by content.
- **Decision:** `core/embeddings.py` caches vectors keyed by `sha256(model|dim|task_type|text)` in
  `data/cache/embeddings.sqlite`, with a snapshot on S3 (`scripts/bootstrap.py --push-cache/--pull-cache`).
  Ingest skips chunks whose hash is already in the index. The index header records model + dim and refuses a
  mismatch. The index lives only in S3 (create-only upload; bucket `DeletionPolicy`/`UpdateReplacePolicy: Retain`);
  `data/` is excluded from the image, and no deploy step embeds.
- **Why:** the Gemini embed free tier is ~1,000 requests/day and a full KB build can take most of it;
  re-spending it on a rebuild or redeploy could block a demo day.
- **Trade-off:** a cache to keep consistent with the model (handled by the key), and an S3 object that outlives
  `sam delete` (deleted by hand when the project ends).

## D12. Market holidays: known gap, safe default
- **Options:** a holiday calendar (exchange_calendars / hard-coded list); ignore holidays; skip when there's no data.
- **Decision:** no calendar. If the quote's trading date isn't today's New York date, the scan skips all price
  rules for that run (logged), so a holiday never produces an alert from yesterday's numbers.
- **Why:** a wrong alert is worse than none; a calendar is extra scope for a 5–7 day build.
- **Trade-off:** on the day after a holiday the "why" session window may start at the wrong previous close
  (e.g. Tuesday after a Monday holiday looks back to Monday 4 pm, missing weekend headlines). Listed as a
  production improvement.

## D13. Scope cut for speed (2026-11-05)
- **Options:** keep the full SPEC in 6 phases; cut everything the brief doesn't require and that the demo can
  live without, in 4 phases.
- **Decision:** 4 phases (A local core + KB + agents + local eval; B AWS + Postman + price alerts/Telegram/DLQ;
  C Streamlit + logs/alarms + deployed eval; D docs + demo). Cut from SPEC: SEC filing alerts + XBRL earnings
  snapshot (F10), auto-ingest on watchlist add / new filings (F11; replaced by the manual `cli.py ingest`),
  X-Ray, the CloudWatch dashboard, the OIDC deploy workflow (manual `sam deploy`; `ci.yml` stays), the
  `data/raw` download script (D9 context), and the S3 embedding-cache snapshot (the local cache in D11 stays).
  v1's eval is **not re-run** (supersedes D9's same-day rerun): `docs/eval/v1-baseline.md` is generated by the
  frozen scorer from v1's golden-tested 2026-11-03 results, with that date stated. The Reviewer runs after
  Phases A and B only.
- **Why:** every brief requirement is still covered; the cut items are distinctive but not required, and each
  costs a day-part of build and test time.
- **Trade-off:** less to show in the watchdog story (price alerts only) and in ops (logs + alarms, no traces);
  v1→v2 comparison mixes two days of live data. All cut items are listed as production improvements in
  `docs/DESIGN.md`. Where SPEC still describes them, this entry and `docs/PLAN.md` take precedence.

## D14. v1 was re-run on 2026-11-05 (supersedes D13's "not re-run")
- **What happened:** the Builder had already re-run v1's eval on 2026-11-05 under PLAN rev 2's task 1.2, before rev 3
  arrived. It used v1 @ `73b6fda` from a copy outside both repos, with v1's config unchanged plus `SEC_USER_AGENT`.
- **Decision:** keep the rerun. `docs/eval/v1-baseline.md` reports the saved 2026-11-03 run and the 2026-11-05 rerun
  side by side, both scored by the frozen scorer (v1's and v2's scorer agree byte for byte on both). The raw results are
  in `docs/eval/v1-rerun-2026-11-05.json`. The report also records the models configured and actually used: the
  configured primary `gemini-3.8-flash` returned 503/504 all run, so every answer came from the fallbacks. v2 is
  compared against this same-day baseline (by question type). The 11-03 numbers stay as the golden test for the scorer.
- **Why:** the quota was already spent and same-day numbers are more comparable than the 11-03 run (D9).
- **Trade-off:** a v2 local eval on a later day still differs by one or more days of live data. The report states the gap.


## D15. The ECR repository is its own small stack (2026-11-07)
- **Options:** the repository in `template.yaml` (PLAN B.2); create it by hand and import it into the stack;
  SAM's managed repository (`--resolve-image-repos`) with the lifecycle set by a script; a separate stack.
- **Decision:** `infra/ecr.yaml` (stack `research-desk-ecr`) holds the repository and its keep-2 lifecycle policy,
  deployed once before the first `sam deploy`; the main stack takes its URI via `--image-repository`.
- **Why:** SAM pushes the image before CloudFormation creates the main stack's resources, so a repository in
  `template.yaml` doesn't exist when the first push happens. A separate stack keeps it as IaC with nothing to import.
- **Trade-off:** two stacks to deploy (and delete) instead of one; bends PLAN's "infra only in template.yaml".
  Approved by the user on 2026-11-07.

## D16. Switched to iterative delivery (2026-11-07)
- **Options:** keep the phased plan (PLAN rev 3, phases A–D); small iterations that each end deployed or verified.
- **Decision:** `docs/ITERATIONS.md` (I0–I5) is the plan. Old plans are in `docs/archive/`. Every iteration ends
  with its checks passing and a merge to `main`. Code that is already built and tested stays as it is. Reviews
  run only after I1 and I3 and stay under 40 lines. AGENT.md is cut down to a Builder prompt and a Reviewer prompt.
- **Why:** deploys were blocked (D17 pending), `main` held only docs, and a long phase could end with nothing
  submittable. Small iterations keep a working, committed project at every step.
- **Trade-off:** fewer reviews, so the Builder's own checks carry more weight.

## D17. Region is ap-southeast-2, the only one this account can use (2026-11-07)
- **Context:** the account was opened through AWS's "projects" sign-up. An AWS Organizations SCP limits it to one
  Region, which blocked the B.5 deploy to `ap-south-1` (nothing was created there).
- **Probes (read-only, 2026-11-07):** `aws account list-regions` lists 17 enabled Regions. In `ap-southeast-2`
  all six probes succeed (`cloudformation list-stacks`, `lambda list-functions`, `ecr describe-repositories`,
  `s3api list-buckets`, `dynamodb list-tables`, `ssm describe-parameters`), and so do the other services the stack
  uses (`apigatewayv2`, `sqs`, `sns`, `scheduler`, `logs`, `cloudwatch`, `events`, `iam`). In `ap-south-1` and
  `us-east-1` every regional probe is AccessDenied; only S3 ListBuckets (a global call) succeeds.
  `lambda list-functions` is denied in all 16 other enabled Regions.
- **Decision:** deploy to `ap-southeast-2`. It is set in `samconfig.toml`, `.env` / `.env.example`, the ECR
  stack command, the Postman base URL and the bootstrap default. Moto tests keep `ap-south-1` (it has no effect).
- **Trade-off:** higher network latency from India than `ap-south-1` (not measured; small next to LLM time). Gemini, SEC and market-data calls are not
  affected. The Region can't be changed without a different account.
