# Research Desk

An AI investment research assistant. You ask a question about a stock
("What's AMD's P/E and what's in the news?", "What risks does NVIDIA list
in its 10-K?") and get a short answer where every number and claim cites
its source: a live quote, a news headline, or a passage from an SEC filing.

It has two modes:

- **Quick:** picks the tools the question needs, runs them in parallel, and
  makes one Gemini call. A few seconds when warm.
- **Deep:** a supervisor sends the question to three specialists (market,
  news, filings) in parallel. They post evidence to a shared board, and a
  fact-checker checks every number in the final answer against that evidence.

It runs on AWS (Lambda, API Gateway, DynamoDB, S3) and is built to cost
about $0/month: Gemini free tier, a small DynamoDB table, no always-on servers.

## How a Quick question works

1. **Request in.** `POST /ask` arrives through API Gateway at the API
   Lambda, which checks the `x-access-code` header and that the question is
   valid.
2. **Job created.** The API Lambda writes a job to DynamoDB with status
   `pending`.
3. **Worker started.** It starts the AskWorker Lambda in the background and
   immediately returns `202` with a `job_id`. It doesn't wait for the answer.
4. **Setup (cold start only).** The worker reads its API keys from SSM
   Parameter Store. Then it keeps them in memory for later requests.
5. **Tools run in parallel.**
   - Keyword routing decides which tools the question needs and which
     companies it mentions.
   - The tools are market data (yfinance, with Finnhub as a fallback), news,
     and SEC filings search over a 934-chunk vector index loaded from S3.
   - All the tools share one deadline, so a slow tool gets cut off instead of
     holding everything up.
6. **Gemini answers.** Every tool result is labelled `[Doc N]`. One Gemini
   call writes the answer and must cite a `[Doc N]` for every number, so the
   numbers come from the tools, not the model's memory.
7. **Result saved.** The worker writes the answer, its sources and timings to
   the job and marks it `done`.
8. **Poll.** The client calls `GET /ask/{job_id}` until the status is `done`.

**Why 202 and polling instead of just waiting?** API Gateway cuts off any
request after about 30 seconds. A cold-start Quick answer took around 31 s,
and Deep mode makes many Gemini calls and takes longer still. Running the
work in the background and polling for it means a request never hits that
limit, and the API stays fast.

## Run it locally

Requires Python 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env    # then fill in your keys
python -m pytest          # unit tests, fully mocked
python cli.py --help      # local commands: quotes, news, ingest, agent, scan...
```

The keys `.env` needs:

| Key | What it's for |
|---|---|
| `GEMINI_API_KEY` | Answers and embeddings (free tier) |
| `FINNHUB_API_KEY` | Market data fallback and news |
| `SEC_USER_AGENT` | Required by SEC EDGAR (a name and email) |
| `APP_ACCESS_CODE` | Shared secret for the API's `x-access-code` header |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Watchdog alerts |

<!-- TODO (Builder): add one exact `cli.py agent` example with its real arguments. -->

## Use the deployed API

The base URL is given with the submission. Every route except `/health`
needs the header `x-access-code: <code>`.

```text
GET  /health               -> {"ok": true}
POST /ask                  -> 202 {"job_id": ..., "poll": ...}
     body: {"question": "What is AMD's P/E?", "mode": "quick"}
GET  /ask/{job_id}         -> pending ... then done, with answer + sources
```

The Postman collection in `postman/` covers every route. To use it:

1. Import `postman/Research-Desk.postman_collection.json`.
2. Create an environment with `base_url` (the API URL) and `access_code`
   (set its type to **secret**).
3. Run **Health**, then **Ask (Quick)**, then **Poll Quick answer** until
   it shows `done`.

From the command line, run the same collection with newman:
`npx newman run postman/Research-Desk.postman_collection.json --env-var base_url=... --env-var access_code=...`

## Deploy to AWS

<!-- TODO (Builder): exact deploy steps — ECR stack, bootstrap secrets,
sam build, sam deploy (with the flags used in I1), bootstrap index/watchlist,
and the teardown list from PROGRESS.md. -->

## Docs

- [`docs/SPEC.md`](docs/SPEC.md): features, architecture, API, agents
- [`docs/DECISIONS.md`](docs/DECISIONS.md): trade-offs, including why Gemini
  instead of Bedrock
- [`docs/ITERATIONS.md`](docs/ITERATIONS.md): delivery plan (I0–I5)
- [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md): the original brief
- [`AGENT.md`](AGENT.md): Builder and Reviewer prompts

`evals/questions.json` is the 20-question eval set. v1 lives at
`../investment-research-assistant` as a read-only reference.

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