# 05 · Challenges and Interview Prep

## Hard or interesting parts (and how they were solved)

- **API Gateway's 30 s limit vs. slow AI answers.** A cold-start Quick answer once took ~31 s, and Deep can take up to 75 s. **Fix:** an async job pattern. `POST /ask` writes a `pending` job and invokes the worker with `InvocationType="Event"`, then returns `202`. The client polls `GET /ask/{id}` (D7).
- **AI models invent numbers.**
  - Every tool result gets a citation label (`[Doc N]` / `[E#]`).
  - The prompt forbids numbers from memory.
  - `checker.check()` verifies every number in code, and calls the LLM only for unmatched numbers.
  - Result on the deployed eval set: about 1% of numbers were untraceable in both modes.
- **Exactly-once alerts.** The scan runs every 15 min and a move stays true all day, so the same alert would fire again and again. **Fix:** a `dedupe_key` per ticker + rule + day, written in a DynamoDB transaction with a conditional put on a `SEEN#` marker. Two racing scans can't both win.
- **Slow or hanging tools.**
  - A `with ThreadPoolExecutor` block waits for every thread on exit. So the code uses one shared deadline, `future.result(timeout=...)`, and `pool.shutdown(wait=False, cancel_futures=True)`.
  - A hung Yahoo call can't push the answer past its budget.
- **The free Gemini tier returns 503s.** `core/llm.py` tries 4 models in order, with a backoff between models. It uses a per-attempt timeout cut to the time left, and a 60 s cooldown that moves a failing model to the back of the line.
- **Yahoo blocks AWS IP addresses.** The fix is Finnhub as an automatic fallback, and every quote records its `source` (D2).
- **Wrong-company filings.** An Apple question used to retrieve NVIDIA's filings. Fixed in I5: named companies with no indexed filings get none, and the answer says "not covered".
- **"Why did it move" without lying.** After-hours news was credited for the day's move. **Fix:** a strict session time window, code checks first, and the LLM may only *pick* a headline ID, never write its own reason.
- **Paying for embeddings once.** The embed free tier is ~1,000 requests/day. **Fix:** a cache keyed by `sha256(model|dim|task_type|text)`, a `chunk_hash` skip in ingest, and an index that lives only in S3 (D11).
- **A fair ruler for the eval set.** The scorer (`evals/scoring.py`, `core/numbers.py`) is frozen. A golden test makes sure the scores stay reproducible (D8).

## Trade-offs

| Choice | Gained | Gave up |
|---|---|---|
| Gemini instead of Bedrock (D1) | $0 cost | Breaks the brief's "Bedrock" requirement; free-tier rate limits and 503s are outside our control |
| Async 202 + polling | Never hits the 30 s limit; a fast API | Clients must poll (UI: every 2 s); more moving parts |
| Quick = keyword routing (D5) | 0 extra LLM calls, ~6.5 s p50 | Misses open-ended questions ("Tell me about Intel" gets filings only) |
| Deep = multi-agent | Better coverage, traceable evidence | ~4.7 Gemini calls and ~13 s p50 vs 1 call and ~6.5 s |
| FAISS on S3 (D6) | Free, simple, fast in memory | Only one writer at a time (handled with `If-Match`); doesn't scale to huge corpora |
| Provisioned DynamoDB 2/2 | Inside the always-free tier | Low throughput ceiling; throttles under load |
| One container image for 4 functions | One build, one dependency set | 1.27 GB image → slower cold starts |
| Shared access code | Very simple | No per-user identity, no attempt limits |
| Scope cut (D13) | Shipped in the 5–7 day window | No SEC filing alerts, no auto-ingest, no X-Ray, no OIDC deploy |

## Weaknesses / what I'd improve

- **Only 3 companies have filings** (NVDA, AMD, INTC). Adding one is manual (`cli.py ingest` + `bootstrap --replace`). I'd automate ingest when a ticker is added.
- **Quick routing misses open-ended questions.** If a company is named but no keyword matches, I'd run all 3 tools, or add a tiny classifier call.
- **The Deep daily counter isn't atomic.** `_take_daily_slot()` does `get_state` and then `set_state`, so two parallel requests can both take the last slot. A DynamoDB `UpdateItem` with `ADD` and a condition would fix it. *(I noticed this in the code; it isn't in the docs.)*
- **Cold starts:** I'd use lazy imports (pandas/yfinance only when needed), a smaller image, and a warm-up after deploy.
- **IAM:** the API and worker roles still use SAM's broad `DynamoDBCrudPolicy`. I'd narrow them like Scan's.
- **One failed alert in a stream batch** sends the rest of the batch to the DLQ, and there's no resend tool.
- **No holiday calendar** (D12). The "why" window is wrong on the day after a holiday.
- **Polling:** I'd use WebSockets or Server-Sent Events (the server pushes the answer when ready) for a real product.
- **The cost is a target, not a measured bill.**

## Scaling to 10x and 100x users

**The real bottleneck isn't AWS. It's the external quotas:** Gemini free tier, Yahoo, Finnhub (60/min), SEC.

**10x (tens of users):**
- **LLM:** move to a paid Gemini tier, or add a Bedrock adapter behind `core/llm.py`. The interface is already one function, `llm.call()`.
- **Caching:**
  - Cache quotes for ~30–60 s and news for ~5 min, in DynamoDB with TTL or ElastiCache. Many users ask about the same tickers.
  - Cache identical Quick answers for a short time, keyed by question + quote session.
- **DynamoDB:** switch to on-demand or turn on auto-scaling. 2/2 provisioned will throttle.
- **API Gateway:** raise the throttle above 5 req/s, and add per-user usage plans.
- **Auth:** Cognito or per-user API keys, so limits and the Deep cap work per user.

**100x (hundreds to thousands of users):**
- **Hot partitions:** every job uses `pk = "JOB"` and every alert uses `pk = "FEED"`. One partition key means one partition, with a limit of roughly 1,000 writes/s. I'd use `pk = JOB#<job_id>`, and shard the feed by user or day (`FEED#<date>`).
- **A queue between the API and the worker:** put jobs on SQS instead of direct async invokes. The worker then pulls at a controlled rate (reserved concurrency), and retries and DLQ come for free. This smooths spikes against the LLM quota.
- **Push instead of poll:** API Gateway WebSockets, or AppSync subscriptions on the job item, would cut the read load on `GET /ask/{id}` by about 10x.
- **Vector search:** FAISS in memory per container is fine for thousands of chunks. For hundreds of companies, move to OpenSearch Serverless, pgvector or Bedrock Knowledge Bases, with metadata filters by ticker.
- **Watchdog:** a per-user watchlist means fan-out. Have the scan batch quotes once per unique ticker, then fan alerts out per user (SQS/SNS).
- **Cold starts:** provisioned concurrency for the API (it costs money), and a slimmer image.
- **Ops:** X-Ray tracing, a CloudWatch dashboard, and per-tenant cost tracking.

## Security considerations

- **Secrets:** stored in SSM SecureString (KMS-encrypted), read once per cold start, scoped per function by IAM and `SSM_SECRETS`. Never in the template, the image or git (`.env` is git-ignored). Redacted from logs (`core/log.py`). The Telegram URL is never logged because it holds the bot token.
- **Auth:** constant-time compare, fails closed when unconfigured. **Weak spot:** one shared code, the same in the UI and the API, with no attempt limit. Only API Gateway throttling slows a brute-force guess.
- **Input validation** on every route, with size caps (question ≤1000 characters, ≤20 tickers, the `/feed` limit ≤50).
- **Safe errors:** no stack traces to clients, typed error messages.
- **Prompt injection:** headlines and filings go into prompts. They could hold instructions. Mitigations: the fact-checker, citations only from the board, the "why" LLM can only pick an existing headline ID, and no tool can write or delete anything. TODO: confirm whether you added any explicit guard.
- **Abuse / cost:** API throttling, the Deep daily cap (50), and the $1 AWS Budget alarm.
- **Least-privilege IAM** per function. The ECR repository keeps only 2 images.

## Likely interview questions

**1. What does the project do, in one line?**
An AI research assistant that answers stock questions from live prices, news and SEC filings. Every number cites its source, and code fact-checks the numbers.

**2. Why is `/ask` asynchronous?**
API Gateway cuts requests off at ~30 s. Deep answers can take 75 s. So the API saves a pending job, starts the worker in the background, returns 202 with a job ID, and the client polls.

**3. What is RAG, and how did you build it?**
Search your documents first, then give the matching passages to the LLM. `core/ingest.py` splits SEC filings into ~300-token chunks and embeds them with Gemini (768 dims) into a FAISS index on S3. `retriever.search()` over-fetches 50 and keeps at most 3 per company.

**4. Why Gemini and not Bedrock, when the brief asked for Bedrock?**
Cost. Gemini has a free tier, and Bedrock is pay-per-token. All LLM calls go through `core/llm.py`, so adding Bedrock means one adapter, not a redesign. I documented the deviation as D1.

**5. How do you stop the model from making up numbers?**
Every tool result has a citation label. The prompt forbids numbers from memory. In Deep mode the fact-checker extracts every number and matches it against the evidence in code. Only unmatched numbers go to one LLM call ("derived" vs "unsupported"), and unsupported ones are flagged ⚠️.

**6. Quick vs Deep: when would you use each?**
Quick for simple lookups: 1 LLM call, ~6.5 s. Deep for multi-part or "why" questions: ~4.7 calls, ~13 s, better coverage and an evidence trail. Deep falls back to Quick if the model is down.

**7. How does the Watchdog avoid sending the same alert twice?**
Each alert has a key like `AMD:move_down:2026-11-02`. `insert_event()` uses one DynamoDB transaction: put a `SEEN#key` marker only if it doesn't exist, and put the alert. Both land or neither does.

**8. Why separate the scan from Telegram delivery?**
So a Telegram outage can't slow or fail the scan. The scan only writes. A DynamoDB Stream triggers `NotifyFunction`, which retries 3 times and then sends to an SQS dead-letter queue with an alarm.

**9. Why a single DynamoDB table?**
Every access pattern is one `Query` or `GetItem` on `pk`/`sk`. Streams, TTL and transactions all work on one table. Provisioned 2/2 stays inside the always-free tier.

**10. Why FAISS on S3, not OpenSearch?**
The index is a few MB (934 chunks). OpenSearch Serverless has a monthly minimum far above a $0 budget. Each Lambda keeps the index in memory and rechecks its ETag every 5 minutes.

**11. Why a container image for Lambda?**
numpy, pandas, faiss and google-genai together exceed the 250 MB zip limit. One image serves all 4 functions, with a different `Command` each.

**12. How do you handle secrets?**
SSM Parameter Store SecureStrings. Each function reads only the names in its `SSM_SECRETS` list once at cold start, and IAM allows only those exact parameters. Values are redacted from logs.

**13. How did you measure quality?**
A 20-question eval set, run in both modes on the deployed stack. A frozen, code-only scorer measures untraceable numbers, cited lines, routing and latency. There's no LLM judge, so the results are repeatable.

**14. What was the hardest bug?**
TODO: pick your own. Good candidates: the "why" explanation crediting after-hours news to the day's move, Apple questions getting NVIDIA's filings, or threads making the answer wait on a hung tool.

**15. What if Gemini is down?**
`llm.call()` tries 4 models with cooldowns. If all fail, Quick returns a friendly "model busy" error, and Deep falls back to Quick. The job is marked `error`, never left hanging.

**16. What if Yahoo blocks you?**
`core/market.py` falls back to Finnhub per ticker, and every quote records which source it came from. If both fail, that tool is marked unavailable and the answer goes on without it.

**17. What if two scans run at the same time?**
The DynamoDB transaction's conditional put means only one can write the `SEEN#` marker. The other gets `TransactionCanceledException`, which is treated as "already recorded".

**18. How would you scale to 1,000 users?**
Paid LLM tier, caches for quotes and news, SQS between the API and the worker, push instead of polling, per-user auth with Cognito, on-demand DynamoDB with better partition keys (no single `JOB` / `FEED` partition), and a managed vector store when the corpus grows.

**19. What's the weakest part of your design?**
Access is one shared code with no per-user limits, filings cover only 3 companies, and Quick's keyword routing misses open-ended questions. All of these are listed in `docs/DESIGN.md` with fixes.

**20. How did you test without hitting real APIs?**
442 pytest unit tests, fully mocked. `moto` fakes DynamoDB, S3, SSM and SQS in memory. Tests even check `template.yaml` (e.g. that IAM secrets match `SSM_SECRETS`). CI runs them on every push.
