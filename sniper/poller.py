"""Deterministic fare-watching loop. No AI anywhere: config in, alerts out.

Cadence: the spec's 180 s median and 400 calls/day cap conflict for a 4-date
window (86400/180 * 4 = 1920 calls/day). Resolved deterministically:
    interval = max(poll_median_seconds, n_dates * 86400 / daily_request_cap)
then ±jitter_fraction uniform jitter per cycle.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from . import api, notify, state

BOOKING_URL = "https://www.amtrak.com/home.html"
BACKOFF_BASE_S = 300          # 5 min, doubling per consecutive challenge
BACKOFF_CAP_S = 7200          # 2 h
CIRCUIT_BREAK_AFTER = 5


def load_config(path: str = "config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cfg["_raw_dir"] = "data/raw"
    return cfg


def effective_interval(cfg: dict) -> float:
    n = max(1, len(cfg["dates"]))
    floor_s = n * 86400.0 / float(cfg["daily_request_cap"])
    return max(float(cfg["poll_median_seconds"]), floor_s)


def jittered(base_s: float, frac: float) -> float:
    return base_s * random.uniform(1 - frac, 1 + frac)


def log_incident(kind: str, detail: str) -> None:
    p = Path("data/incidents.log")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "type": kind, "detail": detail}) + "\n")


def load_cookies(client: httpx.AsyncClient, path: str = "data/cookies.json") -> int:
    """Import cookies exported by tools/warmup_capture.py (Playwright format)."""
    p = Path(path)
    if not p.exists():
        return 0
    n = 0
    for c in json.loads(p.read_text(encoding="utf-8")):
        client.cookies.set(c["name"], c["value"],
                           domain=c.get("domain", ""), path=c.get("path", "/"))
        n += 1
    return n


async def poll_once(client: httpx.AsyncClient, cfg: dict, st: state.State) -> bool:
    """One full cycle over all dates. Returns False if circuit must stay open."""
    for travel_date in cfg["dates"]:
        if st.requests_today() >= int(cfg["daily_request_cap"]):
            print("[poller] daily request cap reached; sleeping to midnight")
            await sleep_until_midnight()
        try:
            quotes = await api.fetch_fares(client, cfg, travel_date)
        except (api.ChallengeError, httpx.HTTPError) as e:
            # edge 403/429 AND raw network failures (reset/tarpit/timeout)
            # all count as challenges — never crash the loop on them
            status = getattr(e, "status", None)
            streak = st.bump_challenge()
            st.record_poll(travel_date, False, status, None, None, repr(e))
            log_incident("challenge", f"{e!r} streak={streak}")
            st.bump_requests()
            if streak >= CIRCUIT_BREAK_AFTER:
                await notify.send_telegram(
                    cfg, f"🛑 amtrak-sniper: {streak} consecutive challenges "
                         f"(last: {e!r}). Polling halted. "
                         f"Re-warm the session (tools/auto_warmup.py), then restart.")
                return False
            wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (streak - 1)))
            print(f"[poller] challenge #{streak}, backing off {wait}s")
            await asyncio.sleep(wait)
            try:
                await api.warmup(client)  # re-prime cookies before next attempt
            except (api.ChallengeError, httpx.HTTPError):
                pass
            return True  # next cycle retries
        except api.SchemaDriftError as e:
            st.bump_requests()  # it was a real (200) request — count it
            st.record_poll(travel_date, False, 200, None, None, str(e))
            log_incident("schema_drift", str(e))
            await notify.send_telegram(
                cfg, f"⚠️ amtrak-sniper: response schema drift on {travel_date} — "
                     f"{e.reason}. Raw response saved; parser needs review.")
            continue

        st.bump_requests()
        st.reset_challenge()
        st.record_prices(quotes)
        cheapest = min(q.price_cents for q in quotes)
        st.record_poll(travel_date, True, 200, len(quotes), cheapest)
        print(f"[poller] {travel_date}: {len(quotes)} offers, "
              f"min ${cheapest / 100:.2f}")

        for hit in api.filter_hits(quotes, cfg):
            if not st.should_alert(hit):
                continue
            msg = (f"🎯 Fare hit: ${hit.price_usd:.2f} {hit.fare_class or 'coach'} "
                   f"on {hit.travel_date}, train {hit.train_number} "
                   f"({hit.departs} → {hit.arrives}). Book: {BOOKING_URL}")
            if cfg.get("auto_checkout"):
                from . import checkout
                msg += "\nauto_checkout=true: attempting purchase up to 3DS…"
                await notify.send_telegram(cfg, msg)
                await checkout.attempt_checkout(cfg, hit)
            else:
                await notify.send_telegram(cfg, msg)
    return True


async def sleep_until_midnight() -> None:
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0,
                           microsecond=0).timestamp() + 86400
    await asyncio.sleep(max(60, midnight - now.timestamp()))


async def run(config_path: str = "config.yaml") -> None:
    cfg = load_config(config_path)
    st = state.State()
    interval = effective_interval(cfg)
    if interval > float(cfg["poll_median_seconds"]):
        print(f"[poller] {len(cfg['dates'])} dates × cap {cfg['daily_request_cap']}/day "
              f"⇒ effective interval {interval:.0f}s (median request would exceed cap)")
    limits = httpx.Limits(max_connections=2)
    proxy = cfg.get("proxy") or None
    async with httpx.AsyncClient(http2=True, timeout=30, limits=limits,
                                 proxy=proxy,
                                 follow_redirects=True) as client:
        warmed = False
        try:
            n = load_cookies(client)
            await api.warmup(client)
            warmed = True
            print(f"[poller] warmup ok ({n} cookies imported)")
        except (api.ChallengeError, httpx.HTTPError) as e:
            log_incident("warmup_block", repr(e))
            print(f"[poller] warmup blocked ({e!r}); will retry in loop")
        while True:
            if not warmed:
                try:
                    await api.warmup(client)
                    warmed = True
                except (api.ChallengeError, httpx.HTTPError):
                    await asyncio.sleep(BACKOFF_BASE_S)
                    continue
            keep_going = await poll_once(client, cfg, st)
            if not keep_going:
                return  # circuit breaker open; human restarts after re-warming
            nap = jittered(interval, float(cfg["jitter_fraction"]))
            print(f"[poller] sleeping {nap:.0f}s")
            await asyncio.sleep(nap)


if __name__ == "__main__":
    asyncio.run(run())
