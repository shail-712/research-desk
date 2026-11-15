# DESIGN — Research Desk

The key design decisions and why they were made, then what doesn't work well yet. The architecture diagram is
in the [README](../README.md#architecture). Each decision's full entry is in [DECISIONS.md](DECISIONS.md) (D1–D18).
The build history and every measurement are in [PROGRESS.md](PROGRESS.md).

## Key decisions

### Gemini free tier instead of Amazon Bedrock (D1)
- **Why:** the target is ~$0/month. Bedrock charges per token and has no always-free tier. Gemini's free tier
  covers a demo and the whole eval.
- **How:** one provider interface (`core/llm.py`) with a fallback chain across Gemini models, a timeout per
  call and a cooldown for a failing model. Embeddings are paid for once, cached by content hash (D11).
- **Trade-off:** it departs from the brief's "Amazon Bedrock", and the free tier's rate limits and 503s are
  outside our control (see the limitations below). Adding Bedrock would mean one more adapter (Converse API
  with tool use), not a redesign.

### `POST /ask` returns 202, and the client polls (D7)
- **Why:** API Gateway cuts off a request after 30 s. Deep mode is budgeted at 75 s, and even a cold-start
  Quick answer once took ~31 s. So the API Lambda writes a `pending` job to DynamoDB, starts the worker
  asynchronously and returns `202` with a `job_id` at once. The client polls `GET /ask/{id}`.
- **Trade-off:** clients must poll (the UI polls every 2 s, for up to 150 s). Jobs keep a 1-day TTL.
  The worker's async retries are 0, so a failure becomes an `error` job instead of rerunning long LLM work.

### Deep mode: specialists, an evidence board and a fact-checker
- **How:**
  - A planner picks specialists (market, news, filings), which run in parallel.
  - Every tool result goes once onto a shared board as `[E1]`, `[E2]`, …, with its source link and "as of"
    time.
  - The writer may only cite `[E#]`.
  - The fact-checker extracts every number in the answer and matches it against the board in code. Only
    numbers that don't match go to one LLM call, which decides "derived" (e.g. a growth rate) or
    "unsupported". Unsupported numbers are flagged ⚠️ in the text, never silently kept.
  - Simple questions (one intent, one company) take a fast path with one specialist.
- **Why:** in investment research, a number without a source is a liability. The board makes every claim
  traceable, and the code check catches numbers the model invented.
- **Result (deployed eval, answers from 2026-11-09 and 11-11, 20 questions):** 1 of 141 numbers in Deep answers couldn't be traced
  to evidence.
- **Trade-off:** cost and time. Deep is a mean of 4.7 Gemini calls and 13.2 s p50, against Quick's 1.0
  calls and 6.5 s. Quick stays the default for simple questions (D5).

### A frozen, code-only scorer (D8)
- **How:** the metrics (untraceable numbers, cited lines, routing, latency) are computed by code, with no LLM
  judge. `evals/scoring.py` and `core/numbers.py` are frozen, and a golden test re-scores saved results and must
  reproduce them exactly.
- **Why:** if the ruler changes between runs, results can't be compared. An LLM judge would add noise and
  quota cost.
- **Trade-off:** the scorer's quirks are frozen too. For example, a heading with a digit counts as a numeric
  line that needs a citation.

### `EnableWatchdog`: one template, one feature flag
- **How:** `template.yaml` takes `EnableWatchdog`. With `false`, the stack is the ask path only (14
  resources). There's no Scheduler, no DynamoDB Stream and nothing running idle. With `true`, it adds the Scan
  and Notify functions, the Scheduler, the Stream mapping, the DLQ and their alarms (29 resources).
- **Why:** the ask path was deployed and proven first (I1–I2), and the Watchdog was added later (I3) by
  changing one parameter. The flag can also turn off the part that spends Gemini quota every 15 minutes
  without deleting the stack.
- **Trade-off:** conditions spread through the template. A unit test checks both values.

### Least-privilege IAM, per function (I4)
- **Secrets:** each function reads only its own secrets: `ssm:GetParameters` on exact ARNs from its
  `SSM_SECRETS` list, and `kms:Decrypt` only through SSM. The API never sees the Gemini key; Notify only
  sees Telegram's.
- **Scan:** DynamoDB `Query`, `GetItem` and `PutItem` only.
- **Notify:** reads only this table's stream, plus `UpdateItem` on the table to mark an alert `notified`.
- **Worker:** reads S3 only through `s3:GetObject` on `vectors/*`.
- **Why:** a bug or a leaked key in one function can't reach another function's secrets or data.
- **Trade-off:** each new secret means a template change. The API and Worker roles are not narrowed yet (see
  the production improvements).

### Smaller decisions
- Market data comes from yfinance, with Finnhub as the fallback because Yahoo often blocks AWS IPs (D2).
- The vector index is FAISS on S3, not OpenSearch: it's a few MB, and OpenSearch's monthly minimum is over
  budget (D6).
- The ECR repository is its own stack, because SAM pushes the image before the main stack exists (D15).
- The Region is ap-southeast-2, the only one this account can use (D17).
- The scope was cut for speed (D13). The cut items are listed under production improvements.

## Known limitations
- **SEC filings cover 3 companies: NVDA, AMD and INTC.**
  - Quotes (Yahoo, with Finnhub as the fallback) and headlines (Yahoo RSS) work for any listed ticker, so a
    question about another company still gets a price and news, but no filings.
  - Until I5, filings search fell back to all documents when the named company had none. An Apple question
    could get NVIDIA's or AMD's filings, and the answer could cite the wrong company. **Fixed in I5:** a named
    company with no indexed filings gets no filings, and both modes say so in the answer's notice: "SEC filings
    for Apple Inc. (AAPL) aren't covered: the filings index has AMD, INTC and NVDA only."
  - What's left:
    - A Deep filings search with no ticker still searches all documents. The planner usually passes the
      ticker, but nothing enforces it.
    - In Quick, when no company is recognised in the question, the search covers all documents with no
      notice. That's by design for questions like "Which chipmaker…", but an Apple question that doesn't
      name a company the resolver knows (e.g. "the iPhone maker") can still get NVIDIA, AMD or Intel filings.
- **News is per company only.** Each headline comes from that ticker's Yahoo RSS feed, and there's no general
  market news. If no headline names the company, the feed's top stories are used instead.
- **Deep mode can miss the latest period's figures.**
  - When the asked period isn't filed yet (e.g. "NVIDIA's Q3" before NVIDIA files it), the answer reliably
    says so, and names the latest filed period.
  - But it includes that period's revenue only some of the time, because the filings specialist follows the
    "one topic per search" prompt rule unevenly. On AWS, one run left out the $96.2B / +106% figure that a
    local run found.
- **Quick mode's keyword routing misses open-ended questions.**
  - "Tell me about Intel" matches no market or news keyword, so it gets a filings search only: no quote and
    no headlines.
  - Routing by keyword costs no LLM call, which is why it's used (D5).
- **Cold starts and the Gemini free tier.**
  - The image is 1.27 GB. Worker init is 2.6–4.3 s, and once, on the first start of a new image, it hit
    Lambda's 10 s init limit.
  - Free-tier models return 503/504 under load. Falling back costs time: one Quick job took 39.1 s.
  - The daily quota caps the number of Deep questions. When it runs out, a Deep answer falls back to Quick
    or loses a specialist, and its notice says so.
- **Watchdog: a failed alert takes the rest of its stream batch with it.**
  - Notify reads up to 10 records per batch and reports the first failed one, so Lambda retries from there.
    After 3 retries, the rest of the batch goes to the DLQ as one message (metadata only), and the alarm
    emails.
  - A real alert behind a failed one stays `notified False` in the feed. Resending is manual.
  - The demo failure (`fail_delivery`) is refused during the session, so it can't share a batch with a real
    alert.
- **The session check has a 3-minute gap at the close.**
  - `in_session` drops the seconds, so `fail_delivery` is allowed from 16:01:00 ET.
  - But the 16:00 scheduled scan starts at ~16:00:16 and can run for up to 120 s. A demo failure started in
    that window could still share a batch with a real alert.
- **The UI's access code is the API's access code.**
  - Anyone who can open the UI holds the key to the API.
  - The UI gate has no limit on attempts. API Gateway's throttling (10 burst, 5 requests/s) is the only brake.
  - This is fine for a single-user demo, not for real users.
- **No market-holiday calendar (D12).** A scan whose quote is from another day skips all price rules, so a
  holiday never alerts on stale data. But on the day after a holiday, the "why" window starts at the wrong
  previous close.
- **Cost is a design target, not a measurement.** "About $0/month" rests on the free tiers, a PROVISIONED
  2/2 table and no always-on servers. There's a $1 budget alarm, but no bill has been measured.

## Production improvements
- **Filings:** rewrite the search in code from the filing metadata. When the asked period is newer than the
  latest filed one, search for "latest results". This replaces relying on the prompt.
- **Routing:** when a company is named but no intent word matches, run all three tools. Or route with a
  small classifier call.
- **Coverage and news:**
  - General market news and company news from Finnhub. Today Finnhub is used only as the quote fallback and
    for the optional market cap and P/E.
  - Adding a company is a documented step, not a code change: `python cli.py ingest <TICKERS>`, then
    `python -m scripts.bootstrap --replace` to swap the S3 index (it replaces exactly the version it read),
    then add the ticker to the watchlist.
  - Make a Deep filings search with no ticker use the plan's companies.
- **Latency:**
  - Lazier imports (pandas and yfinance only when a quote is needed), a smaller image, and a warm-up call
    after each deploy.
  - A paid model tier, or shorter timeouts per model before falling back.
- **IAM:** narrow the API and Worker roles from SAM's `DynamoDBCrudPolicy` (which includes `Scan`,
  `DeleteItem` and the batch actions) to the actions each one uses, as was done for Scan.
- **Watchdog:**
  - Refuse `fail_delivery` until the close plus the scan timeout (16:03 ET), or have Notify send demo items
    straight to the DLQ.
  - A resend tool for `notified False` items.
  - A holiday calendar.
- **Access:** real users (e.g. Cognito, or one API key per user) with a limit on attempts. The UI then
  stops sharing one code with the API.
- **Cut for speed (D13), still worth building:**
  - SEC filing alerts with an XBRL earnings snapshot.
  - Auto-ingest when a ticker is added or a new filing appears (today it's the manual `cli.py ingest`).
  - X-Ray traces and a CloudWatch dashboard.
  - An OIDC deploy workflow (today it's a manual `sam deploy`; CI runs the tests).
  - A script that downloads the raw filings.
  - An S3 snapshot of the embedding cache.
