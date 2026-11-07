# Research Desk (v2)

Clean rebuild of the AI investment research assistant. Start here:

1. [`docs/SPEC.md`](docs/SPEC.md) — what to build (features, architecture, API, agents, gotchas)
2. [`docs/DECISIONS.md`](docs/DECISIONS.md) — trade-offs, including deviations from the brief
3. [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md) — the original brief
4. [`docs/ITERATIONS.md`](docs/ITERATIONS.md) — the delivery plan (I0–I5)
5. [`AGENT.md`](AGENT.md) — Builder and Reviewer prompts

v2's knowledge base comes from SEC EDGAR directly. `evals/questions.json` is the 20-question eval set.
v1 lives at `../investment-research-assistant` as a read-only reference.

## Repo map

| Path | What it is | Used in |
|---|---|---|
| `core/` | Agents, tools, LLM client, SEC ingest, vectors, store, market data, news, alerts | all |
| `aws_lambda/` | Lambda handlers: `api`, `ask_worker` (ask path); `scan`, `notify` (Watchdog) | I1–I3 |
| `jobs/` | Price-move scan logic, shared by `cli.py scan` and the scan Lambda | I3 |
| `cli.py` | Local CLI: `quotes`, `market`, `news`, `resolve`, `ingest`, `retriever`, `agent`, `scan` | I0–I5 |
| `scripts/` | `bootstrap.py`: SSM secrets, create-only index upload, watchlist seed | I1, I3 |
| `infra/` | `ecr.yaml`: the ECR repository stack, deployed before `sam deploy` (D15) | I1 |
| `template.yaml`, `samconfig.toml`, `Dockerfile` | SAM stack and the Lambda image (Region in `samconfig.toml`, D17) | I1–I4 |
| `postman/` | Postman collection for every API route (run with `newman`) | I1–I3 |
| `evals/` | 20-question eval set, runner and frozen scorer | I2, I4 |
| `tests/` | Unit tests (fully mocked; `python -m pytest`) | all |
| `.github/` | CI: pytest on every push | all |
| `docs/` | Spec, decisions, iterations, progress, eval results, reviews; `archive/` holds old plans | all |
| `data/` (git-ignored) | Local FAISS index (`index/`) and embedding cache (`cache/`) | I1 upload, local runs |
| `.cache/` (git-ignored) | SEC ticker map and the local SQLite store | local runs |
