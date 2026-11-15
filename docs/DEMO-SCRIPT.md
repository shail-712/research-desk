# Demo script: 15 min explanation + 20 min demo + 15 min Q&A

The run sheet for the interview. The step-by-step reference for every feature, with the commands and what
proves each one, is [`example_demo.md`](example_demo.md). Timings and call counts below are measured on AWS
(PROGRESS, the deployed eval).

**Gemini budget for one run:** about 25 calls (Deep brief query 10–16, Quick 1, Deep comparison about 5,
replay 1, Apple Quick 1, plus Q1 in Postman 1), and 2 of the 50 daily Deep slots. Rehearse on a different day from the interview, or
keep rehearsals to the Quick steps, so the free-tier quota is full on the day.

---

## Before the interview

**The day before**
- [ ] Two timed rehearsals of part 2, and a screen recording of one as the backup.
- [ ] **NVIDIA's Q3 filing.** The index was built on 2026-11-05, when NVIDIA's latest 10-Q was Q2 FY2027.
      If NVIDIA has since filed Q3 (expected late November), the brief's query will say "latest available
      is Q2" while SEC already has Q3. Either re-index (`python cli.py ingest NVDA`, then
      `python -m scripts.bootstrap --replace`) or say it out loud as the known limitation it is.

**60 minutes before**
- [ ] Stack and alarms (read-only, PowerShell, from the repo root):
  ```powershell
  $region = "ap-southeast-2"
  aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].StackStatus" --output text
  aws cloudwatch describe-alarms --region $region --query "MetricAlarms[].[AlarmName,StateValue]" --output table
  ```
  Expected: `UPDATE_COMPLETE`, and all 5 alarms `OK`.
- [ ] Pick the replay ticker: one that hasn't been replayed today (each replay ticker works once per New York
      day). Write it down.

**15 minutes before**
- [ ] Open the UI (https://research-desk-pnivvnmyzvtsqizfwgbxuz.streamlit.app/). If Community Cloud shows that
      the app is asleep, wake it now; that takes about a minute.
- [ ] Enter the access code and ask one Quick question ("What is NVIDIA's stock price?"). This warms both
      Lambdas, so the demo doesn't start with a cold start (worker init 2–4 s, and the first start of a new
      image has once taken 10 s).
- [ ] Tabs in order: README (architecture diagram), the UI, Postman (the demo collection, environment
      selected), Telegram on your phone or desktop, CloudWatch Logs for the worker, DESIGN.md.

---

## Part 1: the solution (15 min)

| Min | Topic | Show | Say |
|---|---|---|---|
| 0–2 | The problem | README top | Research that takes days per company, answered in seconds, with every number cited to a quote, a headline or an SEC filing passage. |
| 2–6 | Architecture | README diagram | **Ask path:** API Gateway → API Lambda writes a `pending` job and returns `202` → worker Lambda answers → client polls. Why: API Gateway cuts requests at 30 s, and Deep takes longer. **Watchdog:** Scheduler → scan writes alerts → DynamoDB Stream → notify → Telegram; failures → DLQ → alarm email. **Data:** FAISS index on S3 (934 chunks from NVDA, AMD and INTC 10-K, 10-Q and earnings 8-K), DynamoDB for jobs, watchlist and feed. |
| 6–10 | The agents | DESIGN "Deep mode" | **Quick:** keyword routing (no LLM call), tools in parallel under one deadline, one Gemini call, `[Doc N]` citations. **Deep:** a Gemini planner picks specialists, which run in parallel and post to an evidence board (`[E1]`, `[E2]` with source and time). The writer may only cite `E#`. The fact-checker matches every number to the board in code, and only the misses go to one LLM call. Unsupported numbers get ⚠️, never kept silently. Simple questions take a one-specialist fast path. |
| 10–13 | Decisions | DESIGN "Key decisions" | **Gemini, not Bedrock (D1):** a ~$0/month target, and Bedrock has no free tier. One provider interface, so Bedrock is one adapter, not a redesign. **FAISS on S3, not OpenSearch (D6):** the index is a few MB, and OpenSearch's minimum is over budget. **SAM, one image for 4 functions. Least-privilege IAM per function**, checked live. |
| 13–15 | Results and limits | README "Results", DESIGN "Known limitations" | Deployed eval: 1% of numbers not traceable in both modes, p50 6.5 s Quick and 13.2 s Deep. Limits, said plainly: filings for 3 companies (others get a "not covered" notice), keyword routing misses open questions, and Deep sometimes leaves out the latest period's figures. |

---

## Part 2: live demo (20 min)

| Min | Where | Do | Expect (measured) | If it goes wrong |
|---|---|---|---|---|
| 0–2 | UI | Show the gate, the watchlist with live quotes, the alert feed. | Quotes with their source (`yfinance` or `finnhub`) and session date; feed newest first. | Quotes show `error`: Yahoo blocked, and Finnhub is the fallback; say so. |
| 2–5 | UI, Quick | "What is AMD's market cap and P/E ratio, and the latest headlines?" | 6–13 s, 1 Gemini call. Sources: a market quote and headlines, each cited `[Doc N]`. | — |
| 5–10 | UI, Deep | The brief's query: "Analyze NVIDIA's Q3 earnings report. Compare revenue growth with AMD and Intel. Provide any information on top companies that are using NVIDIA GPUs (and revenue from them), competitive threats to NVIDIA. Summarize recent news sentiment and provide a risk assessment based on current market conditions." While it runs, explain the planner. | 15–30 s, 10–16 calls. The trace shows the plan (market, news, filings), each specialist's steps and time, and the checker (e.g. 14/14 numbers supported). The answer says Q3 FY2027 isn't filed yet and uses Q2 FY2027, with the 16% customer concentration. | A Gemini 503 adds time (the next model answers). Over 75 s you get a partial answer with a notice. If Deep fails, it falls back to Quick and says so: show the notice, it's a designed path. |
| 10–13 | UI, Deep | "Compare NVIDIA's and AMD's data center segment revenue" | ~11 s in the deployed eval (Deep averages 4.7 calls). Both companies' segment figures, each cited `E#`. | — |
| 13–16 | PowerShell + Telegram + UI | Replay with the ticker you picked (`example_demo.md` 7.1, change `INTC` if needed). | Telegram within seconds: "📉 <TICKER> -7.5% on … (replay)", its "why", and "Demo replay: this move did not happen." Then the UI feed shows it first, `notified: true`. Run it again: 0 events, "Already alerted" (dedupe). | No Telegram message: check the feed's `notified`; if it's `false`, the DLQ and alarm path takes over (DESIGN). |
| 16–18 | Postman, CloudWatch | **A2** (no code → 401), then **Q1** + Poll job (202, then `done`). Copy `x-request-id` and find it in the API and worker logs (`example_demo.md` 6.1). | One request ID in both Lambdas' logs, from `POST /ask -> 202` to "Ask job … done". | Logs take a few seconds to appear. |
| 18–19 | UI, Quick | "What risks does Apple highlight in its SEC filings?" | ~8 s. The notice: "SEC filings for Apple Inc. (AAPL) aren't covered…", and no other company's filings cited. | — |
| 19–20 | — | Buffer, or show DESIGN's production improvements. | | |

**During the New York session** (20:00–02:30 IST in the US winter): real alerts can arrive in Telegram
during the demo; point them out. Replays still work. `fail_delivery` is refused in the session, and you can
show that refusal as a safety feature (`example_demo.md` 7.2).

**If everything fails:** switch to the recording, and walk through PROGRESS's I2–I5 deploy logs as evidence
that it ran.

---

## Part 3: Q&A (15 min), likely questions

| Question | Short answer | Where |
|---|---|---|
| Why not Bedrock, as the brief asks? | Cost: the target is ~$0/month and Bedrock has no free tier. All LLM calls go through one interface with a model fallback chain; Bedrock is one more adapter (Converse API with tool use). | DESIGN, D1 |
| How do you stop hallucinated numbers? | Evidence board plus a code fact-checker: every number in a Deep answer is matched to tool evidence; misses go to one LLM call (derived or unsupported), unsupported ones are flagged ⚠️. Quick must cite `[Doc N]` per number. Eval: 1% not traceable. | DESIGN, eval |
| How was it evaluated? | 20 questions × 2 modes, scored by frozen code (no LLM judge): traceable numbers, citations, routing, companies, latency, calls. Run locally and on AWS. | `docs/eval/v2-results.md`, D8 |
| Why async 202 + polling? | API Gateway's 30 s limit; Deep is budgeted at 75 s. Jobs live in DynamoDB with a 1-day TTL. | DESIGN, D7 |
| How does it scale? | Lambda scales per request; the index is in memory per container (ETag re-check every 5 min); DynamoDB is provisioned 2/2 now and would go on-demand. The limit today is the Gemini free tier, not AWS. | DESIGN |
| Security? | Access code on every route but `/health` (constant-time compare, fails closed); per-function IAM, each reads only its own SSM secrets; throttling 10 burst / 5 per s. Production: real users (Cognito or per-user keys). | DESIGN |
| Observability? | JSON logs with one request ID across Lambdas, EMF metrics (latency, Gemini calls, fallbacks, errors), 5 alarms emailing through SNS, a $1 budget alarm. | README, DESIGN |
| What would you do next? | DESIGN's production improvements: deterministic latest-period search, better routing, auto-ingest for new companies and filings, Finnhub news, narrower IAM for API and worker, X-Ray. | DESIGN |
| What was hardest? | The "Q3 isn't filed yet" case: the model dropped valid evidence because it wasn't "Q3". Two prompt rounds fixed the gap statement; the figures are still uneven, so the real fix is in code. | DESIGN, Known limitations |
| What does it cost? | Designed for ~$0/month on free tiers; not a measured bill. $1 budget alarm. | README "Results" |
