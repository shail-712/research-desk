"""
Settings read from the environment.

Locally they come from `.env` (loaded once, here; real environment variables win). On AWS they come
from the Lambda environment and SSM (aws_lambda/runtime.py), on Streamlit Community Cloud from its
secrets, so every runtime reads settings the same way. Adapted from v1 core/config.py (Supabase cut).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

# Writable folder for small caches (SEC ticker list, local SQLite store).
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ROOT / ".cache"))
# Local FAISS index and the embedding cache (both git-ignored, never in the image).
INDEX_DIR = Path(os.environ.get("INDEX_DIR", ROOT / "data" / "index"))
EMBED_CACHE_PATH = Path(os.environ.get("EMBED_CACHE_PATH", ROOT / "data" / "cache" / "embeddings.sqlite"))

STORE_BACKENDS = ("local", "dynamodb")
VECTOR_BACKENDS = ("local", "s3")


class ConfigError(ValueError):
    """A setting has a value the app can't use."""


def env(name: str, default: str = "") -> str:
    """Returns a stripped environment value, or the default if unset/blank."""
    value = os.environ.get(name, "")
    return value.strip() if value and value.strip() else default


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def _choice(name: str, allowed: tuple[str, ...]) -> str:
    value = env(name, allowed[0]).lower()
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} is not one of {', '.join(allowed)}")
    return value


def store_backend() -> str:
    """`local` (SQLite, default) or `dynamodb`."""
    return _choice("STORE_BACKEND", STORE_BACKENDS)


def vector_backend() -> str:
    """`local` (FAISS files, default) or `s3`."""
    return _choice("VECTOR_BACKEND", VECTOR_BACKENDS)


def setup_logging(level: str | int | None = None) -> None:
    """JSON logs for the CLI, jobs and Lambdas (see core/log.py)."""
    from core import log

    log.setup(level or env("LOG_LEVEL", "INFO"))
