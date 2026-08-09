"""SQLite persistence for the options journal.

Design notes, and the reasoning behind the non-obvious choices:

* Money and prices are REAL. Float64 carries 15-17 significant digits; the
  largest value in this account is ~2,350 and the most precise is a
  7-decimal commission, so representation error is around 1e-12. Exactness
  costs more in query ergonomics than it buys.

* Contract quantities are declared INTEGER for the common case: option
  quantities are always integral, and integers make the episode flat-test
  exact. Stock lots are legitimately fractional (dividend reinvestment buys
  1.79 shares), and SQLite's INTEGER *affinity* stores those losslessly as
  REAL in the same column -- so nothing is truncated; history applies a dust
  epsilon to fractional quantities instead (see history._flat).

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

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from optjournal.locks import locked

__all__ = ["DEFAULT_BROKER", "SCHEMA_VERSION", "connect", "migrate",
           "open_journal", "schema_is_current"]

log = logging.getLogger(__name__)

SCHEMA_VERSION = 7

#: The broker a row came from. Defaulted rather than nullable, because every row
#: already in a journal came from IBKR -- the only source this project has ever
#: had -- so the default states a fact rather than guessing one.
DEFAULT_BROKER = "ibkr"

#: Columns added to existing tables after their CREATE statement shipped.
#: `executescript(_SCHEMA)` uses CREATE TABLE IF NOT EXISTS, which is a no-op on
#: a table that already exists -- so a new column in _SCHEMA reaches new
#: databases only. Existing ones need the ALTER, and every journal on disk is an
#: existing one. Idempotent: the column list is read first, so re-running is
#: free and an interrupted migration resumes.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("trades", "ib_commission_currency", "TEXT"),
    # Identity is per broker, not global. See _rekey_by_broker.
    ("trades", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("cash_transactions", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("position_snapshots", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("statements", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("securities", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("equity_summaries", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
)

_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS trades (
  broker                  TEXT    NOT NULL DEFAULT 'ibkr',
  trade_id                TEXT    NOT NULL,
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
  -- The currency IBKR billed the commission in. Stored rather than assumed:
  -- ib_commission_base is ib_commission x fx_rate_to_base, and that rate
  -- belongs to the INSTRUMENT's currency. The two agree on every row observed
  -- so far, but if IBKR ever bills in a third currency the conversion would
  -- silently apply the wrong rate, so the assumption is now recorded and
  -- checked at ingest instead of being invisible.
  ib_commission_currency  TEXT,
  taxes                   REAL,
  fifo_pnl_realized       REAL,
  fifo_pnl_realized_base  REAL,
  mtm_pnl                 REAL,
  raw                     TEXT    NOT NULL,
  source_file             TEXT    NOT NULL REFERENCES statements(source_file),
  first_seen_at           TEXT    NOT NULL,
  -- Identity is per broker: two brokers may both number a fill 1.
  PRIMARY KEY (broker, trade_id)
);
"""

#: Hoisted for the same reason as _TRADES_DDL: `_rekey_by_broker` rebuilds these
#: tables from the SHIPPED definition rather than from the live one, so a journal
#: migrated today and a journal created today are identical.
_CASH_DDL = f"""
CREATE TABLE IF NOT EXISTS cash_transactions (
  broker           TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}',
  transaction_id   TEXT NOT NULL,
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
  first_seen_at    TEXT NOT NULL,
  -- A transaction id is the issuing broker's, not a global handle.
  PRIMARY KEY (broker, transaction_id)
);
"""

_POSITIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS position_snapshots (
  broker               TEXT    NOT NULL DEFAULT '{DEFAULT_BROKER}',
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
  -- A conid is IBKR's numbering; another broker may reuse the integer. Two
  -- brokers holding "contract 12345" on the same date are two positions.
  PRIMARY KEY (broker, report_date, conid)
);
"""

_NAV_DDL = f"""
CREATE TABLE IF NOT EXISTS equity_summaries (
  broker        TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}',
  report_date   TEXT NOT NULL,
  account_id    TEXT NOT NULL,
  currency      TEXT NOT NULL,
  cash_base     REAL,
  stock_base    REAL,
  options_base  REAL,
  total_base    REAL NOT NULL,
  raw           TEXT NOT NULL,
  source_file   TEXT NOT NULL REFERENCES statements(source_file),
  ingested_at   TEXT NOT NULL,
  -- Per broker: each reports the value of ITS OWN account. Keyed on the date
  -- alone, the second broker's NAV for a day overwrites the first's, so the
  -- "gain as % of net liquidation" denominator silently becomes one account's
  -- value measured against both accounts' P&L.
  PRIMARY KEY (broker, report_date)
);
"""

_SECURITIES_DDL = f"""
CREATE TABLE IF NOT EXISTS securities (
  broker             TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}',
  conid              TEXT NOT NULL,
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
  updated_at         TEXT NOT NULL,
  -- `conid` is the BROKER's numbering, not the exchange's, and this table
  -- upserts. Keyed on conid alone, a second broker's contract 12345 overwrites
  -- the first's -- so one broker's TSLA option becomes another's unrelated
  -- contract, with no error anywhere. `bars.underlying_ids` reads this table to
  -- resolve a symbol that has no trade row, so a wrong row here misattributes
  -- price history.
  PRIMARY KEY (broker, conid)
);
"""

#: Indexes over columns _ADDED_COLUMNS may still be about to create, so they
#: cannot live in _SCHEMA: `executescript` runs BEFORE the ALTERs, and
#: CREATE INDEX validates its column list even under IF NOT EXISTS -- on a
#: pre-migration journal that is "no such column: broker".
#:
#: Per broker, like the primary key: an execution id is unique within the broker
#: that issued it, not across brokers.
_LATE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS trades_exec ON trades(broker, ib_exec_id)",
)

_SCHEMA = f"""
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

{_TRADES_DDL}
CREATE INDEX        IF NOT EXISTS trades_date       ON trades(trade_date);
CREATE INDEX        IF NOT EXISTS trades_order      ON trades(ib_order_id);
CREATE INDEX        IF NOT EXISTS trades_underlying ON trades(underlying_symbol, trade_date);
CREATE INDEX        IF NOT EXISTS trades_asset      ON trades(asset_category, trade_date);

{_CASH_DDL}
CREATE INDEX IF NOT EXISTS cash_type_date ON cash_transactions(type, date_time);

{_POSITIONS_DDL}

-- Daily Net Asset Value, from the EquitySummaryInBase statement section.
-- The one figure a trade ledger cannot reconstruct: cash balances need a
-- starting balance no Activity statement carries, so NAV must be reported,
-- not derived. Replace-on-date like position_snapshots: re-fetching a day
-- corrects rather than duplicates.
{_NAV_DDL}
{_SECURITIES_DDL}

-- Historical OHLCV, for underlyings and option contracts alike. One table
-- rather than two because the shape is identical and every reader wants both
-- series on one time axis; which is which is already answerable by joining
-- `conid` against trades/securities, so a discriminator column would only
-- duplicate what the journal knows.
--
-- Bars are immutable once a session closes, so this is a cache that only ever
-- grows: `ts` is the bar's OPEN in epoch seconds UTC, and the primary key
-- makes a re-fetch idempotent. `source` records provenance so a later,
-- better-trusted fetch can upgrade a row in place (see marketdata.SOURCE_RANK)
-- without a migration and without re-fetching what is already good.
--
-- Prices are nullable on purpose. A quiet option strike has no print on
-- roughly one session in five, and writing 0.0 there would render the
-- position's value collapsing to nothing.
CREATE TABLE IF NOT EXISTS price_bars (
  conid       TEXT    NOT NULL,
  symbol      TEXT    NOT NULL,
  bar_size    TEXT    NOT NULL,
  ts          INTEGER NOT NULL,
  open        REAL,
  high        REAL,
  low         REAL,
  close       REAL,
  volume      INTEGER,
  source      TEXT    NOT NULL,
  fetched_at  TEXT    NOT NULL,
  PRIMARY KEY (conid, bar_size, ts)
);

-- Economic and geopolitical events, for the Market tab.
--
-- Keyed (source, event_id) for the reason `trades` is keyed (broker, trade_id):
-- an id belongs to the feed that issued it, and a second feed numbering an event
-- the same way would silently overwrite the first's row. That took two migrations
-- to learn on the broker tables; it is free here.
--
-- `event_id` is OURS, not the feed's -- ForexFactory supplies no id, so it is a
-- hash of (starts_at, country, title). Verified unique across all 99 rows of a
-- real week. A re-fetch therefore CORRECTS a revised forecast in place rather
-- than adding a second copy of the same event.
--
-- Rows persist rather than being replaced per fetch, and that is what makes this
-- table more than a cache: the feed serves one week only (verified -- nextweek,
-- thismonth and lastweek all 404), so weekly fetches accumulate a past calendar
-- the source itself will not serve.
CREATE TABLE IF NOT EXISTS market_events (
  source       TEXT    NOT NULL,
  event_id     TEXT    NOT NULL,
  -- Epoch seconds UTC. The feed sends ISO with an offset; storing the instant
  -- keeps one timeline, and the page renders it in bars.MARKET_TZ like every
  -- other stamp in this journal.
  starts_at    INTEGER NOT NULL,
  country      TEXT    NOT NULL,
  title        TEXT    NOT NULL,
  -- The FEED's judgement of importance, not the journal's. Stored verbatim so
  -- the page can attribute it rather than presenting it as our own assessment.
  impact       TEXT    NOT NULL,
  forecast     TEXT,
  previous     TEXT,
  raw          TEXT    NOT NULL,
  fetched_at   TEXT    NOT NULL,
  PRIMARY KEY (source, event_id)
);
CREATE INDEX IF NOT EXISTS market_events_when ON market_events(starts_at);

-- Symbols being watched. The first table here that is USER INPUT rather than
-- ingested fact, which is why it has no `broker` column and should not gain one:
-- a symbol you are watching is not a broker's record of anything. Nothing joins
-- it to `trades`; the watchlist view looks up prices and positions by symbol.
CREATE TABLE IF NOT EXISTS watchlist (
  symbol     TEXT PRIMARY KEY,
  note       TEXT,
  added_at   TEXT NOT NULL
);

-- One row per (order, leg). Collapses partial fills, which IBKR marks with
-- note code 'P' and which share an ib_order_id. Covers every asset category:
-- the consumer scopes by asset_category, the view does not pre-decide.
--
-- GROUPed by broker as well as (ib_order_id, conid), because an order id is the
-- issuing broker's. Without it two brokers' fills for "order 1232923637" SUM
-- into one leg -- verified: 18 trades collapsed to 8 legs carrying doubled
-- quantities (-6 for a position of -3). The rows were stored correctly; the
-- view merged them on read, which is the harder version of the bug to see.
CREATE VIEW IF NOT EXISTS trade_legs AS
SELECT
  broker,
  ib_order_id,
  conid,
  account_id,
  asset_category,
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
GROUP BY broker, ib_order_id, conid;

-- One row per order. A multi-leg order is a strategy: leg_count > 1 means
-- a spread, straddle, condor and so on, submitted as a single order.
CREATE VIEW IF NOT EXISTS trade_orders AS
SELECT
  broker,
  ib_order_id,
  account_id,
  -- An order never mixes categories (verified: IBKR order ids are per
  -- instrument), so MIN is selection, not aggregation.
  MIN(asset_category)            AS asset_category,
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
FROM trade_legs
GROUP BY broker, ib_order_id;

-- OPT-scoped wrappers, kept for their names: "option orders" is the journal's
-- home view and half the codebase says so.
CREATE VIEW IF NOT EXISTS option_legs AS
SELECT * FROM trade_legs WHERE asset_category = 'OPT';

CREATE VIEW IF NOT EXISTS option_orders AS
SELECT * FROM trade_orders WHERE asset_category = 'OPT';

-- Current option book, from the most recent snapshot only.
--
-- "Most recent" is per broker, via the correlated subquery. A single MAX over
-- the whole table asks one broker's statement date to decide whether ANOTHER
-- broker's positions are current -- so the broker whose statements lag drops out
-- of the book entirely, silently, and the page shows a shorter position list
-- rather than an error. `history._latest_snapshot` takes its own MAX and would
-- need the same scoping; it already keys episodes on (broker, account_id, conid).
CREATE VIEW IF NOT EXISTS current_option_positions AS
SELECT *
FROM position_snapshots p
WHERE asset_category = 'OPT'
  AND report_date = (
    SELECT MAX(report_date) FROM position_snapshots
    WHERE asset_category = 'OPT' AND broker = p.broker
  );
"""


#: How long a writer waits for another writer before giving up.
#:
#: Set EXPLICITLY, because it was previously whatever sqlite3 defaulted to (5000ms
#: today) -- a number nothing in this project chose, tested, or would notice
#: changing. WAL lets readers run during a write but still allows only one writer,
#: so any second writer needs a wait or it raises "database is locked" immediately.
#:
#: Sized from measurement rather than taste: 5,000 price-bar upserts commit in 6ms
#: on this journal, so ordinary writes are three orders of magnitude inside this.
#: The one write that could plausibly approach it is a migration's table rebuild,
#: which is exactly when a second process must wait rather than fail.
BUSY_TIMEOUT_MS = 15_000


def connect(path: Path) -> sqlite3.Connection:
    """Open the journal database with sane pragmas applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Two processes writing is the normal case here, not an edge: a scheduled sync
    # while a page is open. Without this, the second one raises rather than waits.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


#: Views are code, not data: their definitions belong to this file, not to
#: whichever version of it first created the database. Dropped and recreated
#: on every migrate, because CREATE VIEW IF NOT EXISTS would silently leave an
#: existing database on the old definition forever -- which is how the
#: OPT-only order views survived into a journal that stores every category.
_VIEWS = ("trade_legs", "trade_orders", "option_legs", "option_orders",
          "current_option_positions")


def _backfill_commission_currency(conn: sqlite3.Connection) -> int:
    """Fill ib_commission_currency from each row's own stored `raw` payload.

    The column arrived after these rows were written, so a journal that has been
    ingesting for months holds the value in `raw` and NULL in the column. No
    broker request is needed to recover it: `raw` is the statement's own trade
    object, ibCommissionCurrency included.

    Not gated on schema_version. The version is stamped the first time migrate()
    runs after the bump, which for any journal that has merely been OPENED since
    then is already in the past -- a one-shot hook would silently never fire.
    Instead the work defines its own guard: rows that still need it, and whose
    `raw` can actually supply it. After one pass that set is empty and this costs
    a single indexless count on a table of a few hundred rows; it can never loop.

    Self-terminating in practice: after one pass the only rows still matching are
    those whose `raw` carries the key with an empty value, which no statement
    observed does -- and those cost a parse per open, never a write. The narrower
    alternative (only rows with non-zero commission) left a repaired journal
    disagreeing with a fresh ingest of the same archive on 127 zero-commission
    rows, which makes "rebuild from the archive and compare" useless as a check.

    Returns the number of rows filled, so a caller can log or test it.
    """
    pending = conn.execute(
        "SELECT trade_id, raw FROM trades"
        " WHERE ib_commission_currency IS NULL"
        "   AND raw LIKE '%ibCommissionCurrency%'"
    ).fetchall()
    filled = 0
    for row in pending:
        try:
            payload = json.loads(row["raw"])
        except (TypeError, ValueError):
            continue  # a raw we cannot parse is not a reason to fail an open
        ccy = payload.get("ibCommissionCurrency")
        if not ccy:
            continue
        conn.execute(
            "UPDATE trades SET ib_commission_currency = ? WHERE trade_id = ?",
            (str(ccy), row["trade_id"]),
        )
        filled += 1
    return filled


def _repair_base_commission(conn: sqlite3.Connection) -> int:
    """Recompute ib_commission_base where it was converted at the wrong rate.

    `fxRateToBase` belongs to the INSTRUMENT's currency, and the original ingest
    applied it to the commission unconditionally. That is right whenever the two
    currencies agree -- which is every row on this account except FX conversions,
    where IBKR bills the commission in the BASE currency while the row's currency
    is the pair's quote. A EUR amount times a SEK->EUR rate stored a figure 11x
    too small.

    Only rows the data can definitively correct are touched: commission billed in
    the base currency needs no conversion at all, so the base value IS the native
    value. A commission in some third currency has no rate anywhere in the
    statement and is deliberately left alone rather than guessed at.

    Self-terminating for the same reason as the backfill: after one pass no row
    matches the WHERE, so this costs one count per open. Returns rows repaired.
    """
    rows = conn.execute(
        "SELECT t.trade_id, t.ib_commission, s.base_currency"
        " FROM trades t JOIN statements s ON s.source_file = t.source_file"
        " WHERE t.ib_commission IS NOT NULL AND t.ib_commission <> 0"
        "   AND t.ib_commission_currency IS NOT NULL"
        "   AND t.ib_commission_currency <> t.currency"
        "   AND t.ib_commission_currency = s.base_currency"
        "   AND t.ib_commission_base IS NOT t.ib_commission"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE trades SET ib_commission_base = ib_commission WHERE trade_id = ?",
            (row["trade_id"],),
        )
    return len(rows)


#: The tables whose identity was IBKR's own numbering, and the key each needs
#: once a second broker exists. One entry per table, so the rebuild below is
#: written once: `trades` needed it first and the other two need it for exactly
#: the same reason, which was easy to miss because each looks fine alone.
_REKEYED_TABLES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("trades", ("broker", "trade_id"), _TRADES_DDL),
    ("cash_transactions", ("broker", "transaction_id"), _CASH_DDL),
    ("position_snapshots", ("broker", "report_date", "conid"), _POSITIONS_DDL),
    ("securities", ("broker", "conid"), _SECURITIES_DDL),
    ("equity_summaries", ("broker", "report_date"), _NAV_DDL),
)


def _rekey_by_broker(conn: sqlite3.Connection, table: str,
                     want_pk: tuple[str, ...], ddl: str) -> bool:
    """Put `broker` at the front of `table`'s PRIMARY KEY.

    Returns True when a rebuild happened. Idempotent: the current key is read
    first, so re-running is free.

    Why a rebuild at all: SQLite cannot alter a PRIMARY KEY, and each of these
    tables was keyed on an identifier that is IBKR's rather than universal --
    `trade_id`, `transaction_id`, `(report_date, conid)`. A second broker
    numbering a fill `1`, or holding its own "contract 12345", would either
    collide (raising) or, worse, be silently swallowed by the ingest's
    `ON CONFLICT ... DO NOTHING` and reported as an already-seen duplicate. The
    silent case is the dangerous one: the ingest would report success.

    `trades` also had a UNIQUE index on `ib_exec_id` alone, now `(broker,
    ib_exec_id)` in `_LATE_INDEXES`, for the same reason.

    Done as the standard twelve-step table rebuild, with two safety properties
    that matter because this runs against a journal whose statements cost IBKR
    requests to refetch:

    * It counts rows before and after and raises rather than committing a partial
      copy, so a failure leaves the original table in place.
    * `INSERT INTO ... SELECT` names its columns explicitly rather than using
      `SELECT *`, so a column added later cannot silently shift into the wrong
      position.
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if not cols:
        return False  # table does not exist yet; _SCHEMA will create it keyed
    if "broker" not in cols:
        return False  # _ADDED_COLUMNS has not run yet; nothing to rekey
    # `pk` is 1-based rank in the key, not a boolean, so a composite key must be
    # ordered by it -- sorting by name would compare ("broker","trade_id")
    # against a key that is really (trade_id, broker) and call them equal.
    keyed = sorted(
        ((r["pk"], r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")
         if r["pk"]),
    )
    if tuple(name for _rank, name in keyed) == want_pk:
        return False  # already rekeyed

    before = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    # Only the columns BOTH tables have. The old table can be missing one the
    # shipped DDL declares -- `broker` itself on a journal whose ALTER has not run
    # in this process, or any column added by a later _ADDED_COLUMNS entry -- and
    # naming it in the SELECT is "no such column". Anything absent takes its DDL
    # default, which for `broker` is exactly the fact we want recorded.
    live = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    shipped = {
        line.strip().split()[0]
        for line in ddl.splitlines()
        if line.startswith("  ") and not line.strip().startswith(("--", "PRIMARY"))
    }
    ordered = [c for c in live if c in shipped]
    names = ", ".join(ordered)
    scratch = f"{table}_rekeyed"
    # Rebuilt from the shipped DDL rather than from the live table, so the new
    # table is exactly what a fresh journal gets -- otherwise a journal migrated
    # today and one created today would differ.
    new_ddl = ddl.replace(
        f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {scratch}"
    )
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        # The views must go first: SQLite validates them on RENAME, so
        # `trades_rekeyed RENAME TO trades` fails with "error in view trade_legs:
        # no such table: main.trades" while any view still selects from the table
        # being replaced. migrate() drops them at the top and _SCHEMA recreates
        # them below, but this runs between those two points, so it drops them
        # again rather than depending on where it sits in that sequence.
        for view in _VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        conn.execute(f"DROP TABLE IF EXISTS {scratch}")
        conn.executescript(new_ddl)
        conn.execute(f"INSERT INTO {scratch} ({names}) SELECT {names} FROM {table}")
        after = conn.execute(f"SELECT COUNT(*) AS n FROM {scratch}").fetchone()["n"]
        if after != before:
            raise RuntimeError(
                f"refusing to swap in a partial copy of {table}: {before} rows in, "
                f"{after} out. The original table is untouched."
            )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {scratch} RENAME TO {table}")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    # The caller re-runs _SCHEMA, which recreates the views and the indexes the
    # dropped table took with it.
    conn.executescript(_SCHEMA)
    conn.commit()
    log.info("rekeyed %d %s rows on %s", before, table, want_pk)
    return True


def _lock_path(conn: sqlite3.Connection) -> Path | None:
    """The migration lock file beside this connection's database.

    Derived from the connection rather than passed in, so `migrate(conn)` keeps
    its signature and every existing caller is protected without being edited --
    there are seven, and one forgotten call site would be a silent hole.

    None for an in-memory database, which no other process can see and therefore
    cannot race on.
    """
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(f"{file}.migrate.lock") if file else None
    return None


def _shipped_view_sql() -> dict[str, str]:
    """Each view's definition as `_SCHEMA` spells it, keyed by name.

    Parsed from the schema text rather than maintained beside it, because a second
    copy of five view bodies is a second thing to forget to update -- and the
    failure would be silent, since a stale copy simply means the view is never
    refreshed.

    SQLite stores `sql` verbatim minus `IF NOT EXISTS` (verified), so a stored
    definition and a shipped one are directly comparable once that clause is
    dropped and surrounding whitespace is stripped.

    Located from the CREATE keyword to the next `;`, NOT by splitting `_SCHEMA` on
    `;` and keeping chunks that start with CREATE VIEW. That was the first attempt
    and it found one view of five: every other view is preceded by an explanatory
    `--` comment in the same chunk, so the chunk starts with the comment. The bug
    was invisible because the caller treated "not parsed" as "up to date" -- which
    would have disabled view refreshing altogether while every test still passed.
    Hence the assertion below: a view this cannot parse is a hard error, because
    the alternative is silently never refreshing it.
    """
    out: dict[str, str] = {}
    for match in re.finditer(r"CREATE VIEW IF NOT EXISTS (\w+)", _SCHEMA):
        end = _SCHEMA.index(";", match.start())
        body = _SCHEMA[match.start():end].strip()
        out[match.group(1)] = body.replace(
            "CREATE VIEW IF NOT EXISTS ", "CREATE VIEW ", 1)
    missing = set(_VIEWS) - set(out)
    if missing:
        raise RuntimeError(
            f"_shipped_view_sql could not parse {sorted(missing)} out of _SCHEMA. "
            f"Refusing to continue: treating an unparsed view as current would "
            f"mean its definition is never refreshed on an existing journal."
        )
    return out


def _stale_views(conn: sqlite3.Connection) -> list[str]:
    """The views that are missing or whose stored definition is out of date.

    This is what makes a migration safe to run beside a reader. Dropping and
    recreating ALL FIVE views on every request is what let one request delete the
    view another was querying -- measured at 4,941 failures with readers and
    migrators as separate threads, and reachable over HTTP from four concurrent
    requests up. In the steady state this returns an empty list and no view is
    touched at all.

    Comparing SQL rather than checking existence, because a view can exist and
    still be WRONG: `CREATE VIEW IF NOT EXISTS` never updates, which is how the
    OPT-only order views would have survived into a journal storing every category.
    `test_db` pins exactly that, by planting a garbage definition.
    """
    stored = {
        r["name"]: (r["sql"] or "").strip()
        for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view'")
    }
    shipped = _shipped_view_sql()
    return [
        name for name in _VIEWS
        if name not in stored or stored[name] != shipped[name]
    ]


def schema_is_current(conn: sqlite3.Connection) -> bool:
    """Whether a migration would change anything structural. Cheap, and strict.

    Checked, in order of cost: the version stamp, then every view's DEFINITION
    (not merely its existence), then every `_ADDED_COLUMNS` column.

    An earlier draft checked only version and view existence, and `test_db` caught
    it -- the suite already pinned the contract it broke. `migrate` HEALS a journal
    whose stamp looks right: `_ADDED_COLUMNS` runs ALTERs and two backfills repair
    rows predating a column. "The version matches" is a different question from
    "nothing needs doing", and answering the wrong one silently disabled every
    self-healing path here.

    The row-level BACKFILLS are deliberately not part of this, which is why
    `migrate` does not use this function to skip itself -- see there. This answers
    a narrower question: is the STRUCTURE current.
    """
    try:
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return False        # no schema_version table: nothing has been applied yet
    if not row or row["v"] != SCHEMA_VERSION:
        return False
    if _stale_views(conn):
        return False
    for table in {table for table, _column, _decl in _ADDED_COLUMNS}:
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not columns:
            return False    # the table itself is missing
        wanted = {c for t, c, _d in _ADDED_COLUMNS if t == table}
        if not wanted <= columns:
            return False
    return True


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the schema when it is not already applied. Returns the version.

    TWO guards, and the ORDER OF DISCOVERY here is worth recording because the
    first one alone looked sufficient and was not.

    `open_journal` migrates on every request, and the first thing a migration does
    is DROP EVERY VIEW. Two overlapping requests meant one dropping the views the
    other was querying, reaching the browser as `HTTP 500: no such table:
    current_option_positions`.

    The cross-process lock (`locks`) was the first fix, and it is necessary: two
    migrations must not interleave, and a `threading.Lock` cannot say that across
    the cron and the server. But it is NOT SUFFICIENT, and the test that said
    otherwise was mine and was wrong. It ran migrate-then-read inside each worker,
    so every reader happened to hold the lock while reading. A real reader takes no
    lock at all -- `/api/state` migrates, releases, and only then runs its SELECTs.
    Re-measured with readers and migrators as separate threads: 4,941 failures.
    Over real HTTP it appears from four concurrent requests upward (3 of 40), which
    my original "0 in 10" missed only because it used two.

    So the second guard is the load-bearing one: DO NOT MIGRATE WHEN THE SCHEMA IS
    ALREADY CURRENT. A migration that does not run cannot drop a view, and the
    steady state of a journal is that no migration is needed. The lock still covers
    the case where one IS needed -- startup, or the first open after a version bump.

    The check is deliberately outside the lock. Reading a version and a view list
    needs no exclusion: if it races with a real migration it can only answer "not
    current", and the lock then serialises the work.

    WHY THIS DOES NOT SIMPLY RETURN EARLY WHEN `schema_is_current`. That was the
    first attempt and `test_db` refused it, correctly: three tests require that
    merely OPENING a journal heals it -- an ALTER for a column added after ship, and
    two backfills that repair rows written before one. Skipping the whole migration
    on a current-looking stamp disabled all of that silently. The row-level repairs
    cannot be detected more cheaply than they can be performed, so they run every
    time; they are idempotent and measured in milliseconds.

    So the fix is narrower and lands where the damage actually was: `_stale_views`
    means a view is dropped only when its stored SQL differs from the shipped SQL.
    In the steady state nothing is dropped, so there is nothing for a concurrent
    reader to miss, and the healing still happens.
    """
    lock = _lock_path(conn)
    if lock is None:
        return _migrate_unlocked(conn)
    with locked(lock):
        return _migrate_unlocked(conn)


def _migrate_unlocked(conn: sqlite3.Connection) -> int:
    """The migration itself. Call `migrate`, which holds the lock."""
    # ONLY the views whose definition has actually changed. This used to drop all
    # five unconditionally, which is what let one request delete the view another
    # request was mid-query on -- the HTTP 500. `executescript` below recreates
    # anything dropped here, since every view is CREATE VIEW IF NOT EXISTS.
    for view in _stale_views(conn):
        conn.execute(f"DROP VIEW IF EXISTS {view}")
    conn.executescript(_SCHEMA)
    # After the script, because a table the script just created already has the
    # column and the existence check below then makes this a no-op.
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # After the ALTER, so the column exists to be written into. Here rather than
    # behind the version bump: the stamp is written the first time migrate() runs
    # after the bump, so for a journal merely OPENED since then a one-shot hook
    # would silently never fire. The backfill guards itself instead.
    # After the ALTER that adds `broker`, and before the backfills, so anything
    # they write lands in the rebuilt table rather than in one about to be dropped.
    for table, want_pk, ddl in _REKEYED_TABLES:
        _rekey_by_broker(conn, table, want_pk, ddl)
    for statement in _LATE_INDEXES:
        conn.execute(statement)
    _backfill_commission_currency(conn)
    # After the backfill, which is what makes the mismatch detectable: the
    # repair's WHERE compares ib_commission_currency, so on a journal that has
    # not been backfilled yet there is nothing for it to find.
    _repair_base_commission(conn)
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
        # Roll back before closing, and do it explicitly rather than relying on
        # close(). A statement that raises leaves the connection IN A TRANSACTION
        # holding the write lock -- measured: a refused INSERT leaves
        # `in_transaction` True, and the next writer then blocks for the whole
        # BUSY_TIMEOUT_MS before failing with "database is locked" (15.49s).
        #
        # `close()` happens to release it today, verified, so this is hardening
        # rather than a live fix. It matters for what comes next: a scheduler thread
        # holds a connection across many operations instead of one per request, and
        # there the leak has no close() to save it. Cheap, and it makes the
        # guarantee a property of this function rather than of sqlite3's cleanup.
        if conn.in_transaction:
            conn.rollback()
        conn.close()
