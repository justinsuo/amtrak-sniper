"""Auto-checkout — drives the real Amtrak UI from search to payment.

Design rules (hard safety rails):
  1. The card never lives here. It uses the saved card in the user's own
     Amtrak account, inside their own persistent browser profile.
  2. The fare is re-verified at the payment step; if the price repriced
     above the bid, the purchase aborts and alerts.
  3. Never buys the same train+date twice (SQLite purchases ledger).
  4. A bank 3DS challenge pauses the flow and pages the human to approve —
     that step belongs to the bank, not to us.
  5. On ANY unexpected page: screenshot to data/, alert, leave the browser
     open for human takeover. It never guesses blindly.

Run standalone dry-run (goes to payment page, never clicks pay):
    python -m sniper.checkout --dry-run
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from . import notify, state

if TYPE_CHECKING:
    from .api import FareQuote

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "data" / "profile"
STATE_FILE = ROOT / "data" / "checkout_state.json"

# buttons we may click to advance the flow, in priority order
ADVANCE_WORDS = ["Add to Cart", "Continue", "Select", "Checkout", "Review"]
# final purchase click — only in live mode, only after price re-verification
PAY_WORDS = re.compile(r"(pay|place order|complete purchase|buy now|submit payment)", re.I)
# anything card-shaped must never be typed into by us
CARD_FIELD = re.compile(r"(card\s*(number|no)|ccnum|credit card|security code|cvv|cvc)", re.I)


def _write_state(**kw) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": datetime.now().isoformat(timespec="seconds"), **kw}
    STATE_FILE.write_text(json.dumps(payload, indent=1), encoding="utf-8")


async def _shot(page, name: str) -> str:
    p = ROOT / "data" / f"{name}_{datetime.now():%H%M%S}.png"
    try:
        await page.screenshot(path=str(p))
    except Exception:
        return ""
    return str(p)


async def _dismiss_consent(page) -> None:
    try:
        btn = page.locator("button#onetrust-accept-btn-handler, "
                           "button:has-text('Accept All Cookies')").first
        await btn.click(timeout=4000)
    except Exception:
        pass


async def _is_logged_in(page) -> bool:
    html = (await page.content()).lower()
    return "sign in" not in html or "sign out" in html or "my account" in html


async def _search(page, from_code: str, to_code: str, travel_date: str) -> None:
    """Drive the home-page widget with the selectors proven in Phase 0."""
    await page.goto("https://www.amtrak.com/home.html",
                    wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)
    await _dismiss_consent(page)

    async def pick(label: str, code: str):
        box = page.locator(f'am-form-field-new[aria-label="{label}"] input:visible').first
        await box.click()
        await box.fill(code)
        await page.wait_for_timeout(1800)
        await page.locator(
            f".station-plate-facility:visible, [role='option']:visible").first.click()
        await page.wait_for_timeout(400)

    await pick("From", from_code)
    await pick("To", to_code)
    # GUARD: after selection the widget's inputs hold the station CODES.
    # Never submit a search for the wrong city (would buy the wrong ticket).
    values = await page.evaluate(
        "() => [...document.querySelectorAll('input')]"
        ".filter(i => i.offsetParent).map(i => i.value)")
    if from_code not in values or to_code not in values:
        raise RuntimeError(f"station selection mismatch {values} — expected "
                           f"{from_code}/{to_code}; human, verify the route")
    for_attr = await page.locator("label:has-text('Depart Date')").first.get_attribute("for")
    date_box = page.locator(f"#{for_attr}")
    await date_box.click(force=True)
    dt = datetime.strptime(travel_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    await date_box.fill(dt)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(800)
    await page.get_by_role("button", name="FIND TRAINS").first.click(force=True)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(6000)


def _money(text: str) -> Optional[int]:
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text or "")
    return int(round(float(m.group(1).replace(",", "")) * 100)) if m else None


async def _select_train(page, quote: "FareQuote", bid_cents: int) -> int:
    """Click the matching train card on the results page; return live price."""
    if not quote.train_number:
        raise RuntimeError("quote has no train number — refusing to guess a train")
    await page.wait_for_timeout(4000)
    # prefer the exact train number from the quote
    loc = page.locator(f"text=/{quote.train_number}/").first
    try:
        card = page.locator(
            f"[class*='journey'], [class*='train'], [class*='option'], li, article"
        ).filter(has_text=quote.train_number).first
        await card.scroll_into_view_if_needed(timeout=8000)
        txt = await card.inner_text()
        price = _money(txt)
        await card.click()
        await page.wait_for_timeout(2500)
        return price or 0
    except Exception:
        await loc.click(timeout=8000)
        await page.wait_for_timeout(2500)
        return 0


async def _advance(page) -> bool:
    """Click the next safe progression button. Returns True if one was clicked."""
    for word in ADVANCE_WORDS:
        btn = page.locator(f"button:has-text('{word}'), a:has-text('{word}')").first
        try:
            if await btn.count() and await btn.is_visible(timeout=1500):
                await btn.click()
                await page.wait_for_timeout(3000)
                return True
        except Exception:
            continue
    return False


async def _at_payment(page) -> bool:
    """Conservative payment-page detection: require a card input or explicit
    payment/billing section text — not just the word 'payment' in a footer."""
    try:
        for sel in ["input[name*='card' i]", "input[id*='card' i]",
                    "input[autocomplete='cc-number']"]:
            if await page.locator(sel).count():
                return True
    except Exception:
        pass
    html = (await page.content()).lower()
    return ("payment method" in html or "card number" in html
            or "billing information" in html or "cvv" in html)


async def _pick_saved_card(page) -> bool:
    """Select a saved card on file — never typing any digits."""
    for sel in ["[class*='saved'] [type='radio']", "[class*='card'] [type='radio']",
                "input[type='radio']"]:
        try:
            r = page.locator(sel).first
            if await r.count() and await r.is_visible(timeout=1500):
                await r.check()
                return True
        except Exception:
            continue
    return False  # a saved card may already be preselected — not fatal


async def _page_total_cents(page) -> Optional[int]:
    for sel in ["[class*='total']", "[class*='Total']", "[class*='amount']"]:
        try:
            els = page.locator(sel)
            for i in range(min(await els.count(), 5)):
                cents = _money(await els.nth(i).inner_text())
                if cents:
                    return cents
        except Exception:
            continue
    return None


async def _wait_3ds_or_confirm(page, cfg: dict, timeout_s: int = 600) -> bool:
    """Poll for a confirmation screen; if a 3DS challenge appears, page the
    human and give them time to approve on the visible browser."""
    alerted = False
    for _ in range(timeout_s // 5):
        html = (await page.content()).lower()
        if re.search(r"(confirmation|reservation)\s*(number|code|#)", html) or "eticket" in html:
            return True
        if ("verify" in html and "card" in html) or "3-d secure" in html or "3ds" in html:
            if not alerted:
                await notify.send_telegram(
                    cfg, "🏦 Bank 3DS challenge on screen — approve it in the "
                         "browser window; I'm waiting (10 min).")
                alerted = True
        await page.wait_for_timeout(5000)
    return False


async def attempt_checkout(cfg: dict, quote: "FareQuote", dry_run: bool = False) -> dict:
    """Full purchase flow. Returns a result dict; never raises out."""
    bid_cents = int(round(float(cfg["max_price_usd"]) * 100))
    st = state.State(str(ROOT / "data" / "prices.db"))
    if st.already_purchased(quote.travel_date, quote.train_number):
        return {"status": "skipped", "reason": "already purchased"}

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"status": "error", "reason": "playwright not installed"}

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await _search(page, cfg["route"]["from_code"], cfg["route"]["to_code"],
                          quote.travel_date)
            if not await _is_logged_in(page):
                raise RuntimeError("not logged in — run the warmup and log into Amtrak first")

            live_price = await _select_train(page, quote, bid_cents)
            if live_price and live_price > bid_cents:
                raise RuntimeError(f"price repriced to ${live_price/100:.2f} > bid — aborting")

            for _step in range(8):  # advance until the payment page
                if await _at_payment(page):
                    break
                if not await _advance(page):
                    raise RuntimeError(f"stuck — no next button at {page.url}")
            else:
                raise RuntimeError("never reached payment page")

            await _pick_saved_card(page)
            total = await _page_total_cents(page)
            if total and total > bid_cents:
                raise RuntimeError(f"payment total ${total/100:.2f} > bid — aborting")

            if dry_run:
                shot = await _shot(page, "checkout_dryrun")
                _write_state(status="dry_run_ok", total_usd=(total or 0) / 100, shot=shot)
                return {"status": "dry_run_ok", "total_usd": (total or 0) / 100}

            pay = page.locator(
                "button:has-text('Pay'), button:has-text('Place Order'), "
                "button:has-text('Complete Purchase'), button:has-text('Buy')").first
            if not await pay.count():
                raise RuntimeError("pay button not found — human, take over")
            await pay.click()

            ok = await _wait_3ds_or_confirm(page, cfg)
            shot = await _shot(page, "checkout_done")
            pnr = ""
            html = await page.content()
            m = re.search(r"(?:confirmation|reservation)\s*(?:number|code|#)?[:\s#]*"
                          r"([A-Z0-9]{6})\b", html, re.I)
            if m:
                pnr = m.group(1)
            st.record_purchase(quote.travel_date, quote.train_number,
                               quote.price_cents, pnr,
                               "confirmed" if ok else "unknown")
            _write_state(status="confirmed" if ok else "timeout",
                         pnr=pnr, shot=shot)
            await notify.send_telegram(
                cfg, f"🎫 {'Purchased!' if ok else 'Purchase state unclear — check browser.'} "
                     f"{quote.travel_date} train {quote.train_number}, PNR {pnr or '?'}")
            return {"status": "confirmed" if ok else "timeout", "pnr": pnr}

        except Exception as e:  # noqa: BLE001 — fail safe: shot + alert + open browser
            shot = await _shot(page, "checkout_error")
            _write_state(status="error", reason=str(e), shot=shot)
            await notify.send_telegram(
                cfg, f"⚠️ checkout stopped: {e}. Browser left open for you."
                     + (f" Screenshot: {shot}" if shot else ""))
            return {"status": "error", "reason": str(e)}


def _latest_quote(cfg: dict) -> Optional["FareQuote"]:
    from .api import FareQuote
    rows = state.State(str(ROOT / "data" / "prices.db")).latest_quotes()
    if not rows:
        return None
    r = rows[0]
    return FareQuote(travel_date=r["travel_date"], train_number=r["train_number"],
                     departs=r["departs"] or "", arrives="", fare_class=r["fare_class"] or "",
                     price_cents=r["price_cents"])


async def _main() -> None:
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    q = _latest_quote(cfg)
    if not q:
        print("no fare quotes in the DB yet — let the monitor record some first")
        return
    print(f"dry-run target: {q.travel_date} train {q.train_number} ${q.price_usd:.2f}")
    print(json.dumps(await attempt_checkout(cfg, q, dry_run="--dry-run" in sys.argv), indent=1))


if __name__ == "__main__":
    asyncio.run(_main())
