# ITERATIONS — Research Desk

The plan from 2026-11-07 on (D16). Older plans are in `docs/archive/` and should be ignored.

**Rules**
- Do the iterations in order, one at a time, on a branch `iter-N`.
- An iteration is done when its checks pass. Then merge to `main` and commit, so `main` is always submittable.
- The code already exists, so most tasks are **deploy + verify**. Tasks that need new code say **code**.
- After each iteration, add a few lines to `docs/PROGRESS.md`: what was done and the commands that proved it.
- The Reviewer runs after **I1** and **I3** only.
- 👤 marks a step only you can do.

---

## I0 — Unblock AWS
**Goal:** know which Region this account can use, and point everything at it.

| Task | Kind |
|---|---|
| Run read-only probes in candidate Regions. Try `ap-southeast-2` first, then the others from `aws account list-regions`. Probes: `cloudformation list-stacks`, `lambda list-functions`, `ecr describe-repositories`, `s3api list-buckets`, `dynamodb list-tables`, `ssm describe-parameters`. | verify |
| Switch the Region in `samconfig.toml`, `.env` / `.env.example` (`AWS_REGION`), README and AGENT.md. In SPEC, change "Region ap-south-1" to point to D17. Moto test Regions can stay. | code (config) |
| Write D17 in DECISIONS: the account was opened through AWS's "projects" sign-up and is limited to one Region; include the probe results. | docs |
| Merge `phase-B-aws` into `main` (all of its code is built and tested). | git |

**Checks:** all six probes succeed in the chosen Region. `grep -rn ap-south-1` finds it only in history (archive, reviews, DECISIONS, tests). `python -m pytest` passes. `sam validate --lint` passes.

**Submittable after I0:** the local app (CLI, agents, eval) and the AWS code, not yet deployed.

---

## I1 — Minimal deployed slice
**Goal:** `/health` and a Quick `/ask` work on AWS from Postman.

| Task | Kind |
|---|---|
| Add the SAM parameter `EnableWatchdog` (default `false`) and a Condition on the scan, notify, scheduler, stream mapping, DLQ and DLQ alarm resources. Add a template test for both values. | code |
| 👤 Put `APP_ACCESS_CODE` in `.env`. | 👤 |
| Deploy `infra/ecr.yaml`, then `sam build` → show the change set → 👤 approve → `sam deploy`. | deploy |
| `python -m scripts.bootstrap`: SSM secrets, create-only index upload, watchlist seed. | deploy |
| Run Postman / `newman` on the non-watchdog routes. | verify |
| Write a short README: what the project is, local setup, deploy commands, the API URL, and how to use Postman. | docs |

**Checks:**
- `curl $URL/health` returns `{"ok":true}`.
- A request without `x-access-code` gets 401.
- A Quick `/ask` returns `done` with sources.
- `newman` passes on the health, watchlist, quotes, news and Quick ask routes.
- The stack contains no scan or notify resources.

**Submittable after I1:** a deployed API answering Quick questions, plus a Postman collection and a README. **→ Reviewer.**

---

## I2 — Deep mode on AWS
**Goal:** the brief's example query answered in Deep mode on the deployed stack.

| Task | Kind |
|---|---|
| Deep `/ask` with the brief query; poll until done. | verify |
| Deployed eval on a subset: `python -m evals.run --target api --only s1 c1 c4 f3 w1` → Deployed section of `docs/eval/v2-results.md`. | verify |
| Cold start: `REPORT Init Duration` from 3 cold invokes per function. Record it in PROGRESS. | verify |

**Checks:** Deep returns `done` with all three specialists in the trace and `[E#]` citations. The eval subset is written to the results file. Cold start numbers are in PROGRESS.

**Submittable after I2:** the full ask path (Quick + Deep) deployed and measured.

---

## I3 — Watchdog
**Goal:** a ±3% move reaches Telegram once, with a "why".

| Task | Kind |
|---|---|
| 👤 Set `TELEGRAM_CHAT_ID` in `.env`, then re-run bootstrap for SSM. | 👤 |
| Deploy with `EnableWatchdog=true` (show the change set, 👤 approves). | deploy |
| Replay an INTC −7.5% move: invoke ScanFunction twice. | verify |
| Force a Telegram failure (bad token in a test invoke) and check that it reaches the DLQ and the alarm. | verify |

**Checks:** the two invokes produce one FEED item and one Telegram message. The forced failure lands in the DLQ and the alarm goes to ALARM. `GET /feed` shows the alert.

**Submittable after I3:** everything except the UI. **→ Reviewer.**

---

## I4 — Streamlit, logs and alarms, full deployed eval
**Goal:** a UI for the demo, and the final numbers.

| Task | Kind |
|---|---|
| `app.py` adapted from v1: API client only, with a Quick/Deep toggle and the trace. 👤 Deploy it on Streamlit Community Cloud. | code |
| Check that one `request_id` appears in the logs of both API and worker. Add the API 5xx alarm. | code + verify |
| Full deployed eval (all 20 questions) → Deployed section of `v2-results.md`. | verify |
| Review I3 findings 1–4, in **one image rebuild and one change set** (👤 approves the change set): **(1)** `aws_lambda/scan.py` refuses `fail_delivery` during the New York session (`in_session()`), with a unit test; optionally, Notify sends `fail_delivery` items straight to the DLQ. **(2)** Each function reads only the SSM secrets it needs: a per-function list of parameter ARNs (`ssm:GetParameters`) in the template, and `aws_lambda/runtime.py` loads only the names in a per-function env var. **(3)** Scan's `DynamoDBCrudPolicy` becomes an explicit statement (Query, GetItem, PutItem, ConditionCheckItem on `Table.Arn`), checked with the moto tests. **(4)** Notify's stream read is limited to `Table.StreamArn` (`ListStreams` stays on `*`). | code + deploy |

**Checks:** the Streamlit URL answers in both modes. `aws logs tail` shows one `request_id` across both functions. The 5xx alarm exists. The eval report covers all 20 questions. After the I3 fixes: a `fail_delivery` replay during the session is refused. A replay outside the session still reaches the DLQ and the alarm. Each function's role reads only its own SSM parameters, and IAM shows no `Resource: "*"` stream read.

**Submittable after I4:** a full demo with a UI.

---

## I5 — Docs and demo
**Goal:** submission-ready.

| Task | Kind |
|---|---|
| Architecture diagram image (`docs/architecture.png`), matching what is actually deployed. | docs |
| `docs/DESIGN.md`, 1–2 pages: design choices (D1–D17), challenges, and production improvements, including everything cut in D13. | docs |
| Final README: the diagram, setup, deploy, Postman and Streamlit links, eval summary and cost. | docs |
| Resume numbers, measured ones only (latency, cold start, eval scores, cost). | docs |
| Two timed rehearsals of the demo (15 + 20 + 15 min) and a recorded backup. | verify |

**Checks:** the README steps work on a fresh clone (clone → `.venv` → `pytest` → `cli.py market NVDA`). The demo runs end to end twice.

**Submittable after I5:** the final submission.

---

## Demo queries
1. **Deep:** the brief's NVIDIA/AMD/Intel query.
2. **Quick:** "What is AMD's market cap and P/E ratio, and the latest headlines?"
3. **Deep:** "Compare NVIDIA's and AMD's data center segment revenue".
4. **Watchdog:** INTC −7.5% replay → Telegram (if I3 is done).

## Never cut / cut order
- **Never cut:** I0, I1, I5.
- **If behind, cut in this order:**
  1. Streamlit (Postman covers the brief).
  2. The full deployed eval (keep I2's subset).
  3. The Watchdog (I3).
