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
import sqlite3
from contextlib import contextmanager
from pathlib import Path

__all__ = ["DEFAULT_BROKER", "SCHEMA_VERSION", "connect", "migrate", "open_journal"]

log = logging.getLogger(__name__)

SCHEMA_VERSION = 5

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
    # Identity is per broker, not global. See _rekey_trades_by_broker.
    ("trades", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("cash_transactions", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("position_snapshots", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
    ("statements", "broker", f"TEXT NOT NULL DEFAULT '{DEFAULT_BROKER}'"),
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
CREATE TABLE IF NOT EXISTS equity_summaries (
  report_date   TEXT PRIMARY KEY,
  account_id    TEXT NOT NULL,
  currency      TEXT NOT NULL,
  cash_base     REAL,
  stock_base    REAL,
  options_base  REAL,
  total_base    REAL NOT NULL,
  raw           TEXT NOT NULL,
  source_file   TEXT NOT NULL REFERENCES statements(source_file),
  ingested_at   TEXT NOT NULL
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


def connect(path: Path) -> sqlite3.Connection:
    """Open the journal database with sane pragmas applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
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


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the schema. Returns the resulting schema version."""
    for view in _VIEWS:
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
        conn.close()
