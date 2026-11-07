# PLAN — Research Desk (v2)

> Written by the Planner on 2026-11-04 from `docs/SPEC.md`, `docs/DECISIONS.md`, `docs/ASSESSMENT.md` and a
> read-through of v1 (`../investment-research-assistant`, code only).
> **Status: APPROVED rev 3, 2026-11-05. Submission: ✏️ [date].**
> Four phases, starting 2026-11-05. Phase D is a full day of docs and demo rehearsal.
> **Scope cuts versus SPEC are recorded in DECISIONS D13.** Where SPEC and this plan differ, D13 and this plan win.

**Legend**
- **CORE**: required by the brief, needed for a CORE item to work in the deployed demo, or something you asked for. Never cut.
- **STRETCH**: cut first if we fall behind ([§4](#4-cut-order)).
- 👤 = only you can do it.
- **PORT** = copy as is, apart from imports and deleting SPEC-cut branches. **ADAPT** = copy, then change. **REWRITE** = new code, with v1 as reference only. **NEW** = no v1 equivalent. **DROP** = not carried over.

**Brief coverage (all CORE):**

| Brief item | Covered by |
|---|---|
| Agent with tool orchestration | Deep mode |
| Market data tool | F1 |
| News tool | F2 |
| Knowledge base for 3–5 companies (FAISS) | F4 |
| API Gateway + Lambda | Phase B |
| Postman collection | Phase B |
| Python, AWS (Lambda, S3), SAM as IaC | Throughout |
| Error handling, logging, basic observability (JSON logs + alarms) | Throughout, Phase C |
| Decisions documented | DECISIONS.md |
| README, architecture diagram, 3+ demo queries, design doc | Phase D |

Bedrock is a recorded deviation (D1).

---

## 0. Pre-flight findings (checked 2026-11-04)

### 0.1 Path and config mismatches
| # | Finding | Resolution |
|---|---|---|
| M1 | The v1 path in the prompt was a placeholder. The real path is `C:\Shail\Projects\investment-research-assistant`. A stale `-v2` stub existed. | 👤 You're deleting the stub. |
| M2 | The repo was already initialised. AGENT.md had an uncommitted change. | Done (committed). |
| M3 | The eval report path was `docs/EVAL.md`. | SPEC now says `docs/eval/` (`v1-baseline.md`, `v2-results.md`). |
| M4 | The README calls `data/raw/` "press releases, in the repo". The files are git-ignored and are actually three old 10-Q PDFs: AMD Q1 2025, INTC Q3 2025, NVDA Q3 FY2026. | Reword the README: local only, not used by v2 (task A.0). No download script (D13). |
| M5 | `.gitignore` has a v1-only `aws_artifacts/` line. | Delete it (A.0). |
| M6 | `core/logging.py` shadows the stdlib module. | SPEC now says `core/log.py`. |
| M7 | v1's `.env` uses `gemini-3.8-flash`. | Moot now that the rerun is skipped. v2 verifies model names in A.4. |
| M9 | The v1 API reads `{"query"}`, but SPEC says `{"ticker" or "name"}`. | Follow SPEC. |
| M10 | The v1 scheduler uses a UTC cron. | Use `America/New_York` (B.6). |

### 0.2 Secrets and `.gitignore`
- No secrets in the folder or its git history. The v2 `.env` exists and is ignored.
- Task A.0 adds these lines: `.env.*`, `!.env.example`, `env.json`, `*.db`, `*.sqlite*`, `*.faiss`, `data/index/`, `data/cache/`, `vectors/`, `evals/results/`, `.coverage`, `htmlcov/`.
- The golden fixture in `tests/fixtures/` **is** committed. It holds answers and public data only.

### 0.3 The frozen eval scorer
- The scoring lives in v1's `evals/run.py`: `QUICK_TOOL_TO_AGENT`, `CITATION`, `score`, `pct`, `quantile`, `summarise`.
- It depends on `core/agents/checker.py` stage 1: `verify_numbers`, `extract_numbers`, `_supported`, `Number`, `_NUM`, `SCALES`, `REL_TOLERANCE`, `TABLE_UNITS`, `UNVERIFIED_MARK`.
- `checker.py` imports `google.genai`, so stage 1 is split out into `core/numbers.py`.
- **Golden reference:** v1's saved results from 2026-11-03, re-scored read-only with v1's code, reproduce SPEC §9 exactly.

  | Mode | Answered | Unmatched | Companies | Agents | Citations | p50 / p90 | Calls | Other |
  |---|---|---|---|---|---|---|---|---|
  | Quick | 20/20 | 1/109 | 100% | 75% | 51% | 4.7 / 29.5 s | 1.0 | |
  | Deep | 20/20 | 0/98 | 100% | 95% | 90% | 23.4 / 49.3 s | 6.5 | 1 follow-up, 2 fallbacks |

- **Result contract** that v2 must keep (tested in A.13):
  - Keys: `answer`, `context`, `used_tools`, `companies[].ticker`, `mode`, `error`, `trace.rounds`, `trace.checker.unsupported`, `trace.fallback`.
  - Quick tool names: `market` / `news` / `rag`. Deep tool names: `market` / `news` / `filings`.
  - Citations: `[E#]` or `[Doc N]`.
  - The scorer also uses `llm.calls_made()`.
- **Caveats** (these go in `v1-baseline.md`):
  - The baseline is dated **2026-11-03** (v1 was not re-run, D13).
  - v1's knowledge base was the three old 10-Q PDFs: 1,491 chunks, no 10-K, no filing metadata.
  - Live data differs between days, so v1 vs v2 is indicative rather than same-day.

### 0.4 Decisions on record
| Date | Decision |
|---|---|
| 11-04 | "NVIDIA's Q3" means the latest filed quarter. Eval reports go in `docs/eval/`. `include_context` is eval-only (D10). Holidays are a known gap: no session means no alert (D12). `core/log.py`. Embeddings are paid for once (D11). |
| 11-05 | Four phases. Cuts are listed in D13. The v1 rerun is skipped and the 11-03 golden results are the baseline. The Reviewer runs only after Phases A and B. Start date 2026-11-05. Accounts ready: AWS ap-south-1, Finnhub, Gemini, SEC email. Telegram is needed before Phase B. |

---

## 1. Reuse table (every v1 module)

| v2 file | v1 source | Verdict | Why |
|---|---|---|---|
| `evals/scoring.py` | `evals/run.py`: `QUICK_TOOL_TO_AGENT, CITATION, score, pct, quantile, summarise` | **PORT verbatim, frozen** | Keeps v1 and v2 scores comparable (D8). |
| `core/numbers.py` | `core/agents/checker.py` stage 1 (`UNVERIFIED_MARK` … `verify_numbers`) | **PORT verbatim, frozen** | The scorer depends on it. Splitting it out removes the `google.genai` import. |
| `evals/run.py` | `evals/run.py` runner | ADAPT | `--target local\|api`; `--baseline <json>` writes `v1-baseline.md`. Imports the frozen scoring. |
| `core/agents/checker.py` | same | ADAPT | Stage 2 kept. Stage 1 imported from `core/numbers.py`. |
| `core/config.py` | same | ADAPT | Drop Supabase. Validate the backends. |
| `core/log.py` | `config.setup_logging` | REWRITE | JSON logs plus a request-ID contextvar. |
| `core/llm.py` | same | PORT | Already matches SPEC §6. |
| `core/embeddings.py` | same | ADAPT | **Local** embedding cache in `data/cache/embeddings.sqlite`, keyed by `sha256(model\|dim\|task_type\|text)`. No S3 snapshot (D13). |
| `core/market.py` | same | ADAPT | Finnhub fallback, `source`, QQQ/SPY, `trading_date`. |
| `core/news.py`, `core/tickers.py`, `core/sec.py` | same | PORT | Match SPEC. |
| `core/ingest.py` | helpers from `core/filings.py` (`html_to_text, document_text, relevant_section, chunk_words`) and `src/rag/build_embeddings.py` batching | REWRITE | SEC filing → sections → 300/50 chunks → cached embeddings → index. Skips already-indexed chunk hashes. **This is also the manual ingest CLI** (replaces auto-ingest, D13). |
| `core/vectors.py` | same | ADAPT | Local + S3 (in-memory copy per container re-checked by ETag, `If-Match` on upload). No pgvector. Header carries `model`, `dim` and per-row `chunk_hash`. Lambda only reads. |
| `core/retriever.py` | same | ADAPT | Drop pgvector. Keep balanced retrieval. |
| `core/agent.py` | same | PORT | Quick mode. Already meets the scorer contract. |
| `core/agents/{evidence,loop,specialists,supervisor,deep}.py` | same | PORT | Implement SPEC §6. Their tests pass. |
| `core/agents/tools.py` | same | ADAPT | Filing metadata in citations. |
| `core/alerts.py` | same | ADAPT | Skip when there's no session today (D12). |
| `core/explain.py` | same | PORT | Session window. |
| `core/telegram.py` | same | PORT | |
| `core/store.py` | same | ADAPT | LocalStore only. `get_store()` returns `local` or `dynamodb`. |
| `core/store_dynamo.py` | same | PORT | SPEC §4. |
| `core/desk.py` | same | ADAPT, folded into `app.py` | Keep `ApiDesk` only. |
| `core/filings.py`, `core/watchdog.py` | | **DROP** | Filing alerts, XBRL snapshot and auto-ingest are cut (D13). The helpers move into `ingest.py`. |
| `core/mailer.py`, digest files | | DROP | D3 |
| `aws_lambda/runtime.py`, `notify.py` | same | PORT | |
| `aws_lambda/scan.py` | same | ADAPT | Prices only, no filings. |
| `aws_lambda/api.py` | same | ADAPT | SPEC §5, validation, `include_context`, request ID. |
| `aws_lambda/ask_worker.py` | same | ADAPT | Returns `context` only with `include_context` (capped at 100 KB). |
| `jobs/scan.py` | same | ADAPT | Prices only. Price-move replay, `--dry-run`, holiday skip. Delivery via Streams. |
| `cli.py` | same | ADAPT | `market \| news \| resolve \| retriever \| ingest \| agent [--deep] \| scan [--dry-run]` |
| `app.py` | same | ADAPT | API client only. |
| `scripts/bootstrap.py` | `scripts/aws_bootstrap.py` | ADAPT | SSM secrets; create-only index upload (`--replace` to update); seed the watchlist. No cache push/pull (D13). |
| `template.yaml` | same | ADAPT | Drop Digest/SES and filing resources. ECR repo with a keep-2 lifecycle. Throttling. 14-day logs. Least-privilege IAM. Retain policy on the bucket. **No `Tracing: Active`, no dashboard** (D13). |
| `Dockerfile`, `.dockerignore` | same | PORT | `.dockerignore` excludes `data/`. |
| `.github/workflows/ci.yml` | same | PORT | `pytest` on push. |
| `deploy.yml`, `scan.yml`, `infra/github-deploy-policy.json` | | **DROP** | Manual `sam deploy` instead (D13); D4. |
| `scripts/load_vectors.py`, `db/schema.sql`, `src/*` | | DROP | Supabase and PDF pipeline. |
| `tests/unit/*` | same | PORT with their modules | Add `pyyaml`, `boto3` and `moto` to dev requirements. |
| `requirements*.txt`, `pytest.ini` | same | ADAPT | Drop `supabase`. Pin versions. |
| `postman/…collection.json` | | NEW | SPEC F13 |
| `evals/questions.json` | same | PORT | Already in the repo. |

---

## 2. Phases

Work on branch `phase-<A|B|C|D>-<name>`, with one commit per task and `python -m pytest` after each task. A phase is done only when every acceptance check has actually been run. **The Reviewer runs after Phase A and after Phase B only.** For C and D the Builder self-verifies and records the evidence in PROGRESS.md.

### Phase A — Local core, knowledge base, agents, local eval *(2026-11-05 → 11-06)*
**Goal:** the brief's example query answered on a laptop, with all three specialists, citations and a fact-check. Scores are comparable to v1 on the frozen scorer.

**Demo:**
- `cli.py agent --deep "<brief query>"` gives a cited answer.
- `cli.py market NVDA` (with Yahoo failing) shows `source: finnhub`.
- Re-running ingest makes 0 embedding calls.
- `docs/eval/v1-baseline.md` and the Local section of `v2-results.md`.

| # | Task | CORE/STRETCH |
|---|---|---|
| A.0 | **Housekeeping.** Confirm `git status` is clean. Extend `.gitignore` (§0.2). Reword the README's `data/raw` line (M4). Add `.env.example` with blank `GEMINI_API_KEY, FINNHUB_API_KEY, SEC_USER_AGENT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APP_ACCESS_CODE` plus `STORE_BACKEND=local, VECTOR_BACKEND=local`. Add `requirements*.txt`, `pytest.ini` and package `__init__`s. | CORE |
| **A.1** | **Port the eval scorer verbatim, before any other module.** (a) `core/numbers.py` is v1 checker stage 1 with identical bodies, stdlib only. (b) `evals/scoring.py` is v1's scoring functions with identical bodies. The only change is `from core import numbers as checker`. Header: source path, v1 commit `73b6fda`, "FROZEN — D8". (c) `tests/fixtures/v1_eval_2026-11-03.json` is a copy of v1's `evals/results/latest.json`. `test_eval_scoring.py` asserts `summarise()` equals §0.3 exactly, and that both modules import with `google.genai` blocked. (d) Port v1's stage-1 checker tests. | CORE |
| A.2 | **`docs/eval/v1-baseline.md`** generated from the fixture by the frozen scorer: `python -m evals.run --baseline tests/fixtures/v1_eval_2026-11-03.json`. It states "run 2026-11-03, not re-run (D13)", the model chain is unrecorded, the KB was three old 10-Q PDFs, and it lists the caveats. (A minimal runner stub for this flag is fine; the full runner comes in A.13.) | CORE |
| A.3 | `core/config.py` (ADAPT), `core/log.py` (REWRITE: JSON, `request_id`, `log_tool`), `ci.yml` (PORT). | CORE |
| A.4 | `core/llm.py` (PORT) and tests. List the models available to the key. Set the default chain to models that exist and record it in PROGRESS.md. | CORE |
| A.5 | `core/market.py` (ADAPT: Finnhub, `source`, QQQ/SPY, `trading_date`) and tests. | CORE |
| A.6 | `core/news.py`, `core/tickers.py`, `core/sec.py` (PORT) and tests. | CORE |
| A.7 | `core/store.py` (ADAPT, LocalStore) and tests. `cli.py market \| news \| resolve`. | CORE |
| A.8 | `core/embeddings.py` (ADAPT, local cache: same text twice → 1 API text; model change → cache miss). `core/vectors.py` (ADAPT, Local + S3 read path, header checks, moto tests). | CORE |
| A.9 | `core/ingest.py` (REWRITE) = `cli.py ingest <TICKER…> [--dry-run]`. For each ticker: the latest filed 10-K, 10-Q and earnings 8-K (EX-99.1). Sections: Items 1, 1A, MD&A, segment and concentration-of-revenue notes. Then 300/50 chunks, skip existing `chunk_hash`, cached embeddings under `FILING_EMBED_DAILY_CAP=400`, and save the index. The dry run prints the filings, sections, chunk counts and how many chunks need embedding. | CORE |
| A.10 | **Build the KB** for NVDA, AMD and INTC. Start it early, because the embedding quota may need both days. Record the accessions, chunk counts and embedding calls in PROGRESS.md. | CORE |
| A.11 | `core/retriever.py` (ADAPT, balanced) and `cli.py retriever`. `core/agent.py` Quick (PORT) and `cli.py agent`. | CORE |
| A.12 | `core/agents/*` (PORT; `tools.py` ADAPT; `checker.py` imports `core.numbers`) with failure-path tests: malformed JSON, LLM timeout → falls back to Quick, 75 s budget → partial answer. `cli.py agent --deep`. | CORE. Stage 2 of the checker is STRETCH |
| A.13 | `evals/run.py` (ADAPT: `--target local\|api`, results in `evals/results/v2-<target>.json`, renders `docs/eval/v2-results.md`) plus a **contract test** that mocked Quick and Deep results carry every key the scorer reads. Run `--target local` → Local section, compared with v1's 11-03 baseline by question type (with the day-drift caveat). | CORE |

**Acceptance:**
- `pytest` passes, including the golden scorer test (exact match to §0.3).
- `v1-baseline.md` exists, and its numbers equal §0.3.
- With yfinance mocked to fail, the test gets `source == "finnhub"`. `cli.py market QQQ` works live.
- `cli.py resolve "nonexistent co"` gives a friendly "not found".
- `cli.py ingest NVDA AMD INTC --dry-run` shows an NVDA concentration-of-revenue section. A second real ingest logs `api_texts=0`.
- `cli.py retriever "Compare NVIDIA and AMD data center revenue"` returns at least one chunk for each company.
- The Deep brief query's trace shows all 3 specialists and the checker, and every numeric line is cited.
- A planted fake number is flagged ⚠️unverified (test).
- `v2-results.md` has its Local section.
- `git check-ignore .env.local data/index/x.faiss data/cache/embeddings.sqlite local.db` matches all four paths.

**Risks:**
- Gemini model names may have changed (A.4).
- Embedding quota: mitigated by section filtering, the daily cap, the cache and the dry run.
- NVIDIA's 10-K names customers only as "Customer A…", so named buyers come only from news, labelled as such.
- Fiscal periods differ by company, so answers must state each company's period.

**→ Reviewer: Phase A.**

### Phase B — AWS deploy, Postman, price alerts + Telegram + DLQ *(2026-11-07 → 11-08)*
**Goal:** the agent runs behind API Gateway + Lambda in ap-south-1 at about $0, and a ±3% move reaches Telegram exactly once.

**Demo:**
- Postman: `/health`, then `POST /ask` (Deep, brief query), then poll for the cited answer.
- Invoke `ScanFunction` twice with a replayed INTC −7.5% move: one FEED item and one Telegram message with a "why".
- A forced Telegram failure lands in the DLQ and the alarm fires.

| # | Task | CORE/STRETCH |
|---|---|---|
| B.0 | 👤 **Before Phase B:** set `TELEGRAM_CHAT_ID` (message the bot, then `getUpdates`) and `APP_ACCESS_CODE` in `.env`. Start Docker Desktop. Provide the Budget email. | — |
| B.1 | `core/store_dynamo.py` (PORT) and moto tests. | CORE |
| B.2 | `template.yaml` (ADAPT), ask path: ECR repo with lifecycle, HttpApi with throttling, `ApiFunction`, `AskWorkerFunction`, `ResearchDesk` table (2/2, TTL, Streams), data bucket (public access blocked, SSE, `DeletionPolicy`/`UpdateReplacePolicy: Retain`), 14-day log groups, SSM read on `/research-desk/*`, $1 Budget. `test_template.py` (PORT) asserts the never-list and the Retain policies. | CORE |
| B.3 | `Dockerfile`, `.dockerignore`, `runtime.py` (PORT), `api.py`, `ask_worker.py` (ADAPT) and tests (`include_context` defaults off; when on, the context is capped). | CORE |
| B.4 | `scripts/bootstrap.py` (ADAPT): SSM secrets (values never printed), create-only upload of `vectors/index.npz`, watchlist seed. | CORE |
| B.5 | `sam validate --lint` → `sam build` → **show the change set → 👤 approve** → `sam deploy` → `python -m scripts.bootstrap`. Then the Postman collection (every SPEC §5 route, `{{base_url}}`, `{{access_code}}`, a polling test) and a live smoke run. | CORE |
| B.6 | `core/alerts.py` (ADAPT, no-session skip), `core/explain.py` (PORT), `jobs/scan.py` (ADAPT, prices only, replay, `--dry-run`), `cli.py scan`, with tests: duplicate scan → 1 alert; after-hours headline → next session; holiday or stale quote → 0 alerts. | STRETCH, cut last |
| B.7 | `core/telegram.py`, `aws_lambda/scan.py`, `aws_lambda/notify.py` (PORT). Template additions: `ScanFunction`; Scheduler `cron(*/15 9-16 ? * MON-FRI *)` in `America/New_York`, with the handler dropping runs outside 9:30–16:00; `NotifyFunction` with a Stream filter on FEED INSERTs; SQS DLQ; DLQ alarm → SNS email. Deploy (👤 approves), then run a live replay and a forced failure. | STRETCH, cut last |

**Acceptance:**
- `sam validate --lint` is clean and `sam build` succeeds.
- `curl $URL/health` returns `{"ok":true}`. A request without the access code gets a 401 JSON error.
- `/ask` returns 202, then `done` with sources, in both modes. There's no `context` unless it was requested.
- A Postman run passes.
- The image has no `data/` directory.
- A second `sam deploy` leaves the index ETag unchanged and makes 0 embedding calls.
- Two replay invokes produce 1 alert, and it reaches the phone.
- The forced failure lands in the DLQ and the alarm goes to ALARM.

**Risks:**
- Yahoo blocks AWS IPs, so expect `source: finnhub`.
- Cold start and FAISS load time.
- Image size: keep it under about 1 GB.
- Market hours are 20:00–02:30 IST, so the demo uses the replay.

**→ Reviewer: Phase B.**

### Phase C — Streamlit, logs and alarms, deployed eval *(2026-11-09)*
**Goal:** a demo UI, visible operations and measured numbers on AWS.

**Demo:**
- Streamlit URL: the brief query in Deep mode, with the trace expander.
- `aws logs tail` shows a single `request_id` across the API and the worker.
- `v2-results.md` has its Deployed section.

| # | Task | CORE/STRETCH |
|---|---|---|
| C.1 | JSON logs with the request ID carried from API → worker → agents → tools. EMF metrics from SPEC §7 (via `runtime.emit_metrics`). Alarms: API 5xx, scan errors, Deep p90, DLQ (already done in B.7), all → SNS email. | CORE (logs + API 5xx alarm). Other alarms: STRETCH |
| C.2 | `app.py` (ADAPT, API client: Quick/Deep toggle, polling, trace). 👤 Deploy to Streamlit Community Cloud with the secrets `DESK_API_URL` and `APP_ACCESS_CODE`. | STRETCH |
| C.3 | `python -m evals.run --target api --url $URL` (sends `include_context: true`) → Deployed section. Measure cold start (`REPORT Init Duration`, 5 cold invokes) and the in-memory index copy hit rate. | CORE |

**Acceptance:**
- The Streamlit URL answers in both modes.
- One `request_id` is visible across both functions.
- Alarms exist, and a forced API 5xx (a test route or a bad payload) triggers its alarm.
- The Deployed section reports every scorer metric, including untraceable numbers.
- PROGRESS.md records how each check was run.

### Phase D — Docs and demo *(2026-11-10, full day; 11-11 buffer)*
| # | Task |
|---|---|
| D.1 | `README.md`: overview, architecture image, local setup (Gemini key only; `cli.py ingest` builds the KB), AWS deploy steps, Postman and Streamlit links, eval summary, cost. |
| D.2 | `docs/architecture.png`, exported from SPEC §3 (mermaid-cli or draw.io), with X-Ray, the dashboard and OIDC removed from the drawing. |
| D.3 | `docs/DESIGN.md` (1–2 pages): design choices (D1–D13, especially Bedrock), challenges, and a **production improvements** section that lists the D13 cuts (download script for `data/raw`, S3 embedding-cache snapshot, X-Ray, CloudWatch dashboard, OIDC deploy, SEC filing alerts + XBRL earnings snapshot, auto-ingest on watchlist add and on new filings) plus a Bedrock adapter, a market-holiday calendar, OpenSearch at scale, auth and multi-user. |
| D.4 | `docs/AWS_SETUP.md`: the 👤 steps, click by click. |
| D.5 | Resume claims (§6), using measured numbers only. Check cost after at least 48 h. |
| D.6 | Rehearse the demo twice against the deployed stack (15 + 20 + 15 min). Record a backup capture. No eval runs on demo day. Check EDGAR for a new NVIDIA quarter; if there is one, run `cli.py ingest NVDA` and `bootstrap --replace` (§3 row 5). |

**Acceptance:**
- Every item in SPEC §11 is done.
- The demo script runs without edits.
- A fresh clone works from the README: clone → `.venv` → `pytest` → `cli.py market NVDA`.

---

## 3. Demo script
| # | Mode | Query | Tools / agents | Answered from |
|---|---|---|---|---|
| 1 | Deep | The brief's query: NVIDIA "Q3", AMD and Intel growth, GPU customers, threats, sentiment, risk | Supervisor → Market (`get_quotes` NVDA, AMD, INTC, QQQ, SPY), News (`get_news` ×3), Filings (`search_filings`, `latest_filing`); checker | NVDA's latest filed 10-Q / 8-K (period stated); AMD's and INTC's latest 10-Q / 8-K; NVDA concentration-of-revenue note (customers anonymised; named buyers come from news, labelled); NVDA 10-K Item 1 "Competition" and 1A; headlines; QQQ/SPY |
| 2 | Quick | "What is AMD's market cap and P/E ratio, and what are the latest headlines?" | `market`, `news` | Quote (with `source`), RSS links |
| 3 | Deep | "Compare NVIDIA's and AMD's data center segment revenue" | Filings | Segment notes, at least one chunk per company |
| 4 | Watchdog | Replay an INTC −7.5% move | Scan → DynamoDB → Stream → Notify → Telegram | Market check, a session-window headline, or "no clear catalyst found" |
| 5 | *(optional)* | If NVIDIA has filed a new quarter: `cli.py ingest NVDA` + `bootstrap --replace`, then a Deep question that cites it | Manual ingest | The new filing; existing chunks are not re-embedded |

---

## 4. Cut order
If we fall half a day or more behind, cut in this order:
1. Fact-checker stage 2 (the LLM "derived" check). The code stage stays.
2. Extra alarms beyond API 5xx and DLQ (C.1).
3. C.2 Streamlit. Postman already covers the brief.
4. B.6/B.7 ±3% price alert + Telegram + DLQ. **Cut last; keep it if at all possible.**

**Never cut:** Phase D, the frozen scorer (A.1) and its baseline (A.2), the local embedding cache, and every CORE item.

---

## 5. Cross-phase risks
- **Gemini free tier.** It's used for generation, embedding, the eval and the demo. Mitigations: the model chain and cooldown, `DEEP_MODE_DAILY_CAP`, the embedding cache, no eval runs on demo day, and a recorded backup.
- **v1 baseline is from another day.** It's from 11-03, not re-run, so live-data questions drift. `v2-results.md` reports by question type and says so (D13).
- **Holidays.** No session means no alert (D12). The next holiday is 2026-11-26.
- **Losing the index.** The bucket has a Retain policy and the upload is create-only. The local cache plus `data/index/` can rebuild it without embedding again.

---

## 6. Resume claims to measure. Fill these in with measured numbers only.
| Claim | How to measure | Value |
|---|---|---|
| Deep / Quick p50 and p90 | `v2-results.md`, Deployed section | _tbd_ |
| Routing, cited lines, untraceable numbers vs v1 | `v2-results.md` vs `v1-baseline.md`, same frozen scorer | _tbd_ |
| Alert latency (scan → Telegram) | `FEED.created_at` vs the notify log's `sent_at`, 5 replays, p50 | _tbd_ |
| Cold start | `REPORT Init Duration`, 5 cold invokes per function | _tbd_ |
| In-memory index copy hit rate | EMF `IndexCacheHit` / `IndexCacheMiss` over the deployed eval | _tbd_ |
| Embedding reuse | `cache_hits / (cache_hits + api_texts)` across rebuilds | _tbd_ |
| Cost per month | Cost Explorer after ≥48 h | _tbd_ |
| Gemini calls per question | scorer `calls` | _tbd_ |

---

## 7. Proposed additions (outside the phases, need your OK)
- **`cli.py models`**: lists the Gemini models available to the key (about 5 lines).

---

## 8. Still open
1. **Submission date:** still ✏️. The schedule assumes Phase D on 2026-11-10 with 11-11 as buffer.
2. **SPEC text** still describes the full design. D13 records the cuts, so the Builder and Reviewer follow this plan. Say if you want SPEC edited to match as well.
