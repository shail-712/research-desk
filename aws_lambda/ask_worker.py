"""
Async worker for POST /ask: runs Quick (core.agent) or Deep (core.agents.deep), stores the result
on the job item and emits latency / Gemini-call metrics (EMF).

Invoked with InvocationType=Event by ApiFunction (async retries 0 in the template: the job item
records failures, and long LLM work isn't rerun). The request ID from the API is reused, so one
ID covers the API and the worker logs.

`context` (the evidence text) is kept only when the job asked for it (`include_context`, D10, eval
runner only), capped at MAX_CONTEXT_BYTES with a `context_truncated` flag, so the item stays under
DynamoDB's 400 KB limit.

Adapted from v1 aws_lambda/ask_worker.py (73b6fda): include_context, request ID, llm_calls in
`timings` for the eval runner.
"""

import json
import logging
import time

from aws_lambda.runtime import bootstrap, emit_metrics

bootstrap()

from core import agent, llm, log  # noqa: E402
from core.agents.deep import answer_deep  # noqa: E402
from core.store import get_store  # noqa: E402

logger = logging.getLogger(__name__)

MAX_CONTEXT_BYTES = 100_000   # SPEC §5 / D10
MAX_RESULT_BYTES = 250_000    # everything else; with the context the item stays well under 400 KB
MAX_SOURCES_WHEN_TRIMMED = 30


def _size(obj) -> int:
    return len(json.dumps(obj, default=str).encode("utf-8"))


def cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cuts `text` to at most `max_bytes` of UTF-8 without splitting a character."""
    raw = (text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return text or "", False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def compact(result: dict, include_context: bool) -> dict:
    """The job result: no evidence text unless asked for (then capped); if still too big, the
    trace goes first, then sources are trimmed."""
    out = {k: v for k, v in result.items() if k != "context"}
    if _size(out) > MAX_RESULT_BYTES:
        out.pop("trace", None)
        out["trace_dropped"] = True
        out["sources"] = (out.get("sources") or [])[:MAX_SOURCES_WHEN_TRIMMED]
    if include_context:
        out["context"], out["context_truncated"] = cap_text(result.get("context") or "", MAX_CONTEXT_BYTES)
    return out


def handler(event, context):
    log.new_request_id(event.get("request_id") or getattr(context, "aws_request_id", None))
    job_id, question = event["job_id"], event["question"]
    mode = event.get("mode", "quick")
    include_context = event.get("include_context") is True
    store = get_store()
    started, calls_before = time.monotonic(), llm.calls_made()
    try:
        result = answer_deep(question) if mode == "deep" else agent.answer(question)
    except Exception as e:
        logger.exception("Ask job %s failed", job_id)
        store.finish_job(job_id, "error", error=f"Answer failed ({type(e).__name__})")
        emit_metrics({"AskErrors": 1}, {"Mode": mode}, {"AskErrors": "Count"})
        return {"job_id": job_id, "status": "error"}

    seconds = time.monotonic() - started
    calls = llm.calls_made() - calls_before
    result["timings"] = {**(result.get("timings") or {}), "llm_calls": calls}
    status = "error" if result.get("error") else "done"
    store.finish_job(job_id, status, compact(result, include_context), error=result.get("error"))
    emit_metrics(
        {"AgentLatency": seconds, "GeminiCalls": calls,
         "DeepFallbacks": 1 if result.get("mode") == "quick-fallback" else 0,
         "AskErrors": 1 if status == "error" else 0},
        {"Mode": mode},
        {"AgentLatency": "Seconds", "GeminiCalls": "Count", "DeepFallbacks": "Count", "AskErrors": "Count"})
    logger.info("Ask job %s %s in %.1fs (%d LLM calls)", job_id, status, seconds, calls,
                extra={"job_id": job_id, "mode": mode, "seconds": round(seconds, 2), "status": status})
    return {"job_id": job_id, "status": status, "seconds": round(seconds, 2)}
