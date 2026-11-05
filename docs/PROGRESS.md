# PROGRESS

Written by the Builder. One row per task; newest phase first.

## Phase 1 — Frozen eval scorer, v1 baseline, local core (branch `phase-1-core`, started 2026-11-05)

Note: Day 1 is 2026-11-05, not 2026-11-04 as PLAN.md assumed. The v1 rerun is dated 2026-11-05.

| Task | Status | What changed | How verified |
|---|---|---|---|
| 1.1 Frozen scorer | done | `core/numbers.py` = v1 `core/agents/checker.py` lines 24–106 (`UNVERIFIED_MARK` … `verify_numbers`, incl. `Verdict`), extracted with `sed`; `evals/scoring.py` = v1 `evals/run.py` lines 40–41 + 66–129, import changed to `from core import numbers as checker`. Fixture `tests/fixtures/v1_eval_2026-11-03.json` is a byte copy of v1 `evals/results/latest.json`. | `diff` of the ported range vs v1 = empty; `cmp` fixture = identical; v1's own `evals.run` and v2's `evals.scoring` produce identical JSON summaries on the fixture (scratch script, `PYTHONDONTWRITEBYTECODE=1`, v1 `git status` clean after). `pytest`: 16 passed (golden summaries both modes, import with `google`/`google.genai` blocked, stage-1 checker cases). |
| 1.0 Housekeeping | done | `.gitignore` extended (PLAN §0.2), stale `aws_artifacts/` line removed; `.env.example` (blank values + `STORE_BACKEND=local`, `VECTOR_BACKEND=local`); `requirements.txt` / `requirements-dev.txt` pinned (v1 pins, `pyyaml` added for template tests); `pytest.ini`; `.venv` on Python 3.12.10. Done after 1.1 at the user's request (scorer first). | `git check-ignore .env.local data/index/x.faiss data/cache/embeddings.sqlite local.db` lists all four; `.env.example` not ignored; `pip install -r requirements-dev.txt` OK; `pytest`: 16 passed. |

## Found while building
