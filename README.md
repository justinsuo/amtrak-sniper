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
> price re-verified before paying), but the risk is yours. See
> [ARCHITECTURE.md](ARCHITECTURE.md#risk-acceptance-conscious-decision-not-an-accident).

**No AI or paid services needed — ever.** Everything below runs locally on
your computer. The only accounts involved are your own Amtrak account and
(optionally) Telegram.

## 1 · Install (about 5 minutes)

**Prerequisites**

- A Windows 10/11 or macOS computer with a display (the one-time warmup opens
  a real browser window). No Linux/headless servers for now.
- **Python 3.12 or newer** from <https://www.python.org/downloads/>.
  On Windows, tick **“Add Python to PATH”** during installation. Check with
  `python --version` in a terminal (Command Prompt / Terminal).

**Install**

```bash
git clone https://github.com/justinsuo/amtrak-sniper.git
cd amtrak-sniper
pip install -r requirements.txt
playwright install chromium
```

No git? Download the repo as a ZIP from GitHub (**Code → Download ZIP**),
unzip it, open a terminal in that folder, and run the last three commands.

**Start the interface**

```bash
python app.py
```

Open **http://localhost:7100** in your browser. Everything else happens there.

## 2 · Warmup (one time, 1 minute)

Click **“1 · Run warmup browser”** in the dashboard. A real Chromium window
opens and performs a search automatically:

1. **Log into amtrak.com** in that window (required for auto-buy). This is
   *your* account and *your* saved card; everything stays in the local
   browser profile under `data/profile/`. Also make sure a **credit card is
   saved in your Amtrak account** if you want auto-buy.
2. If it asks you to run a search manually, do any search once. The tool
   saves a real API response (to validate the fare parser) and exports the
   session cookies the poller needs.

Why this matters: Amtrak sits behind Akamai. A warm, human-looking session is
the difference between clean JSON and a wall of 403s. If your network is
heavily blocked, set `proxy` in Setup to a residential proxy.

## 3 · Alerts: nothing to set up (Telegram optional)

Every alert — fare hits, circuit-breaker halts, parser warnings, purchase
outcomes — appears in the web UI **Alerts** feed automatically. No account,
no app, nothing.

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

In **Setup**:

- **Route** — 3-letter Amtrak station codes. Examples: `WAS` Washington DC,
  `NYP` New York Penn, `BOS` Boston South Station, `CHI` Chicago, `PHL`
  Philadelphia 30th St. Find yours on amtrak.com's station search.
- **Dates** — comma-separated, `YYYY-MM-DD`, e.g. `2026-11-20, 2026-11-21`.
- **Max price** — your bid in USD, e.g. `59.99`.

Save, then click **“2 · Start monitor”**. The dashboard's **Setup checklist**
turns green as each stage completes.

**Want it to buy, not just alert?** Tick **Auto-buy** in Setup, and first
click the dashboard's **dry-run** button (“Test checkout flow — never pays”)
to watch it walk the purchase flow safely.

## 5 · Troubleshooting

| Symptom | What it means | What to do |
|---|---|---|
| Checklist stuck at “Run warmup” | No session cookies yet | Click **Run warmup browser**, complete a search in the opened window |
| Challenge streak climbs to 5/5 | Akamai is blocking your network | Re-run warmup. If it persists, set a residential `proxy` in Setup |
| No fares appear after warmup | Parser met a new response shape | It dumps the raw JSON to `data/raw/` and alerts — share that file when reporting an issue |
| “Not logged in” during checkout | Amtrak session expired | Re-run warmup and log in again |
| Auto-buy stopped mid-flow | Page didn't match expectations | Check the alert: it includes a screenshot path (`data/checkout_error_*.png`) and leaves the browser open for you |
| Bank 3DS prompt appears | Your bank wants approval | Approve it in the browser window within 10 minutes — the bot waits |
| UI shows “stopped” after a crash | Monitor process exited | Read `data/poller.log`, then click Start again |

Still stuck? Open an issue on GitHub with the checklist state and the last
lines of the **Incidents** page.

## 6 · FAQ

**Does the app store my credit card?** No. It only selects the card already
saved in your Amtrak account, inside your own browser profile. It never
types, reads, or stores card numbers.

**Does it need Telegram / any AI service?** No. Alerts show in the web UI.
Telegram is optional phone push. There is no AI anywhere in the code.

**Can it run with the browser closed?** The monitor needs no visible browser
(only the one-time warmup and checkout open windows). Keep `python app.py`
running and your computer awake; the UI's Start button manages the monitor.

**How often does it poll?** Median 180 s with ±30 % jitter, but the daily
request cap (400) takes precedence: with 4 dates that works out to ~14 min
between cycles. Raise the cap in `config.yaml` if you accept the extra load.

**Will it buy twice?** No. A SQLite ledger records purchases, and even an
uncertain outcome blocks a retry. It also re-verifies the price on the
payment page and aborts if Amtrak repriced above your bid.

**How do I update?** `git pull` in the repo folder, then restart `python
app.py`. Your `config.yaml` and `data/` are yours and untouched by updates.

**How do I uninstall?** Close the app, delete the folder. Nothing is
installed globally except Python packages (`pip uninstall httpx pyyaml
playwright flask` if you want those gone too).

## 7 · What was verified (and what wasn't)

Verified locally: unit tests for the fare parser, bid filtering, alert
dedup, purchase ledger and interval math; the full UI (dashboard, setup
validation, alerts feed, Start/Stop on Windows); automated warmup driving a
real Amtrak search.

Not verified end-to-end: a **live purchase**. The development machine's IP is
blocked by Akamai, so the checkout flow was written defensively against the
real UI but must be proven on your machine first — use the **dry-run**
button; it walks to the payment page without ever paying.

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
  state.py             # SQLite: price history, caps, alert dedup, ledger
  notify.py            # UI alert feed + optional Telegram + captcha hook
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
local browser profile. The web UI binds to `127.0.0.1` and has no login —
do not expose it to the internet.
