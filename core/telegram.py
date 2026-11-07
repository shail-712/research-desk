"""
Telegram push messages (free, no card): TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.

Set up once: message @BotFather -> /newbot -> copy the token; send your bot any
message; then open https://api.telegram.org/bot<TOKEN>/getUpdates and copy
"chat":{"id": ...}.

Ported from v1 core/telegram.py (73b6fda): one log line per send (log_tool), never the URL
(it contains the bot token); 5xx retried like 429; the filing icon dropped (D13).
"""

import html
import logging
import time

import requests

from core.config import env
from core.log import log_tool

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 10
MAX_LEN = 4000  # Telegram's limit is 4096 characters
MAX_ATTEMPTS = 3


def is_configured() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"))


def format_event(event: dict) -> str:
    """An events row -> a short HTML message."""
    icons = {"price_move": "📈", "price_above": "⬆️", "price_below": "⬇️"}
    icon = icons.get(event.get("kind"), "🔔")
    if event.get("kind") == "price_move" and " -" in (event.get("title") or ""):
        icon = "📉"
    parts = [f"{icon} <b>{html.escape(event.get('title') or '')}</b>"]
    if event.get("body"):
        parts.append(html.escape(event["body"]))
    if event.get("url"):
        parts.append(f'<a href="{html.escape(event["url"], quote=True)}">Open</a>')
    return "\n\n".join(parts)[:MAX_LEN]


def send(text_html: str) -> bool:
    """Sends one message. Returns True on success; never raises."""
    if not is_configured():
        logger.info("Telegram not configured; skipping message")
        return False
    url = API_URL.format(token=env("TELEGRAM_BOT_TOKEN"))
    payload = {"chat_id": env("TELEGRAM_CHAT_ID"), "text": text_html, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    started = time.monotonic()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
            if r.status_code == 200:
                log_tool("telegram.send", time.monotonic() - started, True, attempts=attempt)
                return True
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2
                if r.status_code == 429:
                    try:
                        wait = int(r.json().get("parameters", {}).get("retry_after", 2))
                    except ValueError:
                        pass
                logger.warning("Telegram HTTP %s, attempt %d", r.status_code, attempt)
                time.sleep(min(wait, 10))
                continue
            # Never log the URL: it contains the bot token. The body is Telegram's own error text.
            logger.error("Telegram send failed: HTTP %s %s", r.status_code, r.text[:200])
            break
        except requests.RequestException as e:
            logger.warning("Telegram send error (%s), attempt %d", type(e).__name__, attempt)
            time.sleep(attempt)
    log_tool("telegram.send", time.monotonic() - started, False)
    return False
