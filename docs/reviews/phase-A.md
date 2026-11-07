# Review — Phase A (local core, knowledge base, agents, local eval)

Reviewer, 2026-11-05. Branch `phase-1-core` @ `7037b2d`. I read PLAN rev 3 Phase A, SPEC F1–F7/F14 and §6–§9,
DECISIONS D8–D14, PROGRESS, and every module in `core/`, `core/agents/`, `evals/` and `cli.py`. I ran the checks myself.
D13 scope cuts were not counted as deviations.

**Process note.** While I was reviewing, `cli.py ingest NVDA AMD INTC --focus NVDA` was running in this repo
(PID 33068, started 14:17 IST). The index grew from 389 to 736 rows during the review, and the quota file reads
**736/400** for Pacific day 2026-11-05. That means the run has `FILING_EMBED_DAILY_CAP` raised above the 400 in PROGRESS,
which says "Not changed without your OK". Because of this run I did not do a real ingest myself: a second writer would
race `data/index/index.npz`. AGENT.md asks for one session at a time.

## Verdict: **APPROVED WITH FIXES**

The phase delivers what it promised. The frozen scorer reproduces v1 exactly, the Finnhub fallback works, the KB has the
NVDA customer-concentration note, and the brief's Deep query runs with all three specialists and a clean fact-check.
There is one MUST-FIX: earnings 8-Ks show the SEC "report date" as the fiscal period, which puts a false statement in tool
output and in answers. It is small, and fixing it needs no re-embedding. Phase B may start once it's fixed. The KB
(A.10) and the Local eval (A.13) are still provisional, and PROGRESS says so.

## Acceptance

| Criterion | Result | Evidence |
|---|---|---|
| `pytest` passes, incl. golden scorer test | **pass** | `python -m pytest -q`: 245 passed, 0 failed. |
| `v1-baseline.md` exists, numbers equal PLAN §0.3 | **pass** | The 11-03 columns match: Quick 1/109, 75%, 51%, 4.7/29.5 s, 1.0; Deep 0/98, 95%, 90%, 23.4/49.3 s, 6.5, 1 follow-up, 2 fallbacks. The 11-05 rerun is reported beside it (D14). |
| yfinance mocked to fail → `source == "finnhub"`; `cli.py market QQQ` live | **pass** | `test_get_quote_falls_back_to_finnhub_when_yahoo_fails` and the per-ticker batch tests pass. Live QQQ: price 691.95, `trading_date 2026-11-04`, `source yfinance`. |
| `cli.py resolve "nonexistent co"` → friendly not found | **pass** | Prints "Not found: no US-listed company…", exit 1, no traceback. |
| Dry run shows NVDA concentration section; second real ingest `api_texts=0` | **pass / unverified** | `cli.py ingest NVDA --dry-run` shows `Note: Concentration of Revenue and Customers` in both the 10-K (7 chunks) and the 10-Q (4). It also shows NVDA at "not yet indexed 0, need embedding 0". I did not run the real second ingest because of the concurrent ingest. The unit test (`test_ingest.py`) and the Builder's scratch-index run (118 cache hits, 0 API texts) cover it. |
| `cli.py retriever "Compare NVIDIA and AMD data center revenue"` → ≥1 chunk each | **pass** | AMD 10-Q MD&A ×2, AMD 10-K MD&A, NVDA 10-Q MD&A ×2. |
| Deep brief query: 3 specialists + checker, numeric lines cited | **pass** | Live run, 38.0 s, 15 Gemini calls. Plan: filings, news, market for NVDA/AMD/INTC, plus 1 follow-up round. Checker: 12/12 supported. Spot-checked against the indexed SEC text: AMD Q3 $13.4B (8-K), INTC $16.5B +21% (8-K), NVDA $96.2B +106% (8-K), NVDA "one direct customer 22%" FY2026 (10-K note): all exact. Uncited: one heading "(Q3 2026)" (known D8 quirk) and a closing date footnote. |
| Planted fake number flagged ⚠️unverified | **pass** | `test_planted_fake_number_is_flagged_unverified`. |
| `v2-results.md` has its Local section | **pass (provisional)** | Present. Marked provisional: KB 389/934 chunks. |
| `git check-ignore` matches 4 paths | **pass** | All four printed. |

## Findings

### MUST-FIX

**M1. Earnings 8-Ks show the SEC event date as the "fiscal period".** This affects
[core/ingest.py:106](../../core/ingest.py#L106), [core/agents/tools.py:163](../../core/agents/tools.py#L163),
[core/ingest.py:317](../../core/ingest.py#L317) and [core/agent.py:88](../../core/agent.py#L88).
- **What's wrong:** `period` is taken from the submissions feed's `reportDate`. For an 8-K, that field is the date of the
  event, not a fiscal period end. So `latest_filing` tells the model "NVDA latest earnings release 8-K: fiscal period ended
  2026-08-26". The quarter actually ended 2026-07-26. AMD's 8-K becomes "period ended 2026-11-03". The same value goes
  into every 8-K chunk's `source`/`period` label, and from there into the Quick `[Doc N]` labels and the Deep evidence labels.
- **Evidence:** the eval's c6 Deep answer states "Earnings Release 8-K: Fiscal period ended 2026-11-03 [E3]". The dry run
  prints "NVDA 8-K … period 2026-08-26".
- **Why it matters:** the brief's query turns on "which quarter is NVIDIA's Q3", and both prompts tell the model to "state
  the filing period". A wrong period from a tool breaks the rule that facts come from tools and are correct. In an
  interview demo it is an easy catch.
- **Fix:** for 8-Ks, don't call `reportDate` a period. Store it as `event_date` and label it "event date" or "filed",
  or take the period from the same company's latest 10-Q/10-K period when the 8-K was filed after it. `chunk_hash` doesn't
  include `period`, so the existing 8-K rows' metadata can be rewritten in place, with **0 embedding calls**. Add a test
  that an 8-K never renders as "fiscal period ended".

### SHOULD-FIX

**S1. Internal error strings leak into user answers.** See [core/agents/specialists.py:114](../../core/agents/specialists.py#L114),
[core/agents/loop.py:68](../../core/agents/loop.py#L68) and [core/agents/supervisor.py:110](../../core/agents/supervisor.py#L110).
- **What's wrong:** a specialist that loses its model adds the gap `"Filings Analyst stopped early: model unavailable
  (gemini-3.8-flash: HTTP 503)"`. The writer gets this as a "NOT FOUND" finding.
- **Evidence:** the c6 Deep answer says "…the filings analyst model was unavailable (HTTP 503 ⚠️unverified) [Findings]".
  It shows a model name and HTTP code to the user, cites a fake `[Findings]` source, and adds 1 of Deep's 3 untraceable
  numbers in v2-results.
- **Fix:** keep the detail in `report.error` and the trace. Give the writer a neutral gap such as "Filings Analyst could
  not complete its search".

**S2. The Deep writer can cite metadata evidence for numbers that aren't there.** This is the c1 Deep result in `evals/results/v2-local.json`.
- **What's wrong:** the filings specialist called `latest_filing` for AMD but never ran `search_filings` for AMD. The writer
  then produced "AMD: $23,302 million … [E4]" and "$6,819 million … [E5]". E4 and E5 are only "AMD latest 10-K: fiscal
  period ended…" lines. Neither number appears anywhere in the evidence, so they came from model memory.
- **What went right:** the checker caught both and marked them ⚠️unverified, as F7 intends.
- **Why it still matters:** this is 2 of Deep's 3 untraceable numbers (3/90, against v1's 0/78 on the same day). SPEC §9's
  target is 0.
- **Fix (pick one or both):** when the plan has more than one company, make the filings instruction say "search each of
  NVDA, AMD, INTC". Have the checker also flag a number whose cited `[E#]` is a `latest_filing` or metadata item.
  Re-measure after the KB is complete.

**S3. The 75 s Deep budget doesn't bound tool execution.** See [core/agents/loop.py:82](../../core/agents/loop.py#L82)
and [core/agents/deep.py:72](../../core/agents/deep.py#L72).
- **What's wrong:** the deadline is checked only before each LLM call. Both `with ThreadPoolExecutor` blocks wait for every
  tool to return.
- **Worst case:** `market.get_quote` with Yahoo hanging and Finnhub slow costs about 2×10 s (Yahoo) + 2 s, then up to
  3 Finnhub endpoints × (8+2+8) s. That's about 75 s for one tool call. Yahoo's `fast_info` and `.info` calls pass no
  timeout.
- **Why it matters:** on Lambda (Phase B) Yahoo is often blocked, so this path gets exercised. The worker could run past
  the budget, and in the worst case past its Lambda timeout.
- **Fix:** `future.result(timeout=deadline - now)` per tool, returning `{"error": "timed out"}` (as Quick already does at
  [core/agent.py](../../core/agent.py) `answer()`), plus `shutdown(wait=False)`. In `_finnhub_detail`, skip
  profile/metric when little time is left.

**S4. The local eval and the demo don't run on the chain PROGRESS records.**
- **What's wrong:** `.env` sets `GEMINI_MODEL=gemini-3.8-flash`, which is v1's primary and returned 503/504 all day on
  11-05. It overrides the verified default at [core/llm.py:29](../../core/llm.py#L29). The v2-results header shows that
  chain, and my Deep run's first call was a 3.8-flash 503.
- **Why it matters:** it matches the v1 rerun (arguably fair), but it isn't recorded as a choice. It also adds a failed
  first call and a cooldown to every cold process, including the demo.
- **Fix:** decide which chain to use. Either clear `GEMINI_MODEL` in `.env`, or record "v2 local eval uses v1's chain
  for parity" in DECISIONS. Put the chain actually used in PROGRESS A.4/A.13.

**S5. The live fact-checker misses capitalised scale words.** See [core/agents/checker.py:43](../../core/agents/checker.py#L43).
- **What's wrong:** frozen stage 1 parses "$4,832.5 Billion" as 4,832.5, but parses "billion" correctly. The value really
  is in the evidence ("market cap $4,832.5B").
- **Evidence:** Quick c5's 2 untraceable numbers (`$4,832.5`, `$309.7`, from a markdown table with "Billion") are this
  quirk, not errors. In Deep the same text would be marked ⚠️unverified unless stage 2 rescues it.
- **Fix:** keep `core/numbers.py` frozen (D8). In `checker.check` (not frozen), match on `answer.lower()`, which has the
  same offsets, so the flags still land in the right place. Note the scorer quirk under D8 in v2-results.

**S6. Finish the KB before Phase B uploads the index, and re-run the filing questions.**
- **What's wrong:** A.10 is still in progress (736/934 rows now). The Local section was scored on 389 chunks, and f2
  (Intel risks) fell back to Quick with "no evidence". B.4's upload is create-only, so an early upload needs `--replace`
  later.
- **Fix:** update PROGRESS A.10 with the final counts and the cap actually used (see the process note). Re-run at least
  the f-questions and c4/c6 on the full KB before calling A.13 done.

**S7. Stale filings stay in the index when a new quarter is filed.**
- **What's wrong:** `chunk_hash` includes the accession ([core/ingest.py:303](../../core/ingest.py#L303)) and ingest only
  appends, so after PLAN D.6's "`cli.py ingest NVDA` for a new quarter" the old 10-Q and 8-K chunks remain. Retrieval can
  then mix two quarters, while `latest_filing` reports only the new one.
- **Fix:** before D.6, drop the rows of a (ticker, form) whose accession is no longer the latest. Their vectors stay in the
  embedding cache, so this costs nothing.

### NIT

- [core/vectors.py:189](../../core/vectors.py#L189): `S3Index.cache_file` is never used. The "/tmp warm copy" is really an
  in-memory copy per container, which is fine, but the docstring and SPEC's "/tmp" wording should match.
  `head_object` at line 217 isn't wrapped in `VectorIndexError` (it matters in Phase B).
- [core/agents/tools.py:165](../../core/agents/tools.py#L165): the `latest_filing` source label `"SEC 10-K 2026-02-04"`
  has no ticker, so in the UI AMD's and NVDA's entries look the same.
- [core/market.py:256](../../core/market.py#L256): the Yahoo `get_quote` path returns unrounded floats (`691.9500122070312`).
  The Finnhub path and `get_quotes` round to 2 decimal places.
- [core/ingest.py](../../core/ingest.py) `NOTE_PATTERNS`: `customer\s+[A-Z]\b` runs under `re.I`, so `[A-Z]` matches any
  letter.
- `deep.answer_deep` `used_tools` lists round-1 agents only. Follow-up agents appear only in `trace.rounds`. That's fine
  for the frozen scorer, but say so in the UI trace.

## What's good

- The scorer port was done carefully: a byte-diff against v1, a golden test that blocks `google.genai`, and both scorers
  agreeing byte for byte on both runs. The v1→v2 comparison can be defended.
- The ingest design meets D11 well. Chunks are hashed, the embedding cache is keyed by model and dim, the index header
  check refuses a mismatch, the index is saved per batch, and the priority order put demo-critical text first on a capped
  day. The section extraction handles Intel's layout and skips TOC entries.
- Failure paths are tested against real code, not only mocks: malformed plan, writer timeout, budget-partial, the request
  ID across threads, and the `run_in_context` regression. The `[Doc N]` change lifted Quick's cited lines from 44% to 83%.

## Out of scope ideas

- `cli.py models`, to list the available Gemini models (already in PLAN §7).
- A per-company coverage check in the eval report: "every company in the plan has ≥1 filing passage".
