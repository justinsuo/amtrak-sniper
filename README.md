# 🚄 amtrak-sniper

Watches Amtrak fares for your route and dates, and alerts you — in the web
UI, plus Telegram if you want phone push — the moment a fare drops to your
bid price. Turn on **auto-buy** and it completes the purchase with the card
saved in your Amtrak account — pausing only for your bank's 3DS approval.
Your card never lives in this app. Pure Python, no AI, on your own machine.

```
You set one number (max price) → the bot does everything else.
```

> ⚠️ **Read first.** Automating purchases on amtrak.com may violate Amtrak's
> Terms of Service and can get your account flagged or suspended. This tool is
> built to be polite (human-scale polling, request caps, no CAPTCHA bypass,
> human confirms payment), but the risk is yours. See
> [ARCHITECTURE.md](ARCHITECTURE.md#risk-acceptance-conscious-decision-not-an-accident).

## 1 · Install (2 minutes)

Requirements: Python 3.12+ and a desktop with a display for the one-time warmup.

```bash
git clone <this-repo> && cd amtrak-sniper
pip install -r requirements.txt
playwright install chromium
python app.py
```

Open **http://localhost:7100** — the web UI walks you through the rest.

## 2 · Warmup (one time, 1 minute)

Click **“1 · Run warmup browser”** in the UI. A real Chromium window opens:

1. (Recommended) Log into amtrak.com — this is *your* account and *your* saved
   card; everything stays in the local browser profile under `data/profile/`.
2. Run any fare search once. The tool saves a real API response (to validate
   the fare parser) and exports session cookies for the poller.

Why this matters: Amtrak sits behind Akamai. A warm, human-looking session is
the difference between clean JSON and a wall of 403s. If your IP is heavily
blocked, set `proxy` in Setup to a residential proxy.

## 3 · Alerts: nothing to set up (Telegram optional)

Every alert — fare hits, circuit-breaker halts, parser warnings — appears in
the web UI **Alerts** feed automatically. No account, no app, nothing.

Want push notifications on your phone too? Two ways:

- **Your own bot (2 min):** message **@BotFather** → `/newbot` → copy the
  token; press **Start** on your new bot; read your chat id from
  `https://api.telegram.org/bot<TOKEN>/getUpdates`; paste both into Setup.
- **A shared bot (30 s):** one bot can alert many people. Press **Start** on
  the shared bot, put *your* chat id in Setup, and have the owner send you
  the bot token **privately** — never commit it to git. Whoever holds a bot
  token controls that bot (the owner can revoke it anytime via
  @BotFather → `/revoke`).

## 4 · Set your bid & start

In **Setup**: route (station codes), travel dates, max price. Save, then click
**“2 · Start monitor”**. That's it — fares land in your Telegram.

Want it to **buy**, not just alert? Tick **Auto-buy** in Setup. First click the
dashboard's **dry-run** button to watch it walk the flow without paying.

## How it works

- **JSON, not DOM.** The poller calls Amtrak's internal fare-search endpoint
  directly (`POST /dotcom/journey-solution-option` — documented with a real
  captured request in [docs/endpoints.md](docs/endpoints.md)). No scraping.
- **Discipline by design:** poll median 180 s with ±30% jitter, 400
  requests/day cap, exponential backoff on 403/429, and a 5-strike circuit
  breaker that halts polling and alerts you instead of hammering.
- **Schema-tolerant parser:** if Amtrak changes the response shape, the bot
  dumps the raw payload and alerts you instead of misparsing a price.
- **Auto-buy, fail-safe:** on a fare hit it drives the real UI — search →
  pick train → cart → payment — selects the card **saved in your Amtrak
  account**, re-verifies the total is still ≤ your bid before paying, buys
  once per train/date (ledger in SQLite), and pauses for your bank's 3DS
  approval. Any unexpected page → screenshot + alert + browser left open for
  you. Prove it safely first with the dashboard's **dry-run** button (goes to
  the payment page, never clicks pay).

## Repo map

```
app.py                 # web UI (dashboard / setup / controls)
config.example.yaml    # copy → config.yaml (git-ignored, stays local)
sniper/
  poller.py            # the deterministic watch loop
  api.py               # fare endpoint client + tolerant parser
  state.py             # SQLite: price history, caps, alert dedup
  notify.py            # Telegram + solve_captcha hook (no OCR solver)
  checkout.py          # auto-buy flow: saved card, price re-verify, 3DS pause
tools/
  auto_warmup.py       # hands-free warmup + response capture
  warmup_capture.py    # manual variant (drive the search yourself)
docs/endpoints.md      # the captured API contract
ARCHITECTURE.md        # design, traffic rules, risk acceptance
```

## Privacy

Everything runs locally. `config.yaml` (Telegram token, chat id), `data/`
(cookies, price DB, browser profile) are git-ignored and never leave your
machine. Your Amtrak login and card live only in Amtrak's own site and your
local browser profile.
