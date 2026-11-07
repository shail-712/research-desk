# Research Desk (v2)

Clean rebuild of the AI investment research assistant. Start here:

1. [`docs/SPEC.md`](docs/SPEC.md) — what to build (features, architecture, API, agents, gotchas)
2. [`docs/DECISIONS.md`](docs/DECISIONS.md) — trade-offs, including deviations from the brief
3. [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md) — the original brief
4. [`AGENT.md`](AGENT.md) — Planner → Builder → Reviewer prompts

`data/raw/` (local only, git-ignored, not used by v2) holds v1's three old 10-Q PDFs (AMD Q1 2025, INTC Q3 2025,
NVDA Q3 FY2026). v2's knowledge base comes from SEC EDGAR directly. `evals/questions.json` is the 20-question eval set.
v1 lives at `../investment-research-assistant` as a read-only reference.
