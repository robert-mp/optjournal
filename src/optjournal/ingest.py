"""Fold archived Flex statements into the journal database.

Ingest is idempotent by construction. Statements overlap -- a 30-day and a
365-day query both contain the same fills, verified against real data -- so
trades and cash transactions are inserted with first-write-wins on IBKR's
own identifiers, and `first_seen_at` records when the journal first saw a
row rather than when it was last re-presented.

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

from optjournal.db import DEFAULT_BROKER
from optjournal.flex import load
from optjournal.sections import raw_sections
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


@dataclass(slots=True)
class IngestResult:
    source_file: str
    already_ingested: bool = False
    #: Set when this file was skipped because another already-ingested file
    #: holds byte-identical content. Distinct from `already_ingested` alone,
    #: which also covers re-ingesting the same filename.
    duplicate_of: str | None = None
    trades_inserted: int = 0
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


def _qty(value: Any) -> int | float | None:
    """Coerce a quantity, keeping integers exact and fractions lossless.

    Options quantities are always integral and are stored as ints, which is
    what keeps the episode flat-test exact. Stock and currency quantities are
    legitimately fractional -- dividend reinvestment buys 1.79 shares, and a
    full SIVE sale was 413.22 of them -- so those keep their value rather
    than being truncated to a different position size. SQLite's INTEGER
    affinity stores a non-integral value as REAL, losslessly.
    """
    f = _f(value)
    if f is None:
        return None
    i = int(round(f))
    return i if abs(f - i) < 1e-9 else f


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _enum_value(value: Any) -> str | None:
    """py_ibkr yields Enum members; store the wire value, not 'Class.NAME'."""
    if value is None:
        return None
    return _s(getattr(value, "value", value))


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
) -> IngestResult:
    """Ingest one archived statement. Safe to call repeatedly.

    `broker` selects the statement source (see sources.py) and is stamped on
    every trade row. Defaults to IBKR, the only source today, so existing
    callers are unchanged.
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

    resp = load(path)
    sections = raw_sections(path)
    asset_filter = ",".join(assets) or "ALL"

    for stmt in resp.FlexStatements:
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
                _s(stmt.accountId),
                _s(stmt.fromDate),
                _s(stmt.toDate),
                _s(stmt.whenGenerated),
                _base_currency(sections),
                asset_filter,
                _now(),
            ),
        )

        _ingest_cash(conn, stmt, path.name, result, broker=broker)

    # Trades come through the broker seam (sources.py), not by reading py_ibkr
    # attributes here -- so a second broker is a new source, not an edit to this
    # writer. Run after the statement rows above, because a trade's source_file
    # references one. The other sections still read py_ibkr/raw dicts directly;
    # moving them across the same boundary is future work, and the trade path is
    # where the IBKR vocabulary was densest.
    base_currency = _base_currency(sections)
    for _account_id, fills in source.statements(path):
        _ingest_trades(conn, fills, path.name, assets, result,
                       base_currency=base_currency, broker=broker)

    _ingest_positions(conn, sections, path.name, assets, result, broker=broker)
    _ingest_securities(conn, sections, assets, result)
    _ingest_equity_summaries(conn, sections, path.name, result)

    conn.commit()
    return result


def _base_currency(sections: dict[str, list[dict[str, str]]]) -> str:
    rows = sections.get("AccountInformation") or []
    return (rows[0].get("currency") if rows else None) or "EUR"


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


def _ingest_trades(conn, fills, source_file: str, assets, result: IngestResult,
                   base_currency: str | None = None,
                   broker: str = DEFAULT_BROKER) -> None:
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
    """
    for fill in fills:
        if not _matches_filter(fill.asset_category, assets):
            result.trades_filtered_out += 1
            continue

        qty = fill.quantity
        if qty is None:
            result.warnings.append(f"trade {fill.trade_id}: unparseable quantity")
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

        cur = conn.execute(
            "INSERT INTO trades (broker, trade_id, ib_exec_id, transaction_id, ib_order_id,"
            " account_id, trade_date, date_time, asset_category, symbol, conid,"
            " underlying_symbol, underlying_conid, put_call, strike, expiry,"
            " multiplier, buy_sell, open_close, notes, level_of_detail, quantity,"
            " trade_price, currency, fx_rate_to_base, proceeds, proceeds_base,"
            " ib_commission, ib_commission_base, ib_commission_currency, taxes,"
            " fifo_pnl_realized,"
            " fifo_pnl_realized_base, mtm_pnl, raw, source_file, first_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(broker, trade_id) DO NOTHING",
            (
                broker,
                fill.trade_id, fill.exec_id, fill.transaction_id, fill.order_id,
                fill.account_id, fill.trade_date, fill.date_time,
                fill.asset_category, fill.symbol, fill.conid,
                fill.underlying_symbol, fill.underlying_conid, fill.put_call,
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
                source_file, _now(),
            ),
        )
        if cur.rowcount:
            result.trades_inserted += 1
            # Warned here rather than beside the conversion, because a warning
            # is a report of a decision TAKEN -- and on a duplicate row no
            # decision is taken, the INSERT is a no-op. Emitting it before the
            # insert made the nightly cron report "0 new trade(s)" next to a
            # per-trade warning, every run, about one row settled on
            # 2026-08-03. It would have gone on firing until that row aged out
            # of IBKR's rolling window in August 2027, and a warning that fires
            # daily on correctly-handled data is one nobody reads when it
            # finally means something.
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
        else:
            result.trades_skipped_existing += 1


def _ingest_cash(conn, stmt, source_file: str, result: IngestResult,
                 broker: str = DEFAULT_BROKER) -> None:
    for c in stmt.CashTransactions or ():
        rate = _f(c.fxRateToBase) or 1.0
        amount = _f(c.amount) or 0.0
        cur = conn.execute(
            "INSERT INTO cash_transactions (broker, transaction_id, account_id,"
            " date_time,"
            " settle_date, type, description, symbol, conid, amount, currency,"
            " fx_rate_to_base, amount_base, raw, source_file, first_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(broker, transaction_id) DO NOTHING",
            (
                broker,
                _s(c.transactionID), _s(stmt.accountId), _s(c.dateTime),
                _s(getattr(c, "settleDate", None)), _enum_value(c.type),
                _s(c.description), _s(c.symbol), _s(c.conid), amount,
                _s(c.currency), rate, amount * rate,
                json.dumps(_model_dump(c), default=str, sort_keys=True),
                source_file, _now(),
            ),
        )
        if cur.rowcount:
            result.cash_inserted += 1
        else:
            result.cash_skipped_existing += 1


def _ingest_positions(conn, sections, source_file: str, assets, result,
                      broker: str = DEFAULT_BROKER) -> None:
    for row in sections.get("OpenPositions") or ():
        cat = (row.get("assetCategory") or "").upper()
        if not _matches_filter(cat, assets):
            continue
        rate = _f(row.get("fxRateToBase")) or 1.0
        value = _f(row.get("positionValue"))
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
                _s(row.get("reportDate")), _s(row.get("conid")),
                _s(row.get("accountId")), _s(row.get("symbol")), cat,
                _s(row.get("underlyingSymbol")), _s(row.get("putCall")),
                _f(row.get("strike")), _s(row.get("expiry")),
                _f(row.get("multiplier")), _qty(row.get("position")),
                _f(row.get("markPrice")), value,
                None if value is None else value * rate,
                _f(row.get("costBasisMoney")), _f(row.get("costBasisPrice")),
                _f(row.get("fifoPnlUnrealized")), _s(row.get("side")),
                _s(row.get("openDateTime")), _s(row.get("currency")) or "EUR",
                rate, json.dumps(row, sort_keys=True), source_file, _now(),
            ),
        )
        result.positions_written += 1


def _ingest_securities(conn, sections, assets, result) -> None:
    for row in sections.get("SecuritiesInfo") or ():
        cat = (row.get("assetCategory") or "").upper()
        if not _matches_filter(cat, assets):
            continue
        conn.execute(
            "INSERT INTO securities (conid, symbol, description, asset_category,"
            " sub_category, currency, multiplier, strike, expiry, put_call,"
            " underlying_conid, underlying_symbol, isin, listing_exchange, raw,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(conid) DO UPDATE SET"
            " symbol=excluded.symbol, description=excluded.description,"
            " raw=excluded.raw, updated_at=excluded.updated_at",
            (
                _s(row.get("conid")), _s(row.get("symbol")),
                _s(row.get("description")), cat, _s(row.get("subCategory")),
                _s(row.get("currency")), _f(row.get("multiplier")),
                _f(row.get("strike")), _s(row.get("expiry")),
                _s(row.get("putCall")), _s(row.get("underlyingConid")),
                _s(row.get("underlyingSymbol")), _s(row.get("isin")),
                _s(row.get("listingExchange")), json.dumps(row, sort_keys=True),
                _now(),
            ),
        )
        result.securities_written += 1


def _ingest_equity_summaries(conn, sections, source_file: str, result) -> None:
    """Daily Net Asset Value rows, from the EquitySummaryInBase section.

    Only present when the Flex query template has the "Equity Summary in Base"
    section enabled; absent sections simply yield nothing here. NAV is the one
    figure the trade ledger cannot reconstruct -- deriving cash needs a
    starting balance no Activity statement carries -- so this is reported
    data, not derived.

    Field access is tolerant of IBKR's shape: some deployments emit a single
    `cash`/`stock`/`options` figure, others split them into `*Long`/`*Short`
    pairs. Both are accepted; `total` is required, because a NAV row without
    a NAV is noise.
    """
    def combined(row: dict[str, str], name: str) -> float | None:
        whole = _f(row.get(name))
        if whole is not None:
            return whole
        long_, short = _f(row.get(f"{name}Long")), _f(row.get(f"{name}Short"))
        if long_ is None and short is None:
            return None
        return (long_ or 0.0) + (short or 0.0)

    base = _base_currency(sections)
    for row in sections.get("EquitySummaryInBase") or ():
        day = _s(row.get("reportDate"))
        total = combined(row, "total")
        if not day or total is None:
            result.warnings.append(
                f"equity summary row skipped: reportDate={day!r} total missing"
            )
            continue
        conn.execute(
            "INSERT INTO equity_summaries (report_date, account_id, currency,"
            " cash_base, stock_base, options_base, total_base, raw,"
            " source_file, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(report_date) DO UPDATE SET"
            " cash_base=excluded.cash_base, stock_base=excluded.stock_base,"
            " options_base=excluded.options_base, total_base=excluded.total_base,"
            " raw=excluded.raw, source_file=excluded.source_file,"
            " ingested_at=excluded.ingested_at",
            (
                day, _s(row.get("accountId")) or "", base,
                combined(row, "cash"), combined(row, "stock"),
                combined(row, "options"), total,
                json.dumps(row, sort_keys=True), source_file, _now(),
            ),
        )
        result.equity_summaries_written += 1


def _model_dump(model: Any) -> dict[str, Any]:
    """Best-effort dict of a pydantic model, for the `raw` column."""
    for attr in ("model_dump", "dict"):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - raw column is best-effort
                break
    return {}
