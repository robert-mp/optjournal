"""SQLite persistence for the options journal.

Design notes, and the reasoning behind the non-obvious choices:

* Money and prices are REAL. Float64 carries 15-17 significant digits; the
  largest value in this account is ~2,350 and the most precise is a
  7-decimal commission, so representation error is around 1e-12. Exactness
  costs more in query ergonomics than it buys.

* Contract quantities are INTEGER. Not for magnitude but for exactness on
  zero: deciding whether an option position is closed means checking that
  fills net to zero, and float makes that unreliable. Verified safe --
  option quantities are always integral (stock is not; it can be fractional).

* Every table keeps a `raw` JSON column holding the full source attribute
  dict. py_ibkr's models use extra="ignore" and do not model four of the
  seven statement sections, so this is insurance: a field can be promoted
  to a real column later without re-fetching from IBKR, which costs against
  a request lockout budget.

* Three write semantics. trades/cash are append-only with first-write-wins,
  so `first_seen_at` stays truthful and "new since yesterday" is answerable.
  position_snapshots replaces on (report_date, conid) so re-fetching a day
  corrects rather than duplicates. securities upserts.

* position_snapshots is not optional. A position opened before the earliest
  statement has no opening trade on record, so the snapshot is the only
  source of its cost basis.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

__all__ = ["SCHEMA_VERSION", "connect", "migrate", "open_journal"]

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER NOT NULL,
  applied_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS statements (
  source_file     TEXT PRIMARY KEY,
  sha256          TEXT NOT NULL,
  account_id      TEXT NOT NULL,
  from_date       TEXT NOT NULL,
  to_date         TEXT NOT NULL,
  when_generated  TEXT,
  base_currency   TEXT NOT NULL,
  asset_filter    TEXT NOT NULL,
  ingested_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
  trade_id                TEXT    PRIMARY KEY,
  ib_exec_id              TEXT    NOT NULL,
  transaction_id          TEXT    NOT NULL,
  ib_order_id             TEXT,
  account_id              TEXT    NOT NULL,
  trade_date              TEXT    NOT NULL,
  date_time               TEXT,
  asset_category          TEXT    NOT NULL,
  symbol                  TEXT    NOT NULL,
  conid                   TEXT,
  underlying_symbol       TEXT,
  underlying_conid        TEXT,
  put_call                TEXT,
  strike                  REAL,
  expiry                  TEXT,
  multiplier              REAL,
  buy_sell                TEXT,
  open_close              TEXT,
  notes                   TEXT,
  level_of_detail         TEXT,
  quantity                INTEGER NOT NULL,
  trade_price             REAL,
  currency                TEXT    NOT NULL,
  fx_rate_to_base         REAL    NOT NULL,
  proceeds                REAL,
  proceeds_base           REAL,
  ib_commission           REAL,
  ib_commission_base      REAL,
  taxes                   REAL,
  fifo_pnl_realized       REAL,
  fifo_pnl_realized_base  REAL,
  mtm_pnl                 REAL,
  raw                     TEXT    NOT NULL,
  source_file             TEXT    NOT NULL REFERENCES statements(source_file),
  first_seen_at           TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS trades_exec       ON trades(ib_exec_id);
CREATE INDEX        IF NOT EXISTS trades_date       ON trades(trade_date);
CREATE INDEX        IF NOT EXISTS trades_order      ON trades(ib_order_id);
CREATE INDEX        IF NOT EXISTS trades_underlying ON trades(underlying_symbol, trade_date);
CREATE INDEX        IF NOT EXISTS trades_asset      ON trades(asset_category, trade_date);

CREATE TABLE IF NOT EXISTS cash_transactions (
  transaction_id   TEXT PRIMARY KEY,
  account_id       TEXT NOT NULL,
  date_time        TEXT NOT NULL,
  settle_date      TEXT,
  type             TEXT NOT NULL,
  description      TEXT,
  symbol           TEXT,
  conid            TEXT,
  amount           REAL NOT NULL,
  currency         TEXT NOT NULL,
  fx_rate_to_base  REAL NOT NULL,
  amount_base      REAL NOT NULL,
  raw              TEXT NOT NULL,
  source_file      TEXT NOT NULL REFERENCES statements(source_file),
  first_seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cash_type_date ON cash_transactions(type, date_time);

CREATE TABLE IF NOT EXISTS position_snapshots (
  report_date          TEXT    NOT NULL,
  conid                TEXT    NOT NULL,
  account_id           TEXT    NOT NULL,
  symbol               TEXT    NOT NULL,
  asset_category       TEXT    NOT NULL,
  underlying_symbol    TEXT,
  put_call             TEXT,
  strike               REAL,
  expiry               TEXT,
  multiplier           REAL,
  position             INTEGER NOT NULL,
  mark_price           REAL,
  position_value       REAL,
  position_value_base  REAL,
  cost_basis_money     REAL,
  cost_basis_price     REAL,
  fifo_pnl_unrealized  REAL,
  side                 TEXT,
  open_date_time       TEXT,
  currency             TEXT    NOT NULL,
  fx_rate_to_base      REAL    NOT NULL,
  raw                  TEXT    NOT NULL,
  source_file          TEXT    NOT NULL REFERENCES statements(source_file),
  ingested_at          TEXT    NOT NULL,
  PRIMARY KEY (report_date, conid)
);

CREATE TABLE IF NOT EXISTS securities (
  conid              TEXT PRIMARY KEY,
  symbol             TEXT NOT NULL,
  description        TEXT,
  asset_category     TEXT,
  sub_category       TEXT,
  currency           TEXT,
  multiplier         REAL,
  strike             REAL,
  expiry             TEXT,
  put_call           TEXT,
  underlying_conid   TEXT,
  underlying_symbol  TEXT,
  isin               TEXT,
  listing_exchange   TEXT,
  raw                TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

-- One row per (order, leg). Collapses partial fills, which IBKR marks with
-- note code 'P' and which share an ib_order_id.
CREATE VIEW IF NOT EXISTS option_legs AS
SELECT
  ib_order_id,
  conid,
  account_id,
  symbol,
  underlying_symbol,
  put_call,
  strike,
  expiry,
  multiplier,
  buy_sell,
  open_close,
  currency,
  COUNT(*)                                            AS fills,
  MIN(date_time)                                      AS first_fill_at,
  MAX(date_time)                                      AS last_fill_at,
  SUM(quantity)                                       AS quantity,
  SUM(ABS(quantity) * trade_price) / SUM(ABS(quantity)) AS avg_price,
  SUM(proceeds)                                       AS proceeds,
  SUM(proceeds_base)                                  AS proceeds_base,
  SUM(ib_commission)                                  AS commission,
  SUM(ib_commission_base)                             AS commission_base,
  SUM(fifo_pnl_realized)                              AS realized_pnl,
  SUM(fifo_pnl_realized_base)                          AS realized_pnl_base
FROM trades
WHERE asset_category = 'OPT'
GROUP BY ib_order_id, conid;

-- One row per order. A multi-leg order is a strategy: leg_count > 1 means
-- a spread, straddle, condor and so on, submitted as a single order.
CREATE VIEW IF NOT EXISTS option_orders AS
SELECT
  ib_order_id,
  account_id,
  COUNT(*)                       AS leg_count,
  SUM(fills)                     AS fills,
  MIN(first_fill_at)             AS first_fill_at,
  MAX(last_fill_at)              AS last_fill_at,
  GROUP_CONCAT(DISTINCT underlying_symbol) AS underlyings,
  GROUP_CONCAT(DISTINCT expiry)  AS expiries,
  SUM(proceeds)                  AS proceeds,
  SUM(proceeds_base)             AS proceeds_base,
  SUM(commission)                AS commission,
  SUM(commission_base)           AS commission_base,
  SUM(realized_pnl)              AS realized_pnl,
  SUM(realized_pnl_base)         AS realized_pnl_base
FROM option_legs
GROUP BY ib_order_id;

-- Current option book, from the most recent snapshot only.
CREATE VIEW IF NOT EXISTS current_option_positions AS
SELECT *
FROM position_snapshots
WHERE asset_category = 'OPT'
  AND report_date = (
    SELECT MAX(report_date) FROM position_snapshots WHERE asset_category = 'OPT'
  );
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open the journal database with sane pragmas applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the schema. Returns the resulting schema version."""
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    if current < SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) "
            "VALUES (?, datetime('now'))",
            (SCHEMA_VERSION,),
        )
    conn.commit()
    return SCHEMA_VERSION


@contextmanager
def open_journal(path: Path):
    """A migrated connection, closed on exit.

    Every entry point repeated the same three lines -- connect, migrate,
    close-in-finally -- and two of them had, at different times, forgotten
    one of the three. The pattern is policy (a journal connection is always
    migrated before use), so it lives here rather than being re-derived at
    each call site.

    Yields a connection rather than caching one: sqlite3 objects cannot
    cross threads, and the web server is threaded, so per-use connections
    are the correctness requirement, not an inefficiency.
    """
    conn = connect(path)
    try:
        migrate(conn)
        yield conn
    finally:
        conn.close()
