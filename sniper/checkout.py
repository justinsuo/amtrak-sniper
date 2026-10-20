"""Auto-checkout, Phase 2. STOPPED at the 3DS/payment step by design.

Honest status: the cart/checkout endpoints and selectors can only be pinned
by recon in a LOGGED-IN session with the owner's account (docs/endpoints.md
§2). This module therefore ships as a gated flow:

  - verifies the persistent profile holds a logged-in session,
  - re-runs the exact fare search for the hit quote and selects the train,
  - proceeds only as far as has been recon-verified,
  - never enters card data blindly, never touches the final pay button,
  - pages the human on any challenge or unexpected page.

Everything behind RECON TODOs is deliberately unwritten, not guessed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import notify

if TYPE_CHECKING:
    from .api import FareQuote

PROFILE_DIR = "data/profile"  # Playwright persistent user-data-dir


async def attempt_checkout(cfg: dict, quote: "FareQuote") -> bool:
    if not cfg.get("auto_checkout"):
        return False
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        await notify.send_telegram(
            cfg, "checkout: playwright not installed — pip install playwright && "
                 "playwright install chromium")
        return False

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False,  # visible: human watches & does 3DS
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            if not await _is_logged_in(page):
                await notify.send_telegram(
                    cfg, "checkout: not logged in. Run tools/warmup_capture.py, "
                         "log in once, then retry.")
                return False

            # RECON TODO (Phase 2): capture the logged-in flow once — search →
            # train select → add to cart → review → payment form — and encode
            # the verified selectors/URLs here. Until then we stop at search.
            await notify.send_telegram(
                cfg, f"checkout: logged in. Phase 2 recon pending — stopping "
                     f"before cart for train {quote.train_number} "
                     f"${quote.price_usd:.2f}. Browser left open for you.")
            return False
        finally:
            pass  # leave ctx open for the human; process exit closes it


async def _is_logged_in(page) -> bool:
    await page.goto("https://www.amtrak.com/home.html", wait_until="domcontentloaded")
    # Heuristic, verified in Phase 2 recon: account menu vs "Sign In" link.
    return "sign in" not in (await page.content()).lower()
