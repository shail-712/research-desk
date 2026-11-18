# 02 · Tech Stack

Reasons marked "(likely)" are my best guess. Everything else is written down in `docs/DECISIONS.md` (D1–D18) or in code comments.

## Frontend

| Technology | What it's used for | Why it was chosen | Alternative I could mention |
|---|---|---|---|
| **Streamlit** 1.66 (`ui/app.py`) | Web UI with 4 tabs: Ask, Watchlist, Alerts, How it works | Pure Python, so no separate JS codebase (likely). Free hosting on Streamlit Community Cloud (D4) | React + Next.js, or Gradio |
| **Streamlit Community Cloud** | Hosts the UI for free | $0 cost, deploys straight from the GitHub repo | Vercel / Netlify (if React), or S3 + CloudFront |
| **requests** (`ui/client.py`) | The UI talks to the API over HTTP only | Keeps the UI tiny: it installs only `streamlit` + `requests` (`ui/requirements.txt`) | `httpx` |
| **Postman / newman** (`postman/`) | API demo collections, run from the command line with `newman` | The brief asked for "a simple frontend or Postman collection" | Bruno, curl scripts |

## Backend

| Technology | What it's used for | Why it was chosen | Alternative I could mention |
|---|---|---|---|
| **Python 3.12** | All backend code | Required by the brief | — |
| **AWS Lambda** (4 functions, one container image) | `ApiFunction`, `AskWorkerFunction`, `ScanFunction`, `NotifyFunction` | No servers to run, pay per request, free tier, so $0 when idle | ECS Fargate, or one EC2 server |
| **Lambda container image** (`Dockerfile`, ECR) | Packages the code for all 4 functions | numpy + pandas + faiss + google-genai are bigger than Lambda's 250 MB zip limit | Lambda layers, or trimming dependencies |
| **API Gateway HTTP API** | Public HTTPS endpoint, throttled to burst 10, 5 requests/s | Cheaper than a REST API. Throttling protects the free API quotas | API Gateway REST API, or a Lambda Function URL |
| Small regex router (`aws_lambda/api.py`, `ROUTES`) | Maps method + path to a handler function | No framework needed for 9 routes (likely) | FastAPI + Mangum, or AWS Lambda Powertools |

## Data

| Technology | What it's used for | Why it was chosen | Alternative I could mention |
|---|---|---|---|
| **DynamoDB** (one table, provisioned 2 read / 2 write) | Watchlist, alert feed, dedupe markers, small state, async jobs | Always-free tier covers *provisioned* capacity (on-demand isn't free). Supports TTL, Streams and transactions | PostgreSQL (RDS), or on-demand DynamoDB |
| **DynamoDB Streams** | Triggers `NotifyFunction` when a new alert is written | Separates writing an alert from sending it, so a Telegram outage can't break the scan | SQS queue, or EventBridge Pipes |
| **FAISS** (`faiss-cpu`) + **S3** | Vector search over SEC filing chunks, stored as one `index.npz` file | The index is a few MB. OpenSearch Serverless has a monthly minimum far above budget (D6) | OpenSearch, Bedrock Knowledge Bases, pgvector, Pinecone |
| **SQLite** (`core/store.py` `LocalStore`) | The same store interface for local runs and tests | The app and tests run on a laptop without AWS (D4) | Docker DynamoDB Local |

## AI

| Technology | What it's used for | Why it was chosen | Alternative I could mention |
|---|---|---|---|
| **Google Gemini** (`google-genai`, `core/llm.py`) | Writing answers, Deep-mode planning, function calling, fact-check stage 2, the "why" pick | Free tier, so the ~$0/month target holds. Bedrock has no always-free tier (D1) | **Amazon Bedrock** (the brief asked for it), OpenAI, Claude API |
| Model fallback chain | Tries `gemini-3.5-flash-lite`, then 3 other models, on 429 / 5xx / timeout | Free-tier models often return 503 under load | One model with retries |
| **gemini-embedding-001** (768 dims) | Embeddings for filings and search queries | Same provider and free (likely). Cached by content hash, so each chunk is paid for once (D11) | Amazon Titan Embeddings, OpenAI `text-embedding-3-small` |
| Gemini **function calling** + JSON schema output | Specialists call tools in a loop. The supervisor returns plans as strict JSON | Reliable parsing, and every tool step is visible in the trace | LangChain / LangGraph, Bedrock Agents |

## Auth & security

| Technology | What it's used for | Why it was chosen | Alternative I could mention |
|---|---|---|---|
| Shared access code (`x-access-code` header) | Guards every route except `/health` | Simple enough for a single-user demo | Amazon Cognito, API keys per user, OAuth |
| **SSM Parameter Store** (SecureString) | Stores API keys and tokens. Each function reads only its own, once per cold start | Free. Secrets Manager costs money per secret | AWS Secrets Manager |
| **IAM** least privilege | Each function's role allows only what it needs | A leaked key in one function can't reach another function's secrets | — |

## Hosting, deployment & ops

| Technology | What it's used for | Why it was chosen | Alternative I could mention |
|---|---|---|---|
| **AWS SAM** (`template.yaml`, `samconfig.toml`) | Infrastructure as Code: the full stack in one template | The brief allowed Terraform/CDK/SAM. SAM is the simplest for Lambda-heavy apps (likely) | Terraform, AWS CDK |
| **ECR** (`infra/ecr.yaml`, its own stack) | Stores the Docker image, keeps the 2 newest | SAM pushes the image before the main stack exists (D15) | SAM's managed repository |
| **EventBridge Scheduler** | Runs the scan every 15 min, Mon–Fri, New York time | Has a time zone setting, so daylight saving needs no second cron | EventBridge rules (UTC only), cron on a server |
| **SQS** dead-letter queue | Holds alerts that failed 3 retries | Failed alerts aren't lost, and they trigger an alarm | — |
| **CloudWatch** logs, EMF metrics, 5 alarms → **SNS** email | Observability | EMF (Embedded Metric Format) turns a printed JSON line into a metric with no extra API call | Datadog, X-Ray (cut for time) |
| **AWS Budgets** ($1) | Emails if the bill goes above $1 | Guards the $0 target | — |
| **GitHub Actions** (`.github/workflows/ci.yml`) | Runs `pytest` on every push | Free CI | GitLab CI |

## Key libraries & external APIs

| Technology | What it's used for | Why it was chosen | Alternative I could mention |
|---|---|---|---|
| **yfinance** | Primary quotes (one batched download) | Free, no key | Alpha Vantage (only ~25 requests/day free) |
| **Finnhub** API | Quote fallback | Yahoo often blocks AWS IP addresses. Finnhub's free tier gives ~60 calls/min (D2) | Polygon.io, IEX |
| **Yahoo Finance RSS** + `feedparser` | News headlines per ticker | Free, no key | NewsAPI, Finnhub news |
| **SEC EDGAR** (`data.sec.gov`, `company_tickers.json`) | Filings to index, and name → ticker → CIK lookup | Official and free. Needs a `SEC_USER_AGENT` header | — |
| **Telegram Bot API** | Push alerts to a phone | Free, no card needed (`core/telegram.py`) | SNS SMS, Slack webhook, SES email |
| **numpy / pandas** | Vector math, and yfinance's data frames | Standard | — |
| **pytest + moto** | 442 unit tests, fully mocked. `moto` fakes AWS in memory | Tests need no network and no secrets | LocalStack |
