"""Fully automated warmup: real-browser search flow, no human needed.

Launches a persistent Chromium profile, performs the WAS->BOS search through
the real UI (so Akamai's sensor JS executes exactly as for a human), captures
the fare-search response, exports cookies for the poller.

    python tools/auto_warmup.py [--headless]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "data" / "profile"
SAMPLES_DIR = ROOT / "data" / "samples"
COOKIES_OUT = ROOT / "data" / "cookies.json"
RESULT_OUT = ROOT / "data" / "warmup_result.json"

ORIGIN, DEST, DATE = "WAS", "BOS", "11/20/2026"


async def main() -> int:
    headless = "--headless" in sys.argv
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    result: dict = {"status": "no_response", "http": None, "sample": None}

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=headless,
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_response(resp):
            if "journey-solution-option" not in resp.url or result["http"]:
                return
            result["http"] = resp.status
            if resp.status == 200:
                body = await resp.json()
                out = SAMPLES_DIR / "fare_response_auto.json"
                out.write_text(json.dumps(body, indent=1), encoding="utf-8")
                result["sample"] = str(out)
                result["status"] = "captured"

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        seen_requests: list[str] = []
        page.on("request", lambda r: seen_requests.append(f"{r.method} {r.url}")
                if "dotcom" in r.url else None)

        await page.goto("https://www.amtrak.com/home.html",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)  # let sensor JS run

        # dismiss the OneTrust cookie banner if present (it intercepts clicks)
        try:
            btn = page.locator("button#onetrust-accept-btn-handler, "
                               "button:has-text('Accept All Cookies')").first
            await btn.click(timeout=5000)
            await page.wait_for_timeout(800)
        except Exception:
            pass  # no banner

        # drive the real booking widget
        async def pick_station(label: str, code: str, match: str):
            box = page.locator(f'am-form-field-new[aria-label="{label}"] input:visible').first
            await box.click()
            await box.fill(code)
            await page.wait_for_timeout(1800)
            opt = page.locator(f".station-plate-facility:has-text('{match}'):visible, "
                               f"[role='option']:has-text('{match}'):visible").first
            await opt.click()
            await page.wait_for_timeout(500)

        await pick_station("From", ORIGIN, "Union Station")
        await pick_station("To", DEST, "South Station")
        for_attr = await page.locator("label:has-text('Depart Date')").first \
                             .get_attribute("for")
        date_box = page.locator(f"#{for_attr}")
        await date_box.click(force=True)
        await date_box.fill(DATE)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(800)
        await page.get_by_role("button", name="FIND TRAINS").first.click(force=True)

        # wait up to 90s for the fare call (page may navigate first)
        for _ in range(18):
            if result["http"]:
                break
            await page.wait_for_timeout(5000)
            # after navigation the results page fires the dated search itself

        COOKIES_OUT.write_text(json.dumps(await ctx.cookies(), indent=1),
                               encoding="utf-8")
        result["cookies"] = len(json.loads(COOKIES_OUT.read_text()))
        result["final_url"] = page.url
        result["dotcom_requests"] = seen_requests[-10:]
        await page.screenshot(path=str(ROOT / "data" / "warmup_debug.png"),
                              full_page=False)
        result["form_values"] = await page.evaluate(
            "() => [...document.querySelectorAll('input')].filter(i=>i.offsetParent)"
            ".map(i=>i.value).slice(0,8)")
        RESULT_OUT.write_text(json.dumps(result, indent=1))
        print(json.dumps(result, indent=1))
        await ctx.close()

    if result["status"] == "captured":
        sys.path.insert(0, str(ROOT))
        from sniper import api
        return api._selftest(str(SAMPLES_DIR))
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
