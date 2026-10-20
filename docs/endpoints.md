# Amtrak fare endpoints — Phase 0 recon

Recon date: 2026-10-19. Method: live session in a real Chromium browser (in-app browser panel) on amtrak.com with network capture + in-page XHR/fetch hooks. Everything marked **OBSERVED** was captured verbatim from wire traffic; anything marked **PENDING** was not and must not be guessed.

## Verdict up front

- The fare search is a single internal JSON API: `POST https://www.amtrak.com/dotcom/journey-solution-option`. Request schema fully captured (**OBSERVED**). This is what the poller should call — no DOM scraping needed for monitoring.
- The official Open Data API is **not viable** for fares (see §3). Build the bot around the internal endpoint.
- Amtrak sits behind Akamai Bot Manager. From this machine the API consistently returned **HTTP 403** (6/6 attempts) while plain pages returned 200. One successful (200) response capture is still **PENDING** — see §5.

## 1. Internal fare-search API (**OBSERVED**)

| | |
|---|---|
| URL | `https://www.amtrak.com/dotcom/journey-solution-option` |
| Method | `POST` |
| Content-Type | `application/json` |
| Accept | `application/json, text/plain, */*` |
| Custom headers sent by the site | `x-amtrak-trace-id: <56 hex chars>` (per-request), `CSRF-Token` (sent with value `null` in anonymous state — likely populated after login) |
| Transport headers seen on retried calls | `traceparent`, `tracestate` (W3C trace context, added by the site's telemetry; optional for us) |

### Request body — captured verbatim (WAS → BOS, one adult, one-way)

This is the exact payload the amtrak.com home widget sends:

```json
{
  "journeyRequest": {
    "fare": { "pricingUnit": "DOLLARS" },
    "type": "OW",
    "journeyLegRequests": [
      {
        "origin": { "code": "WAS", "schedule": { "departureDateTime": "" } },
        "destination": { "code": "BOS" },
        "passengers": [ { "id": "P1", "type": "F", "initialType": "adult" } ]
      }
    ],
    "customer": { "tierStatus": "MEMBER" },
    "isPassRider": false,
    "isCorporateTraveller": false,
    "tripTags": true,
    "singleAdultFare": true,
    "cascadesWSDOTFilter": false,
    "xDelay": "60"
  },
  "initialJourneyLegOnly": false,
  "reservableAccomodationOptions": "ALL"
}
```

Field notes:

- `type: "OW"` = one-way. (`RT` presumed round-trip — **PENDING**, not observed.)
- `origin.code` / `destination.code` are the 3-letter Amtrak station codes.
- `schedule.departureDateTime`: the home-page widget sends `""` (it only pre-validates the OD pair; the dated search fires on the booking results page). Date format for real searches is **PENDING** a capture from the results page — likely ISO `YYYY-MM-DDTHH:mm:ss`, unverified.
- `passengers[].type: "F"` with `initialType: "adult"` = one full-fare adult.
- `customer.tierStatus: "MEMBER"` is sent even while logged out. Expect this to change with a logged-in session (Guest Rewards tier) — re-capture after login.
- `reservableAccomodationOptions: "ALL"` (their spelling) returns all accommodation classes; coach filtering happens client-side from the response.
- `xDelay: "60"` is a UI debounce artifact, almost certainly safe to drop.

### Response

- **Success (200) schema: PENDING.** Every attempt from this machine was blocked at the edge (see §4). Rather than guessing paths, `sniper/api.py` ships a schema-tolerant structural parser that validates every quote and fails loudly (raw dump + alert) on drift; the first real 200 (captured via `tools/warmup_capture.py`, §5) locks it in.
- **Blocked response (OBSERVED):** `HTTP 403`, `Content-Type: text/html`, an Amtrak-branded Akamai error page: "We are unable to complete your request at this time… Reference # 0.605ed617.1792458130.32ee7509". Treat any `text/html` response or 403/429 from this endpoint as a challenge incident (see `data/incidents.log`).

## 2. What the checkout flow will need (Phase 2, not yet built)

- Login state changes the request: `CSRF-Token` header and `customer.tierStatus` are populated post-login; cart/checkout endpoints are separate `/dotcom/...` calls that must be captured in a logged-in session. **PENDING** — do in Phase 2 recon with the human's account, in the warm browser profile.
- Payment step ends in 3DS — bot stops there by design; human confirms.

## 3. Official Open Data API — not viable (**OBSERVED** 2026-10-19)

- `https://developer.amtrak.com/` — connection fails (host does not answer). The developer portal is effectively dead; no public OAuth2 client-credentials registration was found.
- `https://api.amtrak.com/` — answers `HTTP 401` from `Server: AkamaiNetStorage` with a branded HTML error page. No public docs, no registration path.
- Historically this API exposed stations / train status / alerts — **not live fares**. Even with credentials it would not replace the internal endpoint for price watching.
- Third-party status feeds exist (e.g. api.amtraker.com for train positions) but carry **no fares**. Not useful for this project.

## 4. Bot-defense posture (**OBSERVED**)

| Client | `GET /home.html` | `POST /dotcom/journey-solution-option` |
|---|---|---|
| curl (any headers) | 403 or connection reset | not attempted — edge already blocks |
| Real Chromium, fresh profile (this machine) | 200 | **403, 6/6 attempts**, incl. the site's own XHRs |

Implications, folded into design:

- The block is edge-level (Akamai), IP/fingerprint-reputation based — there is no CAPTCHA to solve; "escalate to human" here means *run polls from a warm, normal-looking session and back off*, not "solve a puzzle".
- A persistent Playwright `user-data-dir` with real browsing history (human logs in once, browses a bit) is the single biggest lever. The poller should reuse that profile's cookies for its `httpx` calls.
- First poll of a session should be preceded by a plain `GET /home.html` to set Akamai cookies (`_abck` etc.), mimicking real navigation order.
- Backoff rules per spec apply verbatim: 429/403/html-response ⇒ exponential backoff; 5 consecutive challenges ⇒ stop polling, Telegram alert, log to `data/incidents.log`.

## 5. To finish Phase 0 — one successful capture (needs the human)

The request side is done; we need exactly one `200` from the endpoint to pin the response schema. Either:

1. **In the browser panel this agent left open**: browse amtrak.com normally for a minute or two (a few page views), then run the WAS→BOS search for 2026-11-20. If results render, tell me — I'll pull the captured response out of the network log. Or:
2. **In your own browser**: DevTools → Network → run the search → filter `journey-solution-option` → right-click → Copy → Copy as HAR (or "Save all as HAR with content") and drop the file in this workspace. I'll extract the schema and the dated-request format from it.

After that, Phase 1 (`poller.py`, `state.py`, `notify.py`) is pure plumbing.
