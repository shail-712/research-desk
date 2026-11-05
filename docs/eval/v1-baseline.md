# v1 baseline: saved 2026-11-03 run vs same-config rerun on 2026-11-05

> **What this is.** It's the baseline v2 is measured against (PLAN task A.2, DECISIONS D8/D9/D14).
> - v1 (`investment-research-assistant` @ `73b6fda`) was re-run with `python -m evals.run --fresh` on **2026-11-05**. The run used a copy of v1 outside both repos, with v1's own `.venv` and v1's `.env` unchanged apart from adding `SEC_USER_AGENT`.
> - It answered 20 questions from `evals/questions.json` in both modes, back to back.
> - Both runs are scored by the frozen scorer, `evals/scoring.py`. It is a verbatim port of v1's scoring (D8).
> - Every metric is computed by code. There is no LLM judge.
> - Raw answers are in [`v1-rerun-2026-11-05.json`](v1-rerun-2026-11-05.json).
>
> PLAN.md called the rerun date 2026-11-04. The build actually started on 2026-11-05, so the file is named for the real date.

## Summary

| Metric | Quick 11-03 | Quick **11-05** | Deep 11-03 | Deep **11-05** |
|---|---|---|---|---|
| Answered | 20/20 | 20/20 | 20/20 | 20/20 |
| **Numbers not traceable to evidence** (lower is better) | 1/109 (1%) | **3/105 (3%)** | 0/98 (0%) | **0/78 (0%)** |
| Answers where every number traces | 95% | 95% | 100% | 100% |
| Right companies found | 100% | 100% | 100% | 100% |
| Right tools/agents used (routing) | 75% | **80%** | 95% | **100%** |
| Lines with numbers that carry a citation | 51% | **44%** | 90% | **88%** |
| Latency p50 | 4.7 s | **3.6 s** | 23.4 s | **16.4 s** |
| Latency p90 | 29.5 s | **14.0 s** | 49.3 s | **47.1 s** |
| Gemini calls per question (mean, includes failed attempts) | 1.0 | 1.2 | 6.5 | 8.3 |
| Follow-up rounds used | — | — | 1 | 5 |
| Numbers flagged ⚠️unverified by the fact-checker | — | — | 0 | 0 |
| Fell back to Quick | — | — | 2 | 1 |

**Reproducibility check.** Each results file was scored twice: once with v1's own `evals/run.py` (read-only) and once with v2's `evals/scoring.py`. The two produced byte-identical JSON summaries for both runs. The 11-03 summary also matches SPEC §9 exactly, and `tests/unit/test_eval_scoring.py` pins that summary.

**What v2 must beat or match (same-day comparison, D9).** These are the 11-05 numbers above, and the 11-03 numbers are kept for reference. Each future v2 run should be compared against a v1 run from the same day where possible. Prices, news and Gemini load change from day to day, as the swings below show.

## By question type

| Type | Mode | Untraceable 11-03 → 11-05 | Routing | Citations | p50 |
|---|---|---|---|---|---|
| single (6) | quick | 1/18 (6%) → 0/20 (0%) | 100% → 100% | 0% → 3% | 20.1 s → 5.8 s |
| single (6) | deep | 0/30 (0%) → 0/23 (0%) | 100% → 100% | 87% → 96% | 30.0 s → 7.4 s |
| comparison (6) | quick | 0/45 (0%) → 3/62 (5%) | 83% → 100% | 49% → 48% | 3.7 s → 3.4 s |
| comparison (6) | deep | 0/34 (0%) → 0/33 (0%) | 83% → 100% | 85% → 81% | 32.2 s → 30.7 s |
| why (4) | quick | 0/26 (0%) → 0/14 (0%) | 0% → 0% | 28% → 27% | 4.2 s → 4.5 s |
| why (4) | deep | 0/24 (0%) → 0/20 (0%) | 100% → 100% | 96% → 92% | 27.1 s → 29.5 s |
| filing (4) | quick | 0/20 (0%) → 0/9 (0%) | 100% → 100% | 100% → 90% | 3.2 s → 2.7 s |
| filing (4) | deep | 0/10 (0%) → 0/2 (0%) | 100% → 100% | 96% → 83% | 9.3 s → 19.6 s |

**Observations.**
- **Quick still can't route "why" questions.** It scores 0% on both days, because keyword routing never sends them to news and filings together. This is the gap Deep mode closes (D5).
- **Quick's 3 untraceable numbers are all in c4**, the data-center segment comparison: `64%`, `$3.674`, `64%`. These look like derived or recalled figures. The 3 PDFs in the KB have mismatched quarters and no segment note for both companies (corpus caveat 1 below).
- **Deep filing answers contain very few numbers on 11-05** (2 numbers across 4 questions, against 10 on 11-03). The KB didn't change, so this is answer variance from running mostly on different models (see "Models"). Deep's citation rate on filings fell from 96% to 83% for the same reason.
- **The Deep fallback on 11-05** was c1. The reason recorded in the trace is `writer unavailable: gemini-flash-lite-latest: HTTP 400`, which happened after the primary model was overloaded.

## Models: configured vs actually used

| | Value |
|---|---|
| Configured chain (v1 `.env` + v1 defaults, unchanged) | `gemini-3.8-flash → gemini-3.1-flash-lite → gemini-flash-lite-latest → gemini-3.5-flash` |
| Model availability check (`models.list`, 2026-11-05) | All four listed. No 404, so **no substitution** was made (PLAN 1.2 step 4 not triggered). |
| Successful Gemini responses by model (from the run log) | `gemini-3.1-flash-lite` **106** · `gemini-flash-lite-latest` **29** · `gemini-3.5-flash` **15** · `gemini-3.8-flash` **0** |
| Failed attempts by model | `gemini-3.8-flash`: 9 (5 × 503, 4 × 504; overloaded all run, then cooled down for 60 s each time) · `gemini-3.1-flash-lite`: 23 × 429, 2 × 504, 1 × 400 · `gemini-flash-lite-latest`: 5 × 503, 1 × 400 · `gemini-3.5-flash`: 1 × 503 |

The **configured primary never answered on 11-05**, so every 11-05 answer came from v1's fallback models. The saved 11-03 run doesn't record which model produced each answer, so per-model comparisons across the two days aren't possible. Part of the 11-03 → 11-05 difference may be the model, not the code. The run log covers whole Gemini responses, not answers: one Deep answer makes several calls. That's why the counts are per response.

## Run conditions

| | |
|---|---|
| Code | v1 @ `73b6fda`. Copied with `robocopy … /E /XD .git .pytest_cache .venv __pycache__` to `C:\Shail\Projects\_v1-eval-run` and run there with v1's `.venv` interpreter. Deleted after the run, because it held the API key. |
| Config | v1's `.env` unchanged, plus `SEC_USER_AGENT` (**on**). Store: local SQLite. Vectors: local FAISS. |
| Knowledge base | v1's local FAISS index over the **three old 10-Q PDFs** in `data/raw/`: AMD Q1 2025, INTC Q3 2025 and NVDA Q3 FY2026. That's 1,491 chunks, with no 10-K and no form, URL or section metadata. |
| Started / finished | 2026-11-05 13:29 / ~13:40 IST (the US market was closed, so prices are the 2026-11-04 session). |
| Answers in results file | 40/40, all timestamped 2026-11-05. No errors recorded. |

## Comparability caveats (D9)

1. **Different corpus.** v2 will index the latest 10-K, 10-Q and earnings 8-K from SEC, with section metadata. Differences on filing questions are partly the corpus, not only the code. This matters especially for c4 and f1–f4, and for the brief's GPU-customer question, which needs 10-K customer-concentration text that v1 doesn't have.
2. **Live data and model load change daily.** Compare v1 and v2 runs from the same day. If a same-day v1 rerun isn't affordable on quota, compare against this 11-05 run and state the date gap.
3. **Derived numbers count as untraceable** in both modes and in both versions. This is a known quirk of the frozen scorer (D8).
4. **"Calls per question" counts attempts, failed ones included** (`llm.calls_made()`). On an overloaded day it rises without any change in the code.
