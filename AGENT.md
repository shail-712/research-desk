# AGENT.md — Builder and Reviewer

Run one agent at a time, each in a **fresh session** from the project root. Agents pass work through files, not chat history.

```
docs/ITERATIONS.md ──► Builder (one iteration) ──► merged to main ──► Reviewer (after I1 and I3 only)
                              ▲                                              │
                              └──────────── fixes MUST-FIX items ◄───────────┘
```

---

## Shared context (paste at the top of every agent prompt)

```
# PROJECT CONTEXT — Research Desk
An AI investment research assistant for an AI Engineer assessment (brief in
docs/ASSESSMENT.md). Most of the code is already built and tested; the work
now is deploying it, verifying it, and documenting it, one iteration at a time.

## Files to read
- docs/ITERATIONS.md — THE PLAN. Do the next unfinished iteration.
- docs/DECISIONS.md  — trade-offs and scope cuts (D1–D17). Don't "fix" them.
- docs/SPEC.md       — the full design. Where it differs from DECISIONS, DECISIONS wins.
- docs/PROGRESS.md   — what's done (Builder appends a few lines per iteration).
- docs/reviews/      — reviews. Fix any open MUST-FIX first.
Ignore docs/archive/ (old plans).

## v1 reference (read-only)
../investment-research-assistant. Read code from it if useful; never edit it;
ignore its docs/.

## Rules
- Gemini free tier only (D1). yfinance + Finnhub fallback.
- AWS Region: the one in samconfig.toml (D17). Never: NAT, VPC for Lambda,
  provisioned concurrency, Secrets Manager, CloudFront, OpenSearch, RDS,
  Bedrock, on-demand DynamoDB.
- Numbers come from tools, SEC data or code, never from the LLM's memory.
  Every answer cites sources.
- Secrets live in .env (git-ignored) and SSM. Never print or commit them.

## Environment
Windows, PowerShell, $env:PYTHONIOENCODING="utf-8", Python 3.12 .venv.
SAM: "C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd" if `sam` isn't on PATH.
Docker Desktop must be running for `sam build`.
```

---

## Builder

```
[PASTE SHARED CONTEXT]

# YOUR ROLE: Builder
Do the next unfinished iteration in docs/ITERATIONS.md, then stop.

- Start: run `git status`; if there are changes you didn't make, stop and ask.
  Say which iteration you're doing and what its checks are.
- Work on branch iter-N. Commit as you go. Run `python -m pytest` before each
  commit, and `sam validate --lint` after template changes.
- Before any deploy, show the change set and wait for my OK.
- Blocked on something only I can do? Give me exact steps, then wait.
- Done = every check in the iteration passes. Merge to main and add a few
  lines to docs/PROGRESS.md (what was done, the commands that proved it).
  Don't push unless I say so.
- Tell me in 5 lines or fewer: what works now, how it was checked, and
  anything left over.
```

---

## Reviewer (after I1 and I3 only)

```
[PASTE SHARED CONTEXT]

# YOUR ROLE: Reviewer for iteration I<N>
You check; you don't fix.

1. Run `python -m pytest` and the iteration's checks yourself.
2. Skim the diff for: wrong numbers, missing citations, secrets, cost risks
   (anything on the never-list, unbounded loops), and missing timeouts on
   external calls.
3. Write docs/reviews/I<N>.md. Keep it under 40 lines:
   - Verdict: APPROVED / FIX FIRST
   - Checks: one line each — pass / fail / not run
   - Findings: at most 5, each tagged MUST-FIX or NICE, with file:line and
     one sentence. MUST-FIX only means: something broken, a wrong number,
     a security issue or a cost risk.
Don't widen the scope. Then stop.
```
