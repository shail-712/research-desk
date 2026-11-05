"""Lambda runtime, API and ask worker against moto (DynamoDB, SSM), with market data, news and the
agents mocked. Adapted from v1 tests/unit/test_lambda.py (73b6fda)."""

import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core import store as store_mod

ROOT = Path(__file__).resolve().parents[2]
KEY = "letmein-test-code"


@pytest.fixture
def aws(dynamo_store, monkeypatch):
    """DynamoDB-backed store + the env the template sets."""
    monkeypatch.setattr(store_mod, "_store", dynamo_store)
    monkeypatch.setenv("APP_ACCESS_CODE", KEY)
    monkeypatch.setenv("ASK_WORKER_FUNCTION", "AskWorker")
    monkeypatch.delenv("SSM_PREFIX", raising=False)
    dynamo_store.seed_watchlist(store_mod.DEFAULT_WATCHLIST)
    return dynamo_store


@pytest.fixture
def api(aws):
    from aws_lambda import api

    return importlib.reload(api)


@pytest.fixture
def worker(aws):
    from aws_lambda import ask_worker

    return importlib.reload(ask_worker)


def http(method, path, body=None, key=KEY, query=None, raw_body=None, request_id="req-abc123"):
    event = {"rawPath": path, "requestContext": {"http": {"method": method}, "stage": "$default",
                                                 "requestId": request_id},
             "headers": {"x-access-code": key} if key else {}}
    if body is not None:
        event["body"] = json.dumps(body)
    if raw_body is not None:
        event["body"] = raw_body
    if query:
        event["queryStringParameters"] = query
    return event


def call(api, *args, **kwargs):
    resp = api.handler(http(*args, **kwargs), None)
    return resp["statusCode"], json.loads(resp["body"])


QUOTE = {"ticker": "AAPL", "price": 270.0, "prev_close": 268.0, "change": 2.0, "change_pct": 0.75,
         "trading_date": "2026-11-02", "source": "yfinance", "fetched_at": "2026-11-03T15:00:00+00:00"}


# ── runtime ──

def test_secrets_load_from_ssm_without_overriding_env(aws_env, monkeypatch, caplog):
    import boto3
    from moto import mock_aws

    from aws_lambda import runtime

    for name in runtime.SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ACCESS_CODE", "from-env")
    with mock_aws():
        ssm = boto3.client("ssm", region_name="ap-south-1")
        for name, value in {"GEMINI_API_KEY": "g-secret-value", "APP_ACCESS_CODE": "from-ssm",
                            "FINNHUB_API_KEY": "f-secret-value", "UNRELATED": "x"}.items():
            ssm.put_parameter(Name=f"/research-desk/{name}", Value=value, Type="SecureString")
        ssm.put_parameter(Name="/other/GEMINI_API_KEY", Value="wrong", Type="SecureString")
        with caplog.at_level("INFO"):
            loaded = runtime.load_secrets("/research-desk/", client=ssm)
    assert sorted(loaded) == ["FINNHUB_API_KEY", "GEMINI_API_KEY"]
    import os

    assert os.environ["GEMINI_API_KEY"] == "g-secret-value" and os.environ["APP_ACCESS_CODE"] == "from-env"
    assert "g-secret-value" not in caplog.text and "GEMINI_API_KEY" in caplog.text  # names only


def test_secrets_loader_rejects_a_non_ssm_path():
    from aws_lambda import runtime

    with pytest.raises(ValueError):
        runtime.load_secrets("research-desk", client=object())


def test_emf_record_shape(capsys):
    from aws_lambda.runtime import emit_metrics

    emit_metrics({"AgentLatency": 12.5, "GeminiCalls": 3}, {"Mode": "deep"}, {"AgentLatency": "Seconds"})
    record = json.loads(capsys.readouterr().out)
    cw = record["_aws"]["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "ResearchDesk" and cw["Dimensions"] == [["Mode"]]
    assert {"Name": "AgentLatency", "Unit": "Seconds"} in cw["Metrics"]
    assert record["AgentLatency"] == 12.5 and record["Mode"] == "deep"


# ── API: auth ──

def test_health_needs_no_code_but_everything_else_does(api):
    assert call(api, "GET", "/health", key=None) == (200, {"ok": True})
    status, body = call(api, "GET", "/watchlist", key=None)
    assert status == 401 and body == {"error": "Missing or wrong x-access-code"}
    assert call(api, "GET", "/watchlist", key="wrong")[0] == 401
    assert call(api, "GET", "/watchlist")[0] == 200


def test_header_name_is_case_insensitive(api):
    event = http("GET", "/watchlist", key=None)
    event["headers"] = {"X-Access-Code": KEY}
    assert api.handler(event, None)["statusCode"] == 200


def test_api_fails_closed_when_the_access_code_is_not_configured(api, monkeypatch):
    monkeypatch.setenv("APP_ACCESS_CODE", "")
    assert call(api, "GET", "/watchlist", key="")[0] == 503
    assert call(api, "GET", "/watchlist", key="anything")[0] == 503
    assert call(api, "GET", "/health", key=None)[0] == 200


def test_request_id_is_returned_and_logged(api, caplog):
    with caplog.at_level("INFO"):
        resp = api.handler(http("GET", "/health", request_id="gw-req-42"), None)
    assert resp["headers"]["x-request-id"] == "gw-req-42"
    from core import log

    assert log.get_request_id() == "gw-req-42"


# ── API: routes ──

def test_watchlist_crud_with_ticker_or_name(api, sec_tickers):
    with mock.patch.object(api.market, "get_quotes", return_value={"AAPL": QUOTE}):
        status, body = call(api, "POST", "/watchlist", {"name": "Apple", "pct_move": 2.5, "price_above": 300})
    assert status == 201
    assert body["added"]["ticker"] == "AAPL" and body["added"]["pct_move"] == 2.5
    assert body["added"]["price_above"] == 300 and body["added"]["cik"] == 320193
    with mock.patch.object(api.market, "get_quotes", return_value={"AAPL": QUOTE}):
        assert call(api, "POST", "/watchlist", {"ticker": "aapl"})[0] == 201  # update, default rule
    rows = {w["ticker"]: w for w in call(api, "GET", "/watchlist")[1]["watchlist"]}
    assert rows["AAPL"]["pct_move"] == 3.0
    assert call(api, "DELETE", "/watchlist/AAPL") == (200, {"removed": "AAPL"})
    assert call(api, "DELETE", "/watchlist/AAPL")[0] == 404


def test_watchlist_rejects_unknown_dataless_and_invalid_input(api, sec_tickers):
    assert call(api, "POST", "/watchlist", {"name": "No Such Company Anywhere"})[0] == 404
    with mock.patch.object(api.market, "get_quotes", return_value={"AAPL": {"ticker": "AAPL", "error": "x"}}):
        assert call(api, "POST", "/watchlist", {"ticker": "AAPL"})[0] == 422
    for bad in ({}, {"ticker": "DROP TABLE"}, {"ticker": "AAPL", "pct_move": -1},
                {"ticker": "AAPL", "pct_move": "abc"}, {"ticker": "AAPL", "price_below": True},
                {"name": "x" * 101}):
        status, body = call(api, "POST", "/watchlist", bad)
        assert status == 400 and set(body) == {"error"}, bad


def test_quotes_default_to_the_watchlist_and_validate_tickers(api):
    with mock.patch.object(api.market, "get_quotes", return_value={"NVDA": QUOTE}) as gq:
        assert call(api, "GET", "/quotes")[0] == 200
        assert gq.call_args.args[0] == ["NVDA", "AMD", "INTC"]  # one batched call
        call(api, "GET", "/quotes", query={"tickers": "QQQ,SPY"})
        assert gq.call_args.args[0] == ["QQQ", "SPY"]
    assert call(api, "GET", "/quotes", query={"tickers": "NVDA,<script>"})[0] == 400
    assert call(api, "GET", "/quotes", query={"tickers": ",".join(["A"] * 21)})[0] == 400
    with mock.patch.object(api.market, "get_quotes", side_effect=api.market.MarketDataError("Yahoo down")):
        assert call(api, "GET", "/quotes") == (502, {"error": "Yahoo down"})


def test_feed_and_news(api, aws):
    aws.insert_event({"ticker": "NVDA", "kind": "price_move", "title": "t", "dedupe_key": "k"})
    status, body = call(api, "GET", "/feed", query={"limit": "999"})
    assert status == 200 and [e["title"] for e in body["events"]] == ["t"]
    with mock.patch.object(api.news, "get_news", return_value=[{"title": "h"}]) as gn:
        assert call(api, "GET", "/news/nvda") == (200, {"ticker": "NVDA", "articles": [{"title": "h"}]})
    assert gn.call_args.args[0] == "NVDA"
    assert call(api, "GET", "/news/NV%20DA")[0] == 404  # path params are validated by the route


def test_unknown_routes_methods_bad_json_and_internal_errors(api):
    assert call(api, "GET", "/nope")[0] == 404
    assert call(api, "PUT", "/watchlist")[0] == 405
    assert call(api, "POST", "/ask", raw_body="{not json") == (400, {"error": "Body must be valid JSON"})
    assert call(api, "POST", "/ask", raw_body="[1]")[0] == 400
    with mock.patch.object(api, "get_store", side_effect=RuntimeError("secret detail")):
        status, body = call(api, "GET", "/watchlist")
    assert status == 500 and body == {"error": "Internal error"}  # no stack trace or detail


# ── API: ask ──

def test_ask_creates_a_job_and_invokes_the_worker_async(api, aws):
    with mock.patch.object(api, "_invoke_worker") as invoke:
        status, body = call(api, "POST", "/ask", {"question": "Why did AMD fall?", "mode": "deep"},
                            request_id="gw-1")
    assert status == 202 and body["poll"] == f"/ask/{body['job_id']}" and body["status"] == "pending"
    payload = invoke.call_args.args[0]
    assert payload == {"job_id": body["job_id"], "question": "Why did AMD fall?", "mode": "deep",
                       "include_context": False, "request_id": "gw-1"}
    assert call(api, "GET", body["poll"])[1]["status"] == "pending"


@pytest.mark.parametrize("payload, message", [
    ({}, "Missing 'question'"),
    ({"question": "   "}, "Missing 'question'"),
    ({"question": 42}, "Missing 'question'"),
    ({"question": "x" * 1001}, "Questions are limited to 1000 characters"),
    ({"question": "ok", "mode": "slow"}, "'mode' must be one of quick, deep"),
    ({"question": "ok", "include_context": "yes"}, "'include_context' must be true or false"),
])
def test_ask_validates_input(api, payload, message):
    with mock.patch.object(api, "_invoke_worker") as invoke:
        assert call(api, "POST", "/ask", payload) == (400, {"error": message})
    invoke.assert_not_called()


def test_ask_records_a_worker_start_failure(api, aws):
    with mock.patch.object(api, "_invoke_worker", side_effect=RuntimeError("throttled")):
        status, _ = call(api, "POST", "/ask", {"question": "q"})
    assert status == 503
    job = aws.table.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": "JOB"})["Items"][0]
    assert job["status"] == "error"


def test_unknown_job_is_404(api):
    assert call(api, "GET", "/ask/" + "0" * 32) == (404, {"error": "Unknown job"})
    assert call(api, "GET", "/ask/not-a-job-id")[0] == 404


# ── worker ──

RESULT = {"answer": "NVDA is $210 [E1].", "error": None, "used_tools": ["market"], "mode": "deep",
          "companies": [{"ticker": "NVDA"}], "sources": [{"id": "E1"}], "trace": {"rounds": [[]]},
          "timings": {"total": 9.5}, "context": "E1: NVDA price 210 ... " * 10}


def _run_job(api, worker, aws, include_context=None, result=RESULT, mode="deep"):
    body = {"question": "NVDA?", "mode": mode}
    if include_context is not None:
        body["include_context"] = include_context
    with mock.patch.object(api, "_invoke_worker") as invoke:
        _, job = call(api, "POST", "/ask", body)
    target = "answer_deep" if mode == "deep" else None
    with mock.patch.object(worker, "answer_deep", return_value=dict(result)) as deep, \
         mock.patch.object(worker.agent, "answer", return_value=dict(result)) as quick:
        out = worker.handler(invoke.call_args.args[0], None)
    (deep if target else quick).assert_called_once_with("NVDA?")
    return out, call(api, "GET", job["poll"])[1]


def test_worker_result_has_no_context_by_default(api, worker, aws, capsys):
    out, polled = _run_job(api, worker, aws)
    assert out["status"] == "done" and polled["status"] == "done"
    assert polled["answer"] == RESULT["answer"] and polled["sources"] == [{"id": "E1"}]
    assert polled["trace"] == {"rounds": [[]]} and polled["mode"] == "deep" and polled["question"] == "NVDA?"
    assert "context" not in polled and "context_truncated" not in polled
    assert polled["timings"]["total"] == 9.5 and "llm_calls" in polled["timings"]
    emf = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"_aws"')]
    assert emf and emf[-1]["Mode"] == "deep" and emf[-1]["DeepFallbacks"] == 0 and emf[-1]["AskErrors"] == 0


def test_worker_keeps_context_only_when_asked_and_caps_it(api, worker, aws, monkeypatch):
    _, polled = _run_job(api, worker, aws, include_context=True, mode="quick")
    assert polled["context"] == RESULT["context"] and polled["context_truncated"] is False
    monkeypatch.setattr(worker, "MAX_CONTEXT_BYTES", 50)
    big = {**RESULT, "context": "é" * 100}  # 2 bytes each: the cap must not split a character
    _, polled = _run_job(api, worker, aws, include_context=True, result=big)
    assert polled["context"] == "é" * 25 and polled["context_truncated"] is True


def test_context_cap_is_100_kb_and_the_item_fits_dynamodb(worker):
    assert worker.MAX_CONTEXT_BYTES == 100_000
    huge = {**RESULT, "context": "x" * 500_000, "trace": {"rounds": ["y" * 400_000]},
            "sources": [{"id": f"E{i}"} for i in range(200)]}
    out = worker.compact(huge, include_context=True)
    assert out["context_truncated"] is True and len(out["context"]) == 100_000
    assert "trace" not in out and out["trace_dropped"] is True and len(out["sources"]) == 30
    assert len(json.dumps(out).encode()) < 400_000 - 10_000  # room for the job's own fields
    assert "context" not in worker.compact(huge, include_context=False)


def test_worker_records_failures_and_fallbacks(api, worker, aws, capsys):
    with mock.patch.object(api, "_invoke_worker") as invoke:
        _, job = call(api, "POST", "/ask", {"question": "q", "mode": "deep"})
    with mock.patch.object(worker, "answer_deep", side_effect=RuntimeError("boom")):
        assert worker.handler(invoke.call_args.args[0], None)["status"] == "error"
    polled = call(api, "GET", job["poll"])[1]
    assert polled["status"] == "error" and polled["error"] == "Answer failed (RuntimeError)"
    assert '"AskErrors": 1.0' in capsys.readouterr().out
    _, polled = _run_job(api, worker, aws, result={**RESULT, "mode": "quick-fallback"})
    assert polled["mode"] == "quick-fallback"  # the result's mode wins over the job's


def test_worker_reuses_the_api_request_id(worker, aws):
    from core import log

    job_id = aws.create_job("q", "quick")
    with mock.patch.object(worker.agent, "answer", return_value=dict(RESULT)):
        worker.handler({"job_id": job_id, "question": "q", "mode": "quick", "request_id": "gw-77"},
                       SimpleNamespace(aws_request_id="lambda-1"))
    assert log.get_request_id() == "gw-77"


# ── image ──

def test_image_has_code_only_and_no_data_dir():
    ignore = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert ignore[0] == "*"
    allowed = {line[1:].rstrip("/") for line in ignore if line.startswith("!")}
    assert allowed == {"core", "aws_lambda", "jobs", "requirements-lambda.txt"}
    copies = re.findall(r"^COPY (\S+)", (ROOT / "Dockerfile").read_text(), re.M)
    assert not [c for c in copies if c.split("/")[0] in ("data", ".env", "tests", "docs", ".")]


def test_lambda_requirements_match_the_main_pins():
    def pins(name):
        return dict(re.findall(r"^([A-Za-z0-9_.\-\[\]]+)==(\S+)", (ROOT / name).read_text(), re.M))

    lam, main = pins("requirements-lambda.txt"), pins("requirements.txt")
    assert lam and all(main.get(pkg) == ver for pkg, ver in lam.items())
    assert "streamlit" not in lam
