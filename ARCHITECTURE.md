# ARCHITECTURE — amtrak-sniper

## Mission recap

Watch Amtrak fares for one configured route + date window on amtrak.com. When any offered coach fare ≤ `max_price_usd`, proceed to checkout with the owner's own logged-in account and saved card, stopping at the payment/3DS step for human confirmation. One number in, everything else automated.

## Stack decision

**Python 3.12** (chosen over Node/TS):

- `httpx[http2]` for fare polling — the endpoint is a plain JSON POST (see `docs/endpoints.md`), and HTTP/2 + cookie reuse gets us close to browser-shaped traffic without a browser.
- **Playwright** for login-session warmup and checkout only. A persistent `user-data-dir` profile is the anti-block lever: the human logs in once, the profile accumulates a normal reputation, and both poller (cookies exported) and checkout (live browser) benefit.
- **SQLite** (`data/prices.db`) for poll state, price history, and the consecutive-challenge counter. Zero-ops, single-user, fits the scale.
- **PyYAML** config, **asyncio** loop. Telegram Bot API for alerts (one-way, no webhook — outbound POSTs only).

Node/TS would have been equally fine for Playwright; Python wins on httpx + SQLite + the owner's stated preference order in the spec.

## Component map

```
config.yaml          # single source of truth (route, dates, bid, cadence, caps)
sniper/
  poller.py          # asyncio loop: jittered interval, httpx POST, parse, threshold check
  checkout.py        # Playwright flow: warm profile → cart → checkout → STOP at 3DS
  state.py           # SQLite schema + dedup (don't re-alert on the same fare) + request counter
  notify.py          # Telegram send; solve_captcha hook lives here as an interface stub
data/
  prices.db          # state + price history
  incidents.log      # every challenge/403/429 event, one JSON line each
docs/endpoints.md    # Phase 0 recon — the contract poller.py is written against
```

## Traffic-shaping rules (hard requirements, from spec)

1. Poll interval: median 180 s, ±30% uniform jitter.
2. Daily cap: 400 fare-search calls/day, enforced in `state.py` (reset at local midnight).
3. Backoff: on 403/429/HTML-instead-of-JSON, exponential backoff starting at 5 min, doubling, capped at 2 h.
4. Circuit breaker: 5 consecutive challenges ⇒ stop polling entirely + Telegram alert + incident log. Human resumes manually.
5. First request of a session is a plain `GET /home.html` to set Akamai cookies before any API call (observed necessity, see endpoints.md §4).

## CAPTCHA / challenge policy

Escalate, never defeat. A challenge pauses the loop, fires a Telegram alert, keeps the browser session warm, and waits for the human. `notify.py` exposes a `solve_captcha` interface stub so an OCR plug-in can be added later; **no OCR solver in v1**.

## Risk acceptance (conscious decision, not an accident)

The owner accepts that automating purchases on amtrak.com may violate Amtrak's Terms of Service and could result in the account being flagged, temporarily suspended, or (worst case) banned. Mitigations chosen — single account, human-scale cadence, request caps, no CAPTCHA bypass, human-in-the-loop at payment — trade residual ToS risk for a far lower chance of detection than aggressive automation. If the account is suspended, tickets booked remain valid per Amtrak policy; the loss is the account itself. Accepted.

## Phase gates

- **Phase 0 (recon)** — `docs/endpoints.md`. Request schema captured live;
  response handled by a schema-tolerant parser with fail-loud raw dumps.
- **Phase 1 (monitor)** — poll → record → alert (web UI feed, Telegram optional).
- **Phase 2 (auto-buy, shipped)** — on a hit, Playwright drives the real UI to
  payment with the saved card in the user's Amtrak account. Guards: station
  codes verified before searching, no train-number guessing, price re-verified
  at payment (abort on reprice), once-per-train/date ledger (unknown outcomes
  block retries), bank 3DS pauses for human approval, and every failure leaves
  a screenshot plus an open browser for human takeover. A dashboard dry-run
  walks the same flow without ever clicking pay.
