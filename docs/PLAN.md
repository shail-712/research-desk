# PLAN — Research Desk (v2)

> Written by the Planner on 2026-11-04 (Wed) from `docs/SPEC.md`, `docs/DECISIONS.md`, `docs/ASSESSMENT.md`
> and a read-through of v1 (`../investment-research-assistant`, code only). **Status: awaiting approval.**
> Six phases, one per day, Day 7 is buffer. Phase 6 is a full day of docs and demo rehearsal.

**Legend**
- **CORE**: the brief requires it, a CORE item needs it to work in the deployed demo, or you asked for it. Never cut.
- **STRETCH**: cut first if we fall behind, in the order in [§4 Cut order](#4-cut-order). The SPEC calls these must-haves, but the brief doesn't require them.
- 👤 = only you can do it (credentials, accounts, approvals). These are placed early so they don't block.
- **PORT** = copy as is, apart from import paths and deleted SPEC-cut branches. **ADAPT** = copy, then change behaviour or interface. **REWRITE** = write new, using v1 for reference only. **DROP** = not carried over.

---

## 0. Pre-flight findings (checked 2026-11-04)

### 0.1 Path and config mismatches. You need to fix or confirm these.
| # | Where | Says | Reality | Suggested fix |
|---|---|---|---|---|
| M1 | Your Planner prompt | v1 at `C:\Shail\Projects\<v1-folder-name>` | Placeholder. The real v1 is `C:\Shail\Projects\investment-research-assistant`, as AGENT.md says. A second folder, `investment-research-assistant-v2` (Aug, 6 files, a stale stub), is easy to confuse with it. | Fill in the prompt template. Rename the stub to `_old-v2-stub` or archive it. |
| M2 | AGENT.md "How to run" step 1 | `git init`, commit, create the GitHub repo | Already done: `main` with 1 commit, `origin` = `github.com/shail-712/research-desk`. One **uncommitted** change in AGENT.md (title line "Research Desk v2" → "Research Desk"). | Commit it (👤 or Builder task 1.0). Otherwise the Builder's `git status` check stops on it. |
| M3 | SPEC §2 F14 and §8 vs your request | v2 eval report at `docs/EVAL.md`. v1 baseline at `docs/eval/v1-baseline.md`. | Two eval locations. `docs/eval/` isn't in SPEC §8. v1 wrote `docs/05_EVAL.md`. | Pick one (question Q3). The plan assumes `docs/eval/` for everything: `v1-baseline.md`, `v2-local.md`, `v2-deployed.md`. |
| M4 | README.md | "`data/raw/` holds the v1 earnings press releases" | They exist locally, but `.gitignore` has `data/raw/*`, so they are **not in the repo**. A fresh clone won't have them. v2's KB (SPEC F4) uses SEC filings and doesn't need them. | Reword the README: "local only, not used by v2". Or drop the folder. |
| M5 | `.gitignore` | `aws_artifacts/` "Old AWS-era index copy" | A v1-only path. It doesn't exist in v2. | Delete the line (task 1.0). |
| M6 | SPEC §8 | `core/logging.py` | It shadows the stdlib `logging` module whenever a file in `core/` runs as a script, or `core/` lands on `sys.path`. | Rename it to `core/log.py` (question Q6). The plan uses `core/log.py`. |
| M7 | SPEC §6 model chain vs v1 `.env` | Chain starts at `gemini-3.5-flash-lite` | v1's `.env` sets `GEMINI_MODEL=gemini-3.8-flash`, so v1 currently runs a different primary model than the SPEC chain. v1's saved results don't record which chain produced them (`_models` is empty). | The rerun records the chain it actually used (task 1.2). |
| M8 | SPEC §2 F14 | `python -m evals.run` uses "v1's" questions.json | Identical to v1's apart from the `_about` text ✓ | None |
| M9 | SPEC §5 `POST /watchlist` | Body `{"ticker" or "name"}` | v1 API reads `{"query"}` (falls back to `ticker`). | Follow SPEC. The Postman collection uses `ticker`/`name`. |
| M10 | SPEC §3 Scheduler | "Mon–Fri 9:30–16:00 New York" | v1 template uses a UTC cron `7/15 13-21`, which drifts by an hour across DST. | `ScheduleExpressionTimezone: America/New_York` (task 4.4). |

These AGENT.md references were checked and are correct: `../investment-research-assistant` and its `core/explain.py`, `core/agents/checker.py`, `core/store_dynamo.py`, `core/sec.py`, `template.yaml`, `tests/`. SAM CLI is at `C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd` ✓, Python 3.12 is available via `py -3.12` ✓, Docker 29.1 is installed ✓.

### 0.2 Secrets and `.gitignore`
- **No secrets in the folder or git history.** I grepped for Gemini/AWS/Telegram/OpenAI key patterns and `key=`/`token=` assignments across all files and `git log -p --all`: 0 hits. There is no `.env` in v2 yet.
- v1's `.env` holds a real `GEMINI_API_KEY`. Never copy v1's `.env` into this repo.
- **Covered:** `.env`, `.streamlit/secrets.toml`, `.venv/`, `.cache/` (SQLite if it stays there), `data/processed/*` (FAISS if it stays there), `.aws-sam/`.
- **Not covered** (tested with `git check-ignore`): `.env.local` and other `.env.*` files, a FAISS index anywhere other than `data/processed/` (`*.faiss`, `*.npz`, `*.bin`), SQLite outside `.cache/` (`*.db`, `*.sqlite`), and `env.json` (`sam local` env-vars file, often holds secrets). Task 1.0 adds:
  ```
  .env.*
  !.env.example
  env.json
  *.db
  *.sqlite*
  *.faiss
  data/index/
  vectors/
  evals/results/
  .coverage
  htmlcov/
  ```
  `evals/results/` is ignored because raw answers can be regenerated. The **golden fixture** used by the scorer test lives in `tests/fixtures/` and *is* committed. It contains only answers and public market data, no secrets.

### 0.3 v1 eval scoring: what "port unchanged" means
- The scoring lives in `evals/run.py`: `QUICK_TOOL_TO_AGENT`, `CITATION`, `score()`, `pct()`, `quantile()`, `summarise()`.
- It is **not self-contained**. `score()` calls `core/agents/checker.py: verify_numbers()` and uses `UNVERIFIED_MARK`. Those depend on `extract_numbers`, `_supported`, `Number`, `_NUM`, `SCALES`, `REL_TOLERANCE` and `TABLE_UNITS`.
- `checker.py` imports `google.genai` and `core.llm` at module top. So the pure stage-1 code must be split out before the scorer can be ported "before any other module".
- In v1's terms:
  - **"Routing accuracy"** = `agents_ok` ("Right tools/agents used"): 75% Quick → 95% Deep.
  - **"Cited figures"** = `cited_lines / numeric_lines`: 51% → 90%.
- **The golden reference reproduces exactly.** I re-scored v1's saved `evals/results/latest.json` (2026-11-03) read-only with v1's own code:
  - Quick: 20/20 answered, unmatched 1/109, companies 100%, agents 75%, citations 51%, p50 4.7 s, p90 29.5 s, 1.0 calls.
  - Deep: 20/20, unmatched 0/98, companies 100%, agents 95%, citations 90%, p50 23.4 s, p90 49.3 s, 6.5 calls, 1 follow-up, 2 fallbacks.
  - This matches SPEC §9, so the v2 port can be tested against it to the character.
- **The scorer also fixes a result contract.** Every v2 answer dict must carry these keys, or scores aren't comparable (tested in task 2.7):
  - `answer`, `context` (evidence text), `used_tools`, `companies[].ticker`, `mode`, `error`
  - `trace.rounds`, `trace.checker.unsupported`, `trace.fallback`
  - Quick tool names are `market`/`news`/`rag`. Deep `used_tools` are `market`/`news`/`filings`.
  - Citations look like `[E#]` (Deep) or `[Doc N]` (Quick).
  - The runner also needs `llm.calls_made()`.
- **Comparability caveats** (recorded as DECISIONS D8/D9):
  1. **v1's local KB held only the 3 press-release PDFs.** That's 1,491 chunks (Intel 912, AMD 359, NVIDIA 220) with no `form`/`url`/`section` metadata. v2 indexes 10-K/10-Q/8-K. So filings-question scores change because the *corpus* changed, not only the code.
  2. Live prices and news change daily. Compare v1 and v2 runs from the same day.
  3. **v1's deployed `/ask` drops `context`** (`ask_worker._compact`), so "unmatched numbers" can't be scored against the deployed stack without a change (question Q4).
- v1 test health: the agents, explain, watchdog, filings, market, tickers and alerts test files pass in v1's `.venv`. The AWS tests (moto) can't run there because `boto3`/`moto`/`pyyaml` aren't installed, so they are unverified. They're carried over and must pass in v2.

---

## 1. Reuse table (every v1 module)

| SPEC component (v2 file) | v1 source | Verdict | Why |
|---|---|---|---|
| `evals/scoring.py` | `evals/run.py`: `QUICK_TOOL_TO_AGENT, CITATION, score, pct, quantile, summarise` | **PORT, verbatim, frozen** | You asked for this, so v1 and v2 scores are comparable. Only the import line changes. |
| `core/numbers.py` | `core/agents/checker.py` lines `UNVERIFIED_MARK` … `verify_numbers` (pure code) | **PORT, verbatim, frozen** | The scorer depends on it. Splitting it out removes the `google.genai` import so the scorer is pure stdlib. |
| `evals/run.py` (runner) | `evals/run.py`: `run_one, load/save_results, write_report, main` | ADAPT | Output paths (`docs/eval/…`). An `--api URL` option for the deployed eval. Report title. Scoring imported from `evals/scoring.py`. |
| `core/agents/checker.py` | same | ADAPT | Stage 2 (LLM `derived`/`unsupported`) is kept as is. Stage 1 is imported from `core/numbers.py`. |
| `core/config.py` | same | ADAPT | Drop the Supabase comments. Add `STORE_BACKEND`/`VECTOR_BACKEND` validation (`local`, `dynamodb`, `s3`). |
| `core/log.py` (SPEC: `logging.py`) | `config.setup_logging` (plain text) | REWRITE | SPEC needs JSON logs with a request ID carried API → worker → agents → tools. v1 logs plain text. |
| `core/llm.py` | same | PORT | The chain, timeouts, cooldown and `LLMUnavailableError` already match SPEC §6. Only the default model names are re-verified on day 1. |
| `core/embeddings.py` | same | PORT | Already `gemini-embedding-001`, 768-d, L2-normalised, token-batched. |
| `core/market.py` | same | ADAPT | Add the **Finnhub fallback** and a `source` field on every quote, plus QQQ/SPY. v1 is yfinance only. |
| `core/news.py` | same | PORT | Matches SPEC F2. |
| `core/tickers.py` | same | PORT | Matches SPEC F3, including the offline NVDA/AMD/INTC map. |
| `core/sec.py` | same | PORT | User-Agent and 10 req/s throttle already done. |
| `core/xbrl.py` | `core/watchdog.py` (`xbrl_financials, fetch_facts, compare, pct_change, extract, _quote_supports, build_report`) | ADAPT | SPEC splits XBRL/snapshot into its own module. The logic is proven: v1 tests pass. |
| `core/ingest.py` | `src/rag/*` (PDF pipeline) + `core/filings.py` (`html_to_text, document_text, relevant_section, chunk_words`) + `src/rag/build_embeddings.py` (batching, daily cap) | REWRITE | v1's KB was built from PDFs and has no SEC metadata. v2 builds filing → section → 300/50-token chunks → embeddings with `ticker, form, filed, url, section`. The helpers are copied in. |
| `core/vectors.py` | same | ADAPT | Keep `LocalFaissIndex` and `S3FaissIndex` (If-Match, ETag cache). Delete `PgVectorIndex`. |
| `core/retriever.py` | same | ADAPT | Delete the pgvector path. Keep the balanced over-fetch-50 / ≤3-per-company logic. |
| `core/agent.py` (Quick) | same | PORT | Already keyword routing, parallel tools, citations, and the result contract the scorer reads. |
| `core/agents/{evidence,loop,specialists,supervisor,deep}.py` | same | PORT | They implement SPEC §6 as written: manual function calling, ≤3 steps, one follow-up, 75 s budget, fallback to Quick. Their tests pass. |
| `core/agents/tools.py` | same | ADAPT | `search_filings` returns the new metadata (form, filed, section, url) so citations name the filing and section. |
| `core/alerts.py` | same | PORT | Pure rules with dedupe keys exactly as in SPEC §4. |
| `core/explain.py` | same | PORT | Has the session-window fix (previous close → this close, New York). Known gap: market holidays (§5 risks). |
| `core/filings.py` | same | ADAPT | Keep the watcher (`recent_filings, new_since, check_filings, describe_filing, summarize, index_pending, replay_latest`). Indexing calls `core/ingest.py`. Delete the Supabase/backfill branches SPEC doesn't need. |
| `core/telegram.py` | same | PORT | Tiny, works. |
| `core/store.py` | same | ADAPT | Keep `LocalStore` (SQLite) and `get_store()` (`local` \| `dynamodb`). Delete `SupabaseStore`. |
| `core/store_dynamo.py` | same | PORT | Already the SPEC §4 single table with transactional dedupe. |
| `core/desk.py` | same | ADAPT → fold into `app.py` | Keep `ApiDesk` only. SPEC says Streamlit calls the API only, so `LocalDesk` goes. |
| `core/mailer.py`, `core/watchdog.py` (rest) | | DROP | Email digest is out of scope (D3). The useful watchdog parts move to `xbrl.py`. |
| `aws_lambda/runtime.py` | same | PORT | SSM once per cold start, EMF helper. |
| `aws_lambda/api.py` | same | ADAPT | Routes and bodies follow SPEC §5 (`ticker`/`name`), ticker regex, 1,000-char question cap, request ID. |
| `aws_lambda/ask_worker.py` | same | ADAPT | Request ID propagation. Optionally keep `context` for eval jobs (Q4). |
| `aws_lambda/scan.py`, `notify.py` | same | PORT | Already Scheduler → scan and Streams → Telegram with `batchItemFailures`. |
| `aws_lambda/digest.py`, `jobs/digest.py` | | DROP | D3 |
| `jobs/scan.py` | same | ADAPT | Remove `notify_pending` (the GitHub-cron delivery path). Delivery is Streams only (D4). Keep `--dry-run`, `--only`, `--replay-filing`. Add a price-move replay for F8's acceptance test. |
| `cli.py` | same | ADAPT | Commands per SPEC §8: `market | news | resolve | retriever | agent [--deep] | scan [--dry-run]`. |
| `app.py` | same | ADAPT | API client only: Quick/Deep toggle, polling, trace expander. Remove in-process paths. |
| `scripts/bootstrap.py` | `scripts/aws_bootstrap.py` | ADAPT | Rename. SSM secrets (adds `FINNHUB_API_KEY`), index upload, seed watchlist. |
| `scripts/load_vectors.py`, `db/schema.sql` | | DROP | Supabase only (D4). |
| `template.yaml` | same | ADAPT | Delete Digest/SES. Add `AWS::ECR::Repository` with a keep-2 lifecycle. Scheduler in `America/New_York`. Explicit log groups (14 d). HTTP API stage throttling. Least-privilege IAM per function. The ap-south-1 Budget is kept. |
| `Dockerfile`, `.dockerignore` | same | PORT | Lambda Python 3.12 base image, one image for 4 handlers. |
| `.github/workflows/ci.yml` | same | PORT | `pytest` on push. |
| `.github/workflows/deploy.yml` | same | PORT | Already OIDC (`id-token: write`, `role-to-assume`). |
| `.github/workflows/scan.yml` | | DROP | GitHub-cron backend (D4). |
| `infra/github-deploy-policy.json` | same | ADAPT | Scope it to this stack's resources and the new ECR repo. |
| `tests/unit/*` | same | PORT with their modules | Bring each test file with its module, and drop tests for dropped code (digest, Supabase, desk-local). Add `pyyaml`, `boto3`, `moto` to `requirements-dev.txt`, which v1's venv lacked. |
| `requirements*.txt`, `pytest.ini` | same | ADAPT | Remove `supabase`/`pdfplumber`. Pin versions. Add `finnhub` (or plain `requests`). |
| `postman/Research-Desk.postman_collection.json` | (none) | NEW | SPEC F13 |
| `evals/questions.json` | same | PORT | Already in repo, identical. |

---

## 2. Phases

Each phase runs on branch `phase-N-<name>`, one commit per task, and runs `python -m pytest` after each task. A phase is done when every acceptance check below has been **run**, not merely written.

### Phase 1 — Frozen eval scorer, v1 baseline, local core *(Day 1)*
**Goal:** score comparability locked in first; tools work from the CLI on a laptop with a Gemini key (+ Finnhub).
**Demo:** `pytest` green (incl. golden scorer test); `cli.py market NVDA` with Yahoo forced off shows `source: finnhub`; `cli.py resolve "advanced micro devices"` → AMD + CIK; `docs/eval/v1-baseline.md` with today's v1 numbers.

| # | Task | Files | CORE/STRETCH |
|---|---|---|---|
| 1.0 | Housekeeping: commit the pending AGENT.md change (M2); extend `.gitignore` (§0.2), delete `aws_artifacts/` line (M5); `.env.example` with blank `GEMINI_API_KEY, FINNHUB_API_KEY, SEC_USER_AGENT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APP_ACCESS_CODE, STORE_BACKEND=local, VECTOR_BACKEND=local`; `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, empty `core/__init__.py`, `evals/__init__.py`. | `.gitignore .env.example requirements*.txt pytest.ini` | CORE |
| **1.1** | **Port the eval scorer verbatim — before any other module.** (a) `core/numbers.py` = v1 `checker.py` from `UNVERIFIED_MARK` through `verify_numbers` (incl. `Number`, `extract_numbers`, `_supported`, `TABLE_UNITS`), byte-identical bodies, stdlib only. (b) `evals/scoring.py` = v1 `run.py`'s `QUICK_TOOL_TO_AGENT`, `CITATION`, `score`, `pct`, `quantile`, `summarise`, bodies identical; only change `from core.agents import checker` → `from core import numbers as checker` so the bodies stay character-for-character. Header comment: source path, v1 commit `73b6fda`, "FROZEN — see DECISIONS D8". (c) `tests/fixtures/v1_eval_2026-11-03.json` = copy of v1 `evals/results/latest.json`; `tests/unit/test_eval_scoring.py` asserts `summarise()` of each mode equals the exact dicts in §0.3 and that `core.numbers` imports without `google.genai`. (d) Port v1's stage-1 checker tests from `tests/unit/test_agents.py` (`$57.0B`↔`57,006 million`, `-1.3%`↔`-1.26%`, planted fake number). | `core/numbers.py evals/scoring.py tests/fixtures/ tests/unit/test_eval_scoring.py tests/unit/test_numbers.py` | CORE (your request) |
| **1.2** | **Re-run v1's eval today → `docs/eval/v1-baseline.md`.** Never inside v1's folder (its runner writes `evals/results/latest.json` and `docs/05_EVAL.md` into v1). Steps: (1) `robocopy C:\Shail\Projects\investment-research-assistant C:\Shail\Projects\_v1-eval-run /E /XD .git .pytest_cache /XF .env`. (2) 👤 create `_v1-eval-run\.env` with your `GEMINI_API_KEY` (+ `SEC_USER_AGENT` if you want SEC-backed ticker resolution; record either way), `STORE_BACKEND=local`, `VECTOR_BACKEND=local`. (3) List models available to the key: `.venv\Scripts\python -c "from google import genai; c=genai.Client(); print([m.name for m in c.models.list() if 'flash' in m.name])"` (use v1's venv: `..\investment-research-assistant\.venv\Scripts\python`). (4) If any chain model is missing, set `GEMINI_MODEL` / `GEMINI_FALLBACK_MODELS` in that `.env` to the closest available (record why). (5) `$env:PYTHONIOENCODING="utf-8"; python -m evals.run --fresh` (≈20–40 min, ≈150 Gemini calls; resumes if interrupted). (6) Re-score the new `latest.json` with **v2's** `evals/scoring.py` too and confirm both scorers agree. (7) Write `docs/eval/v1-baseline.md`: table of saved 2026-11-03 vs rerun 2026-11-04 (both modes, all §0.3 metrics), the model chain actually used, KB = 3 press-release PDFs, SEC_USER_AGENT on/off, caveats; save raw results as `docs/eval/v1-rerun-2026-11-04.json`. If it doesn't reproduce (404 models, quota), say so in the file with the error and what was changed — don't hide it. | `docs/eval/v1-baseline.md docs/eval/v1-rerun-2026-11-04.json` | CORE (your request) |
| 1.3 | `core/config.py` (ADAPT), `core/log.py` (REWRITE: JSON formatter, `request_id` contextvar, `log_tool(name, seconds, ok, tokens)`); `.github/workflows/ci.yml` (PORT). | `core/config.py core/log.py .github/workflows/ci.yml` | CORE |
| 1.4 | `core/llm.py` (PORT) + tests; set default chain to what 1.2 step 3 found; record names in PROGRESS.md. | `core/llm.py tests/unit/test_llm.py` | CORE |
| 1.5 | `core/market.py` (ADAPT): `get_quote`, `get_quotes`; Finnhub fallback on exception/empty/blocked; `source` on every quote; QQQ/SPY. 👤 Finnhub key. | `core/market.py tests/unit/test_market.py` | CORE |
| 1.6 | `core/news.py`, `core/tickers.py`, `core/sec.py` (PORT) + tests. 👤 `SEC_USER_AGENT`. | | CORE |
| 1.7 | `core/store.py` (ADAPT, LocalStore only) + tests; `cli.py` with `market | news | resolve`. | `core/store.py cli.py` | CORE |

**Acceptance:**
- `python -m pytest` passes, including `test_eval_scoring.py`, which matches v1's 11-03 summary exactly.
- `python -c "import core.numbers, evals.scoring"` succeeds in a venv **without** `google-genai` installed, or with it stubbed out in the test.
- `docs/eval/v1-baseline.md` exists, with rerun numbers or a documented failure.
- With `$env:YF_DISABLE="1"` (a test hook) or a mocked failure, `python cli.py market NVDA` prints `source: finnhub`.
- `python cli.py market QQQ` works on the free Finnhub key.
- `python cli.py resolve "nonexistent co"` prints a friendly "not found".
- `git check-ignore .env.local data/index/x.faiss local.db` lists all three paths.

**Risks:**
- Gemini model names have changed: step 1.2(3) handles this.
- v1's rerun uses the same free quota as the v2 work, so run 1.2 early on Day 1. It's ≈150 generate calls and a few dozen embed calls.
- Finnhub's free tier may not include ETFs. If it doesn't, the QQQ/SPY benchmark falls back to yfinance only, and that gets recorded.

### Phase 2 — Knowledge base, Quick + Deep agents, local eval *(Day 2)*
**Goal:** the brief's example query answered locally with all three specialists, citations and fact-check; v2 local eval vs v1.
**Demo:** `python cli.py agent --deep "<brief query>"` → answer with `[E#]` citations to quotes, news URLs and **10-K sections**; `python cli.py retriever "Compare NVIDIA and AMD data center revenue"` shows chunks from both; `docs/eval/v2-local.md`.

| # | Task | Files | CORE/STRETCH |
|---|---|---|---|
| 2.1 | `core/embeddings.py` (PORT), `core/vectors.py` (ADAPT: Local + S3 classes, no pgvector) + tests (moto for S3). | | CORE |
| 2.2 | `core/ingest.py` (REWRITE). `fetch(ticker, forms=("10-K","10-Q","8-K:2.02"))` → `html_to_text` → `sections()` (10-K/10-Q Items 1, 1A, 7 and Notes: segment information, **customer concentration** / "Concentration of Revenue"; 8-K EX-99.1) → `chunk(300, overlap=50)` → `embed_documents` (token batches, `FILING_EMBED_DAILY_CAP=400`, resumable state) → index. `python -m core.ingest NVDA AMD INTC --dry-run` prints chunks per filing/section **before** spending quota. | `core/ingest.py tests/unit/test_ingest.py` | CORE |
| 2.3 | **Build the KB** (start early in the day; may need 2 days of embed quota — see risks). Commit nothing from `data/`; record chunk counts and filings (accession, filed date) in PROGRESS.md. | | CORE |
| 2.4 | `core/retriever.py` (ADAPT) + balanced-retrieval test. `cli.py retriever`. | | CORE |
| 2.5 | `core/agent.py` Quick (PORT) + tests; `cli.py agent`. | | CORE |
| 2.6 | `core/agents/*` (PORT; `tools.py` ADAPT for filing metadata; `checker.py` imports `core.numbers`) + tests (malformed LLM JSON, LLM timeout → Quick fallback, 75 s budget → partial). `cli.py agent --deep`. | | CORE (supervisor + specialists); fact-checker stage 2: STRETCH |
| 2.7 | `evals/run.py` runner (ADAPT) + **contract test**: Quick and Deep results (mocked LLM) contain every key `evals/scoring.py` reads (§0.3). | `evals/run.py tests/unit/test_eval_contract.py` | CORE |
| 2.8 | Run `python -m evals.run` locally → `docs/eval/v2-local.md`; add a "vs v1 rerun (same day)" table by question type (single/comparison/why/filing). | | CORE |

**Acceptance:**
- `pytest` green.
- `python -m core.ingest NVDA AMD INTC --dry-run` lists sections per filing, including a customer-concentration section for NVDA's 10-K.
- `cli.py retriever` returns at least one NVDA chunk and at least one AMD chunk for the comparison query.
- `cli.py agent --deep "<brief query>"`: the trace shows market, news and filings specialists, the checker runs, and every numeric line has `[E#]`.
- A test with a planted fake number shows it marked ⚠️unverified.
- `docs/eval/v2-local.md` exists.

**Risks:**
- **Embedding quota.** ~1,000 requests a day, and each text in a batch counts. Three 10-Ks plus 10-Qs plus 8-Ks is easily 1,500–2,500 chunks if done whole. Mitigations: index only the sections listed in 2.2, run `--dry-run` first, and spread the build over Day 2 and Day 3 if needed (the cap resumes next run).
- NVIDIA's 10-K names its large customers only as "Customer A/B/…". Named GPU buyers come from news. The demo answer must say so rather than invent names.
- NVIDIA's fiscal Q3 FY2027 report is due around mid/late November 2026. Until then, "Q3 earnings" means the latest filed quarter. Check on EDGAR (Q1).

### Phase 3 — AWS: ask path deployed *(Day 3)*
**Goal:** the agent runs behind API Gateway + Lambda in ap-south-1 at ~$0.
**Demo:** Postman: `GET /health`, then `POST /ask` (deep, brief query) → 202 → poll `GET /ask/{id}` → cited answer from the deployed stack.

| # | Task | CORE/STRETCH |
|---|---|---|
| 3.0 | 👤 Before Day 3 (do it on Day 1): `aws configure` / SSO for ap-south-1, check `aws sts get-caller-identity`, set the Budget email, start Docker Desktop. | CORE |
| 3.1 | `core/store_dynamo.py` (PORT) + moto tests; `get_store()` picks `dynamodb`. | CORE |
| 3.2 | `template.yaml` (ADAPT): ECR repo + lifecycle (keep 2), HttpApi + stage throttling, `ApiFunction`, `AskWorkerFunction`, `ResearchDesk` table (PROVISIONED 2/2, TTL, Streams on), S3 bucket (public access blocked, SSE), log groups (14 d), SSM read on `/research-desk/*` only, Budget $1. `tests/unit/test_template.py` (PORT) checks the never-list (no NAT/VPC/on-demand/Secrets Manager…). | CORE |
| 3.3 | `Dockerfile` (PORT), `aws_lambda/runtime.py` (PORT), `api.py` (ADAPT to SPEC §5), `ask_worker.py` (ADAPT) + tests. | CORE |
| 3.4 | `scripts/bootstrap.py` (ADAPT): SSM secrets from `.env` (values never printed), upload the index to S3, seed the NVDA/AMD/INTC watchlist. | CORE |
| 3.5 | `sam validate --lint` → `sam build` → **show the change set → 👤 approve** → `sam deploy` → `python -m scripts.bootstrap`. | CORE |
| 3.6 | `postman/Research-Desk.postman_collection.json` with every SPEC §5 route, `{{base_url}}`, `{{access_code}}`, and a test script that polls the ask job. Live smoke test: `curl` health, then ask (quick and deep). | CORE |

**Acceptance:**
- `sam validate --lint` is clean and `sam build` succeeds.
- After deploy, `curl $URL/health` returns `{"ok":true}`.
- A call without `x-access-code` returns 401 JSON.
- `POST /ask` returns 202, and polling returns `done` with sources, for both modes.
- A Postman run of the collection passes.
- The CloudFormation resources contain no NAT, VPC or on-demand table (checked by `test_template.py`).

**Risks:**
- Yahoo blocks AWS IPs. Expect Finnhub on Lambda, and check that the `source` field shows it.
- Deep-mode cold start plus FAISS load could exceed the budget. Measure the cold start. The `/tmp` ETag cache helps.
- The image size limit: keep it under ~1 GB, with no dev dependencies in the image.

### Phase 4 — Watchdog on AWS *(Day 4)* — **all STRETCH**, built in cut order
**Goal:** the watchlist scan writes alerts once, and Telegram delivers them along with a "why".
**Demo:** invoke `ScanFunction` with a replayed −7.5% INTC move twice → exactly one FEED item → one Telegram message with a "why" line. `GET /feed` shows it.

| # | Task | Cut order |
|---|---|---|
| 4.0 | 👤 Before Day 4 (do it on Day 1 or 2): create the Telegram bot with @BotFather and get your chat ID. | — |
| 4.1 | `core/alerts.py`, `core/explain.py` (PORT), `jobs/scan.py` (ADAPT, with a price-move replay and `--dry-run`), `cli.py scan` + tests (duplicate scan → 1 alert; after-hours headline → next session). | keep longest |
| 4.2 | `core/telegram.py`, `aws_lambda/notify.py`, `aws_lambda/scan.py` (PORT). Template: `ScanFunction`, `NotifyFunction`, Stream event-source mapping with a filter on FEED INSERTs, SQS DLQ, and a DLQ-depth alarm → SNS email. | keep |
| 4.3 | `core/filings.py` (ADAPT), `core/xbrl.py` (ADAPT from `watchdog.py`): filing alerts, XBRL snapshot, earnings 8-K extraction (verbatim-quote check). | cut 2nd |
| 4.4 | Scheduler `cron(*/15 9-16 ? * MON-FRI *)`, `America/New_York`, the handler drops runs before 9:30 or after 16:00; filings queued from `POST /watchlist` and `FILING_EMBED_DAILY_CAP` (F11). | cut 1st |
| 4.5 | Deploy (👤 approves the change set), then run the live replay and a forced Telegram failure (bad token in a test invoke). | — |

**Acceptance:**
- `pytest` green, including the duplicate-scan, session-window and Telegram-failure tests.
- Two replay invokes produce one alert, and the phone receives it.
- A forced failure lands in the DLQ and the alarm reaches the ALARM state.
- `cli.py scan --dry-run` lists what would fire.
- If 4.3 is built, NVDA's latest 10-Q snapshot numbers match EDGAR (check by hand against `companyfacts`).

**Risks:**
- US market hours are 20:00–02:30 IST, so live alerts won't happen during an IST daytime interview. That's why the demo uses the replay.
- `explain.session_window` ignores market holidays (SPEC gap; logged, not fixed, unless you add it in Q5).

### Phase 5 — Frontend, observability, CI/CD, deployed eval *(Day 5)*
**Goal:** the demo has a UI, ops are visible, deploys run from GitHub, and the numbers are measured on AWS.
**Demo:** Streamlit Cloud URL: ask the brief query in Deep mode and show the "How I answered" trace. The CloudWatch dashboard and an X-Ray trace from the same request. A push to `main` deploys.

| # | Task | CORE/STRETCH |
|---|---|---|
| 5.1 | EMF metrics (SPEC §7 list) and alarms: API 5xx, scan errors, Deep p90, DLQ. Request ID visible in logs across API → worker. | CORE (logs + API 5xx alarm); the rest is STRETCH |
| 5.2 | `app.py` (ADAPT, API client only). 👤 deploy to Streamlit Community Cloud with secrets `DESK_API_URL` and `APP_ACCESS_CODE`. | STRETCH (cut last: Postman covers the brief) |
| 5.3 | CloudWatch dashboard and X-Ray (`Tracing: Active`). | STRETCH |
| 5.4 | `deploy.yml` (PORT) and `infra/github-deploy-policy.json` (ADAPT). 👤 create the OIDC provider and role, then set the repo variable `AWS_DEPLOY_ROLE_ARN`. | STRETCH (a manual `sam deploy` satisfies the brief) |
| 5.5 | Eval against the deployed stack: `python -m evals.run --api $URL` → `docs/eval/v2-deployed.md` (see Q4 for unmatched numbers). Measure cold start and `/tmp` cache hit rate. | CORE (latency, routing, citations) |

**Acceptance:**
- The Streamlit URL answers in both modes.
- `aws logs tail` shows JSON lines sharing one `request_id` across both functions.
- The dashboard renders.
- An X-Ray trace shows the API → worker hop.
- A push to `main` runs `deploy.yml` green. 👤 says when to push.
- `docs/eval/v2-deployed.md` exists.

### Phase 6 — Docs and demo *(Day 6, full day)*
| # | Task |
|---|---|
| 6.1 | `README.md`: what it is, the architecture image, local setup (Gemini key only), AWS deploy steps, Postman and Streamlit links, eval summary, cost. |
| 6.2 | Architecture diagram exported as `docs/architecture.png` from SPEC §3 (mermaid-cli `mmdc` or draw.io), showing every AWS component, the data flows and the external services. |
| 6.3 | `docs/DESIGN.md` (1–2 pages): design choices (D1–D9, especially the Bedrock deviation), challenges (Gemini outages, Yahoo blocking, session-window bug, embed quota), production improvements (Bedrock adapter, OpenSearch at scale, holidays calendar, auth, multi-user). |
| 6.4 | `docs/AWS_SETUP.md`: the 👤 steps, click by click. |
| 6.5 | Fill in "Resume claims" (§6) with measured numbers only. Check cost in Cost Explorer after at least 48 h. |
| 6.6 | Rehearse the demo script (§3) twice against the deployed stack, timed to the 15 + 20 + 15 min format. Record a backup screen capture in case of a Gemini outage on the day. |

**Acceptance:** every SPEC §11 box ticked; demo script runs end to end without edits; README setup steps followed on a fresh clone (`git clone` → `.venv` → `pytest` → `cli.py market NVDA`).

---

## 3. Demo script

| # | Mode | Query | Expected tools / agents | What answers each part |
|---|---|---|---|---|
| 1 | Deep | *"Analyze NVIDIA's Q3 earnings report. Compare revenue growth with AMD and Intel. Provide any information on top companies that are using NVIDIA GPUs (and revenue from them), competitive threats to NVIDIA. Summarize recent news sentiment and provide a risk assessment based on current market conditions."* | Supervisor → Market (`get_quotes` NVDA, AMD, INTC, QQQ, SPY), News (`get_news` ×3), Filings (`search_filings` ×N, `latest_filing`); checker | Earnings and revenue growth: NVDA's latest 10-Q / earnings 8-K EX-99.1, AMD's and INTC's Q3 2026 10-Q / 8-K. **GPU customers:** NVDA 10-K *Concentration of Revenue* note, where customers are anonymised ("Customer A…"). Named buyers come from news only and are labelled as such. Competitive threats: NVDA 10-K Item 1 "Competition" and Item 1A. Sentiment: headlines. Market conditions: QQQ/SPY quotes. |
| 2 | Quick | "What is AMD's market cap and P/E ratio, and what are the latest headlines?" | `market`, `news` (one Gemini call, ~5 s) | Quote (shows `source`) and RSS headlines with links |
| 3 | Deep | "Compare NVIDIA's and AMD's data center segment revenue" | Filings (balanced retrieval) | Segment notes in both 10-Qs. Shows ≥1 chunk per company. |
| 4 | Watchdog (STRETCH) | Replay an INTC −7.5% move | `ScanFunction` → DynamoDB → Stream → `NotifyFunction` → Telegram | Explain step: market check (QQQ/SPY), then a session-window headline, or "no clear catalyst found" |
| 5 | Watchdog (STRETCH) | `POST /watchlist {"name":"Microsoft"}`, then the next scan, then a Deep filing question about MSFT | F11 indexing | MSFT's latest 10-Q |

---

## 4. Cut order
If we fall a half-day or more behind, cut STRETCH items in this order:
1. 4.4 (KB follows the watchlist; scheduler polish)
2. 4.3 (filing alerts + XBRL snapshot)
3. 5.3 (dashboard + X-Ray)
4. 5.4 (OIDC deploy)
5. Fact-checker stage 2 (LLM `derived` check)
6. 4.1/4.2 (price alerts + Telegram)
7. 5.2 (Streamlit)

Never cut Phase 6, task 1.1 or task 1.2.

---

## 5. Cross-phase risks
- **Gemini free tier is the single point of failure.** It covers generate, embed, the eval and the demo. Mitigations: the model chain and cooldown, `DEEP_MODE_DAILY_CAP`, no eval runs on demo day, and a recorded backup demo.
- **Results drift between days.** Compare v1 and v2 on the same day only (D9).
- **Market holidays** aren't handled in the session window or the scheduler. Thanksgiving is 2026-11-26, after the likely deadline.

---

## 6. Resume claims to measure (SPEC §12). Fill in only with measured numbers.
| Claim | How to measure | Value |
|---|---|---|
| Deep / Quick p50 and p90 latency | `docs/eval/v2-deployed.md` (from `evals/scoring.py`) | _tbd_ |
| Routing accuracy, cited lines, untraceable numbers (vs v1) | `docs/eval/v2-deployed.md` vs `docs/eval/v1-baseline.md`, same scorer | _tbd_ |
| Alert latency (scan → Telegram) | `FEED.created_at` vs the notify log's `sent_at` for the replay, over 5 runs, p50 | _tbd_ |
| Cold start | X-Ray / `REPORT Init Duration` in the logs, 5 cold invokes per function | _tbd_ |
| `/tmp` index cache hit rate | EMF counter `IndexCacheHit` / `IndexCacheMiss` over the eval run | _tbd_ |
| Cost per month | Cost Explorer after ≥48 h, plus Budget status | _tbd_ |
| Gemini calls per question | `evals/scoring.py` `calls` | _tbd_ |

---

## 7. Proposed additions (outside the phases, need your OK)
- **`cli.py models`**: list the Gemini models available to the key. Model names changed twice during v1. *(5 lines, saves debugging time on demo day.)*
- **`include_context` flag on `POST /ask`**: eval-only. The worker keeps `context`, capped at 100 KB, so untraceable numbers can be scored on AWS (Q4).
- **US market holiday list** in `explain.py` and the scan, for correct "why" on the day after a holiday.
