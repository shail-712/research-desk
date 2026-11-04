# PLAN — Research Desk (v2)

> Written by the Planner on 2026-11-04 (Wed) from `docs/SPEC.md`, `docs/DECISIONS.md`, `docs/ASSESSMENT.md`
> and a read-through of v1 (`../investment-research-assistant`, code only).
> **Status: APPROVED 2026-11-04 (revision 2, which applies your answers in §0.4).** Two answers are still blank (§8). The Builder asks for them when they're needed.
> Six phases, one per day, Day 7 is buffer. Phase 6 is a full day of docs and demo rehearsal.

**Legend**
- **CORE**: the brief requires it, a CORE item needs it to work in the deployed demo, or you asked for it. Never cut.
- **STRETCH**: cut first if we fall behind, in the order in [§4 Cut order](#4-cut-order). The SPEC calls these must-haves, but the brief doesn't require them.
- 👤 = only you can do it (credentials, accounts, approvals). These are placed early so they don't block.
- **PORT** = copy as is, apart from import paths and deleted SPEC-cut branches. **ADAPT** = copy, then change behaviour or interface. **REWRITE** = write new, using v1 for reference only. **NEW** = no v1 equivalent. **DROP** = not carried over.

---

## 0. Pre-flight findings (checked 2026-11-04)

### 0.1 Path and config mismatches
| # | Where | Finding | Resolution |
|---|---|---|---|
| M1 | Planner prompt | The v1 path was the placeholder `<v1-folder-name>`. The real v1 is `C:\Shail\Projects\investment-research-assistant`, as AGENT.md says. A stale stub, `investment-research-assistant-v2`, also exists. | 👤 You'll delete the stub. |
| M2 | AGENT.md "How to run" step 1 | It says to `git init`, but the repo already exists (`origin` = `github.com/shail-712/research-desk`). There's one uncommitted AGENT.md change. | 👤 You'll commit it before the Builder starts. |
| M3 | SPEC F14 / §8 | The eval report path was `docs/EVAL.md`. | **Fixed in SPEC:** all reports are in `docs/eval/` (`v1-baseline.md`, `v2-results.md`). |
| M4 | README.md, `data/raw/` | The README calls them "earnings press releases" and says they're in the repo. They're git-ignored, and they are actually **three 10-Q PDFs**: AMD Q1 2025 (filed 2025-05-07), INTC Q3 2025 (filed 2025-11-06), NVDA Q3 FY2026 (accession `0001045810-25-000230`). | Task 1.8 adds `scripts/download_raw.py` and a manifest, and rewords the README. The PDFs are not committed. |
| M5 | `.gitignore` | The `aws_artifacts/` line is a v1-only path. | Delete it (task 1.0). |
| M6 | SPEC §8 `core/logging.py` | It shadows the stdlib `logging` module. | **Fixed in SPEC:** `core/log.py`. |
| M7 | v1 `.env` | `GEMINI_MODEL=gemini-3.8-flash`, which isn't in SPEC's chain. v1's saved results don't record their chain. | The v1 rerun uses v1's config as is (task 1.2). v2 uses the SPEC chain after verifying the names. |
| M8 | `evals/questions.json` | Identical to v1's apart from `_about` ✓ | None |
| M9 | SPEC §5 `POST /watchlist` | v1's API reads `{"query"}`. SPEC says `{"ticker" or "name"}`. | Follow SPEC. |
| M10 | SPEC §3 Scheduler | v1 uses a UTC cron, which drifts across DST. | `America/New_York` (task 4.4). |

These were checked and are correct: the v1 modules named in AGENT.md, the SAM CLI path, `py -3.12`, and Docker 29.1.

### 0.2 Secrets and `.gitignore`
- **No secrets in the folder or git history.** I grepped all files and `git log -p --all` for key patterns: 0 hits.
- v1's `.env` holds a real `GEMINI_API_KEY`. Never copy it into this repo.
- **Covered today:** `.env`, `.streamlit/secrets.toml`, `.venv/`, `.cache/`, `data/processed/*`, `data/raw/*`, `.aws-sam/`.
- **Not covered:** `.env.*`, a FAISS index outside `data/processed/`, SQLite outside `.cache/`, `env.json`.
- Task 1.0 adds:
  ```
  .env.*
  !.env.example
  env.json
  *.db
  *.sqlite*
  *.faiss
  data/index/
  data/cache/
  vectors/
  evals/results/
  .coverage
  htmlcov/
  ```
- The golden scorer fixture lives in `tests/fixtures/` and **is** committed. It contains answers and public data only.

### 0.3 v1 eval scoring: what "port unchanged" means
- **Where the scoring lives:** `evals/run.py`: `QUICK_TOOL_TO_AGENT`, `CITATION`, `score()`, `pct()`, `quantile()`, `summarise()`.
- **What it depends on:** `score()` calls `core/agents/checker.py: verify_numbers()` and `UNVERIFIED_MARK`, which in turn depend on `extract_numbers`, `_supported`, `Number`, `_NUM`, `SCALES`, `REL_TOLERANCE` and `TABLE_UNITS`.
- **Why it must be split out:** `checker.py` imports `google.genai` and `core.llm` at module top. The pure stage-1 code has to be separated so the scorer can be ported first, with no other modules in place.
- **How v1 names the metrics:**
  - *Routing accuracy* = `agents_ok` (75% Quick, 95% Deep).
  - *Cited figures* = `cited_lines / numeric_lines` (51%, 90%).
- **Golden reference:** re-scoring v1's saved `evals/results/latest.json` (2026-11-03) read-only with v1's code reproduces SPEC §9 exactly:
  - Quick: 20/20 answered, unmatched 1/109, companies 100%, agents 75%, citations 51%, p50 4.7 s, p90 29.5 s, 1.0 calls.
  - Deep: 20/20 answered, unmatched 0/98, companies 100%, agents 95%, citations 90%, p50 23.4 s, p90 49.3 s, 6.5 calls, 1 follow-up, 2 fallbacks.
- **Result contract the scorer reads.** v2 must keep it (tested in task 2.7):
  - `answer`, `context`, `used_tools`, `companies[].ticker`, `mode`, `error`
  - `trace.rounds`, `trace.checker.unsupported`, `trace.fallback`
  - Quick tools are `market`/`news`/`rag`. Deep `used_tools` are `market`/`news`/`filings`.
  - Citations are `[E#]` or `[Doc N]`.
  - The runner also needs `llm.calls_made()`.
- **Comparability caveats** (DECISIONS D8/D9):
  1. **Different corpus.** v1's local KB = the three old 10-Q PDFs above: 1,491 chunks (INTC 912, AMD 359, NVDA 220), with mismatched quarters, no 10-K, and no `form`/`url`/`section` metadata. v2 indexes the latest 10-K/10-Q/earnings 8-K from SEC.
  2. **Live data changes daily.** Compare v1 and v2 runs from the same day.
  3. **Deployed scoring.** v1's deployed `/ask` drops `context`. This is solved by `include_context` (D10).
- **v1 test health:** agents, explain, watchdog, filings, market, tickers and alerts tests pass in v1's `.venv`. The AWS/moto tests weren't run there (no `boto3`/`moto`/`pyyaml`), so they're unverified until they pass in v2.

### 0.4 Your decisions (2026-11-04)
| Q | Decision | Where applied |
|---|---|---|
| 1 | "NVIDIA's Q3" = the **latest filed quarter at demo time**. Build and test on the latest filed 10-Q now. If NVIDIA files a new quarter before the interview, ingest it through the watchlist auto-ingest. That's a demo moment, not a requirement. | 2.3, 4.3, §3 demo |
| 2 | Cut order if behind: (a) SEC filing alerts → (b) dashboard / X-Ray / OIDC → (c) price alerts. Keep the ±3% price alert and Telegram if at all possible. | §4, Phase 4 order |
| 3 | All eval reports go in `docs/eval/`: `v1-baseline.md`, `v2-results.md`. | SPEC F14/§8/§9, 1.2, 2.8, 5.5 |
| 4 | `include_context` on `POST /ask`, off by default, used only by the eval runner. | SPEC §5, D10, 3.3, 5.5 |
| 5 | Market holidays are a **known gap**. If there's no trading data for the day, skip the alert rather than guess. | SPEC F8/§10, D12, 4.1 |
| 6 | `core/logging.py` → `core/log.py` | SPEC §8, 1.3 |
| 7 | The v1 rerun uses v1's config exactly as it is today (`gemini-3.8-flash`), plus `SEC_USER_AGENT`. On a 404, substitute the nearest available model and record exactly what was used. | 1.2 |
| + | You'll commit the AGENT.md change and delete the v2 stub. `data/raw` gets a download script instead of committed PDFs. **Embeddings are cached to disk and S3, so quota is spent once and nothing is re-embedded on deploy.** | M1, M2, 1.8, SPEC F4/F11, D11, 2.1, 2.2, 3.2, 3.4 |

---

## 1. Reuse table (every v1 module)

| SPEC component (v2 file) | v1 source | Verdict | Why |
|---|---|---|---|
| `evals/scoring.py` | `evals/run.py`: `QUICK_TOOL_TO_AGENT, CITATION, score, pct, quantile, summarise` | **PORT, verbatim, frozen** | You asked for it. Keeps v1 and v2 scores comparable (D8). |
| `core/numbers.py` | `core/agents/checker.py`, `UNVERIFIED_MARK` … `verify_numbers` | **PORT, verbatim, frozen** | The scorer depends on it. Split out to drop the `google.genai` import. |
| `evals/run.py` (runner) | `evals/run.py` (`run_one, load/save_results, write_report, main`) | ADAPT | `--target local|api` writes the matching section of `docs/eval/v2-results.md`. The API target sends `include_context: true`. Scoring is imported. |
| `core/agents/checker.py` | same | ADAPT | Stage 2 (LLM) is kept as is. Stage 1 is imported from `core/numbers.py`. |
| `core/config.py` | same | ADAPT | Drop Supabase. Validate `STORE_BACKEND` (`local`, `dynamodb`) and `VECTOR_BACKEND` (`local`, `s3`). |
| `core/log.py` | `config.setup_logging` (plain text) | REWRITE | JSON logs with a request ID contextvar (SPEC §7). |
| `core/llm.py` | same | PORT | The chain, timeouts and cooldown already match SPEC §6. |
| `core/embeddings.py` | same | **ADAPT** | Add an **embedding cache** keyed by `sha256(model|dim|task_type|text)`. Local SQLite `data/cache/embeddings.sqlite`, snapshot to S3 (D11). |
| `core/market.py` | same | ADAPT | Add the Finnhub fallback, `source` on every quote, QQQ/SPY, and the quote's trading date (used for the holiday skip). |
| `core/news.py`, `core/tickers.py`, `core/sec.py` | same | PORT | They match SPEC F2/F3 and the SEC rules. |
| `core/xbrl.py` | `core/watchdog.py` (XBRL and snapshot functions) | ADAPT | SPEC gives XBRL its own module. The logic and tests are proven. |
| `core/ingest.py` | `src/rag/*` + `core/filings.py` helpers (`html_to_text, document_text, relevant_section, chunk_words`) + `src/rag/build_embeddings.py` batching/cap | REWRITE | v1 built its KB from PDFs with no metadata. v2 goes SEC filing → sections → 300/50 chunks → cached embeddings → index, and **skips chunks whose hash is already indexed**. |
| `core/vectors.py` | same | ADAPT | Keep Local and S3 (If-Match, ETag) and delete pgvector. Store `chunk_hash`, `model` and `dim` in the index header and refuse to load on a model mismatch. |
| `core/retriever.py` | same | ADAPT | Delete pgvector. Keep balanced retrieval. |
| `core/agent.py` (Quick) | same | PORT | Already produces the result contract the scorer reads. |
| `core/agents/{evidence,loop,specialists,supervisor,deep}.py` | same | PORT | They implement SPEC §6, and their tests pass. |
| `core/agents/tools.py` | same | ADAPT | `search_filings` returns form, filed date, section and URL for citations. |
| `core/alerts.py` | same | ADAPT | Skip all price rules when the quote's trading date ≠ today's New York date (D12). |
| `core/explain.py` | same | PORT | Has the session-window fix. Holidays are a known gap. |
| `core/filings.py` | same | ADAPT | Split it into `detect_new()` (shared), `index_new()` (F11) and `alert_new()` (F10), so filing alerts can be cut while auto-ingest stays. Delete the Supabase/backfill branches. |
| `core/telegram.py` | same | PORT | Small, and it works. |
| `core/store.py` | same | ADAPT | Keep `LocalStore`. `get_store()` = `local` or `dynamodb`. Delete `SupabaseStore`. |
| `core/store_dynamo.py` | same | PORT | SPEC §4 table with transactional dedupe. |
| `core/desk.py` | same | ADAPT → fold into `app.py` | Keep `ApiDesk` only. |
| `core/mailer.py`, rest of `core/watchdog.py` | | DROP | D3 |
| `aws_lambda/runtime.py`, `scan.py`, `notify.py` | same | PORT | Already the SPEC design. |
| `aws_lambda/api.py` | same | ADAPT | SPEC §5 bodies, ticker regex, question cap, `include_context` (bool, default false), request ID. |
| `aws_lambda/ask_worker.py` | same | ADAPT | Keeps `context` (capped at 100 KB, `context_truncated` flag) **only** when `include_context`. Request ID. |
| `aws_lambda/digest.py`, `jobs/digest.py` | | DROP | D3 |
| `jobs/scan.py` | same | ADAPT | Delivery is Streams only (drop `notify_pending`). Keep `--dry-run`, `--only` and `--replay-filing`. Add a price-move replay. Add the holiday skip. |
| `cli.py` | same | ADAPT | SPEC §8 commands. |
| `app.py` | same | ADAPT | API client only. |
| `scripts/bootstrap.py` | `scripts/aws_bootstrap.py` | ADAPT | SSM secrets (adds `FINNHUB_API_KEY`), **create-only** index upload, embedding-cache snapshot upload/restore, and the seed watchlist. |
| `scripts/download_raw.py` + `data/raw_manifest.json` | (none) | NEW | Lets a fresh clone fetch the three v1 10-Q PDFs (M4). |
| `scripts/load_vectors.py`, `db/schema.sql`, `src/build_chunks.py` | | DROP | Supabase / PDF pipeline. |
| `template.yaml` | same | ADAPT | Delete Digest/SES. Add an ECR repo with keep-2 lifecycle, Scheduler in New York time, 14 d log groups, throttling, least-privilege IAM. **Data bucket gets `DeletionPolicy`/`UpdateReplacePolicy: Retain`.** |
| `Dockerfile`, `.dockerignore` | same | PORT | `.dockerignore` excludes `data/` so no index or cache ships in the image. |
| `.github/workflows/ci.yml`, `deploy.yml` | same | PORT | `deploy.yml` is already OIDC and never runs ingest. |
| `.github/workflows/scan.yml` | | DROP | D4 |
| `infra/github-deploy-policy.json` | same | ADAPT | Scope to this stack and the ECR repo. |
| `tests/unit/*` | same | PORT with their modules | Drop the tests for dropped code. Add `pyyaml`, `boto3`, `moto` to dev requirements. |
| `requirements*.txt`, `pytest.ini` | same | ADAPT | Remove `supabase`. `pdfplumber` is dev-only if kept. Pin versions. |
| `postman/Research-Desk.postman_collection.json` | (none) | NEW | SPEC F13 |
| `evals/questions.json` | same | PORT | Already in the repo. |

---

## 2. Phases

Each phase runs on branch `phase-N-<name>`, one commit per task, with `python -m pytest` after each task. A phase is done only when every acceptance check below has actually been run.

### Phase 1 — Frozen eval scorer, v1 baseline, local core *(Day 1)*
**Goal:** score comparability is locked in first, and the tools work from the CLI on a laptop.

**Demo:**
- `pytest` passes, including the golden scorer test.
- `cli.py market NVDA` with Yahoo failing shows `source: finnhub`.
- `cli.py resolve "advanced micro devices"` returns AMD and its CIK.
- `docs/eval/v1-baseline.md` shows today's v1 numbers.

| # | Task | Files | CORE/STRETCH |
|---|---|---|---|
| 1.0 | **Housekeeping.** Check `git status` is clean (👤 commits AGENT.md first). Extend `.gitignore` (§0.2) and delete the `aws_artifacts/` line. Create `.env.example` with blank `GEMINI_API_KEY, FINNHUB_API_KEY, SEC_USER_AGENT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APP_ACCESS_CODE`, plus `STORE_BACKEND=local`, `VECTOR_BACKEND=local`. Create `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `core/__init__.py`, `evals/__init__.py`. | as listed | CORE |
| **1.1** | **Port the eval scorer verbatim, before any other module.** (a) `core/numbers.py` = v1 `checker.py` from `UNVERIFIED_MARK` through `verify_numbers`, with identical bodies and stdlib only. (b) `evals/scoring.py` = v1 `run.py`'s `QUICK_TOOL_TO_AGENT`, `CITATION`, `score`, `pct`, `quantile`, `summarise`, with identical bodies. The only change is the import, `from core import numbers as checker`. Add a header naming the source and v1 commit `73b6fda`, with "FROZEN — DECISIONS D8". (c) `tests/fixtures/v1_eval_2026-11-03.json` = a copy of v1's `evals/results/latest.json`. `tests/unit/test_eval_scoring.py` asserts each mode's `summarise()` equals the §0.3 dicts exactly, and that `core.numbers` and `evals.scoring` import without `google.genai`. (d) Port v1's stage-1 checker tests. | `core/numbers.py evals/scoring.py tests/fixtures/ tests/unit/test_eval_scoring.py tests/unit/test_numbers.py` | CORE (you asked) |
| **1.2** | **Re-run v1's eval today and write `docs/eval/v1-baseline.md`.** Never run it inside v1, because its runner writes into its own folder. Steps (1)–(7) are listed below this table. | `docs/eval/v1-baseline.md docs/eval/v1-rerun-2026-11-04.json` | CORE (you asked) |
| 1.3 | `core/config.py` (ADAPT), `core/log.py` (REWRITE: JSON formatter, `request_id` contextvar, `log_tool(name, seconds, ok, tokens)`), and `ci.yml` (PORT). | | CORE |
| 1.4 | `core/llm.py` (PORT) + tests. Set the default chain from the model list in 1.2 step (3), and record it in PROGRESS.md. | | CORE |
| 1.5 | `core/market.py` (ADAPT): Finnhub fallback, `source`, QQQ/SPY, `trading_date` on each quote. 👤 Finnhub key. | | CORE |
| 1.6 | `core/news.py`, `core/tickers.py`, `core/sec.py` (PORT) + tests. 👤 `SEC_USER_AGENT`. | | CORE |
| 1.7 | `core/store.py` (ADAPT, LocalStore) + tests. `cli.py` with `market | news | resolve`. | | CORE |
| 1.8 | **`scripts/download_raw.py` + `data/raw_manifest.json`**. See the details below this table. | `scripts/download_raw.py data/raw_manifest.json README.md tests/unit/test_download_raw.py` | CORE (you asked; small) |

**Task 1.2 steps (v1 rerun):**
1. Copy v1 to a folder outside both repos, with its `.env` as is: `robocopy C:\Shail\Projects\investment-research-assistant C:\Shail\Projects\_v1-eval-run /E /XD .git .pytest_cache`.
2. Add `SEC_USER_AGENT="<name> <email>"` to `_v1-eval-run\.env` (👤 supplies the value). Change nothing else: `GEMINI_MODEL=gemini-3.8-flash` stays, along with v1's default fallbacks and backends (they resolve to local SQLite and local FAISS).
3. List the models the key can use: `..\investment-research-assistant\.venv\Scripts\python -c "from google import genai; print([m.name for m in genai.Client().models.list()])"`.
4. Only if a chain model is missing or returns 404, set the nearest available model in that `.env` and note "configured X → used Y, reason: 404".
5. Run `$env:PYTHONIOENCODING="utf-8"; ..\investment-research-assistant\.venv\Scripts\python -m evals.run --fresh` (about 20–40 min and about 150 Gemini calls; it resumes if interrupted).
6. Re-score the new results with **v2's** `evals/scoring.py` as well, and confirm both scorers agree.
7. Write `docs/eval/v1-baseline.md` and save the raw results as `docs/eval/v1-rerun-2026-11-04.json`. Then delete `_v1-eval-run` (it holds the key).
   - The report has a table of the saved 2026-11-03 run against the 2026-11-04 rerun, for both modes and every §0.3 metric.
   - It records the exact model chain configured and actually used (count of answers per model if the log shows it).
   - It records the KB (the three old 10-Q PDFs), `SEC_USER_AGENT=on`, and the caveats.
   - If the numbers don't reproduce, it says so with the error.

**Task 1.8 details (`download_raw.py`):**
- The manifest lists, for each file: `filename`, `ticker`, `form`, `period`, `filed`, `accession`, `url`, `sha256`.
- Hashes are already computed:
  - `AMD_PressRelease.pdf` = `a01d8517…50ce5`
  - `Intel_PressRelease.pdf` = `a644c6c2…7e7cf`
  - `NVIDIA_PressRelease.pdf` = `87be27da…ff0`
  - (Full hashes come from `sha256sum data/raw/*.pdf`.)
- The Builder finds the source URLs. AMD's and Intel's PDFs come from their IR sites (Equisolve). NVIDIA's is an EDGAR-rendered PDF of accession `0001045810-25-000230`.
- The script downloads each file, sends `SEC_USER_AGENT` for sec.gov, verifies the sha256, and skips files already present.
- If a URL is gone, it falls back to the same filing's primary HTML document on EDGAR and warns that the hash differs.
- README: "`data/raw/` = v1's three 10-Q PDFs, local only. Run `python -m scripts.download_raw`. v2's KB comes from SEC directly."

**Acceptance:**
- `pytest` passes, including `test_eval_scoring.py`, which matches v1's 11-03 summary exactly.
- `core.numbers` and `evals.scoring` import with `google.genai` blocked (test).
- `docs/eval/v1-baseline.md` exists, with rerun numbers and exact models, or a documented failure.
- A unit test with yfinance mocked to fail gets `source == "finnhub"`. `python cli.py market QQQ` works live.
- `python cli.py resolve "nonexistent co"` returns a friendly "not found".
- After deleting `data/raw/*.pdf`, running `python -m scripts.download_raw` restores three files with matching sha256 (or prints the documented fallback).
- `git check-ignore .env.local data/index/x.faiss data/cache/embeddings.sqlite local.db` lists all four.

**Risks:**
- Gemini model names (handled by 1.2 step 3).
- The rerun uses the same free quota, so start 1.2 first on Day 1.
- Finnhub's free tier may lack ETFs. If so, QQQ/SPY use yfinance only, and that gets recorded.
- IR-site PDF URLs may have moved (hence the EDGAR fallback).

### Phase 2 — Knowledge base, Quick + Deep agents, local eval *(Day 2)*
**Goal:** the brief's example query answered locally with all three specialists, citations and fact-checking, built on the **latest filed** filings. Embeddings are paid for once.

**Demo:**
- `cli.py agent --deep "<brief query>"` answers with `[E#]` citations to quotes, news URLs and 10-K/10-Q sections.
- Re-running ingest makes **0** embed calls.
- `docs/eval/v2-results.md` has its "Local" section.

| # | Task | CORE/STRETCH |
|---|---|---|
| 2.1 | `core/embeddings.py` (ADAPT): `EmbeddingCache` (SQLite at `data/cache/embeddings.sqlite`, key `sha256(model|dim|task_type|text)`, stores the float32 vector). `embed_documents` and `embed_query` look up the cache first and send only misses to the API. Counters `cache_hits` and `api_texts` are logged. `core/vectors.py` (ADAPT, Local + S3, header has `model`, `dim`, per-row `chunk_hash`, refuses a mismatch). Tests: same text twice → 1 API text; model change → miss; S3 via moto. | CORE |
| 2.2 | `core/ingest.py` (REWRITE). For each ticker, it fetches the **latest filed** 10-K, 10-Q and earnings 8-K (Item 2.02, EX-99.1) from SEC submissions. It runs `html_to_text`, then `sections()`: Items 1, 1A, 2/7 MD&A, and the Notes on segment information and concentration of revenue/customers. Then it chunks (300 tokens, 50 overlap) and **skips any `chunk_hash` already in the index**. The remaining chunks are embedded through the cache under `FILING_EMBED_DAILY_CAP=400`, then the index is appended and saved. `python -m core.ingest NVDA AMD INTC --dry-run` prints filings (accession, period, filed), sections, chunk counts, and how many need embedding. | CORE |
| 2.3 | **Build the KB** on the latest filed filings (NVDA's latest 10-Q is "Q3" for the demo until a newer one is filed). Start early in the day, because the quota may need two days. Record accessions, chunk counts and embed calls in PROGRESS.md. Commit nothing from `data/`. | CORE |
| 2.4 | `core/retriever.py` (ADAPT) + a balanced-retrieval test. `cli.py retriever`. | CORE |
| 2.5 | `core/agent.py` Quick (PORT) + tests. `cli.py agent`. | CORE |
| 2.6 | `core/agents/*` (PORT. `tools.py` ADAPT. `checker.py` imports `core.numbers`) + failure-path tests (malformed JSON, LLM timeout → Quick, 75 s budget → partial). `cli.py agent --deep`. | CORE. Fact-checker stage 2 is STRETCH |
| 2.7 | `evals/run.py` runner (ADAPT: `--target local|api`, saves results to `evals/results/v2-<target>.json`, renders both sections into `docs/eval/v2-results.md`) + a **contract test** that mocked Quick and Deep results contain every key `scoring.py` reads. | CORE |
| 2.8 | `python -m evals.run --target local` → "Local" section of `docs/eval/v2-results.md`, with a same-day comparison against v1 by question type. | CORE |

**Acceptance:**
- `pytest` passes.
- `ingest --dry-run` shows a concentration-of-revenue section for NVDA.
- Running `python -m core.ingest NVDA AMD INTC` twice: the second run logs `api_texts=0`.
- `cli.py retriever "Compare NVIDIA and AMD data center revenue"` returns at least one chunk for each company.
- The Deep brief query's trace shows all three specialists and the checker. Every numeric line is cited.
- A planted fake number is flagged ⚠️unverified (test).
- `docs/eval/v2-results.md` has the Local section.

**Risks:**
- **Embedding quota.** Section filtering plus the cap plus the cache bound it. The dry run shows the cost first.
- NVIDIA's 10-K anonymises its large customers ("Customer A…"). Named GPU buyers come only from news, labelled as such.
- The latest filed quarters differ by company, because fiscal calendars differ. Answers must state each company's period.

### Phase 3 — AWS: ask path deployed *(Day 3)*
**Goal:** the agent runs behind API Gateway + Lambda in ap-south-1 at ~$0, using the index and embeddings built on Day 2, with nothing re-embedded.

**Demo:** in Postman, `GET /health`, then `POST /ask` (deep, brief query) → 202 → poll → cited answer.

| # | Task | CORE/STRETCH |
|---|---|---|
| 3.0 | 👤 By the end of Day 1: AWS credentials for ap-south-1 (`aws sts get-caller-identity`), the Budget email, and Docker Desktop running. | CORE |
| 3.1 | `core/store_dynamo.py` (PORT) + moto tests. | CORE |
| 3.2 | `template.yaml` (ADAPT): the ECR repo with lifecycle, HttpApi with throttling, `ApiFunction`, `AskWorkerFunction`, and the `ResearchDesk` table (2/2, TTL, Streams). The **data bucket** has public access blocked, SSE, and `DeletionPolicy: Retain` + `UpdateReplacePolicy: Retain`. 14 d log groups, SSM read on `/research-desk/*`, and the $1 Budget. `test_template.py` (PORT) also asserts the Retain policies and the never-list. | CORE |
| 3.3 | `Dockerfile`, `.dockerignore` (excludes `data/`), `runtime.py` (PORT), `api.py`, and `ask_worker.py` (ADAPT, with `include_context`) + tests: default → no `context` key; `include_context: true` → `context` present and capped. | CORE |
| 3.4 | `scripts/bootstrap.py` (ADAPT): SSM secrets (values never printed). A **create-only** index upload to `s3://<bucket>/vectors/index.npz` (refuses if one exists unless `--replace`). An embedding-cache snapshot `--push-cache` / `--pull-cache` to `s3://<bucket>/embeddings/cache-<model>-<dim>.sqlite`, so a fresh clone or a rebuild reuses vectors. Seeds the watchlist. | CORE |
| 3.5 | `sam validate --lint` → `sam build` → **show the change set → 👤 approve** → `sam deploy` → `python -m scripts.bootstrap --push-cache`. | CORE |
| 3.6 | Postman collection (every SPEC §5 route, `{{base_url}}`, `{{access_code}}`, a polling test script). Live smoke test. | CORE |

**Acceptance:**
- `sam validate --lint` is clean and `sam build` succeeds.
- `curl $URL/health` returns `{"ok":true}`, and a call without the access code returns 401 JSON.
- `/ask` returns 202, then `done` with sources, in both modes. `include_context` is omitted from responses unless requested.
- A Postman run passes.
- The image contains no `data/` (`docker run --entrypoint ls … /var/task`).
- **A second `sam deploy` leaves `vectors/index.npz`'s ETag unchanged and logs 0 embed calls.**

**Risks:**
- Yahoo blocks AWS IPs, so Finnhub is expected. Verify the `source` field.
- Cold start and FAISS load time: measure them.
- Keep the image under ~1 GB.

### Phase 4 — Watchdog on AWS *(Day 4)* — **all STRETCH**, built in reverse cut order
**Goal:** the ±3% price alert and its "why" reach Telegram exactly once. New filings are indexed (and alerted, if time allows).

**Demo:**
- Invoke `ScanFunction` twice with a replayed −7.5% INTC move. Result: one FEED item and one Telegram message with a "why" line.
- `GET /feed` shows it.

| # | Task | Cut position (§4) |
|---|---|---|
| 4.0 | 👤 By Day 2: create the Telegram bot with @BotFather and get your chat ID. | — |
| 4.1 | `core/alerts.py` (ADAPT: **skip all price rules when `quote.trading_date` ≠ today's New York date**, log `price_rules_skipped: no_session`), `core/explain.py` (PORT), `jobs/scan.py` (ADAPT: price-move replay, `--dry-run`), `cli.py scan` + tests: duplicate scan → 1 alert; after-hours headline → next session; a holiday or stale quote → 0 alerts. | **kept longest** (c) |
| 4.2 | `core/telegram.py`, `aws_lambda/notify.py`, `aws_lambda/scan.py` (PORT). Template: `ScanFunction`, `NotifyFunction`, a Stream mapping filtered to FEED INSERTs, the SQS DLQ, and a DLQ alarm → SNS email. Scheduler `cron(*/15 9-16 ? * MON-FRI *)` in `America/New_York`. The handler drops runs outside 9:30–16:00. | (c) |
| 4.3 | `core/filings.py` (ADAPT): `detect_new()` + **`index_new()` (F11: new filings for watchlist tickers are ingested through the cache and the daily cap; `POST /watchlist` queues the latest 10-K, 10-Q and 8-K)**. If NVIDIA files its next quarter before the interview, this indexes it live. | F11, see §4 |
| 4.4 | `core/filings.py` `alert_new()` + `core/xbrl.py` (ADAPT): filing alerts, the XBRL snapshot, earnings 8-K extraction (verbatim-quote check), and 3-sentence summaries. | **cut first** (a) |
| 4.5 | Deploy (👤 approves the change set). Then run a live replay, a forced Telegram failure, and, if 4.3 is built, `POST /watchlist {"name":"Microsoft"}` followed by a scan. | — |

**Acceptance:**
- `pytest` passes: duplicate scan, session window, no-session skip, Telegram failure.
- Two replay invokes produce one alert, and the phone receives it.
- A forced failure lands in the DLQ and the alarm enters ALARM.
- `cli.py scan --dry-run` lists what would fire.
- If 4.3 is built: after adding MSFT, the next scan indexes its filings, and the second scan makes 0 embed calls.
- If 4.4 is built: NVDA's latest 10-Q snapshot matches EDGAR `companyfacts` (hand check).

**Risks:**
- Market hours are 20:00–02:30 IST, so the demo uses the replay.
- Holidays are a known gap. The day-after-holiday session window may start at the wrong previous close (D12).

### Phase 5 — Frontend, observability, CI/CD, deployed eval *(Day 5)*
**Goal:** the demo has a UI, ops are visible, and the numbers are measured on AWS.

**Demo:**
- On Streamlit, ask the brief query in Deep mode and show the trace.
- The dashboard and an X-Ray trace for the same request.
- A push to `main` deploys.

| # | Task | CORE/STRETCH |
|---|---|---|
| 5.1 | EMF metrics (SPEC §7) and alarms: API 5xx, scan errors, Deep p90, DLQ. Request ID appears across API → worker. | CORE: logs + API 5xx alarm. The rest is STRETCH |
| 5.2 | `app.py` (ADAPT, API client). 👤 Deploy to Streamlit Community Cloud with the secrets `DESK_API_URL`, `APP_ACCESS_CODE`. | STRETCH |
| 5.3 | CloudWatch dashboard and X-Ray. | STRETCH (b) |
| 5.4 | `deploy.yml` (PORT) and the deploy policy (ADAPT). 👤 Create the OIDC provider and role, and set the repo variable `AWS_DEPLOY_ROLE_ARN`. | STRETCH (b) |
| 5.5 | `python -m evals.run --target api --url $URL` (sends `include_context: true`) → "Deployed" section of `docs/eval/v2-results.md`. Measure cold start and the `/tmp` index-cache hit rate. | CORE |

**Acceptance:**
- The Streamlit URL works in both modes.
- `aws logs tail` shows one `request_id` across both functions.
- The dashboard renders, and X-Ray shows the API → worker hop.
- `deploy.yml` passes on a push (👤 says when).
- The "Deployed" section of `docs/eval/v2-results.md` reports every metric, including untraceable numbers.

### Phase 6 — Docs and demo *(Day 6, full day)*
| # | Task |
|---|---|
| 6.1 | `README.md`: overview, the architecture image, local setup (Gemini key only, `download_raw`, `bootstrap --pull-cache` to skip re-embedding), the AWS deploy, Postman and Streamlit links, the eval summary, and cost. |
| 6.2 | `docs/architecture.png`, exported from SPEC §3 (mermaid-cli or draw.io). |
| 6.3 | `docs/DESIGN.md` (1–2 pages): design choices (D1–D12, especially Bedrock), challenges (Gemini outages, Yahoo blocking, the session-window bug, embed quota), and production improvements (Bedrock adapter, holiday calendar, OpenSearch at scale, auth, multi-user). |
| 6.4 | `docs/AWS_SETUP.md`: the 👤 steps, click by click. |
| 6.5 | Fill in the resume claims (§6) with measured numbers only. Check the cost after at least 48 h. |
| 6.6 | Rehearse the demo twice against the deployed stack (15 + 20 + 15 min). Record a backup capture. No eval runs on demo day, to save quota. Before the interview, check EDGAR for a new NVIDIA quarter (see §3, row 5). |

**Acceptance:**
- Every SPEC §11 box is ticked.
- The demo script runs without edits.
- A fresh clone works by following the README: `git clone`, `.venv`, `pytest`, `download_raw`, then `cli.py market NVDA`.

---

## 3. Demo script

| # | Mode | Query | Expected tools / agents | What answers each part |
|---|---|---|---|---|
| 1 | Deep | The brief's query (NVIDIA "Q3", AMD/Intel growth, GPU customers, threats, sentiment, risk) | Supervisor → Market (`get_quotes` NVDA, AMD, INTC, QQQ, SPY), News (`get_news` ×3), Filings (`search_filings`, `latest_filing`); checker | Earnings: NVDA's **latest filed 10-Q** / earnings 8-K, with the period stated. Growth: AMD's and INTC's latest 10-Q / 8-K. **GPU customers:** the NVDA 10-K/10-Q concentration-of-revenue note (customers are anonymised); named buyers only from news, labelled. Threats: NVDA 10-K Item 1 "Competition" and Item 1A. Sentiment: headlines. Conditions: QQQ/SPY. |
| 2 | Quick | "What is AMD's market cap and P/E ratio, and what are the latest headlines?" | `market`, `news` | Quote (with `source`) and RSS links |
| 3 | Deep | "Compare NVIDIA's and AMD's data center segment revenue" | Filings | Segment notes, at least 1 chunk per company |
| 4 | Watchdog | Replay an INTC −7.5% move | Scan → DynamoDB → Stream → Notify → Telegram | Market check, then a session-window headline, or "no clear catalyst found" |
| 5 | Watchdog *(optional live moment)* | If NVIDIA has filed a new quarter: the scan auto-ingests it (or `POST /watchlist` MSFT, then a scan), and a Deep question cites the new filing | F11 | The newly indexed filing, with 0 re-embeds of existing chunks |

---

## 4. Cut order
If we fall a half-day or more behind, cut in this order. It follows your answer, with the unspecified items placed by me (§8, open item 3).
1. **(a)** 4.4 SEC filing alerts + XBRL snapshot
2. **(b)** 5.3 dashboard / X-Ray, then 5.4 OIDC deploy (a manual `sam deploy` stays)
3. 4.3 F11 auto-ingest (fallback: run `python -m core.ingest NVDA` by hand, then `bootstrap --replace`)
4. Fact-checker stage 2 (the LLM `derived` check; the code stage stays)
5. 5.2 Streamlit (Postman covers the brief)
6. **(c)** 4.1/4.2 ±3% price alert + Telegram. **Last, keep if at all possible.**

Never cut Phase 6, tasks 1.1, 1.2 or 1.8, or the embedding cache.

---

## 5. Cross-phase risks
- **Gemini free tier.** It covers generate, embed, the eval and the demo. Mitigations: the model chain, the cooldown, `DEEP_MODE_DAILY_CAP`, the embedding cache, no eval runs on demo day, and a recorded backup.
- **Day-to-day drift.** Compare v1 and v2 only on the same day (D9).
- **Holidays.** A known gap with a safe default: no session means no alert (D12). The next US holiday is Thanksgiving, 2026-11-26.
- **Losing embeddings.** The bucket has Retain policies, the index upload is create-only, and the cache snapshot is on S3.

---

## 6. Resume claims to measure (SPEC §12). Fill in only with measured numbers.
| Claim | How to measure | Value |
|---|---|---|
| Deep / Quick p50 and p90 | `docs/eval/v2-results.md`, Deployed section | _tbd_ |
| Routing, cited lines, untraceable numbers vs v1 | `v2-results.md` vs `v1-baseline.md`, same frozen scorer | _tbd_ |
| Alert latency (scan → Telegram) | `FEED.created_at` vs the notify log's `sent_at`, 5 replays, p50 | _tbd_ |
| Cold start | `REPORT Init Duration`, 5 cold invokes per function | _tbd_ |
| `/tmp` index cache hit rate | EMF `IndexCacheHit` / `IndexCacheMiss` over the deployed eval | _tbd_ |
| Embedding reuse | `cache_hits / (cache_hits + api_texts)` across rebuilds and deploys | _tbd_ |
| Cost per month | Cost Explorer after ≥48 h | _tbd_ |
| Gemini calls per question | `evals/scoring.py` `calls` | _tbd_ |

---

## 7. Proposed additions (outside the phases, need your OK)
- **`cli.py models`**: lists the Gemini models available to the key. Names changed twice during v1. *(5 lines.)*
- **US market holiday list** for `explain.py` and the scan, to close the D12 gap properly. *(Only if there's buffer on Day 7.)*

---

## 8. Still open
1. **Dates.** Your answer still had `✏️ [submission date] / interview ✏️ [date]`. The Day 1–7 schedule assumes Day 1 = 2026-11-04.
2. **Accounts.** Answer 8 was left blank. The plan assumes none exist yet. Needed: AWS ap-south-1 credentials (by the end of Day 1), a Finnhub key (Day 1), an SEC contact email (Day 1, also for the v1 rerun), and the Telegram bot + chat ID (by Day 2).
3. **Unranked cut items.** Your cut order didn't mention F11 auto-ingest, fact-checker stage 2 or Streamlit. I placed them between (b) and (c) (§4). Say if you want a different order.
