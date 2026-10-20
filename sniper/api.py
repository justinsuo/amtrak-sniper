"""Fare API client + schema-tolerant parser. Deterministic, no AI.

Request contract: OBSERVED on 2026-10-19 (docs/endpoints.md).
Response contract: NOT yet observed. parse_fares() therefore walks the JSON
structurally instead of hard-coding paths, validates every quote it emits,
and raises SchemaDriftError (with a raw dump) rather than guessing.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

ENDPOINT = "https://www.amtrak.com/dotcom/journey-solution-option"
WARMUP_URL = "https://www.amtrak.com/home.html"

# Browser-shaped headers; x-amtrak-trace-id mimics the observed 56-hex format.
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}


class ChallengeError(Exception):
    """403/429/HTML-from-API: Akamai edge block. Caller backs off / breaks circuit."""
    def __init__(self, status: int, reference: str = ""):
        self.status = status
        self.reference = reference
        super().__init__(f"challenge: HTTP {status} ref={reference}")


class SchemaDriftError(Exception):
    """Response parsed but failed validation, or nothing quote-like found."""
    def __init__(self, reason: str, raw_path: Optional[Path] = None):
        self.reason = reason
        self.raw_path = raw_path
        super().__init__(f"schema drift: {reason} (raw: {raw_path})")


@dataclass
class FareQuote:
    travel_date: str           # YYYY-MM-DD requested
    train_number: str
    departs: str               # ISO-ish string as found
    arrives: str
    fare_class: str            # e.g. "Coach" / "" if unknown
    price_cents: int
    seats_available: Optional[int] = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def price_usd(self) -> float:
        return self.price_cents / 100.0


# ---------------------------------------------------------------- request side

def build_payload(cfg: dict, travel_date: str) -> dict:
    """Exact structure captured from amtrak.com's own XHR (docs/endpoints.md).

    departureDateTime format (ISO midnight) is UNVERIFIED until the first
    200 response; if the server rejects it, that response tells us the real
    format and only this function changes.
    """
    return {
        "journeyRequest": {
            "fare": {"pricingUnit": "DOLLARS"},
            "type": "OW",
            "journeyLegRequests": [{
                "origin": {"code": cfg["route"]["from_code"],
                           "schedule": {"departureDateTime": f"{travel_date}T00:00:00"}},
                "destination": {"code": cfg["route"]["to_code"]},
                "passengers": [{"id": "P1", "type": "F", "initialType": "adult"}],
            }],
            "customer": {"tierStatus": "MEMBER"},
            "isPassRider": False,
            "isCorporateTraveller": False,
            "tripTags": True,
            "singleAdultFare": True,
            "cascadesWSDOTFilter": False,
            "xDelay": "60",
        },
        "initialJourneyLegOnly": False,
        "reservableAccomodationOptions": "ALL",  # sic — their spelling
    }


async def warmup(client: httpx.AsyncClient) -> None:
    """Prime Akamai cookies the way real navigation does (observed necessity)."""
    r = await client.get(WARMUP_URL, headers={**BASE_HEADERS,
                                              "Accept": "text/html,application/xhtml+xml"})
    if r.status_code != 200:
        raise ChallengeError(r.status_code, _akamai_ref(r.text))


async def fetch_fares(client: httpx.AsyncClient, cfg: dict, travel_date: str) -> list[FareQuote]:
    """One fare-search call. Raises ChallengeError or SchemaDriftError."""
    headers = {**BASE_HEADERS, **API_HEADERS,
               "x-amtrak-trace-id": secrets.token_hex(28)}
    r = await client.post(ENDPOINT, headers=headers,
                          json=build_payload(cfg, travel_date))
    ct = (r.headers.get("content-type") or "").lower()
    if r.status_code != 200 or "json" not in ct:
        raise ChallengeError(r.status_code, _akamai_ref(r.text))
    payload = r.json()
    quotes = parse_fares(payload, travel_date)
    if not quotes:
        raw = dump_raw(payload, cfg.get("_raw_dir", "data/raw"), travel_date)
        raise SchemaDriftError("no quote-like objects in 200 response", raw)
    return quotes


def dump_raw(payload: Any, raw_dir: str, tag: str) -> Path:
    p = Path(raw_dir)
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{datetime.now():%Y%m%d-%H%M%S}_{tag}.json"
    f.write_text(json.dumps(payload, indent=1)[:2_000_000], encoding="utf-8")
    return f


def _akamai_ref(html: str) -> str:
    m = re.search(r"Reference #\s*<span>([^<]+)</span>", html or "")
    return m.group(1) if m else ""


# --------------------------------------------------------------- parsing side
# Structural extraction: find train-ish and price-ish objects anywhere in the
# JSON, link fares to their nearest train-ish ancestor, then validate.

_TRAIN_KEYS = re.compile(r"^(trainNumbers?|train|number|serviceNumber)$", re.I)
_TIME_KEYS = re.compile(r"(departure|arrival|depart|arrive).*(date|time)|"
                        r"^(departureDateTime|arrivalDateTime|departsAt|arrivesAt)$", re.I)
_PRICE_KEYS = re.compile(r"^(price|fare|amount|total|cost|lowestFare|fareAmount)$", re.I)
_MONEY_KEYS = re.compile(r"^(dollars?|cents?|value|amount|total)$", re.I)
_CLASS_KEYS = re.compile(r"(accommodat|class|cabin|fareFamily|fareClass|service)", re.I)
_SEAT_KEYS = re.compile(r"(seats?\s*(available|remaining)|availableSeats|seatsLeft|inventory)", re.I)


def parse_fares(payload: Any, travel_date: str) -> list[FareQuote]:
    parents: dict[int, Optional[dict]] = {}
    dicts: list[dict] = []

    def walk(node: Any, parent: Optional[dict]) -> None:
        if isinstance(node, dict):
            dicts.append(node)
            parents[id(node)] = parent
            for v in node.values():
                walk(v, node)
        elif isinstance(node, list):
            for v in node:
                walk(v, parent)

    walk(payload, None)

    def is_numbery(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def money_cents(d: dict) -> Optional[int]:
        for k, v in d.items():
            if _PRICE_KEYS.match(k):
                if is_numbery(v):
                    return int(round(v * 100)) if v < 10000 else int(v)
                if isinstance(v, dict):
                    dollars = cents = None
                    for k2, v2 in v.items():
                        if not _MONEY_KEYS.match(k2) or not is_numbery(v2):
                            continue
                        lk = k2.lower()
                        if lk.startswith("cent"):
                            cents = int(v2)
                        elif lk.startswith("dollar") or lk in ("value", "amount", "total"):
                            dollars = int(round(v2 * 100)) if v2 < 10000 else int(v2)
                    out = cents if cents is not None else dollars
                    if out is not None:
                        return out
        return None

    def first_str(d: dict, rx: re.Pattern) -> str:
        for k, v in d.items():
            if rx.search(k) and isinstance(v, str) and v:
                return v
        return ""

    def nearest_train(d: dict) -> tuple[str, str, str]:
        node: Optional[dict] = d
        for _ in range(6):  # walk up to 6 ancestors
            if node is None:
                break
            train = ""
            for k, v in node.items():
                if _TRAIN_KEYS.match(k):
                    if isinstance(v, list) and v and isinstance(v[0], (str, int)):
                        train = str(v[0])
                    elif isinstance(v, (str, int)):
                        train = str(v)
                    if train:
                        break
            if train:
                dep = first_str(node, re.compile(r"depart|departure", re.I))
                arr = first_str(node, re.compile(r"arriv|arrival", re.I))
                return train, dep, arr
            node = parents.get(id(node))
        return "", "", ""

    quotes: list[FareQuote] = []
    seen: set[tuple] = set()
    for d in dicts:
        cents = money_cents(d)
        if cents is None or not (100 <= cents <= 200_000):  # $1 .. $2000 sanity
            continue
        train, dep, arr = nearest_train(d)
        if not train:
            continue
        cls = first_str(d, _CLASS_KEYS)
        seats = None
        for k, v in d.items():
            if _SEAT_KEYS.search(k) and is_numbery(v):
                seats = int(v)
                break
        key = (train, dep, cls, cents)
        if key in seen:
            continue
        seen.add(key)
        quotes.append(FareQuote(travel_date=travel_date, train_number=train,
                                departs=dep, arrives=arr, fare_class=cls,
                                price_cents=cents, seats_available=seats, raw=d))

    # Whole-batch sanity: a real WAS-BOS day has a handful of trains, not 4000.
    if len(quotes) > 500:
        raise SchemaDriftError(f"implausible quote count {len(quotes)}")
    return quotes


def filter_hits(quotes: list[FareQuote], cfg: dict) -> list[FareQuote]:
    """Deterministic threshold check: coach-ish class, seats, price <= bid."""
    bid_cents = int(round(float(cfg["max_price_usd"]) * 100))
    want_cls = str(cfg.get("seat_class", "coach")).lower()
    min_seats = int(cfg.get("min_seats_available", 1))
    hits = []
    for q in quotes:
        if q.price_cents > bid_cents:
            continue
        if q.fare_class and want_cls not in q.fare_class.lower():
            continue  # class info present but not the one we want
        if q.seats_available is not None and q.seats_available < min_seats:
            continue
        hits.append(q)
    return sorted(hits, key=lambda q: (q.price_cents, q.departs))


# ------------------------------------------------------------------- selftest

def _selftest(sample_dir: str = "data/samples") -> int:
    """Parse every captured response in data/samples/ and print quotes."""
    p = Path(sample_dir)
    files = sorted(p.glob("*.json")) if p.is_dir() else []
    if not files:
        print(f"no samples in {p}/ — run tools/warmup_capture.py once from a "
              f"working session (or drop a HAR-extracted response JSON there)")
        return 1
    ok = True
    for f in files:
        try:
            quotes = parse_fares(json.loads(f.read_text(encoding="utf-8")), "sample")
            print(f"\n{f.name}: {len(quotes)} quotes")
            for q in quotes[:20]:
                print(f"  train {q.train_number:>6}  {q.departs} -> {q.arrives}  "
                      f"{q.fare_class or '?':<12} ${q.price_usd:7.2f}  seats={q.seats_available}")
        except Exception as e:  # noqa: BLE001 - selftest reports, doesn't crash
            ok = False
            print(f"\n{f.name}: FAILED — {e}")
    return 0 if ok else 2


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
