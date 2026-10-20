"""One-time session warmup + first-response capture.

Run this from any machine where amtrak.com works in a normal browser:

    python tools/warmup_capture.py

It opens a persistent Chromium profile, lets you browse / log in like a
normal human, and listens for the fare-search response. The first 200 JSON
is saved to data/samples/ (validating sniper.api.parse_fares against reality)
and all cookies are exported to data/cookies.json for the poller's httpx calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = "data/profile"
SAMPLES_DIR = Path("../data/samples") if Path(__file__).parent.name == "tools" else Path("data/samples")
COOKIES_OUT = Path("../data/cookies.json") if Path(__file__).parent.name == "tools" else Path("data/cookies.json")


async def main() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    captured = asyncio.Event()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_response(resp):
            if "journey-solution-option" not in resp.url or captured.is_set():
                return
            if resp.status != 200:
                print(f"[capture] {resp.status} from fare endpoint — keep browsing "
                      f"and retry the search")
                return
            body = await resp.json()
            out = SAMPLES_DIR / f"fare_response_{resp.request.post_data and 'dated' or 'plain'}.json"
            out.write_text(json.dumps(body, indent=1), encoding="utf-8")
            print(f"[capture] saved 200 response -> {out}")
            captured.set()

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        await page.goto("https://www.amtrak.com/home.html",
                        wait_until="domcontentloaded")
        print("Browser open. Log in if you like, then run your fare search "
              "(e.g. WAS → BOS). Waiting for the first 200 response…")
        try:
            await asyncio.wait_for(captured.wait(), timeout=600)
        except asyncio.TimeoutError:
            print("[capture] no 200 in 10 min; exporting cookies anyway")

        COOKIES_OUT.write_text(json.dumps(await ctx.cookies(), indent=1),
                               encoding="utf-8")
        print(f"[capture] cookies -> {COOKIES_OUT}")
        await ctx.close()

    # validate the parser immediately if we got a sample
    if captured.is_set():
        from sniper import api  # noqa: PLC0415
        raise SystemExit(api._selftest(str(SAMPLES_DIR)))


if __name__ == "__main__":
    asyncio.run(main())
