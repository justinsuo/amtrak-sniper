"""Telegram alerts + the solve_captcha interface hook (no OCR solver in v1)."""

from __future__ import annotations

from typing import Protocol

import httpx

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram(cfg: dict, text: str) -> bool:
    """Outbound-only Bot API call. Needs notify.bot_token AND notify.chat_id.

    Get a token from @BotFather, then send the bot any message and read your
    chat id from https://api.telegram.org/bot<token>/getUpdates.
    """
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
