"""Alert delivery + the solve_captcha interface hook (no OCR solver in v1).

Every alert is ALWAYS recorded to the local SQLite alerts table (shown in the
web UI), so Telegram is strictly optional. If notify.bot_token + chat_id are
configured, the alert is also pushed to Telegram.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
ALERT_DB = Path("data/prices.db")  # relative to the project working dir


def record_alert(text: str, db_path: Path = ALERT_DB) -> None:
    """Append the alert to the UI feed. Never raises — alerting must not
    crash the poller."""
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS alerts_feed ("
                     "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     "ts TEXT NOT NULL, text TEXT NOT NULL)")
        conn.execute("INSERT INTO alerts_feed (ts, text) VALUES (?, ?)",
                     (datetime.now().isoformat(timespec="seconds"), text))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


async def send_telegram(cfg: dict, text: str) -> bool:
    """Record locally, then push to Telegram if configured.

    One bot can serve many people: each user presses Start on the bot and
    puts THEIR chat_id in config. Share the token privately, never via git —
    whoever holds it controls the bot (revoke anytime via @BotFather /revoke).
    """
    record_alert(text)
    n = cfg.get("notify", {})
    token, chat_id = n.get("bot_token", ""), n.get("chat_id", "")
    if not token or not chat_id or "FILL_IN" in (token + chat_id):
        print(f"[notify] telegram not configured, message was:\n{text}")
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(TELEGRAM_API.format(token=token),
                             json={"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": True})
            return r.status_code == 200
    except httpx.HTTPError as e:
        print(f"[notify] telegram send failed: {e}")
        return False


class CaptchaSolver(Protocol):
    """Interface hook for a future OCR/plug-in solver. v1 ships no solver:
    a challenge pauses the loop and pages the human instead (spec rule 2)."""

    async def solve(self, challenge_context: dict) -> str:
        """Return a solution token, or raise to fall back to human escalation."""
        ...


class NoSolver:
    async def solve(self, challenge_context: dict) -> str:
        raise NotImplementedError("v1 escalates challenges to the human by design")
