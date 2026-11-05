"""
API Gateway HTTP API (payload v2) -> one Lambda with a small router (SPEC §5).

    GET    /health                      no auth -> {"ok": true}
    GET    /watchlist
    POST   /watchlist                   {"ticker" or "name", "pct_move"?, "price_above"?, "price_below"?}
    DELETE /watchlist/{ticker}
    GET    /quotes[?tickers=NVDA,AMD]   default: the watchlist (one batched call), each with `source`
    GET    /news/{ticker}
    GET    /feed[?limit=20]
    POST   /ask                         {"question", "mode": "quick" | "deep", "include_context"?: false}
                                        -> 202 {"job_id", "status", "poll"}
    GET    /ask/{job_id}                -> {"status": "pending" | "done" | "error", "answer", "sources",
                                            "trace", "timings", ...}; "context" only with include_context

Why /ask is asynchronous: API Gateway gives an integration at most 30 s and a Deep answer takes
20-75 s. The API records a job in DynamoDB, invokes the worker Lambda asynchronously, and the
client polls.

Auth: header `x-access-code` must equal APP_ACCESS_CODE (from SSM; constant-time compare). If the
code isn't configured the API refuses everything but /health (fails closed). Errors are JSON
`{"error": "<safe message>"}`, never stack traces. The request ID (API Gateway's) is logged on
every line, returned in `x-request-id` and passed to the worker.

Adapted from v1 aws_lambda/api.py (73b6fda): `{"ticker" or "name"}` (was `{"query"}`),
`include_context` (D10), `{"ok": true}`, always fail closed, request IDs, flat job results.
"""

import base64
import hmac
import json
import logging
import os
import re

from aws_lambda.runtime import bootstrap

bootstrap()

from core import log, market, news, tickers  # noqa: E402  (after bootstrap: secrets + /tmp paths)
from core.store import DEFAULT_PCT_MOVE, StoreError, get_store  # noqa: E402

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 1000
MAX_QUOTE_TICKERS = 20
MODES = ("quick", "deep")
TICKER = r"[A-Za-z.\-]{1,10}"
_TICKER_RE = re.compile(rf"^{TICKER}$")
_lambda_client = None


class BadRequest(ValueError):
    """A client error whose message is safe to return."""


def _response(status: int, body) -> dict:
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json", "x-request-id": log.get_request_id()},
            "body": json.dumps(body, default=str)}


def _error(status: int, message: str) -> dict:
    return _response(status, {"error": message})


def _body(event) -> dict:
    raw = event.get("body") or "{}"
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise BadRequest("Body must be valid JSON") from e
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object")
    return data


def _authorized(event) -> bool:
    expected = os.environ.get("APP_ACCESS_CODE", "")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    given = headers.get("x-access-code") or ""
    return hmac.compare_digest(given.encode(), expected.encode())


def _optional_number(data: dict, key: str, low: float, high: float):
    value = data.get(key)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise BadRequest(f"'{key}' must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise BadRequest(f"'{key}' must be a number") from e
    if not low < number <= high:
        raise BadRequest(f"'{key}' must be greater than {low:g} and at most {high:g}")
    return number


def _invoke_worker(payload: dict) -> None:
    global _lambda_client
    import boto3

    _lambda_client = _lambda_client or boto3.client("lambda")
    _lambda_client.invoke(FunctionName=os.environ["ASK_WORKER_FUNCTION"], InvocationType="Event",
                          Payload=json.dumps(payload).encode())


# ── routes ───────────────────────────────────────────────────────────────────

def get_watchlist(event, params):
    return _response(200, {"watchlist": get_store().list_watchlist()})


def post_watchlist(event, params):
    data = _body(event)
    ticker, name = str(data.get("ticker") or "").strip(), str(data.get("name") or "").strip()
    if not ticker and not name:
        raise BadRequest("Send 'ticker' (e.g. AAPL) or 'name' (e.g. Apple)")
    if ticker and not _TICKER_RE.match(ticker.lstrip("$")):
        raise BadRequest("'ticker' must be 1-10 letters, '.' or '-'")
    if len(name) > 100:
        raise BadRequest("'name' is limited to 100 characters")
    pct = _optional_number(data, "pct_move", 0, 50) or DEFAULT_PCT_MOVE
    above = _optional_number(data, "price_above", 0, 1e7)
    below = _optional_number(data, "price_below", 0, 1e7)
    company = tickers.resolve_or_ticker(ticker) if ticker else tickers.resolve(name)
    if company is None:
        return _error(404, f"No US-listed company found for '{(ticker or name)[:60]}'")
    quote = market.get_quotes([company.ticker]).get(company.ticker, {})
    if quote.get("error"):
        return _error(422, f"{company.ticker}: no market data, so it can't be watched")
    store = get_store()
    store.add_ticker(company.ticker, company.name, company.cik, pct, above, below)
    row = next((w for w in store.list_watchlist() if w["ticker"] == company.ticker), None)
    return _response(201, {"added": row or company.to_dict()})


def delete_watchlist(event, params):
    ticker = market.normalise_ticker(params["ticker"])
    store = get_store()
    if not any(w["ticker"] == ticker for w in store.list_watchlist()):
        return _error(404, f"{ticker} is not on the watchlist")
    store.remove_ticker(ticker)
    return _response(200, {"removed": ticker})


def get_quotes(event, params):
    query = (event.get("queryStringParameters") or {}).get("tickers")
    if query:
        symbols = [t.strip() for t in query.split(",") if t.strip()]
        bad = [t for t in symbols if not _TICKER_RE.match(t)]
        if bad:
            raise BadRequest(f"Invalid ticker: {bad[0][:12]!r}")
        if len(symbols) > MAX_QUOTE_TICKERS:
            raise BadRequest(f"At most {MAX_QUOTE_TICKERS} tickers per request")
    else:
        symbols = [w["ticker"] for w in get_store().list_watchlist()]
    return _response(200, {"quotes": market.get_quotes(symbols) if symbols else {}})


def get_news(event, params):
    ticker = market.normalise_ticker(params["ticker"])
    company = tickers.lookup_ticker(ticker)
    articles = news.get_news(ticker, company.name if company else None, 5)
    return _response(200, {"ticker": ticker, "articles": articles})


def get_feed(event, params):
    try:
        limit = max(1, min(int((event.get("queryStringParameters") or {}).get("limit", 20)), 50))
    except ValueError:
        limit = 20
    return _response(200, {"events": get_store().recent_events(limit)})


def post_ask(event, params):
    data = _body(event)
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise BadRequest("Missing 'question'")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise BadRequest(f"Questions are limited to {MAX_QUESTION_CHARS} characters")
    mode = str(data.get("mode") or "quick").lower()
    if mode not in MODES:
        raise BadRequest(f"'mode' must be one of {', '.join(MODES)}")
    include_context = data.get("include_context", False)
    if not isinstance(include_context, bool):
        raise BadRequest("'include_context' must be true or false")
    store = get_store()
    job_id = store.create_job(question, mode)
    try:
        _invoke_worker({"job_id": job_id, "question": question, "mode": mode,
                        "include_context": include_context, "request_id": log.get_request_id()})
    except Exception as e:
        logger.error("Could not start the ask worker: %s", type(e).__name__)
        store.finish_job(job_id, "error", error="Couldn't start the answer. Please try again.")
        return _error(503, "Couldn't start the answer. Please try again.")
    return _response(202, {"job_id": job_id, "status": "pending", "poll": f"/ask/{job_id}"})


def get_ask(event, params):
    job = get_store().get_job(params["job_id"])
    if not job:
        return _error(404, "Unknown job")
    result = job.pop("result", None) or {}
    # Flat, as SPEC §5 shows; the result's own fields (mode: quick-fallback, error) win.
    body = {**job, **result, "status": job["status"], "error": result.get("error") or job.get("error")}
    return _response(200, body)


ROUTES = [
    ("GET", re.compile(r"^/watchlist/?$"), get_watchlist),
    ("POST", re.compile(r"^/watchlist/?$"), post_watchlist),
    ("DELETE", re.compile(rf"^/watchlist/(?P<ticker>{TICKER})/?$"), delete_watchlist),
    ("GET", re.compile(r"^/quotes/?$"), get_quotes),
    ("GET", re.compile(rf"^/news/(?P<ticker>{TICKER})/?$"), get_news),
    ("GET", re.compile(r"^/feed/?$"), get_feed),
    ("POST", re.compile(r"^/ask/?$"), post_ask),
    ("GET", re.compile(r"^/ask/(?P<job_id>[0-9a-f]{32})/?$"), get_ask),
]


def _route(method: str, path: str, event) -> dict:
    if path.rstrip("/") == "/health":
        return _response(200, {"ok": True})
    if not os.environ.get("APP_ACCESS_CODE"):
        logger.error("APP_ACCESS_CODE is not configured (check the SSM parameters); refusing requests")
        return _error(503, "Service is not configured")
    if not _authorized(event):
        return _error(401, "Missing or wrong x-access-code")

    matched_path = False
    for route_method, pattern, fn in ROUTES:
        m = pattern.match(path)
        if not m:
            continue
        matched_path = True
        if route_method != method:
            continue
        try:
            return fn(event, m.groupdict())
        except BadRequest as e:
            return _error(400, str(e))
        except (market.MarketDataError, news.NewsError) as e:
            return _error(502, str(e))
        except StoreError as e:
            return _error(503, str(e))
        except Exception:
            logger.exception("Unhandled error on %s %s", method, path)
            return _error(500, "Internal error")
    return _error(405 if matched_path else 404, "Method not allowed" if matched_path else "Not found")


def handler(event, context):
    ctx = event.get("requestContext") or {}
    log.new_request_id(ctx.get("requestId") or getattr(context, "aws_request_id", None))
    method = (ctx.get("http") or {}).get("method", "GET").upper()
    path = event.get("rawPath") or "/"
    stage = ctx.get("stage")
    if stage and stage != "$default" and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    response = _route(method, path, event)
    logger.info("%s %s -> %d", method, path, response["statusCode"],
                extra={"method": method, "path": path, "status": response["statusCode"]})
    return response
