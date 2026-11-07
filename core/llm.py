"""
Gemini text generation with a model fallback chain.

Tries GEMINI_MODEL first, then each model in GEMINI_FALLBACK_MODELS, moving on
after a rate limit (429), a server error (5xx) or a timeout, all within one total
time budget. Raises LLMUnavailableError when nothing worked, so callers can show a
friendly message instead of a stack trace.

Ported from v1 core/llm.py (73b6fda). Added: one `log_tool` line per attempt with the
model, latency, success and token usage (SPEC §7). Default chain verified available for
this key on 2026-11-05 (models.list): gemini-3.5-flash-lite, gemini-3.1-flash-lite,
gemini-flash-lite-latest, gemini-3.5-flash.
"""

import logging
import threading
import time

from google.genai import Client
from google.genai import errors as genai_errors
from google.genai.types import GenerateContentConfig, HttpOptions

from core.config import env, env_float
from core.log import log_tool

logger = logging.getLogger(__name__)

# gemini-2.5-flash-lite returns 404 for new keys; Google points to 3.5-flash-lite.
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODELS = [
    m.strip()
    # gemini-3.5-flash last: a different model family, so one overloaded family
    # (all flash-lite models returned 503/504 on 2026-11-03) doesn't stop the app.
    for m in env("GEMINI_FALLBACK_MODELS",
                 "gemini-3.1-flash-lite,gemini-flash-lite-latest,gemini-3.5-flash").split(",")
    if m.strip()
]
# Per-call HTTP timeout. Without it the SDK can hang for minutes under load.
GEMINI_TIMEOUT_SECONDS = env_float("GEMINI_TIMEOUT_SECONDS", 30)
# Total time budget for all attempts of one request.
GEMINI_TOTAL_BUDGET_SECONDS = env_float("GEMINI_TOTAL_BUDGET_SECONDS", 45)

RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}
FATAL_STATUS_CODES = {401, 403}
# A model that just failed with an overload/timeout is skipped for a while, so every
# call doesn't wait on it again (cut Deep-mode latency during the 2026-11-03 outage).
COOLDOWN_SECONDS = env_float("GEMINI_COOLDOWN_SECONDS", 60)
COOLDOWN_STATUS_CODES = {500, 502, 503, 504}
MIN_ATTEMPT_SECONDS = 3

_client: Client | None = None
_client_lock = threading.Lock()


class LLMUnavailableError(Exception):
    """Raised when every Gemini model in the chain failed."""


def get_client() -> Client:
    """Creates the Gemini client once (lazily, so a missing key can't crash an import)."""
    global _client
    with _client_lock:
        if _client is None:
            api_key = env("GEMINI_API_KEY")
            if not api_key:
                raise LLMUnavailableError("GEMINI_API_KEY is not configured")
            _client = Client(
                api_key=api_key,
                http_options=HttpOptions(timeout=int(GEMINI_TIMEOUT_SECONDS * 1000)),
            )
        return _client


_calls_lock = threading.Lock()
_calls = 0


def calls_made() -> int:
    """Gemini generate attempts made by this process (for latency/quota reporting)."""
    with _calls_lock:
        return _calls


def _count_call() -> None:
    global _calls
    with _calls_lock:
        _calls += 1


def _tokens(response) -> int | None:
    usage = getattr(response, "usage_metadata", None)
    total = getattr(usage, "total_token_count", None)
    return total if isinstance(total, int) else None


def has_text(response) -> bool:
    try:
        return bool((response.text or "").strip())
    except ValueError:  # the SDK raises when a response has no text parts
        return False


_cooldown_lock = threading.Lock()
_cooldown_until: dict[str, float] = {}


def _cool_down(model: str) -> None:
    with _cooldown_lock:
        _cooldown_until[model] = time.monotonic() + COOLDOWN_SECONDS


def _model_order() -> list[str]:
    """The chain, with models in cooldown moved to the end (never dropped)."""
    models = [GEMINI_MODEL] + [m for m in GEMINI_FALLBACK_MODELS if m != GEMINI_MODEL]
    now = time.monotonic()
    with _cooldown_lock:
        ready = [m for m in models if _cooldown_until.get(m, 0) <= now]
    return ready + [m for m in models if m not in ready]


def reset_cooldowns() -> None:
    with _cooldown_lock:
        _cooldown_until.clear()


def call(contents, config: GenerateContentConfig, budget_seconds: float | None = None,
         accept=has_text):
    """
    One generate_content call through the model fallback chain; returns the raw response.
    `accept(response)` decides whether a response is usable (default: it has text).
    Each attempt's HTTP timeout is cut to the time left, so `budget_seconds` really
    bounds the call. Used directly by the agents' tool loops (function calling, JSON).
    """
    client = get_client()
    deadline = time.monotonic() + (budget_seconds or GEMINI_TOTAL_BUDGET_SECONDS)
    last_error = "no model attempted"

    for i, model in enumerate(_model_order()):
        if i > 0:
            time.sleep(min(2 ** (i - 1), 4) * 0.5)  # short backoff between models
        remaining = deadline - time.monotonic()
        if remaining < MIN_ATTEMPT_SECONDS:
            logger.warning("Gemini time budget used up before trying %s", model)
            break
        timeout = min(GEMINI_TIMEOUT_SECONDS, remaining)
        attempt_config = config.model_copy(update={"http_options": HttpOptions(timeout=int(timeout * 1000))})
        started = time.monotonic()
        try:
            _count_call()
            response = client.models.generate_content(model=model, config=attempt_config, contents=contents)
            if not accept(response):
                log_tool(f"gemini:{model}", time.monotonic() - started, False, _tokens(response), error="unusable")
                last_error = f"{model}: empty response"
                logger.warning("Gemini %s returned an unusable response", model)
                continue
            log_tool(f"gemini:{model}", time.monotonic() - started, True, _tokens(response))
            logger.info("Gemini answered with %s in %.1fs", model, time.monotonic() - started)
            return response
        except genai_errors.APIError as e:
            last_error = f"{model}: HTTP {e.code}"
            log_tool(f"gemini:{model}", time.monotonic() - started, False, error=f"HTTP {e.code}")
            if e.code in COOLDOWN_STATUS_CODES:
                _cool_down(model)
            if e.code in FATAL_STATUS_CODES:  # bad or blocked API key: no model will work
                logger.error("Gemini %s failed with HTTP %s (check GEMINI_API_KEY)", model, e.code)
                break
            # 429/5xx, and also 400/404: those can be model-specific (a retired model, a
            # feature one model rejects; seen 2026-11-03), so the next model may still work.
            logger.warning("Gemini %s failed with HTTP %s; trying next model", model, e.code)
            continue
        except Exception as e:  # timeouts, connection errors
            last_error = f"{model}: {type(e).__name__}"
            log_tool(f"gemini:{model}", time.monotonic() - started, False, error=type(e).__name__)
            _cool_down(model)
            logger.warning("Gemini %s failed (%s); trying next model", model, type(e).__name__)
            continue

    raise LLMUnavailableError(last_error)


def generate(prompt: str, max_output_tokens: int = 1024, temperature: float = 0.4) -> str:
    """Returns the first non-empty text answer from the model chain, or raises LLMUnavailableError."""
    config = GenerateContentConfig(max_output_tokens=max_output_tokens, temperature=temperature)
    return call(prompt, config).text.strip()
