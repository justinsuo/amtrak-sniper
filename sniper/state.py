"""SQLite state: price history, poll log, daily request counter, alert dedup."""

from __future__ import annotations

import sqlite3
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import FareQuote

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    travel_date TEXT,
    ok INTEGER NOT NULL,
    http_status INTEGER,
    n_offers INTEGER,
    min_price_cents INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    train_number TEXT NOT NULL,
    departs TEXT,
    fare_class TEXT,
    price_cents INTEGER NOT NULL,
    seats_available INTEGER
);
CREATE INDEX IF NOT EXISTS idx_prices_lookup
    ON prices (travel_date, train_number, fare_class, ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,          -- travel_date|train|class
    last_price_cents INTEGER NOT NULL,
    last_ts TEXT NOT NULL
);
"""


class State:
    def __init__(self, db_path: str = "data/prices.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)

    # ---- counters (meta) ----
    def _get(self, key: str, default: str = "0") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                          (key, value))
        self.conn.commit()

    def bump_requests(self) -> int:
        key = f"req_{date_type.today():%Y-%m-%d}"
        n = int(self._get(key)) + 1
        self._set(key, str(n))
        return n

    def requests_today(self) -> int:
        return int(self._get(f"req_{date_type.today():%Y-%m-%d}"))

    def bump_challenge(self) -> int:
        n = int(self._get("challenge_streak")) + 1
        self._set("challenge_streak", str(n))
        return n

    def reset_challenge(self) -> None:
        self._set("challenge_streak", "0")

    # ---- records ----
    def record_poll(self, travel_date, ok, http_status, n_offers,
                    min_price_cents, note="") -> None:
        self.conn.execute(
            "INSERT INTO polls (ts, travel_date, ok, http_status, n_offers,"
            " min_price_cents, note) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), travel_date, int(ok),
             http_status, n_offers, min_price_cents, note))
        self.conn.commit()

    def record_prices(self, quotes: "list[FareQuote]") -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT INTO prices (ts, travel_date, train_number, departs,"
            " fare_class, price_cents, seats_available) VALUES (?,?,?,?,?,?,?)",
            [(ts, q.travel_date, q.train_number, q.departs, q.fare_class,
              q.price_cents, q.seats_available) for q in quotes])
        self.conn.commit()

    # ---- alert dedup: re-alert only on new train/class or a further drop ----
    def should_alert(self, q: "FareQuote") -> bool:
        key = f"{q.travel_date}|{q.train_number}|{q.fare_class}"
        row = self.conn.execute("SELECT last_price_cents FROM alerts WHERE key=?",
                                (key,)).fetchone()
        if row is None or q.price_cents < row[0]:
            self.conn.execute(
                "INSERT OR REPLACE INTO alerts (key, last_price_cents, last_ts)"
                " VALUES (?,?,?)",
                (key, q.price_cents, datetime.now().isoformat(timespec="seconds")))
            self.conn.commit()
            return True
        return False
