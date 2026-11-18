# 04 · Data and Features

## Database: one DynamoDB table (single-table design)

**Single-table design** means you keep all item types in one DynamoDB table and tell them apart by the partition key (`pk`) and sort key (`sk`). Each access pattern is then one cheap `Query` or `GetItem`. Code: `core/store_dynamo.py`. Table definition: `template.yaml` `Table`.

| pk | sk | What it is | Fields that matter | TTL |
|---|---|---|---|---|
| `WATCH` | `TICKER#NVDA` | Watchlist row + alert rules | `ticker`, `name`, `cik`, `pct_move` (default 3), `price_above`, `price_below`, `added_at` | none |
| `FEED` | `<created_at>#<8-char id>` | One alert | `ticker`, `kind` (`price_move` / `price_above` / `price_below`), `title`, `body`, `url`, `dedupe_key`, `notified`, `fail_delivery` (demo only) | 30 days |
| `SEEN#<dedupe_key>` | `-` | Dedupe marker | `feed_sk` (points at the FEED item) | 90 days |
| `STATE` | `<key>` e.g. `deep:count:2026-11-15` | Small state as a JSON string | `value`, `updated_at` | none |
| `JOB` | `<job_id>` (32 hex characters) | One async `/ask` job | `status` (`pending`/`done`/`error`), `question`, `mode`, `result` (JSON string), `error`, `created_at`, `finished_at` | 1 day |

- The Stream (`NEW_IMAGE`) is filtered so that only **INSERTs of FEED items** reach `NotifyFunction`.
- The dedupe key looks like `AMD:move_down:2026-11-02`, so it's one per ticker, per rule, per trading day. A replay demo key is prefixed `replay:`.
- Locally, `core/store.py` `LocalStore` uses SQLite tables `watchlist`, `events` (with `dedupe_key UNIQUE`) and `app_state`. It has the same interface.
- **Vectors are not in DynamoDB.** They live in S3 as `index.npz`, with `vectors` (float32), `meta` rows (`ticker`, `form`, `period`, `filed`, `section`, `url`, `text`, `chunk_hash`) and a `header` (model + dimension; a mismatch is refused).

```mermaid
erDiagram
  WATCH ||--o{ FEED : "ticker raises alerts"
  FEED ||--|| SEEN : "dedupe_key, written in one transaction"
  WATCH ||--o{ INDEX_CHUNK : "ticker has filings (S3, not DynamoDB)"
  WATCH {
    string pk "WATCH"
    string sk "TICKER#<ticker>"
    float pct_move
    float price_above
    float price_below
  }
  FEED {
    string pk "FEED"
    string sk "created_at#id"
    string ticker
    string kind
    string dedupe_key
    bool notified
  }
  SEEN {
    string pk "SEEN#dedupe_key"
    string feed_sk
  }
  JOB {
    string pk "JOB"
    string sk "job_id"
    string status
    string result
  }
  STATE {
    string pk "STATE"
    string sk "key"
    string value
  }
  INDEX_CHUNK {
    string ticker
    string form
    string section
    string chunk_hash
  }
```

The relations are *logical*. DynamoDB has no foreign keys, so the code keeps them consistent.

## API endpoints

All routes live in `aws_lambda/api.py` (`ROUTES`).

| Method | Route | What it does | Auth needed? |
|---|---|---|---|
| GET | `/health` | Returns `{"ok": true}` | No |
| GET | `/watchlist` | Lists watched tickers with their rules | Yes |
| POST | `/watchlist` | Adds a ticker by `ticker` or `name` (resolved via SEC). Checks that a quote exists | Yes |
| DELETE | `/watchlist/{ticker}` | Removes a ticker (404 if it isn't there) | Yes |
| GET | `/quotes[?tickers=A,B]` | Quotes for the given tickers (max 20) or the whole watchlist, each with its `source` | Yes |
| GET | `/news/{ticker}` | 5 latest headlines | Yes |
| GET | `/feed[?limit=20]` | Latest alerts, newest first (max 50) | Yes |
| POST | `/ask` | Creates a job, starts the worker, returns `202 {job_id, poll}` | Yes |
| GET | `/ask/{job_id}` | Job status. When done: `answer`, `sources`, `timings`, `trace`, `notice` | Yes |

## Feature 1: Asking a question (async job + Quick mode)

- **Entry:** `api.post_ask()` → `DynamoStore.create_job()` → `_invoke_worker()` (async) → `202`.
- **Worker:** `ask_worker.handler()` → `agent.answer()`.
  - `classify_query()` uses regex rules: "price / P/E / market cap" → market, "news / latest" → news, "revenue / risk / 10-K" → rag. RAG is the default when nothing else matches.
  - `find_companies()` finds up to 3 companies.
  - Tools run in parallel with one deadline: `market.get_quote`, `news.get_news`, `_rag_search` → `retriever.search`.
  - `retriever.search()` embeds the query, over-fetches 50 candidates from FAISS, then `balance()` keeps at most 3 chunks per company, so one company can't fill all the results.
  - A company with no indexed filings gets **no** filings, plus a notice from `not_covered_message()`. This was fixed in I5: before, an Apple question got NVIDIA's filings.
  - `build_prompt()` labels every block `[Doc N]`. Then `llm.generate()` makes one call.
- **Save:** `compact()` drops the evidence text unless `include_context` is set (eval only), and caps sizes under DynamoDB's 400 KB item limit. Then `finish_job()` runs.
- **Read:** `api.get_ask()` merges the job and its result into one flat JSON response.

## Feature 2: Deep mode (multi-agent + fact-checker)

- **Entry:** `core/agents/deep.py` `answer_deep()`. It has a 75 s budget (`DEEP_BUDGET_SECONDS`) and a cap of 50 Deep questions per day (`_take_daily_slot()`, counted in a `STATE` item).
- **Fast path:** one intent, at most one company, and no "why / impact" words → skip planning and run one specialist.
- **Plan:** `supervisor.plan()` makes a Gemini call with a JSON schema (`PLAN_SCHEMA`) and returns the companies plus 1–3 tasks.
- **Specialists in parallel:** `_dispatch()` → `specialists.run_specialist()` → `loop.run_tool_loop()`.
  - Each specialist (`market`, `news`, `filings`) can only use its own tools (`tools.py`: `get_quote`, `get_quotes`, `get_news`, `recent_alerts`, `search_filings`, `latest_filing`).
  - The loop is at most 3 tool steps. Then the tools are taken away and the model must answer.
  - Every tool result goes onto the `EvidenceBoard` as `E1`, `E2`, ….
- **Follow-up:** if a specialist reports gaps and ≥30 s are left, `supervisor.follow_up()` may run one more round (at most 2 tasks).
- **Write:** `supervisor.write()` writes the answer, citing only `[E#]`. Fake citations like `[Findings]` are stripped by a regex.
- **Fact-check:** `checker.check()`.
  - **Stage 1 (code):** `numbers.extract_numbers()` / `verify_numbers()` match each number within its display precision ("$57.0B" matches "57,006 million").
  - **Stage 2 (one LLM call):** only for unmatched numbers. It decides "derived" (e.g. a growth rate) or "unsupported".
  - Unsupported numbers get ⚠️. `miscited()` also flags numbers whose only citation is metadata-only evidence (`latest_filing`).
- **Fallbacks:** if the planner or writer is down, the daily cap is hit, or there's no evidence, `_quick()` returns a Quick answer with `mode: "quick-fallback"` and a notice.

## Feature 3: Watchdog price alerts

- **Trigger:** EventBridge Scheduler `cron(*/15 9-16 ? * MON-FRI *)` in `America/New_York`. `aws_lambda/scan.py` `in_session()` drops runs outside 9:30–16:00.
- **Scan:** `jobs/scan.py` `scan_prices()`.
  - One batched `market.get_quotes()` covers the watchlist + QQQ + SPY.
  - If no quote is from today's New York date (a holiday or weekend), the run is skipped (D12).
- **Rules:** `core/alerts.py` `price_events()` checks `|change_pct| ≥ pct_move`, `price ≥ price_above` and `price ≤ price_below`. Each event gets a `dedupe_key`.
- **Why:** `core/explain.py` `explain_move()`, cheapest and most certain step first:
  1. In code: did QQQ/SPY move ≥1.5% the same way? Then it "moved with the market".
  2. In code: keep only headlines from the previous close to this close that name the company.
  3. The LLM may only *pick* one of those headlines by ID. Otherwise: "no clear catalyst found".
- **Write once:** `DynamoStore.insert_event()` does one `TransactWriteItems`. It puts the `SEEN#` marker only if it doesn't exist (`attribute_not_exists(pk)`) **and** puts the FEED item. Both land or neither does, so there are never duplicates, even with two scans racing. `has_event()` is checked first to skip the paid "why" call.
- **Deliver:** the Stream → `aws_lambda/notify.py` `handler()` → `telegram.send()` → `mark_notified()`.
  - On failure it returns `batchItemFailures` and stops, so Lambda retries from that record.
  - After 3 retries the record goes to the SQS DLQ → `FailedAlertsAlarm` → email.
- **Demo:** `{"replay": "INTC:-7.5"}` fakes a move. `{"fail_delivery": true}` demos the DLQ path. It is refused during market hours.

## Authentication and authorization

- **Authentication (who are you?):** there are no user accounts. Every route except `/health` needs the header `x-access-code` equal to `APP_ACCESS_CODE`, checked with a constant-time compare (`_authorized()`).
- **Fails closed:** if the code isn't configured, every route except `/health` returns `503` ("Service is not configured").
- **The UI gate:** `ui/app.py` `require_access()` asks the viewer for the same code once per session. The UI's secret is the API's secret.
- **Authorization (what can you do?):** at the user level, all-or-nothing. At the service level, IAM least privilege (template.yaml):
  - The API never sees the Gemini key, and Notify only sees Telegram's.
  - The worker can only `s3:GetObject` on `vectors/*`.
  - Scan has only `Query` / `GetItem` / `PutItem`.
- **Secrets:** they're loaded from SSM into environment variables at cold start (`runtime.load_secrets()`), and `core/log.py` redacts secret values from every log line.

## Error handling and validation

- **Input validation** (`api.py`):
  - The body must be a JSON object.
  - The question is ≤1000 characters, `mode` is in `MODES`, and `include_context` is a bool.
  - Tickers must match `[A-Za-z.\-]{1,10}`.
  - Numbers are range-checked (`_optional_number`, e.g. `pct_move` 0–50).
  - Bad input raises `BadRequest` → `400`.
- **Error mapping in `_route()`:** `BadRequest` → 400, `MarketDataError`/`NewsError` → 502, `StoreError` → 503, anything else → 500 `"Internal error"`. Responses are always `{"error": "<safe message>"}`, never a stack trace.
- **Typed errors per layer:** `StoreError`, `RetrieverError`, `VectorIndexError`, `LLMUnavailableError`, `EmbeddingError`, `DeskError` (UI). Each one carries a user-safe message, and the details go to the logs.
- **Graceful degradation:**
  - One tool failing doesn't kill the answer. The prompt just says `[market data unavailable for X]`.
  - The LLM falls back across 4 models, and a failing model cools down for 60 s.
  - Deep falls back to Quick.
  - An alert is never lost because its "why" failed.
- **Timeouts everywhere:** a shared tool deadline, a per-call Gemini HTTP timeout, the Deep budget, and threads that are never joined past their deadline (`pool.shutdown(wait=False)`).
- **Request IDs:** API Gateway's ID is put in a contextvar, passed to the worker and its threads, logged on every JSON line, and returned in `x-request-id`.
