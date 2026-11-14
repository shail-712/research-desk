"""
Cold-start setup shared by every handler.

- Logs: core/log.py JSON lines with the request ID (replaces the Lambda runtime's text handler).
- Secrets: SecureString parameters under SSM_PREFIX (default /research-desk) are read ONCE per
  container with GetParameters and exported as environment variables for core/ (which reads
  os.environ). Each function reads only the names in its SSM_SECRETS env var, and IAM allows only
  those parameters (review I3, finding 2). Nothing secret is in the template or the function
  configuration, and values are never logged (only the names).
- Paths: Lambda's filesystem is read-only except /tmp.
- Metrics: CloudWatch Embedded Metric Format, so a stdout line becomes a metric (no
  PutMetricData calls, no extra latency).

Ported from v1 aws_lambda/runtime.py (73b6fda): JSON logging via core.log, the v2 secret names.
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

SECRET_NAMES = ("GEMINI_API_KEY", "FINNHUB_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "SEC_USER_AGENT", "APP_ACCESS_CODE")
NAMESPACE = "ResearchDesk"

_bootstrapped = False


def wanted_secrets() -> list[str]:
    """The secrets this function reads: SSM_SECRETS (comma-separated), else all of SECRET_NAMES."""
    raw = os.environ.get("SSM_SECRETS", "").strip()
    if not raw:
        return list(SECRET_NAMES)
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in SECRET_NAMES]
    if unknown:
        logger.warning("SSM_SECRETS has unknown name(s), ignored: %s", ", ".join(unknown))
    return [n for n in names if n in SECRET_NAMES]


def load_secrets(prefix: str | None = None, client=None, names: list[str] | None = None) -> list[str]:
    """Exports the named SSM parameters under `prefix` as env vars (existing env vars win).
    Returns the names loaded; names missing in SSM are logged (names only, never values)."""
    import boto3

    path = (prefix or os.environ.get("SSM_PREFIX", "/research-desk")).rstrip("/")  # IAM grants these names
    if not path.startswith("/"):
        raise ValueError(f"SSM_PREFIX must start with '/', got {path!r}")
    wanted = [n for n in (names if names is not None else wanted_secrets()) if not os.environ.get(n)]
    loaded = []
    if wanted:
        client = client or boto3.client("ssm")
        resp = client.get_parameters(Names=[f"{path}/{n}" for n in wanted], WithDecryption=True)
        for p in resp.get("Parameters", []):
            name = p["Name"][len(path) + 1:]
            if name in wanted:
                os.environ[name] = p["Value"]
                loaded.append(name)
        missing = sorted(set(wanted) - set(loaded))
        if missing:
            logger.error("Secret(s) not found in SSM under %s: %s", path, ", ".join(missing))
    logger.info("Loaded %d secret(s) from SSM: %s", len(loaded), ", ".join(sorted(loaded)) or "none")
    return loaded


def bootstrap() -> None:
    """Once per container: JSON logs, /tmp paths, secrets from SSM."""
    global _bootstrapped
    if _bootstrapped:
        return
    os.environ.setdefault("CACHE_DIR", "/tmp/research-desk")
    from core import log  # after CACHE_DIR: core.config reads it at import

    log.setup(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        os.makedirs(os.environ["CACHE_DIR"], exist_ok=True)
        import yfinance as yf

        yf.set_tz_cache_location(os.path.join(os.environ["CACHE_DIR"], "yfinance"))  # default is read-only
    except Exception:  # pragma: no cover - only matters inside Lambda
        pass
    if os.environ.get("SSM_PREFIX"):
        try:
            load_secrets()
        except Exception as e:  # the handler reports a clean error later if a secret is missing
            logger.error("Could not load secrets from SSM: %s", type(e).__name__)
    _bootstrapped = True


def emit_metrics(metrics: dict[str, float], dimensions: dict[str, str] | None = None,
                 units: dict[str, str] | None = None) -> None:
    """Writes one CloudWatch EMF record to stdout (CloudWatch turns it into metrics)."""
    dimensions = dimensions or {}
    units = units or {}
    record = {
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": NAMESPACE,
            "Dimensions": [sorted(dimensions)] if dimensions else [[]],
            "Metrics": [{"Name": k, "Unit": units.get(k, "None")} for k in metrics],
        }]},
        **dimensions, **{k: float(v) for k, v in metrics.items()},
    }
    print(json.dumps(record), flush=True)
