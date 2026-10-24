"""amtrak-sniper web interface — one file, no build step.

    python app.py            # serves http://localhost:7100
    PORT=8080 python app.py  # or python app.py --port 8080

Pages: dashboard (prices/status), setup (writes config.yaml),
controls (warmup / start / stop monitor), incidents log.
The app never sees Amtrak or card credentials: login happens in the
user's own Playwright browser profile, payment ends at human 3DS.
"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from flask import Flask, redirect, render_template_string, request, url_for

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
EXAMPLE = ROOT / "config.example.yaml"
DB = ROOT / "data" / "prices.db"
INCIDENTS = ROOT / "data" / "incidents.log"
PIDFILE = ROOT / "data" / "poller.pid"

app = Flask(__name__)

# --------------------------------------------------------------------- helpers

def load_cfg() -> dict:
    if CONFIG.exists():
        return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if EXAMPLE.exists():
        return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return {}


def save_cfg(cfg: dict) -> None:
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    tmp.replace(CONFIG)


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    if not DB.exists():
        return []
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def poller_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
    except Exception:
        return None
    if os.name == "nt":  # os.kill(pid, 0) is unreliable on Windows
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True)
        return pid if str(pid) in r.stdout else None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def warmup_status() -> dict:
    """What the new user needs to know after clicking 'Run warmup'."""
    cookies = ROOT / "data" / "cookies.json"
    result = ROOT / "data" / "warmup_result.json"
    samples = list((ROOT / "data" / "samples").glob("*.json")) if (ROOT / "data" / "samples").is_dir() else []
    out = {"cookies": 0, "sample": bool(samples), "result": None}
    if cookies.exists():
        import json
        try:
            out["cookies"] = len(json.loads(cookies.read_text()))
        except ValueError:
            pass
    if result.exists():
        import json
        try:
            out["result"] = json.loads(result.read_text()).get("status")
        except ValueError:
            pass
    return out


# ----------------------------------------------------------------------- pages

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>amtrak-sniper</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{font:15px/1.5 system-ui,sans-serif;background:#0f1216;color:#e6e9ee;max-width:980px;margin:0 auto;padding:24px}
h1{font-size:20px}h2{font-size:15px;color:#8b95a3;text-transform:uppercase;letter-spacing:.08em;margin:28px 0 10px}
a{color:#6ab0ff}.card{background:#171c23;border:1px solid #262d38;border-radius:10px;padding:16px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.big{font-size:26px;font-weight:650}.ok{color:#4ade80}.warn{color:#fbbf24}.bad{color:#f87171}.dim{color:#8b95a3}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:6px 8px;border-bottom:1px solid #262d38;text-align:left}
input,select{background:#0f1216;color:#e6e9ee;border:1px solid #333c4a;border-radius:6px;padding:7px 9px;width:100%}
label{font-size:12px;color:#8b95a3;display:block;margin:10px 0 4px}
button,.btn{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:#262d38}
button.danger{background:#b91c1c}
nav a{margin-right:14px;text-decoration:none}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row>div{flex:1;min-width:150px}
code{background:#0f1216;padding:2px 6px;border-radius:5px;font-size:13px}
</style></head><body>
<h1>🚄 amtrak-sniper</h1>
<nav><a href="{{ url_for('dashboard') }}">Dashboard</a><a href="{{ url_for('setup') }}">Setup</a><a href="{{ url_for('incidents') }}">Incidents</a></nav>
{{ body }}
</body></html>"""

DASH = """
<div class="grid">
 <div class="card"><div class="dim">Lowest fare seen</div>
  <div class="big {{ 'ok' if lowest and lowest*100 <= cfg.max_price_usd*100 else '' }}">
  {{ ('$%.2f'|format(lowest/100)) if lowest else '—' }}</div>
  <div class="dim">bid ≤ ${{ '%.2f'|format(cfg.max_price_usd or 0) }}</div></div>
 <div class="card"><div class="dim">Requests today</div><div class="big">{{ req_today }}</div>
  <div class="dim">cap {{ cfg.daily_request_cap }}</div></div>
 <div class="card"><div class="dim">Challenge streak</div>
  <div class="big {{ 'ok' if streak == 0 else ('warn' if streak < 5 else 'bad') }}">{{ streak }}/5</div>
  <div class="dim">{{ 'healthy' if streak == 0 else 'Akamai blocking — re-warm or proxy' }}</div></div>
 <div class="card"><div class="dim">Monitor process</div>
  <div class="big {{ 'ok' if running else 'dim' }}">{{ 'running' if running else 'stopped' }}</div>
  <div class="dim">route {{ cfg.route.from_code if cfg.route else '?' }} → {{ cfg.route.to_code if cfg.route else '?' }}</div></div>
</div>

<h2>Setup checklist</h2>
<div class="card"><table>
<tr><td>{{ '✅' if steps.config else '⬜' }}</td><td>Configure route, dates &amp; bid</td>
<td class="dim">{% if steps.config %}saved{% else %}<a href="{{ url_for('setup') }}">open Setup</a>{% endif %}</td></tr>
<tr><td>{{ '✅' if steps.warmup else '⬜' }}</td><td>Run warmup browser once</td>
<td class="dim">{% if steps.warmup %}{{ ws.cookies }} cookies imported{% if ws.sample %}, parser validated on real response{% endif %}{% else %}button below — log into Amtrak in the window that opens{% endif %}</td></tr>
<tr><td>{{ '✅' if steps.first_poll else '⬜' }}</td><td>First successful poll</td>
<td class="dim">{% if steps.first_poll %}fares flowing{% elif streak >= 3 %}Akamai is blocking this network — re-warm, or set a proxy in Setup{% else %}starts after warmup{% endif %}</td></tr>
<tr><td>{{ '✅' if running else '⬜' }}</td><td>Monitor running</td>
<td class="dim">{{ 'every ~15 min' if running else 'button below' }}</td></tr>
</table></div>

<h2>Controls</h2>
<div class="card"><div class="row">
 <form method="post" action="{{ url_for('warmup') }}"><button class="secondary">1 · Run warmup browser</button></form>
 {% if running %}
 <form method="post" action="{{ url_for('stop') }}"><button class="danger">Stop monitor</button></form>
 {% else %}
 <form method="post" action="{{ url_for('start') }}"><button>2 · Start monitor</button></form>
 {% endif %}
</div><p class="dim">Warmup opens a real browser once (log into Amtrak there — your account,
your saved card; the bot never sees card numbers). Then start the monitor.</p></div>

<h2>Auto-buy</h2>
<div class="card"><div class="row">
 <div><span class="dim">Status:</span>
  {% if cfg.auto_checkout %}<span class="ok">ON</span> — buys at ≤ ${{ '%.2f'|format(cfg.max_price_usd or 0) }} with your saved Amtrak card, stops for bank 3DS
  {% else %}<span class="dim">OFF</span> — alerts only; enable in <a href="{{ url_for('setup') }}">Setup</a>{% endif %}</div>
 <div><form method="post" action="{{ url_for('checkout_test') }}"><button class="secondary">Test checkout flow (dry run — never pays)</button></form></div>
</div>
{% if co %}<p class="dim">Last checkout run: {{ co.ts[5:16] if co.ts else '' }} — <b>{{ co.status }}</b>{% if co.reason %} ({{ co.reason }}){% endif %}{% if co.pnr %} · PNR <b>{{ co.pnr }}</b>{% endif %}</p>{% endif %}
{% if purchases %}
<table><tr><th>Time</th><th>Date</th><th>Train</th><th>Price</th><th>PNR</th><th>Status</th></tr>
{% for p in purchases %}<tr><td class="dim">{{ p.ts[5:16] }}</td><td>{{ p.travel_date }}</td><td>{{ p.train_number }}</td>
<td>{{ ('$%.2f'|format(p.price_cents/100)) if p.price_cents else '' }}</td><td>{{ p.pnr or '—' }}</td>
<td class="{{ 'ok' if p.status == 'confirmed' else 'warn' }}">{{ p.status }}</td></tr>{% endfor %}</table>
{% endif %}</div>

<h2>Alerts</h2>
<div class="card"><table><tr><th>Time</th><th>Alert</th></tr>
{% for a in alerts %}<tr><td class="dim" style="white-space:nowrap">{{ a.ts[5:16] }}</td><td>{{ a.text }}</td></tr>
{% else %}<tr><td colspan="2" class="dim">No alerts yet. Alerts appear here even without Telegram configured.</td></tr>
{% endfor %}</table></div>

<h2>Latest fares</h2>
<div class="card"><table><tr><th>Seen</th><th>Date</th><th>Train</th><th>Departs</th><th>Class</th><th>Price</th><th>Seats</th></tr>
{% for r in prices %}<tr><td class="dim">{{ r.ts[5:16] }}</td><td>{{ r.travel_date }}</td>
<td>{{ r.train_number }}</td><td>{{ (r.departs or '')[11:16] }}</td><td>{{ r.fare_class }}</td>
<td class="{{ 'ok' if r.price_cents <= cfg.max_price_usd*100 }}">${{ '%.2f'|format(r.price_cents/100) }}</td>
<td>{{ r.seats_available if r.seats_available is not none else '—' }}</td></tr>
{% else %}<tr><td colspan="7" class="dim">No fares recorded yet — run warmup, then start the monitor.</td></tr>
{% endfor %}</table></div>

<h2>Recent polls</h2>
<div class="card"><table><tr><th>Time</th><th>Date</th><th>OK</th><th>HTTP</th><th>Offers</th><th>Min $</th><th>Note</th></tr>
{% for p in polls %}<tr><td class="dim">{{ p.ts[5:16] }}</td><td>{{ p.travel_date }}</td>
<td>{{ '✓' if p.ok else '✗' }}</td><td>{{ p.http_status or '' }}</td><td>{{ p.n_offers if p.n_offers is not none else '' }}</td>
<td>{{ ('%.2f'|format(p.min_price_cents/100)) if p.min_price_cents else '' }}</td>
<td class="dim">{{ (p.note or '')[:60] }}</td></tr>
{% else %}<tr><td colspan="7" class="dim">No polls yet.</td></tr>
{% endfor %}</table></div>
"""

SETUP = """
<div class="card"><form method="post">
{% if err %}<p class="bad">{{ err }}</p>{% endif %}
<div class="row"><div><label>From (station code)</label><input name="from_code" value="{{ cfg.route.from_code if cfg.route else '' }}"></div>
<div><label>To (station code)</label><input name="to_code" value="{{ cfg.route.to_code if cfg.route else '' }}"></div>
<div><label>Seat class</label><input name="seat_class" value="{{ cfg.seat_class or 'coach' }}"></div></div>
<label>Travel dates (comma-separated, YYYY-MM-DD)</label>
<input name="dates" value="{{ cfg.dates|join(', ') }}">
<div class="row"><div><label>Max price (USD)</label><input name="max_price_usd" value="{{ cfg.max_price_usd }}"></div>
<div><label>Poll median (seconds)</label><input name="poll_median_seconds" value="{{ cfg.poll_median_seconds or 180 }}"></div>
<div><label>Daily request cap</label><input name="daily_request_cap" value="{{ cfg.daily_request_cap or 400 }}"></div></div>
<label>Telegram bot token <b>optional</b> — alerts also show on this dashboard</label>
<input name="bot_token" value="{{ cfg.notify.bot_token if cfg.notify else '' }}" placeholder="leave empty for UI-only alerts">
<label>Telegram chat id (optional)</label>
<input name="chat_id" value="{{ cfg.notify.chat_id if cfg.notify else '' }}" placeholder="your chat id, or the shared bot owner's">
<label>Proxy (optional, e.g. http://user:pass@host:port)</label>
<input name="proxy" value="{{ cfg.proxy or '' }}">
<label style="margin-top:14px"><input type="checkbox" name="auto_checkout" style="width:auto" {% if cfg.auto_checkout %}checked{% endif %}>
<b>Auto-buy</b> when a fare ≤ bid is found (uses the saved card in your Amtrak account; stops for bank 3DS approval)</label>
<p style="margin-top:16px"><button type="submit">Save config.yaml</button>
<a class="btn secondary" href="{{ url_for('dashboard') }}">Cancel</a></p>
<p class="dim">Secrets stay in <code>config.yaml</code> on this machine — it's git-ignored and never uploaded.</p>
</form></div>
"""

INCIDENTS_PAGE = """
<div class="card"><table><tr><th>Time</th><th>Type</th><th>Detail</th></tr>
{% for i in rows %}<tr><td class="dim">{{ i.ts[5:19] if i.ts else '' }}</td><td>{{ i.type }}</td><td class="dim">{{ i.detail[:120] }}</td></tr>
{% else %}<tr><td colspan="3" class="dim">No incidents logged. 🎉</td></tr>
{% endfor %}</table></div>
"""


def render(body: str, **kw):
    return render_template_string(PAGE.replace("{{ body }}", body), **kw)


@app.route("/")
def dashboard():
    cfg = load_cfg()
    prices = query("SELECT * FROM prices ORDER BY id DESC LIMIT 50")
    polls = query("SELECT * FROM polls ORDER BY id DESC LIMIT 15")
    try:
        alerts = query("SELECT * FROM alerts_feed ORDER BY id DESC LIMIT 20")
    except sqlite3.OperationalError:
        alerts = []
    lowest = query("SELECT MIN(price_cents) m FROM prices")[0]["m"] if DB.exists() else None
    req_today = 0
    streak = 0
    if DB.exists():
        from datetime import date
        r = query("SELECT value FROM meta WHERE key=?", (f"req_{date.today():%Y-%m-%d}",))
        req_today = int(r[0]["value"]) if r else 0
        r = query("SELECT value FROM meta WHERE key='challenge_streak'")
        streak = int(r[0]["value"]) if r else 0
    steps = {
        "config": CONFIG.exists(),
        "warmup": warmup_status()["cookies"] > 0,
        "first_poll": bool(query("SELECT 1 FROM polls WHERE ok=1 LIMIT 1")),
    }
    try:
        purchases = query("SELECT * FROM purchases ORDER BY id DESC LIMIT 10")
    except sqlite3.OperationalError:
        purchases = []
    co = None
    co_file = ROOT / "data" / "checkout_state.json"
    if co_file.exists():
        import json
        try:
            co = json.loads(co_file.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return render(DASH, cfg=cfg, prices=prices, polls=polls, alerts=alerts,
                  lowest=lowest, req_today=req_today, streak=streak,
                  running=poller_pid() is not None, steps=steps,
                  ws=warmup_status(), purchases=purchases, co=co)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    cfg = load_cfg()
    if request.method == "POST":
        f = request.form
        try:
            from datetime import datetime as _dt
            dates = [d.strip() for d in f["dates"].split(",") if d.strip()]
            for d in dates:
                _dt.strptime(d, "%Y-%m-%d")
            cfg["max_price_usd"] = float(f["max_price_usd"])
            cfg["poll_median_seconds"] = int(f["poll_median_seconds"])
            cfg["daily_request_cap"] = int(f["daily_request_cap"])
        except ValueError as e:
            return render(SETUP, cfg=cfg,
                          err=f"Check your input: {e}. Dates must be YYYY-MM-DD, "
                              f"price/intervals must be numbers.")
        cfg["route"] = {"from_code": f["from_code"].strip().upper(),
                        "to_code": f["to_code"].strip().upper()}
        cfg["dates"] = dates
        cfg["seat_class"] = f["seat_class"].strip() or "coach"
        cfg.setdefault("min_seats_available", 1)
        cfg.setdefault("jitter_fraction", 0.3)
        cfg["notify"] = {"channel": "telegram",
                         "bot_token": f["bot_token"].strip(),
                         "chat_id": f["chat_id"].strip()}
        cfg["proxy"] = f["proxy"].strip()
        cfg["auto_checkout"] = f.get("auto_checkout") == "on"
        save_cfg(cfg)
        return redirect(url_for("dashboard"))
    return render(SETUP, cfg=cfg)


@app.route("/incidents")
def incidents():
    rows = []
    if INCIDENTS.exists():
        import json
        for line in INCIDENTS.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return render(INCIDENTS_PAGE, rows=list(reversed(rows)))


@app.route("/control/warmup", methods=["POST"])
def warmup():
    subprocess.Popen([sys.executable, str(ROOT / "tools" / "auto_warmup.py")],
                     cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    return redirect(url_for("dashboard"))


@app.route("/control/checkout_test", methods=["POST"])
def checkout_test():
    subprocess.Popen([sys.executable, "-m", "sniper.checkout", "--dry-run"],
                     cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    return redirect(url_for("dashboard"))


@app.route("/control/start", methods=["POST"])
def start():
    if poller_pid() is None:
        PIDFILE.parent.mkdir(exist_ok=True)
        log = open(ROOT / "data" / "poller.log", "a", encoding="utf-8")
        p = subprocess.Popen([sys.executable, "-m", "sniper.poller"], cwd=ROOT,
                             stdout=log, stderr=subprocess.STDOUT)
        PIDFILE.write_text(str(p.pid))
    return redirect(url_for("dashboard"))


@app.route("/control/stop", methods=["POST"])
def stop():
    pid = poller_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM if hasattr(signal, "SIGTERM") else 9)
        except OSError:
            pass
        time.sleep(1)
    PIDFILE.unlink(missing_ok=True)
    return redirect(url_for("dashboard"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7100")))
    args = ap.parse_args()
    print(f"amtrak-sniper UI → http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
