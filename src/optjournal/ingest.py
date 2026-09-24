"""Fold archived Flex statements into the journal database.

Ingest is idempotent by construction. Statements overlap -- a 30-day and a
365-day query both contain the same fills, verified against real data -- so
trades and cash transactions are keyed on IBKR's own identifiers and a fill
already held is left alone. The one exception is rank (`SOURCE_RANK`): a
same-session Trade Confirmation may be superseded by the next day's settled
Activity Statement, never the reverse. `first_seen_at` records when the
journal first saw a row rather than when it was last re-presented, and a
supersede does not touch it.

Everything is stored by default (`ASSET_FILTER_ALL`). The filter existed
because the journal began options-only, but filtering at ingest made the
database disagree with the archive it came from: the Equities view read
empty not because nothing traded but because ingest had dropped the rows.
Category scoping is a query-time concern -- every consumer already filters
on `asset_category` -- and storage is not: a row dropped here costs a
re-ingest to recover, a row stored costs nothing. The raw XML stays in the
archive either way, so narrowing later is free too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from optjournal.confirms import base_rate, parse_confirms
from optjournal.confirms import statement_meta as confirm_meta
from optjournal.db import ACTIVITY_SOURCE, CONFIRM_SOURCE, DEFAULT_BROKER
from optjournal.sources import source_for

__all__ = [
    "ASSET_FILTER_ALL",
    "IngestResult",
    "ingest_file",
]

log = logging.getLogger(__name__)

#: The empty tuple means "store every category", which is why it needs a name:
#: `assets=()` at a call site reads as "store nothing".
ASSET_FILTER_ALL: tuple[str, ...] = ()
#: What `assets=` means when the caller does not say: everything. A narrower
#: default here is what made the cron and the Sync button quietly re-narrow a
#: database that had been widened by hand.
DEFAULT_ASSET_FILTER = ASSET_FILTER_ALL

#: How much to trust each Flex query, when both describe the same fill.
#:
#: The Activity Statement outranks a Trade Confirmation because it is the settled
#: record: it carries realised P&L, the FIFO match and the final commission, none
#: of which a same-session confirm can know. An unknown value ranks 0 so a source
#: added later cannot silently outrank either of these by accident -- it has to be
#: given a rank here, deliberately.
SOURCE_RANK: dict[str, int] = {CONFIRM_SOURCE: 1, ACTIVITY_SOURCE: 2}

#: Every column `_ingest_trades` writes, in the order it binds them.
#:
#: ONE list, so the column names, the placeholders and the supersede's SET clause
#: cannot disagree. They were three hand-maintained copies, and the count of `?`
#: was written out as a 37-character string -- a column added in the wrong place
#: would have bound every value after it to its neighbour's column, storing a
#: strike as a multiplier without raising anything. Same construction as
#: `bars._UPSERT`, which writes price bars.
_TRADE_COLUMNS: tuple[str, ...] = (
    "broker", "trade_id", "ib_exec_id", "transaction_id", "ib_order_id",
    "account_id", "trade_date", "date_time",
    "asset_category", "symbol", "conid", "underlying_symbol", "underlying_conid",
    "put_call", "strike", "expiry", "multiplier",
    "buy_sell", "open_close", "notes", "level_of_detail",
    "quantity", "trade_price", "currency", "fx_rate_to_base",
    "proceeds", "proceeds_base",
    "ib_commission", "ib_commission_base", "ib_commission_currency", "taxes",
    "fifo_pnl_realized", "fifo_pnl_realized_base", "mtm_pnl",
    "raw", "source_file", "first_seen_at", "source_kind", "fx_rate_estimated",
)

#: The three columns a supersede leaves alone.
#:
#: `broker` and `trade_id` are the key being matched on. `first_seen_at` records
#: when the JOURNAL first saw the fill, which is a fact about the journal rather
#: than about the fill: a supersede is not a new sighting, and re-dating it would
#: make every trade confirmed yesterday read as new the morning its Activity
#: Statement lands -- precisely the day a reader stops needing to be told.
#:
#: Everything else is rewritten, including contract fields that cannot really
#: change, so the row becomes the winning source's row ENTIRE rather than a blend
#: of two. Taking `proceeds_base` while keeping the confirm's `fx_rate_to_base`
#: (the rate it was derived from) would store a row whose own figures disagree.
_KEEP_ON_SUPERSEDE = frozenset({"broker", "trade_id", "first_seen_at"})

#: Ranked, not first-write-wins. The same fill arrives under one `tradeID` from
#: two queries -- a Trade Confirmation the same session, the Activity Statement
#: the next day -- and `DO NOTHING` meant a confirm landing first BLOCKED the
#: authoritative row carrying the realised P&L, the FIFO match and the settled
#: commission. Silently and forever, because a confirm looks like a complete fill.
#:
#: Reaching this statement is what decides a supersede (see `_stored_rank`), so
#: the conflict clause here is unconditional: by then the incoming fill has
#: already outranked the stored one.
_TRADE_UPSERT = (
    f"INSERT INTO trades ({', '.join(_TRADE_COLUMNS)})"
    f" VALUES ({', '.join('?' for _ in _TRADE_COLUMNS)})"
    " ON CONFLICT(broker, trade_id) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}"
                for c in _TRADE_COLUMNS if c not in _KEEP_ON_SUPERSEDE)
)


@dataclass(slots=True)
class IngestResult:
    source_file: str
    already_ingested: bool = False
    #: Set when this file was skipped because another already-ingested file
    #: holds byte-identical content. Distinct from `already_ingested` alone,
    #: which also covers re-ingesting the same filename.
    duplicate_of: str | None = None
    trades_inserted: int = 0
    #: Fills the journal already held, rewritten from a source that knows more
    #: about them: an Activity Statement over a same-session Trade Confirmation.
    #: Counted apart from `trades_inserted` so a morning sync reports three
    #: fills SETTLED rather than three fills NEW, which they would not be.
    trades_superseded: int = 0
    trades_skipped_existing: int = 0
    trades_filtered_out: int = 0
    cash_inserted: int = 0
    cash_skipped_existing: int = 0
    positions_written: int = 0
    securities_written: int = 0
    equity_summaries_written: int = 0
    warnings: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _f(value: Any) -> float | None:
    """Coerce to float, tolerating None and blank strings."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _matches_filter(asset_category: str | None, wanted: Iterable[str]) -> bool:
    allowed = tuple(wanted)
    if not allowed:
        return True
    return (asset_category or "").upper() in allowed


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    assets: Iterable[str] = DEFAULT_ASSET_FILTER,
    reingest: bool = False,
    broker: str = DEFAULT_BROKER,
    source_kind: str = ACTIVITY_SOURCE,
) -> IngestResult:
    """Ingest one archived statement. Safe to call repeatedly.

    `broker` selects the statement source (see sources.py) and is stamped on
    every trade row. Defaults to IBKR, the only source today, so existing
    callers are unchanged.

    `source_kind` says which Flex query produced the file, and defaults to the
    Activity Statement -- the settled record, and everything this journal
    archived before Trade Confirmations existed.
    """
    path = Path(path)
    source = source_for(broker)
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    result = IngestResult(source_file=path.name)

    if not reingest:
        existing = conn.execute(
            "SELECT sha256 FROM statements WHERE source_file = ?", (path.name,)
        ).fetchone()
        if existing and existing["sha256"] == digest:
            result.already_ingested = True
            return result

        # Same bytes under a different name. `flex._archive` dedupes at write
        # time, so `fetch` cannot produce this -- but a direct `ingest`, a
        # copied file, or a restore from backup can, and each one would
        # otherwise add a redundant provenance row claiming to be a distinct
        # statement. The row data itself is protected by the primary keys;
        # this protects `statements` as an audit trail.
        #
        # Scoped to the broker, because "these bytes are already ingested" is a
        # claim about one broker's archive. Unscoped it reasons across brokers,
        # and the conclusion it draws is to SKIP -- so the fix that made every
        # writer broker-aware would have been unobservable, the ingest returning
        # 0 inserted and 0 skipped while reporting success. Two brokers cannot
        # really emit identical bytes; the point is that the guard should not be
        # the thing deciding that.
        twin = conn.execute(
            "SELECT source_file FROM statements WHERE sha256 = ? AND source_file != ?"
            " AND broker = ? ORDER BY ingested_at LIMIT 1",
            (digest, path.name, broker),
        ).fetchone()
        if twin:
            result.already_ingested = True
            result.duplicate_of = str(twin["source_file"])
            result.warnings.append(
                f"{path.name} is byte-identical to already-ingested "
                f"{result.duplicate_of}; skipped. Delete it or run "
                f"`optjournal prune`."
            )
            return result

    asset_filter = ",".join(assets) or "ALL"

    # Every section arrives through the broker seam (sources.py). Nothing below
    # reads a py_ibkr model or an IBKR attribute name, so a second broker is a new
    # StatementSource and an unchanged writer -- which is the whole claim the seam
    # makes, and which was only true of the trade path until now.
    #
    # Provenance first: `statements.source_file` is a foreign key from every other
    # table, so its row has to exist before theirs.
    for meta in source.metadata(path):
        conn.execute(
            "INSERT INTO statements (broker, source_file, sha256, account_id,"
            " from_date, to_date, when_generated, base_currency, asset_filter,"
            " ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_file) DO UPDATE SET"
            " sha256=excluded.sha256, ingested_at=excluded.ingested_at,"
            " asset_filter=excluded.asset_filter",
            (
                broker,
                path.name,
                digest,
                meta.account_id,
                meta.from_date,
                meta.to_date,
                meta.generated_at,
                meta.base_currency,
                asset_filter,
                _now(),
            ),
        )

    base_currency = source.base_currency(path)
    for _account_id, fills in source.statements(path):
        _ingest_trades(conn, fills, path.name, assets, result,
                       base_currency=base_currency, broker=broker,
                       source_kind=source_kind)

    _ingest_cash(conn, source.cash_transactions(path), path.name, result,
                 broker=broker)
    _ingest_positions(conn, source.positions(path), path.name, assets, result,
                      broker=broker)
    _ingest_securities(conn, source.securities(path), assets, result, broker=broker)
    _ingest_equity_summaries(conn, source.equity_summaries(path), path.name, result,
                             broker=broker)

    conn.commit()
    return result


def ingest_confirms(
    conn: sqlite3.Connection,
    path: Path,
    *,
    base_currency: str,
    assets: Iterable[str] = DEFAULT_ASSET_FILTER,
    broker: str = DEFAULT_BROKER,
    rate_for: Any = None,
) -> IngestResult:
    """Ingest one archived Trade Confirmation payload. Safe to call repeatedly.

    A confirm is FILLS ONLY. It carries no cash transactions, no position
    snapshot, no securities and no NAV, so this writes the provenance row and the
    trades and nothing else -- rather than calling `ingest_file`, which would ask
    a confirm reader for five sections it does not have.

    IDEMPOTENT BY RANK, not by digest. `ingest_file` short-circuits on unchanged
    bytes; that is wrong here, because the useful case is the SAME query polled
    again through the session, where the payload grows by a fill and re-reading the
    earlier ones must be free. `_ingest_trades` already does that: a fill whose
    stored row came from an equal-or-better source is skipped, so re-ingesting
    costs a few primary-key lookups and changes nothing.

    `base_currency` is passed in because a confirm has no AccountInformation
    section to read it from -- the journal's own statements are the only honest
    source. `rate_for` is injected so the fetch of a live FX rate is the caller's
    to make, cache and test.
    """
    path = Path(path)
    result = IngestResult(source_file=path.name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if rate_for is None:
        rate_for = _live_rate_for(base_currency)

    # Provenance first: `statements.source_file` is a foreign key from `trades`,
    # so the row has to exist before any fill can reference it. A confirm IS a
    # source file, and recording it keeps every trade row's provenance answerable.
    for meta in confirm_meta(path, base_currency=base_currency):
        conn.execute(
            "INSERT INTO statements (broker, source_file, sha256, account_id,"
            " from_date, to_date, when_generated, base_currency, asset_filter,"
            " ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_file) DO UPDATE SET"
            " sha256=excluded.sha256, ingested_at=excluded.ingested_at",
            (
                broker, path.name, digest, meta.account_id, meta.from_date,
                meta.to_date, meta.generated_at, meta.base_currency,
                ",".join(assets) or "ALL", _now(),
            ),
        )

    pairs = parse_confirms(path, rate_for=lambda ccy: rate_for(ccy)[0])
    for _account_id, fill in pairs:
        estimated = rate_for(fill.currency)[1]
        _ingest_trades(conn, [fill], path.name, assets, result,
                       base_currency=base_currency, broker=broker,
                       source_kind=CONFIRM_SOURCE, fx_rate_estimated=estimated)
    conn.commit()
    return result


def _live_rate_for(base_currency: str) -> Any:
    """A memoised `currency -> (rate, estimated)` for one ingest.

    ONE FETCH PER CURRENCY, not per fill: a session with fourteen USD fills must
    not make fourteen identical HTTP requests, and a rate that moved between the
    first and the last would also store fills converted at different rates for the
    same minute. Memoised per call rather than globally, so a long-running server
    re-reads the rate on the next poll instead of holding one from the open.
    """
    cache: dict[str | None, tuple[float, bool]] = {}

    def resolve(currency: str | None) -> tuple[float, bool]:
        if currency not in cache:
            cache[currency] = base_rate(currency, base_currency)
        return cache[currency]

    return resolve


def _commission_base(
    commission: float | None,
    commission_ccy: str | None,
    instrument_ccy: str | None,
    rate: float,
    base_ccy: str | None,
) -> float | None:
    """The commission in base currency, converted at a rate that applies to it.

    `fxRateToBase` belongs to the INSTRUMENT's currency, so using it on the
    commission is right only while the two currencies agree. On this account
    they disagree on FX conversions: IBKR bills the commission on an EUR.SEK
    conversion in EUR while the row's currency is SEK, and multiplying a EUR
    amount by the SEK->EUR rate stored a figure 11x too small.

    Three cases, in order of what the data can actually support:

    * commission already in base -- no conversion applies, rate is 1.
    * commission in the instrument's currency -- fxRateToBase is its rate.
    * neither -- the statement carries no rate for that currency, so None is
      the honest answer. The native amount is stored regardless, and the
      caller warns; inventing a rate would be worse than admitting the gap.
    """
    if commission is None:
        return None
    if base_ccy and commission_ccy and commission_ccy == base_ccy:
        return commission
    if not commission_ccy or commission_ccy == instrument_ccy:
        return commission * rate
    return None


def _stored_rank(conn, broker: str, trade_id: Any) -> int | None:
    """The rank of the fill already stored under this key, or None if new.

    Read BEFORE the write, and it is what decides the write -- rather than a
    `WHERE` on the upsert, which `bars.upsert_bars` can use because it counts
    only rows touched. Here `trades_inserted` has to keep meaning "fills the
    journal had not seen": it is printed by the nightly cron and the Sync
    button, and `DO UPDATE` reports a supersede and an insert identically, so
    superseding yesterday's confirms would announce them as new fills every
    morning. Knowing whether the row existed is the only way to tell those
    apart, so the rank comparison lives here too, once, in Python.
    """
    row = conn.execute(
        "SELECT source_kind FROM trades WHERE broker = ? AND trade_id = ?",
        (broker, trade_id),
    ).fetchone()
    if row is None:
        return None
    return SOURCE_RANK.get(row["source_kind"], 0)


def _ingest_trades(conn, fills, source_file: str, assets, result: IngestResult,
                   base_currency: str | None = None,
                   broker: str = DEFAULT_BROKER,
                   source_kind: str = ACTIVITY_SOURCE,
                   fx_rate_estimated: bool = False) -> None:
    """Write broker-neutral fills into the trades table.

    Reads `NormalisedFill`s (sources.py), never a broker's own model, so this
    writer is the same for every broker. `broker` stamps the row and joins the
    composite key `(broker, trade_id)`.

    It is a parameter rather than `DEFAULT_BROKER` inline because that constant
    was what this wrote before, which made `ingest_file(broker=...)` accept a
    broker, resolve its source, read its statement -- and then file every row
    under 'ibkr'. Nothing failed: the schema default agreed with the hardcoded
    value while IBKR was the only broker, so the argument was decorative and
    would have stayed decorative until a second broker's rows collided with the
    first's on `(broker, trade_id)`.

    `source_kind` says WHICH Flex query these fills came from, and ranks them
    (`SOURCE_RANK`): a same-session Trade Confirmation may be superseded by the
    next day's Activity Statement, never the other way round.

    `fx_rate_estimated` records that `fill.fx_rate_to_base` did not come from the
    broker. A confirm carries no rate, so the caller fetched a live one and every
    `*_base` figure written here is an estimate until the statement supersedes it.
    Stamped per row rather than derived from `source_kind`, because a confirm
    already quoted in the base currency has a rate of exactly 1.0 and nothing
    about it is estimated.
    """
    incoming_rank = SOURCE_RANK.get(source_kind, 0)
    for fill in fills:
        if not _matches_filter(fill.asset_category, assets):
            result.trades_filtered_out += 1
            continue

        qty = fill.quantity
        if qty is None:
            result.warnings.append(f"trade {fill.trade_id}: unparseable quantity")
            continue

        stored_rank = _stored_rank(conn, broker, fill.trade_id)
        if stored_rank is not None and incoming_rank <= stored_rank:
            # Already known, and this query knows no more about it than the row
            # does. Covers both the overlapping statements this journal has
            # always re-read and a confirm query re-run after the Activity
            # Statement has landed -- which must not walk the settled figures
            # back to what the fill looked like mid-session.
            result.trades_skipped_existing += 1
            continue

        rate = fill.fx_rate_to_base
        proceeds = fill.proceeds
        commission = fill.commission
        realized = fill.realized_pnl
        # `ib_commission_base` is commission x fxRateToBase, and that rate is
        # the INSTRUMENT's. So the conversion is only right while the commission
        # is billed in the instrument's currency. A broker that sends the
        # commission currency separately lets this be checked rather than
        # assumed; it agrees on every IBKR row observed, but agreement that is
        # assumed rather than checked fails silently. A warning, not a raise: a
        # real broker quirk should surface, not abort an ingest -- the native
        # figure is stored correctly either way, only the base conversion is
        # suspect.
        commission_ccy = fill.commission_currency
        commission_base = _commission_base(
            commission, commission_ccy, fill.currency, rate, base_currency
        )

        conn.execute(
            _TRADE_UPSERT,
            (
                broker,
                fill.trade_id, fill.exec_id, fill.transaction_id, fill.order_id,
                fill.account_id, fill.trade_date, fill.date_time,
                fill.asset_category, fill.symbol, fill.contract_id,
                fill.underlying_symbol, fill.underlying_contract_id, fill.put_call,
                fill.strike, fill.expiry, fill.multiplier, fill.buy_sell,
                fill.open_close, fill.notes, fill.level_of_detail, qty,
                fill.trade_price, fill.currency, rate, proceeds,
                None if proceeds is None else proceeds * rate,
                commission,
                commission_base,
                commission_ccy,
                fill.taxes, realized,
                None if realized is None else realized * rate,
                fill.mtm_pnl,
                json.dumps(fill.raw, default=str, sort_keys=True),
                source_file, _now(), source_kind, int(fx_rate_estimated),
            ),
        )
        if stored_rank is None:
            result.trades_inserted += 1
        else:
            result.trades_superseded += 1

        # Warned only for a row this ingest actually wrote, because a warning is
        # a report of a decision TAKEN -- and an already-known fill took no
        # decision, having `continue`d above. Emitting it for every fill read
        # made the nightly cron report "0 new trade(s)" next to a per-trade
        # warning, every run, about one row settled on 2026-08-03. It would have
        # gone on firing until that row aged out of IBKR's rolling window in
        # August 2027, and a warning that fires daily on correctly-handled data
        # is one nobody reads when it finally means something.
        if commission and commission_ccy and commission_ccy != fill.currency:
            handled = (
                f"treated as already-base {base_currency}"
                if commission_ccy == base_currency
                else "left unconverted: the statement carries no rate for it"
            )
            result.warnings.append(
                f"trade {fill.trade_id}: commission billed in {commission_ccy}"
                f" but the instrument trades in {fill.currency}; {handled}"
            )


def _ingest_cash(conn, cash, source_file: str, result: IngestResult,
                 broker: str = DEFAULT_BROKER) -> None:
    """Write broker-neutral cash transactions. `amount_base` is ours to derive."""
    for c in cash:
        cur = conn.execute(
            "INSERT INTO cash_transactions (broker, transaction_id, account_id,"
            " date_time,"
            " settle_date, type, description, symbol, conid, amount, currency,"
            " fx_rate_to_base, amount_base, raw, source_file, first_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(broker, transaction_id) DO NOTHING",
            (
                broker,
                c.transaction_id, c.account_id, c.date_time,
                c.settle_date, c.kind,
                c.description, c.symbol, c.contract_id, c.amount,
                c.currency, c.fx_rate_to_base, c.amount * c.fx_rate_to_base,
                json.dumps(c.raw, default=str, sort_keys=True),
                source_file, _now(),
            ),
        )
        if cur.rowcount:
            result.cash_inserted += 1
        else:
            result.cash_skipped_existing += 1


def _ingest_positions(conn, positions, source_file: str, assets, result,
                      broker: str = DEFAULT_BROKER) -> None:
    """Write broker-neutral position snapshots, replacing the same day's row."""
    for p in positions:
        cat = p.asset_category or ""
        if not _matches_filter(cat, assets):
            continue
        rate = p.fx_rate_to_base
        value = p.position_value
        conn.execute(
            "INSERT INTO position_snapshots (broker, report_date, conid, account_id,"
            " symbol,"
            " asset_category, underlying_symbol, put_call, strike, expiry, multiplier,"
            " position, mark_price, position_value, position_value_base,"
            " cost_basis_money, cost_basis_price, fifo_pnl_unrealized, side,"
            " open_date_time, currency, fx_rate_to_base, raw, source_file, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(broker, report_date, conid) DO UPDATE SET"
            " position=excluded.position, mark_price=excluded.mark_price,"
            " position_value=excluded.position_value,"
            " position_value_base=excluded.position_value_base,"
            " fifo_pnl_unrealized=excluded.fifo_pnl_unrealized,"
            " raw=excluded.raw, source_file=excluded.source_file,"
            " ingested_at=excluded.ingested_at",
            (
                broker,
                p.as_of, p.contract_id,
                p.account_id, p.symbol, cat,
                p.underlying_symbol, p.put_call,
                p.strike, p.expiry,
                p.multiplier, p.quantity,
                p.mark_price, value,
                None if value is None else value * rate,
                p.cost_basis, p.cost_basis_price,
                p.unrealized_pnl, p.side,
                p.opened_at, p.currency or "EUR",
                rate, json.dumps(p.raw, default=str, sort_keys=True),
                source_file, _now(),
            ),
        )
        result.positions_written += 1


def _ingest_securities(conn, securities, assets, result,
                       broker: str = DEFAULT_BROKER) -> None:
    """Write broker-neutral contract definitions, upserting on (broker, conid)."""
    for s in securities:
        cat = s.asset_category or ""
        if not _matches_filter(cat, assets):
            continue
        conn.execute(
            "INSERT INTO securities (broker, conid, symbol, description,"
            " asset_category,"
            " sub_category, currency, multiplier, strike, expiry, put_call,"
            " underlying_conid, underlying_symbol, isin, listing_exchange, raw,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(broker, conid) DO UPDATE SET"
            " symbol=excluded.symbol, description=excluded.description,"
            " raw=excluded.raw, updated_at=excluded.updated_at",
            (
                broker,
                s.contract_id, s.symbol,
                s.description, cat, s.sub_category,
                s.currency, s.multiplier,
                s.strike, s.expiry,
                s.put_call, s.underlying_contract_id,
                s.underlying_symbol, s.isin,
                s.listing_exchange,
                json.dumps(s.raw, default=str, sort_keys=True),
                _now(),
            ),
        )
        result.securities_written += 1


def _ingest_equity_summaries(conn, navs, source_file: str, result,
                             broker: str = DEFAULT_BROKER) -> None:
    """Daily Net Asset Value rows.

    NAV is the one figure the trade ledger cannot reconstruct -- deriving cash
    needs a starting balance no Activity statement carries -- so this is reported
    data, not derived.

    The tolerance for IBKR's two shapes (`cash` versus `cashLong`/`cashShort`)
    moved to `sources._combined`, where the vocabulary belongs. A row missing its
    total is dropped by the source, so there is nothing to warn about here: the
    warning it used to emit named `reportDate`, an IBKR field this writer can no
    longer see, which is the point.
    """
    for nav in navs:
        conn.execute(
            "INSERT INTO equity_summaries (broker, report_date, account_id,"
            " currency,"
            " cash_base, stock_base, options_base, total_base, raw,"
            " source_file, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(broker, report_date) DO UPDATE SET"
            " cash_base=excluded.cash_base, stock_base=excluded.stock_base,"
            " options_base=excluded.options_base, total_base=excluded.total_base,"
            " raw=excluded.raw, source_file=excluded.source_file,"
            " ingested_at=excluded.ingested_at",
            (
                broker, nav.as_of, nav.account_id, nav.currency,
                nav.cash, nav.stock,
                nav.options, nav.total,
                json.dumps(nav.raw, default=str, sort_keys=True),
                source_file, _now(),
            ),
        )
        result.equity_summaries_written += 1


