# Example demo: every deployed feature in one run

One pass through the deployed API, mostly in Postman, then a read-only look at AWS. Every route, field,
message and status code below is taken from `aws_lambda/api.py`, `aws_lambda/ask_worker.py`,
`core/agent.py` and `core/agents/deep.py` (written on branch `iter-3`, 2026-11-12; Watchdog section and
expected results updated on `iter-5`, 2026-11-15, image `8bd7e7f6aa84`).

**Cost of one run:** about **22 Gemini calls**, an estimate (Quick 5 × 1, Deep about 3 + 10 + 4; the I4 run used 18), 3 of the 50 daily Deep
slots, and about 4 minutes. It writes only 8 job items (they expire after 24 h), the Deep daily counter, and
AAPL, which is added and then removed from the watchlist. Run the demo once, with Postman **or** newman, not both.
The Watchdog part (7) adds 1 Gemini call and 1 Telegram message per replay.

---

## 0. Set-up

**Postman:**
1. Import `postman/Research-Desk-Demo.postman_collection.json`.
2. Use the same environment as the main collection: `base_url` and `access_code` (type **secret**).
3. Open the Postman console (View → Show Postman Console). The demo prints job IDs, request IDs, trace
   summaries and a running Gemini-call total there.

**PowerShell** (needed for the values above, for newman and for parts 6 and 7). Run from the repo root:

```powershell
cd C:\Shail\Projects\research-desk
$region = "ap-southeast-2"
$url    = aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text
$table  = aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].Outputs[?OutputKey=='TableName'].OutputValue" --output text
$api    = aws cloudformation describe-stack-resource --stack-name research-desk --logical-resource-id ApiFunction --region $region --query StackResourceDetail.PhysicalResourceId --output text
$worker = aws cloudformation describe-stack-resource --stack-name research-desk --logical-resource-id AskWorkerFunction --region $region --query StackResourceDetail.PhysicalResourceId --output text
$code   = (Select-String -Path .env -Pattern '^APP_ACCESS_CODE=(.+)$').Matches[0].Groups[1].Value
"url: $url   table: $table   api: $api   worker: $worker   code loaded: $([bool]$code)"
Set-Clipboard $code
```

What each line does, in order:
- **`cd`:** moves to the project folder.
- **`$region`:** the only Region this account can use.
- **`$url`:** reads the API's address from the stack outputs.
- **`$table`:** reads the DynamoDB table's name from the stack outputs.
- **`$api` and `$worker`:** read the real names of the two Lambda functions. Their log groups are named
  after them.
- **`$code`:** reads the access code from `.env` without printing it.
- **The `"url: …"` line:** prints what was found. Paste the URL into Postman as `base_url`.
- **`Set-Clipboard`:** copies the access code, so you can paste it into Postman as `access_code` without it
  appearing on screen.

All of these only read from AWS.

## 1. Running it

- **Postman Collection Runner** (recommended): run the whole collection, top to bottom. Each Ask request
  saves its `job_id` and jumps to **Poll job**. Poll job asks every 3 s until the job is no longer `pending`
  (up to 60 tries), runs that step's checks, then jumps to the next Ask. After D3 the run stops.
- **One request at a time:** send an Ask, then send **Poll job** until `status` is `done`. The checks for
  that step run on the final poll.
- **newman** (the same run from the command line):
  ```powershell
  npx newman run postman/Research-Desk-Demo.postman_collection.json --env-var "base_url=$url" --env-var "access_code=$code"
  ```
  This runs the 26 steps against the deployed API, plus Poll job after each Ask (46 requests and 120
  assertions in the I4 run), and prints each test result, plus the console lines.
  The access code is passed as a variable and isn't printed.

---

## 2. API basics (A1–A9): no Gemini calls

| Step | Request | Expected | What proves it |
|---|---|---|---|
| A1 | `GET /health`, no header | 200 `{"ok": true}` | The only route that needs no code. |
| A2 | `GET /watchlist`, no `x-access-code` | 401 `{"error": "Missing or wrong x-access-code"}` | Every other route checks the code first. |
| A3 | `GET /watchlist`, wrong code | 401, same message | Constant-time compare (`hmac.compare_digest`). |
| A4 | `POST /ask`, body `{not json` | 400 `"Body must be valid JSON"` | Checked before any job is created. |
| A5 | `POST /ask`, `{"mode": "quick"}` | 400 `"Missing 'question'"` | |
| A6 | `POST /ask`, `"mode": "fast"` | 400 `"'mode' must be one of quick, deep"` | The only modes are `quick` and `deep`. |
| A7 | `GET /ask/000…0` (32 hex) | 404 `"Unknown job"` | A well-formed job ID that doesn't exist. |
| A8 | `GET /ask/not-a-job-id` | 404 `"Not found"` | Job IDs must be 32 hex characters, or no route matches. |
| A9 | `DELETE /ask` | 405 `"Method not allowed"` | The path exists, but not for that method. |

Every response, errors included, carries an `x-request-id` header. Errors are always `{"error": "…"}`,
never a stack trace.

## 3. Market data, news, watchlist (B1–B9): no Gemini calls

| Step | Request | Expected | What proves it |
|---|---|---|---|
| B1 | `GET /watchlist` | 200, `watchlist` includes NVDA, AMD, INTC | Seeded at deploy, stored in DynamoDB. |
| B2 | `GET /quotes` | 200, `quotes` keyed by ticker | Each quote has `source` (`yfinance` or `finnhub`) and `trading_date` (New York date), or an `error`. One batched call for the whole watchlist. |
| B3 | `GET /quotes?tickers=NVDA,AMD` | 200, exactly NVDA and AMD | |
| B4 | `GET /quotes?tickers=NVDA,N!` | 400 `"Invalid ticker: 'N!'"` | Input validation. |
| B5 | `GET /news/NVDA` | 200, `ticker`, `articles[]` with `title`, `link`, `published` | |
| B6 | `GET /feed?limit=5` | 200, 1–5 `events`, newest first, each with `ticker`, `kind`, `title`, `created_at`, `notified` | Alerts written by the Watchdog (part 7), kept 30 days. If the feed is empty, run a replay (7.1) first. |
| B7 | `POST /watchlist {"name": "Apple", "pct_move": 2.5}` | 201, `added.ticker` = `AAPL` | A company name is resolved to its ticker through the SEC list. |
| B8 | `DELETE /watchlist/AAPL` | 200 `{"removed": "AAPL"}` | |
| B9 | `DELETE /watchlist/AAPL` again | 404 `"AAPL is not on the watchlist"` | |

---

## 4. Quick mode (Q1–Q5): 1 Gemini call each

**How it works:** Quick picks its tools with keyword rules, not an LLM (`core/agent.py:36-57`):
- **market** if the question says price, stock, share, P/E, market cap, move…
- **news** if it says news, latest, recent, headline…
- **filings search (`rag`)** if it says revenue, quarter, guidance, compare, risk, filing… **or if nothing
  else matched.**

The chosen tools run in parallel under one 25 s deadline. Every result is labelled `[Doc N]`, and one
Gemini call writes the answer.

Each Ask returns **202** `{"job_id", "status": "pending", "poll": "/ask/<job_id>"}`. When Poll job sees
`done`, it checks:

| Step | Question | Expected `used_tools` | Trace fields that prove it |
|---|---|---|---|
| Q1 | "What is AMD's stock price?" | `["market"]` | `timings["market:AMD"]`; `sources[0].kind` = `market` and its label names the session; the answer cites `[Doc N]`. |
| Q2 | "Any recent news on AMD?" | `["news"]` | Every `sources[].kind` = `news`. |
| Q3 | "What risks does Intel highlight in its filings?" | `["rag"]` | `sources[].kind` = `document` (filing excerpts with form, period and section); `companies` = INTC. |
| Q4 | "Who are NVIDIA's biggest customers?" | `["rag"]` | No keyword matched, so the filings search is the default; `companies` = NVDA. |
| Q5 | "Compare NVIDIA and AMD stock prices, latest news and revenue" | `market`, `news`, `rag` | Five timings: `market:NVDA`, `market:AMD`, `news:NVDA`, `news:AMD`, `rag`. **Parallel:** their sum is larger than `timings.total − timings.llm`, the time before the Gemini call. In the deployed eval a question of this shape (c5) took 6.8 s of tool time inside 2.1 s. |

Every Quick and Deep poll also checks these:
- `status` = `done`
- `answer` and `sources` are not empty
- there is no `context` field, because it's only returned with `include_context`
- `mode` is `quick` or `deep`

## 5. Deep mode (D1–D3)

| Step | Question | Gemini calls (eval) | Path |
|---|---|---|---|
| D1 | "What is NVIDIA's stock price right now?" | ~3 | Fast path |
| D2 | "Is AMD's selloff related to its guidance?" | ~10 | Planner, specialists, fact-checker |
| D3 | "What was NVIDIA's revenue in fiscal Q3 2027?" | ~3–5 | Fast path to filings, quarter not filed yet |

**D1, fast path.** The question has one intent (market), one company, and no "why", "driving", "related"
or "impact" word (`deep.py:43`, `:119`). So there is no planner call, and it goes straight to the market
specialist.
- `trace.fast_path` = `true`
- `trace.plan.tasks` = one task, agent `market`
- `trace.rounds` has 1 round
- the answer cites `E#`
- `trace.checker.numbers_checked` is a number

**D2, planner, parallel specialists, `[E#]` citations, fact-checker.** The word "related" forces the
planner (`NEEDS_PLANNING`, `deep.py:43`). In the deployed eval (2026-11-11), this exact question planned
**market, news and filings**. All three ran in parallel (market 6.0 s, news 10.2 s, filings 14.5 s). The
checker matched 9/9 numbers, 10 Gemini calls. The planner decides each time, so the agents can differ.
- `trace.fast_path` = `false`
- `trace.plan.tasks[].agent`: 2 or more agents (the console prints them)
- `trace.rounds[0][]`: one report per specialist, with `agent`, `seconds`, `llm_calls`, the `steps[].tool`
  it called, `findings[].claim` with their `evidence` IDs, and `gaps`
- `answer`: cites `E#`; `sources[]` has `id`, `kind`, `label`, `url`, and `cited: true` for those used
- `trace.checker`: `numbers_checked`, `supported`, `derived`, `unsupported`, `llm_calls`. An unsupported
  number gets **⚠️unverified** next to it in the answer.
- **Round 2 (follow-up):** this only runs if a specialist reported gaps **and** more than 30 s of the 75 s
  budget is left (`deep.py:132-141`). If it happens, `trace.rounds` has 2 entries and
  `trace.follow_up_agents` names them; the console prints both. It can't be forced from outside. It ran 3
  times in the local eval and has not run on AWS yet.

**D3, a quarter that isn't filed yet.** One intent (filings) and one company, so the fast path goes to the
filings specialist. The index's latest NVIDIA 10-Q covers the quarter ending 2026-07-26, which is Q2
FY2027. The filings prompt (`specialists.py:54-61`) tells the specialist to report the latest period it
found and add a gap saying the requested period isn't filed yet.
- `trace.plan.tasks[0].agent` = `filings`
- `trace.rounds[0][0].steps[].tool`: the console prints `latest_filing` and/or `search_filings`
- `trace.rounds[0][0].gaps` is not empty. Expected: Q3 FY2027 isn't filed yet.
- `trace.rounds[0][0].findings`: expected to give the Q2 FY2027 figures with their period (in the I2 local
  check: $96.2 billion, up 106%)

**Caveat for D3:** `latest_filing` asks the SEC live, but the filings text comes from the index built on
2026-11-05. Once NVIDIA files its Q3 FY2027 10-Q (expected in late November), `latest_filing` will show it,
but its text won't be searchable until the next ingest. The answer will then differ.

**Other fields worth showing:**
- `timings.total`: seconds inside the worker
- `timings.llm_calls`: Gemini calls for that answer
- `timings["agent:<name>"]`: seconds per specialist
- `trace.fallback`: empty unless Deep fell back to Quick. Then `mode` = `quick-fallback` and `notice`
  explains why.
- `trace.partial`: set only if the 75 s budget cut a specialist short

---

## 6. AWS, read-only

Use the PowerShell variables from part 0. From the Postman console, copy one `job_id` and its `x-request-id`
(the D2 ones are the most interesting).

**6.1 Logs, one request across both Lambdas.** The request ID from API Gateway is logged by the API Lambda
and passed on to the worker.

```powershell
$rid = "<x-request-id from the console>"
aws logs tail "/aws/lambda/$api" --since 30m --region $region | Select-String $rid
aws logs tail "/aws/lambda/$worker" --since 30m --region $region | Select-String $rid
```

- **What they do:** print the last 30 minutes of each function's logs and keep only lines with that request
  ID.
- **Expected:** JSON log lines with `"request_id"`. From the API: `POST /ask -> 202`. From the worker:
  `Ask job <job_id> done in …s (N LLM calls)`.

**6.2 Cold starts.**

```powershell
aws logs tail "/aws/lambda/$worker" --since 30m --region $region | Select-String "Init Duration"
aws logs tail "/aws/lambda/$api" --since 30m --region $region | Select-String "Init Duration"
```

- **What they do:** Lambda writes a `REPORT` line after every invocation. **Only a cold start** (a new
  container) adds `Init Duration: … ms`.
- **Expected:** one line per new container, usually on the first request after a quiet spell. Measured on
  the I5 image (`8bd7e7f6aa84`): API 2.5 s, worker 4.3 s.

**6.3 The job item in DynamoDB, with its TTL.**

```powershell
$job = "<job_id from the console>"
Set-Content -Encoding ascii -Path "$env:TEMP\job-key.json" -Value ('{"pk":{"S":"JOB"},"sk":{"S":"' + $job + '"}}')
aws dynamodb get-item --table-name $table --region $region --key "file://$env:TEMP\job-key.json" --query "Item.{status: status.S, mode: mode.S, created_at: created_at.S, finished_at: finished_at.S, expires_at: expires_at.N}" --output table
aws dynamodb describe-time-to-live --table-name $table --region $region
```

- **`$job`:** the job ID you copied.
- **`Set-Content`:** writes the item's key (`pk = JOB`, `sk = <job_id>`) to a file, which avoids PowerShell
  quoting problems.
- **`get-item`:** reads that one item and shows only `status`, `mode`, `created_at`, `finished_at` and
  `expires_at`.
- **`describe-time-to-live`:** shows the table's expiry setting.

**Expected:**
- `status` = `done`.
- `expires_at` is a Unix time 24 h after `created_at`. To read it as a date:
  `[DateTimeOffset]::FromUnixTimeSeconds(<expires_at>).UtcDateTime`
- TTL `ENABLED` on attribute `expires_at`. DynamoDB deletes expired items in the background, usually within a
  few days of expiry, not at the exact second.

**6.4 The Deep daily counter.**

```powershell
$day = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, "Pacific Standard Time").ToString("yyyy-MM-dd")
Set-Content -Encoding ascii -Path "$env:TEMP\cap-key.json" -Value ('{"pk":{"S":"STATE"},"sk":{"S":"deep:count:' + $day + '"}}')
aws dynamodb get-item --table-name $table --region $region --key "file://$env:TEMP\cap-key.json" --query "Item.value.S" --output text
```

- **What they do:** work out today's Pacific date (Gemini's quota day), then read that day's Deep question
  count.
- **Expected:** a number that went up by 3 during the demo. Questions after 50 fall back to Quick.

**6.5 EMF metrics (namespace `ResearchDesk`).** After each answer the worker prints one metrics record to its
log, and CloudWatch turns it into metrics:
- `AgentLatency` (seconds)
- `GeminiCalls`
- `DeepFallbacks`
- `AskErrors`

Each metric has the dimension `Mode` = `quick` or `deep`.

```powershell
aws cloudwatch list-metrics --namespace ResearchDesk --region $region --query "Metrics[].[MetricName, Dimensions[0].Value]" --output table
$start = (Get-Date).ToUniversalTime().AddHours(-1).ToString("s"); $end = (Get-Date).ToUniversalTime().ToString("s")
aws cloudwatch get-metric-statistics --namespace ResearchDesk --metric-name AgentLatency --dimensions Name=Mode,Value=deep --start-time $start --end-time $end --period 3600 --statistics Average Maximum SampleCount --region $region
aws logs tail "/aws/lambda/$worker" --since 30m --region $region | Select-String "AgentLatency"
```

- **`list-metrics`:** lists the metric names and modes that exist.
- **`$start` / `$end`:** set a one-hour window.
- **`get-metric-statistics`:** gets the average, maximum and count of Deep latency in that window.
- **`logs tail … AgentLatency`:** shows the raw EMF records in the worker log.

**Expected:**
- `SampleCount` 3 for `deep` (plus any other Deep questions in the last hour).
- The data points appear 1–3 minutes after the answers. `list-metrics` can take up to 15 minutes to show a
  metric for the first time.

---

## 7. Watchdog: price alerts, Telegram, the dead-letter queue

**How it works:** EventBridge Scheduler starts the Scan function every 15 minutes, Mon–Fri, in New York time.
Runs outside the 9:30–16:00 session are dropped. The scan reads the watchlist and the quotes, and for each
move past the ticker's threshold (default 3%) it writes one alert with a one-line "why" (headlines + 1
Gemini call). The alert and its dedupe marker go into DynamoDB in one transaction, so each move alerts once
per day. The table's Stream starts the Notify function, which sends the Telegram message and marks the alert
`notified`.

A **replay** writes one labelled demo alert ("Demo replay: this move did not happen.") through the same path,
at any time of day. Each replay ticker works once per day (dedupe key `replay:<TICKER>:move_down:<date>`), so
pick a ticker not yet replayed today.

Extra PowerShell variables (read-only):

```powershell
$scan  = aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].Outputs[?OutputKey=='ScanFunctionName'].OutputValue" --output text
$dlq   = aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].Outputs[?OutputKey=='NotifyDLQUrl'].OutputValue" --output text
$alarm = aws cloudformation describe-stack-resource --stack-name research-desk --logical-resource-id FailedAlertsAlarm --region $region --query StackResourceDetail.PhysicalResourceId --output text
```

They read the Scan function's name, the dead-letter queue's URL and the failed-alerts alarm's name.

**7.1 Replay → Telegram → feed.**

```powershell
Set-Content -Encoding ascii -Path "$env:TEMP\replay.json" -Value '{"replay": "INTC:-7.5"}'
aws lambda invoke --function-name $scan --region $region --payload "fileb://$env:TEMP\replay.json" "$env:TEMP\scan-out.json"
Get-Content "$env:TEMP\scan-out.json"
```

- **`Set-Content`:** writes the event to a file, which avoids PowerShell 5.1 stripping the JSON quotes.
- **`lambda invoke`:** runs the Scan function once with that event and saves its reply.
- **Expected:** `StatusCode 200`, and a reply listing 1 price event. Within a few seconds Telegram shows
  "📉 INTC -7.5% on … (replay)" with its "why" and the label "Demo replay: this move did not happen." Then `GET /feed?limit=5` (B6) or the UI's
  alert feed shows it first, with `notified: true`.
- **Run it again:** the reply has 0 price events, and the Scan log says "Already alerted: replay:INTC:…".
  That's the dedupe.

**7.2 Failure path → DLQ → alarm email → purge → OK.** Run it only **outside the New York session** (Mon–Fri
9:30–16:00 ET; in IST that's 20:00–02:30 in the US winter, 19:00–01:30 in summer). During the session the Scan function refuses it and returns
`{"refused": "fail_delivery only outside market hours (Mon-Fri 9:30-16:00 New York)"}`. A failed record takes
the rest of its stream batch to the DLQ, so a real alert in the same batch would be lost. Wait until a few
minutes after 16:00 ET: the 16:00 scan can still be running until about 16:02.

```powershell
Set-Content -Encoding ascii -Path "$env:TEMP\fail.json" -Value '{"replay": "AMD:-7.5", "fail_delivery": true}'
aws lambda invoke --function-name $scan --region $region --payload "fileb://$env:TEMP\fail.json" "$env:TEMP\scan-out.json"
aws sqs get-queue-attributes --queue-url $dlq --attribute-names ApproximateNumberOfMessages --region $region
aws cloudwatch describe-alarms --alarm-names $alarm --region $region --query "MetricAlarms[0].StateValue" --output text
aws sqs purge-queue --queue-url $dlq --region $region
aws cloudwatch describe-alarm-history --alarm-name $alarm --history-item-type StateUpdate --max-items 4 --region $region --query "AlarmHistoryItems[].[Timestamp,HistorySummary]" --output table
```

- **The invoke:** writes a demo alert that Notify refuses on purpose. Lambda retries it 3 times, then sends
  the batch to the DLQ.
- **`get-queue-attributes`:** expected `1` after the retries.
- **`describe-alarms`:** `ALARM` within about 5 minutes, and an email from SNS.
- **`purge-queue`:** empties the DLQ, so the alarm goes back to `OK` and a real failure can email again.
- **`describe-alarm-history`:** shows `OK → ALARM` and `ALARM → OK` with their times. On 2026-11-14 the alarm
  went to ALARM 5 min 14 s after the invoke and back to OK 3 min 22 s after the purge.
- The demo item stays in the feed with `notified: false` until its 30-day expiry.

**7.3 The other alarms.** Four more alarms email through the same topic: API 5xx, ask errors (Quick + Deep),
worker errors and scan errors. To see one email without breaking anything, force its state; it returns to OK
at its next evaluation:

```powershell
$apiAlarm = aws cloudformation describe-stack-resource --stack-name research-desk --logical-resource-id ApiErrorsAlarm --region $region --query StackResourceDetail.PhysicalResourceId --output text
aws cloudwatch set-alarm-state --alarm-name $apiAlarm --state-value ALARM --state-reason "demo" --region $region
```

Like the replays, this changes state on AWS: it sends one alarm email (the alarms don't email on OK).

**7.4 The web UI.** The same features in a browser: https://research-desk-pnivvnmyzvtsqizfwgbxuz.streamlit.app/
(access code once per session). Quick and Deep answers with sources and the Deep trace, the watchlist with
live quotes, and the alert feed. See the README's "Web UI".

---

## 8. Local

| What | Command | Notes |
|---|---|---|
| Unit tests | `python -m pytest` | All mocked: no AWS, no Gemini. |
| The demo from the command line | `npx newman run postman/Research-Desk-Demo.postman_collection.json --env-var "base_url=$url" --env-var "access_code=$code"` | Same as part 1. It calls AWS and Gemini. |
| B2/B3 quotes | `python cli.py quotes NVDA AMD INTC` | Free. |
| B5 news | `python cli.py news NVDA --count 5` | Free. |
| B7 name → ticker | `python cli.py resolve "Apple"` | Free. |
| Filings search alone | `python cli.py retriever "What risks does Intel highlight in its filings?"` | 1 embedding call, cached after. |
| Q1–Q5 | `python cli.py agent "What is AMD's stock price?"` (and the other Quick questions) | 1 Gemini call each. Same result fields, minus `context`. |
| D1–D3 | `python cli.py agent --deep "Is AMD's selloff related to its guidance?"` (and the others) | The same Gemini calls as on AWS. Uses the local store and index. |

**Local runs cost the same Gemini calls**, so run the questions either locally or on AWS, not both. To save
a Deep result for reading, add `| Out-File -Encoding utf8 "$env:TEMP\deep.json"`; a plain `>` in PowerShell
5.1 writes UTF-16.

---

## 9. Feature checklist

| Feature | Step |
|---|---|
| Health check without auth | A1 |
| Access code (missing, wrong), constant-time compare | A2, A3 |
| Input validation and JSON errors (body, question, mode, ticker) | A4, A5, A6, B4 |
| Unknown job, unknown route, wrong method | A7, A8, A9 |
| Watchlist in DynamoDB (list, add by company name, remove, 404) | B1, B7, B8, B9 |
| Batched quotes with `source` and `trading_date` | B2, B3 |
| News headlines | B5 |
| Alert feed (Watchdog alerts, newest first) | B6, 7.1 |
| Async jobs: 202 + `job_id`, then poll until `done` | every Ask + Poll job |
| Quick keyword routing to market / news / filings | Q1, Q2, Q3 |
| Filings search as the default when no keyword matches | Q4 |
| Quick tools in parallel under one deadline | Q5 |
| `[Doc N]` citations and typed `sources` | Q1–Q5 |
| RAG over the 934-chunk filings index on S3 | Q3, Q4, Q5, D2, D3 |
| Deep fast path (no planner) | D1, D3 |
| Gemini planner, specialists in parallel, evidence board, `[E#]` citations | D2 |
| Fact-checker (`trace.checker`, ⚠️unverified) | D1, D2 |
| Follow-up round (round 2), when it happens | D2 (`trace.rounds`, `follow_up_agents`) |
| Reporting a quarter that isn't filed yet | D3 |
| No `context` unless asked for | every poll |
| One request ID across the API and worker logs | 6.1 |
| Cold start (`Init Duration`) | 6.2 |
| Job item with 24 h TTL | 6.3 |
| Deep daily cap (50 per Pacific day) | 6.4 |
| CloudWatch EMF metrics | 6.5 |
| Watchdog: replay alert with a "why", Telegram via the DynamoDB Stream, dedupe | 7.1 |
| Failed delivery → DLQ → alarm email → purge → OK; refused during the session | 7.2 |
| Error alarms (API 5xx, ask, worker, scan) through SNS | 7.3 |
| Web UI on Streamlit Community Cloud | 7.4 |
| Unit tests, newman, CLI | 8 |

**Not in this demo:**
- A question about a company without indexed filings (e.g. Apple): the answer's `notice` says its filings
  aren't covered (I5). Smoke-tested on AWS (PROGRESS, I5); ask it in the UI to show it.
- The Deep → Quick fallback: it needs a Gemini outage, the 75 s budget to run out, or the daily cap to be
  reached. Unit tests cover it.
