"""
Structured JSON logs with a request ID (SPEC §7).

- One JSON object per line: ts, level, logger, msg, request_id, plus any `extra=` fields.
- The request ID lives in a contextvar, so it follows a request through the API, the worker, the
  agents and their tool threads (use `contextvars.copy_context()` / `run_in_context` for threads).
- `log_tool()` records tool name, latency, success and token usage in one line.
- Secret values (keys and tokens from the environment) are redacted from every record.

Named `log.py`, not `logging.py`, so it never shadows the stdlib module.
"""

import contextvars
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

SECRET_ENV_NAMES = ("GEMINI_API_KEY", "FINNHUB_API_KEY", "TELEGRAM_BOT_TOKEN", "APP_ACCESS_CODE",
                    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
REDACTED = "***"
# Attributes every LogRecord has; anything else came from `extra=` and is logged as a field.
_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
NOISY = ("httpx", "httpcore", "google_genai", "urllib3", "peewee", "botocore", "boto3")


def new_request_id(value: str | None = None) -> str:
    """Sets (and returns) the current request ID; generates one if none is given."""
    rid = value or uuid.uuid4().hex[:12]
    _request_id.set(rid)
    return rid


def get_request_id() -> str:
    return _request_id.get()


def run_in_context(fn):
    """Wraps `fn` so it runs with the caller's context (request ID) in a worker thread."""
    ctx = contextvars.copy_context()
    return lambda *a, **k: ctx.run(fn, *a, **k)


def _secrets() -> list[str]:
    # Short values could redact ordinary words; real keys and tokens are long.
    return [v for v in (os.environ.get(n, "").strip() for n in SECRET_ENV_NAMES) if len(v) >= 8]


def redact(text: str) -> str:
    for secret in _secrets():
        text = text.replace(secret, REDACTED)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and key not in out:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(out, default=str, ensure_ascii=False))


def setup(level: str | int = "INFO", stream=None) -> None:
    """Replaces the root handlers with one JSON handler. Safe to call more than once."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else str(level).upper())
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    # yfinance logs every unknown ticker at ERROR; we report those per ticker ourselves.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)


_tool_logger = logging.getLogger("research_desk.tool")


def log_tool(name: str, seconds: float, ok: bool, tokens: int | None = None, **fields) -> None:
    """One line per tool / external call: name, latency, success, token usage."""
    _tool_logger.info("tool %s %s in %.2fs", name, "ok" if ok else "failed", seconds,
                      extra={"tool": name, "seconds": round(seconds, 3), "ok": ok, "tokens": tokens, **fields})
