# RESUME-FACTS — what the v2 code and records support

Checked 2026-11-11 on branch `iter-2` (HEAD `4f720b1`). Read-only: no code changed, no AWS or Gemini calls.
The only thing run was the mocked unit suite (`python -m pytest`, no network).
Sources: the code (file:line), `docs/PROGRESS.md`, `docs/eval/v2-results.md` and `docs/eval/v1-baseline.md`.
The v1 repo (`../investment-research-assistant` @ `73b6fda`) was used only to check where a claim came from.

---

## 1. Your current bullets, checked

### Bullet 1: "serverless multi-agent architecture on AWS Lambda and API Gateway, with a Gemini-driven orchestrator that classifies queries and routes them to specialized market-data, news, and retrieval agents"

| Claim | Verdict | Evidence |
|---|---|---|
| Serverless on Lambda + API Gateway | **TRUE** | `template.yaml:103` / `:136` (`PackageType: Image` functions), HttpApi throttling `template.yaml:97`. Deployed: PROGRESS I1 (stack CREATE_COMPLETE, 2026-11-07), I2 B3 (UPDATE_COMPLETE, 2026-11-09). |
| Multi-agent with specialized market / news / retrieval agents | **TRUE (Deep mode only)** | `core/agents/specialists.py:26-46` (`SPECIALISTS` = market, news, filings), run in parallel by `core/agents/deep.py:78` (`_dispatch`). Quick mode is not multi-agent: it is one Gemini call over tool results (`core/agent.py:1-9`). |
| "Gemini-driven orchestrator that classifies queries and routes" | **PARTLY TRUE** | **Quick: keyword rules, no Gemini.** `core/agent.py:47-56` (`classify_query`, regexes at `:35-44`). **Deep: a Gemini supervisor plans.** `core/agents/supervisor.py:87-91` (`plan()` makes a structured-JSON Gemini call with `PLAN_SCHEMA`, `:29`). **But Deep also uses the keyword rules first:** `core/agents/deep.py:115-121` sends single-intent, single-company questions down a fast path to one specialist, with no planner call. Accurate wording: "a Gemini supervisor plans multi-part questions; simple ones take a keyword fast path." |

### Bullet 2: "RAG pipeline using FAISS vector search over financial filings, with S3-backed embeddings and /tmp warm-start caching to cut cold-start latency"

| Claim | Verdict | Evidence |
|---|---|---|
| RAG with FAISS over financial filings | **TRUE** | `core/vectors.py:1-2`, `core/retriever.py`. Corpus = latest 10-K, 10-Q and earnings 8-K per ticker from SEC EDGAR (`core/ingest.py:7-16`). |
| S3-backed embeddings | **TRUE** | One `.npz` (vectors + metadata) as one S3 object: `core/vectors.py:205-235` (`S3Index`). Uploaded 934 chunks to S3: PROGRESS I1 (2026-11-07). |
| /tmp caching of the index | **FALSE in v2** (it was v1) | v2 keeps the index **in memory** per container, explicitly *not* in /tmp: `core/vectors.py:14-15`, `:237-251` (warm copy, ETag re-check every 300 s, `REFRESH_SECONDS` `:41`). v1 wrote it to `/tmp/research-desk-index.npz` (v1 `core/vectors.py:153-181`); v2 removed that (PROGRESS "Found while building": "PLAN rev 3 still says `/tmp` cache — fixed"). |
| /tmp caching of embeddings | **PARTLY TRUE** | Only the **query-embedding** SQLite cache lives in /tmp on Lambda: `template.yaml:50-52` (`EMBED_CACHE_PATH: /tmp/research-desk/embeddings.sqlite`), cache class `core/embeddings.py:64-89`. It saves Gemini embed calls for repeated queries in a warm container. It does nothing for cold starts, since /tmp is empty on a new container. |
| "to cut cold-start latency" | **FALSE (not measured)** | No before/after measurement of any cache exists. What *was* measured is cold vs warm on the same image: cold ~10.0 s vs warm ~6.6 s end to end, worker init 3.8 s (PROGRESS I2 step C, 2026-11-09). The in-memory index is a **warm-start** optimisation (download skipped on later invocations), not a cold-start one. |

### Bullet 3: "DynamoDB caching layer for market data and news lookups, reducing redundant API calls and third-party rate-limit failures"

| Claim | Verdict | Evidence |
|---|---|---|
| DynamoDB cache for market data / news | **FALSE (in v2 and in v1)** | v2's table holds watchlist (`WATCH`/`TICKER#`), alert feed + dedupe markers (`SEEN#`), state, and async jobs: `core/store_dynamo.py:100-208`. No quote or news items. `core/market.py` and `core/news.py` have no cache at all. v1's `core/store_dynamo.py` has none either. |
| Reduced redundant API calls / rate-limit failures | **FALSE as stated** | No measurement exists. What *is* real: retries with backoff on 429/5xx for Finnhub (`core/market.py:91`, `:119-120`) and Gemini (`core/llm.py:43-48`), API Gateway throttling (burst 10) to protect the free quotas (`template.yaml:97`), and the SEC embedding daily cap (PROGRESS "Found while building"). |
| What DynamoDB actually does (usable instead) | TRUE | Async job store with 1-day TTL (`core/store_dynamo.py:194-208`); transactional dedupe of alerts (`TransactWriteItems`, 5 racing threads → exactly 1, PROGRESS B.1 moto test); Deep-mode daily cap counter (`core/agents/deep.py:46-60`). |

**Bottom line:** drop bullet 3 as written, rewrite bullet 2 without "/tmp" and "cut cold-start latency", and qualify bullet 1 as "Gemini supervisor (Deep mode)".

---

## 2. Features, with status

Status key: **BUILT+VERIFIED** = in code and shown working on AWS or in a recorded run. **BUILT-NOT-DEPLOYED** = in code and tested, but not live on AWS. **NOT BUILT**.

### Answer paths

| Feature | Status | Evidence |
|---|---|---|
| Quick mode: keyword routing → tools in parallel → 1 Gemini call, `[Doc N]` on every context item | BUILT+VERIFIED | `core/agent.py:1-9`, `:47-56`, pool `:163-164`. AWS: 1 Gemini call per answer (PROGRESS I2 C; eval 1.0 calls/question). |
| Deep mode: Gemini supervisor plan → 3 specialists in parallel | BUILT+VERIFIED | `core/agents/deep.py:98-130`, `core/agents/supervisor.py:87`. AWS brief query: plan NVDA/AMD/INTC, all three specialists ran (PROGRESS I2 check 1 and D). |
| Keyword fast path inside Deep | BUILT | `core/agents/deep.py:118-121`. |
| Specialists = Gemini function-calling loops, max 3 steps, deadline, trace | BUILT+VERIFIED | `core/agents/loop.py:2`, `MAX_STEPS = 3` `:28`, `core/agents/specialists.py:110`. |
| Shared evidence board (`E1, E2…`, agent, tool, summary, source, as-of) | BUILT+VERIFIED | `core/agents/evidence.py:1-20`. AWS: 24–26 `E#` citations per brief answer (PROGRESS I2). |
| Follow-up round on gaps (at most one, only with ≥30 s left) | BUILT; **not triggered on AWS** | `core/agents/deep.py:37`, `:131-141`. Used 3× in the 20-question local Deep eval; 0× in the deployed subset (v2-results). |
| Fact-checker: stage 1 code matching of every number, stage 2 one LLM call only for misses, unsupported numbers flagged | BUILT+VERIFIED | `core/agents/checker.py:1-16`, called at `core/agents/deep.py:152`. AWS: 25/25 (check 1), 9/9 (step D), 0 checker LLM calls in check 1 (PROGRESS I2). |
| Citation enforcement: Quick cites `[Doc N]`, Deep cites `[E#]` | BUILT+VERIFIED | `core/agent.py:4-5`, `:9-12`; eval "cited numeric lines" metric (below). |

### Fallbacks and resilience

| Feature | Status | Evidence |
|---|---|---|
| Shared tool deadline (Quick 25 s; Deep tools cut at the loop deadline) | BUILT (unit-tested) | `core/agent.py:28`, `:164`; `core/agents/loop.py:46-57`. |
| Gemini model chain with retry on 429/5xx and 60 s per-model cooldown | BUILT+VERIFIED | `core/llm.py:29-34`, `:43-48`, `:103-117`. A 503 was absorbed by the next model in I2 step A (PROGRESS I2). |
| Deep → Quick fallback (planner/writer outage, time budget 75 s, daily cap 50) | BUILT (unit-tested); **0 fallbacks on AWS** | `core/agents/deep.py:35-36`, `:63-67`, `:112-113`, `:123-124`. |
| yfinance → Finnhub per ticker | BUILT (unit-tested) | `core/market.py:2`, `:113-120`. All recorded AWS answers used yfinance; Finnhub path not observed live. |

### Knowledge base

| Feature | Status | Evidence |
|---|---|---|
| SEC EDGAR ingest: latest 10-K, 10-Q, earnings 8-K (Item 2.02, EX-99.1) with section metadata | BUILT+VERIFIED | `core/ingest.py:7-16`, `:53` (215-word chunks, 36 overlap), `:100`. |
| Chunk count 934 (AMD 375, INTC 292, NVDA 267) | VERIFIED | PROGRESS A.10 (2026-11-05); v2-results Local header; S3 upload of 934 chunks (PROGRESS I1). |
| Embedding cache (SQLite, key `sha256(model|dim|task|text)`) + `chunk_hash` dedupe so re-ingest never re-embeds | BUILT (unit-tested) | `core/embeddings.py:1-17`, `:64-89`; `core/vectors.py:11-12`. |
| FAISS index, model/dimension header check, S3 create-only upload (`If-None-Match`), replace with `If-Match` | BUILT+VERIFIED | `core/vectors.py:1-17`, `:264-279`. |

### API and AWS

| Feature | Status | Evidence |
|---|---|---|
| Async job API: `POST /ask` → 202 + `job_id`, worker invoked with `InvocationType=Event`, `GET /ask/{id}` poll | BUILT+VERIFIED | `aws_lambda/api.py:102-107`, `:196-202`; Postman/newman (PROGRESS I1). |
| Auth: `x-access-code`, constant-time compare, 401 | BUILT+VERIFIED | `aws_lambda/api.py:83-84`, `:234`; 401 checked live (PROGRESS I1). |
| SAM template, one container image in ECR (keep-2 lifecycle, own stack) | BUILT+VERIFIED | `template.yaml`, `infra/ecr.yaml:20-23`. Image 301 MB compressed in ECR (PROGRESS I2 B4). |
| SSM Parameter Store secrets, read once per container | BUILT+VERIFIED | `aws_lambda/runtime.py:37-39`; IAM `ssm:GetParametersByPath` `template.yaml:115`. |
| Feature flag `EnableWatchdog` (CloudFormation Condition) | BUILT+VERIFIED | `template.yaml:25-35`; deployed with `false` → 14 resources (PROGRESS I1). |
| Change-set review before every deploy, with a written safety rule | VERIFIED (process) | PROGRESS I2 B (one change set held and deleted, the next executed). |
| $1 monthly AWS Budget with email alert | BUILT+VERIFIED | `template.yaml:254-262`; SNS subscription confirmed (PROGRESS I1). |
| JSON logs with `request_id` across threads | BUILT | `core/log.py`, `core/agent.py:24` (`run_in_context`). Cross-function `request_id` check is I4, not done. |
| CloudWatch EMF metrics (AgentLatency, GeminiCalls, DeepFallbacks, AskErrors) | BUILT; **not read back from CloudWatch** | `aws_lambda/runtime.py:74-85`, `aws_lambda/ask_worker.py:81-84`. |
| Watchdog: scheduled price-move scan → Telegram, DLQ + alarm, DynamoDB Stream | BUILT-NOT-DEPLOYED | `aws_lambda/scan.py`, `aws_lambda/notify.py`, conditioned on `WatchdogEnabled`. I3 not run. |
| API 5xx alarm, Streamlit UI | NOT BUILT | ITERATIONS I4. |

### Quality

| Feature | Status | Evidence |
|---|---|---|
| Unit tests: **407**, all mocked (moto for AWS) | VERIFIED 2026-11-11 | `python -m pytest` → `407 passed in 45.16s` (run for this file). |
| CI: GitHub Actions, pytest on every push and PR | BUILT | `.github/workflows/ci.yml`. |
| Postman collection + newman | VERIFIED | 20 requests, 30/30 assertions, 0 failed (PROGRESS I1, 2026-11-07). Deep and Feed routes not in that run. |
| Eval harness: 20 questions, frozen code-only scorer (no LLM judge), local and API targets | BUILT+VERIFIED | `evals/run.py`, `evals/scoring.py`, `evals/questions.json`; scorer byte-identical to v1's (v1-baseline.md). |
| Cost | **NOT MEASURED** | "about $0/month" in the README is a design target (free tier, provisioned 2/2 table, no always-on servers). No bill or Cost Explorer figure is recorded. Only the $1 budget alarm exists. |

---

## 3. Measured numbers (each has a source)

### Latency on AWS (ap-southeast-2)

| Number | Source and date |
|---|---|
| Quick p50 **5.8 s**, p90 **6.0 s** (5 questions, end to end incl. polling) | v2-results Deployed, 2026-11-09 |
| Deep p50 **11.3 s**, p90 **17.6 s** (same 5 questions) | v2-results Deployed, 2026-11-09 |
| Quick warm **~6.6 s** end to end, **4.0 s** in the worker | PROGRESS I2 C, 2026-11-09 |
| Quick cold **~10.0 s** end to end; worker init **3.8 s**, API init 2.0 s | PROGRESS I2 C, 2026-11-09 |
| First-ever cold start on a new image: **~31.5 s** (10 s init timeout, then retry) | PROGRESS I1, 2026-11-07 (the case that justified 202/poll) |
| Brief's 3-company Deep query: **15.81 s**, 13 Gemini calls, checker 9/9 | PROGRESS I2 D, 2026-11-09 |
| Brief's Deep query, earlier image: 17.57 s, 10 Gemini calls, checker 25/25 | PROGRESS I2 check 1, 2026-11-09 |
| Worker max memory 275 MB of 1024 MB | PROGRESS I2 C |

### Latency locally (20 questions)

| Number | Source and date |
|---|---|
| Quick p50 **3.6 s → 3.0 s**, p90 **14.0 s → 4.8 s** (v1 → v2) | v2-results Local, 2026-11-06 vs v1-baseline 2026-11-05 |
| Deep p50 **16.4 s → 8.3 s**, p90 **47.1 s → 38.0 s** (v1 → v2) | same |

### Gemini calls per answer

| Number | Source |
|---|---|
| Quick **1.0** (AWS subset), 1.2 (local, includes failed attempts) | v2-results Deployed / Local |
| Deep **5.6** (AWS subset); **8.3 → 6.2** v1 → v2 (local, 20 questions) | v2-results Deployed / Local |

### Accuracy (frozen code scorer, same as v1's)

| Metric | v1 (20 Q, local, 11-05) | v2 local (20 Q, 11-06) | v2 deployed (5 Q, 11-09) | v2 deployed full 20 Q |
|---|---|---|---|---|
| Untraceable numbers, Quick | 3/105 (3%) | 3/140 (2%) | **0/65 (0%)** | PENDING (I4) |
| Untraceable numbers, Deep | 0/78 (0%) | 2/97 (2%) | **0/47 (0%)** | PENDING |
| Every-number-traces, Quick / Deep | 95% / 100% | 90% / 95% | 100% / 100% | PENDING |
| Routing, Quick / Deep | 80% / 100% | 80% / 100% | 80% / 100% | PENDING |
| Numeric lines with a citation, Quick | **44%** | **87%** | 87% | PENDING |
| Numeric lines with a citation, Deep | 88% | 90% | 79% | PENDING |
| Answered | 40/40 | 40/40 | 10/10 | PENDING |

Caveats you must be ready to state: the v1 corpus was three older 10-Q PDFs (1,491 chunks, no 10-K); v2 uses the latest 10-K/10-Q/8-K (934 chunks), so filing results are not like-for-like (v1-baseline "Comparability caveats"). The deployed subset is 5 questions, so don't compare its totals with v1's 20. "Derived" numbers (e.g. a computed growth rate) count as untraceable in the scorer.

### Other counts

| Number | Source |
|---|---|
| **934** filing chunks (AMD 375, INTC 292, NVDA 267) | PROGRESS A.10, 2026-11-05 |
| **407** unit tests passing | pytest run, 2026-11-11 |
| newman 20 requests / **30 of 30** assertions | PROGRESS I1, 2026-11-07 |
| Stack of 14 resources with the Watchdog off | PROGRESS I1 |
| Image 301 MB compressed | PROGRESS I2 B4 |

**Not measured, so not usable:** monthly cost, cold-start reduction from any cache, API-call reduction from caching, rate-limit failure reduction.

---

## 4. Proposed bullets

### (a) Using only numbers measured to date

1. **Built a serverless AI equity-research API on AWS (Lambda, API Gateway, DynamoDB, S3, SAM) with an async 202/poll job pattern; Quick answers return in 5.8 s median deployed.** (27 words)
   - *How does it work?* POST /ask writes a pending job to DynamoDB, fires the worker Lambda asynchronously and returns 202; the client polls GET /ask/{id}. I chose this because the first cold start took ~31 s, past API Gateway's 30 s limit.

2. **Designed a multi-agent Deep mode: a Gemini supervisor plans tasks for market, news and SEC-filings specialists that run in parallel and post to a shared evidence board.** (28 words)
   - *How does it work?* The planner returns structured JSON (companies plus tasks); each specialist is a Gemini function-calling loop (max 3 steps, shared deadline) that writes evidence E1…En; one optional follow-up round fills gaps; the writer must cite [E#]. Simple questions skip the planner via keyword rules, and any outage falls back to Quick mode.

3. **Built a fact-checker that verifies every number against cited evidence; deployed eval: 0 of 112 numbers untraceable, and Quick's citation coverage rose from 44% to 87% vs v1.** (29 words)
   - *How does it work?* Stage 1 is pure code: extract each number, normalise units and precision ("$57.0B" = "57,006 million"), and match it to the evidence text. Only misses go to one LLM call to judge whether they're derived, and unsupported numbers get flagged in the answer. The 44→87% figure is the 20-question local eval with the same frozen scorer; the 0/112 is the 10-answer deployed subset.

4. **Ingested latest 10-K, 10-Q and earnings 8-K filings from SEC EDGAR into a 934-chunk FAISS index on S3, with content-hash embedding caching so re-ingests never re-embed unchanged text.** (29 words)
   - *How does it work?* Section-aware chunking (215 words, 36 overlap), Gemini embeddings at 768 dims cached in SQLite by sha256 of model, dimension, task and text. The index is one .npz on S3, loaded once per Lambda container and kept in memory, with an ETag re-check every 5 minutes. Uploads are create-only/If-Match so two writers can't clobber each other.

Optional 5th, if you have room: **Shipped with 407 mocked unit tests in GitHub Actions CI, a frozen 20-question eval harness, SSM-managed secrets, change-set-reviewed deploys and a $1 budget alarm.** (27 words). *How?* moto for AWS, no network in tests; the scorer is a byte-identical port of v1's, so v1 and v2 are scored the same way.

### (b) Template for after the full deployed eval (I4)

Bullets 1, 2 and 4 stay as in (a). Replace bullet 3, and optionally extend bullet 1, once the full 20-question deployed run is in `docs/eval/v2-results.md`:

3'. **Built a code-based fact-checker that verifies every number against cited evidence; on a 20-question deployed eval, [U_quick+U_deep]/[N] numbers were untraceable and [C_quick]% of numeric lines cited sources.** (≈29 words)
   - Fill from the Deployed table: "Numbers not traceable" (Quick + Deep) and "Lines with numbers that carry a citation" (Quick). Only quote "vs v1 [3/105]" if the run is on all 20 questions.

1'. …**Quick answers in [P50_QUICK] s and multi-agent Deep answers in [P50_DEEP] s median across 20 deployed eval questions.** (replace the last clause of bullet 1)

Optional: **Cut Deep-mode Gemini calls per answer from 8.3 (v1) to [CALLS_DEEP] on the deployed eval** (only if the full-run number is lower; the local figure today is 6.2).

### Words to avoid (not supported)

"/tmp caching", "cut cold-start latency", "DynamoDB cache for market data/news", "reduced API calls / rate-limit failures by X", "$0/month" stated as a measured cost, "price alerts in production" (the Watchdog isn't deployed), "Streamlit UI".
