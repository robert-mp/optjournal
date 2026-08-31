"""The JSON payload the web page renders, and the CLI emits with --json.

This is the API contract: every key the page's JavaScript reads must be
produced here, and tests/test_web.py's binding guards enforce that in both
directions -- a key read but not sent fails a test, as does a payload
binding the page uses but never registered. Adding a view to the UI means
adding its serializer here and registering the binding with the guard.

Split out of render.py because the two halves change for different
reasons: this module changes when the page or the --json consumers need
new data; render.py changes when a human-readable terminal report needs
to look different. Keeping them together made every payload change wade
through table-formatting code and vice versa.

Serializers take domain objects (a connection, a CostReport, a
HistoryReport) and return JSON-safe dicts. No formatting: numbers stay
numbers, dates become ISO strings, Decimals become floats. Presentation
is the consumer's job -- the page formats for humans, --json does not.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from optjournal.analysis import CostReport
from optjournal.bars import (
    audit_perishable,
    close_series as bars_close_series,
    watch_closes,
    weekly_closes,
)
from optjournal.clock import MARKET_TZ, et_day, parse_day
from optjournal.costs import (
    AUTOFX_MARKUP_BPS,
    AUTOFX_MARKUP_MEASURED_BPS,
    Friction,
)
from optjournal.costs import CostReport as DbCostReport
from optjournal.events import (
    DEFAULT_COUNTRIES,
    DEFAULT_IMPACTS,
    IMPACT_ORDER,
    SOURCE,
    default_scope,
    upcoming,
)
from optjournal.history import HistoryReport
from optjournal.money import FILL_MONEY_FIELDS, Money
from optjournal.sections import raw_sections
from optjournal.stats import first_activity
from optjournal.trend import bucket, bxtrender_short
from optjournal.zdte import plan as zdte_plan
from optjournal.vol import (
    expected_move,
    rank,
    rank_band,
    realised_vol,
    realised_vol_series,
)

Row = dict[str, Any]

def _wire(value: Any) -> str:
    """Enum members stringify as 'AssetClass.OPTION'; we want the wire value.

    py_ibkr yields Enum members throughout, and their default str() leaks the
    class name into output. Prefer .value, falling back to the last
    dotted component for anything that only implements __str__.
    """
    if value is None:
        return "-"
    inner = getattr(value, "value", None)
    if isinstance(inner, str) and inner:
        return inner
    text = str(value)
    return text.rsplit(".", 1)[-1] if "." in text else text

def _num(value: Any) -> float | None:
    """Coerce a money/rate value to float for JSON.

    `analysis` computes in Decimal because py_ibkr parses money that way, but
    Decimal is not JSON-serialisable and `json.dumps(default=str)` silently
    turns it into a *string* -- so consumers received "42.728611289" where a
    number was expected. The schema already stores money as REAL, so float is
    the right wire type; this makes the JSON match that decision.
    """
    if value is None:
        return None
    return float(value)

def _iso(value: Any) -> str | None:
    """Render a date-like value as an ISO string for JSON.

    py_ibkr parses statement periods into `datetime.date`, which json.dumps
    rejects. Coercing here rather than leaning on the caller's `default=str`
    means the payload is self-contained JSON regardless of how it is dumped.
    """
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)

def summary_data(resp, path: Path | None = None) -> Row:
    """Structural summary of a parsed statement. No monetary values."""
    statements: list[Row] = []
    for stmt in resp.FlexStatements:
        trades = list(stmt.Trades or [])
        cash = list(stmt.CashTransactions or [])
        statements.append(
            {
                "account_id": str(stmt.accountId),
                "from_date": str(stmt.fromDate),
                "to_date": str(stmt.toDate),
                "trades": len(trades),
                "by_asset": dict(Counter(_wire(t.assetCategory) for t in trades)),
                "by_open_close": dict(
                    Counter(_wire(t.openCloseIndicator) for t in trades)
                ),
                "by_buy_sell": dict(Counter(_wire(t.buySell) for t in trades)),
                "distinct_orders": len({t.ibOrderID for t in trades if t.ibOrderID}),
                "underlyings": len({t.underlyingSymbol or t.symbol for t in trades}),
                "cash_transactions": len(cash),
                "cash_by_type": dict(Counter(_wire(c.type) for c in cash)),
            }
        )

    data: Row = {"statements": statements}
    if path is not None:
        data["unmodelled_sections"] = {
            k: len(v) for k, v in raw_sections(path).items() if v
        }
    return data

def _replace_with_money(row: Row, monies: dict[str, Row]) -> None:
    """Swap a DERIVED row's flat money triples for one `Money` each, in place.

    Only for rows nothing aggregates from. An order, a strategy group and a
    lifecycle are each derived, and every level derives from the leaf legs
    directly, so their flat triples have no remaining reader: six keys become
    three.

    A leg instead keeps its raw triple and gains its Money under `money` --
    the same split a position snapshot gets, for the same reason. The triple is
    the leaf datum every level above re-aggregates; the Money is one reading of
    it. They cannot be collapsed: a Money whose native is withheld for spanning
    currencies is indistinguishable from one that never had a native, so
    re-gating on it would silently drop a contributor.
    """
    for field, money in monies.items():
        row.pop(f"{field}_base", None)
        row[field] = money


def orders_data(
    conn: sqlite3.Connection,
    order_ids: frozenset[str] | None = None,
    asset_category: str = "OPT",
) -> list[Row]:
    """Orders of one asset category with their legs, optionally restricted
    to a set of order ids.

    Reads the category-generic `trade_orders`/`trade_legs` views with a
    parameter rather than the OPT-only wrappers, because the Trades tab now
    follows the Trade Types control and stocks are a different category, not
    a subset of options. Id filtering stays in Python: the id set comes from
    episode membership, which no view column carries.
    """
    orders = conn.execute(
        "SELECT * FROM trade_orders WHERE asset_category = ?"
        " ORDER BY first_fill_at DESC",
        (asset_category,),
    ).fetchall()
    out: list[Row] = []
    for o in orders:
        if order_ids is not None and str(o["ib_order_id"]) not in order_ids:
            continue
        legs = conn.execute(
            "SELECT * FROM trade_legs WHERE ib_order_id = ?"
            " AND asset_category = ? ORDER BY expiry, strike",
            (o["ib_order_id"], asset_category),
        ).fetchall()
        row = dict(o)
        leg_rows = [dict(lg) for lg in legs]
        # Computed from the raw view columns BEFORE any are replaced -- the
        # order's figures aggregate the legs' natives, so overwriting a leg's
        # `proceeds` with its Money first would leave the order summing dicts.
        order_money = {f: Money.from_rows(leg_rows, f).payload()
                       for f in FILL_MONEY_FIELDS}
        for lg in leg_rows:
            # A single leg is single-currency by construction, so the gate has
            # nothing to decide -- but one code path from fill to lifecycle is
            # worth more than the shortcut.
            # Added under `money`, not replacing: strategy_groups and the
            # lifecycle both re-aggregate these same legs from the flat triple.
            lg["money"] = {f: Money.from_rows([lg], f).payload()
                           for f in FILL_MONEY_FIELDS}
        row["legs"] = leg_rows
        _replace_with_money(row, order_money)
        out.append(row)
    return out

def positions_data(conn: sqlite3.Connection) -> list[Row]:
    """Open-position snapshot rows, each with its money figures interpreted.

    The row is returned verbatim because it IS the record -- a point-in-time
    snapshot IBKR sent. The three `Money` keys beside it are the interpretation:
    a position carries its native amount and its own `fxRateToBase`, and two of
    the three have no base column at all, so the base was previously derived in
    the page by `natCash(v, rate)`. That multiplied to base and then applied the
    display rate, so a USD value shown under a USD toggle had round-tripped
    through EUR at two different rates -- the same defect corrected for
    commission. Deriving here means the page reads a `Money` and shows the
    native verbatim.

    There was a `cost_basis_fallback` parameter here, for "a snapshot row
    without a basis borrows the open episode's, matched on conid". It could
    never fire. Both sides read `position_snapshots.cost_basis_money` for the
    same latest `report_date` -- `current_option_positions` is a view over that
    table, and `history._from_snapshot` sets `Episode.cost_basis` from that
    column -- so whenever the row's basis was NULL the fallback's was too, and
    the caller's dict comprehension dropped it for being None. Measured on the
    archive: the one conid the fallback offered already held that exact value in
    its own row. Removed rather than left as dead insurance, because a fallback
    that cannot fire still reads as a reason to trust the field.
    """
    rows = conn.execute(
        "SELECT * FROM current_option_positions ORDER BY expiry, strike"
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        ccy, rate = row.get("currency"), row.get("fx_rate_to_base")
        row["value"] = Money.at_rate(row.get("position_value"), rate, ccy).payload()
        row["cost_basis"] = Money.at_rate(
            row.get("cost_basis_money"), rate, ccy).payload()
        row["unrealized"] = Money.at_rate(
            row.get("fifo_pnl_unrealized"), rate, ccy).payload()
        out.append(row)
    return out

def _money(base: Any, ledger: dict) -> Money:
    """A cost figure: the base total, plus the as-charged amount where one exists.

    analysis.py carries the per-currency breakdown and deliberately does not
    judge it -- it imports nothing internal and must stay that way. The gate
    lives once, in `Money`, and is applied here, where the base total and the
    ledger are both already in hand.

    Returning one `Money` rather than a `(amount, currency)` tuple is what
    removed the duplication this block used to carry: reaching each half of a
    tuple through subscripting meant `_gate` was called twice per payload key,
    and the `journal_friction` case spelled its whole dict comprehension twice.
    """
    return Money.gated(_num(base) or 0.0, {c: float(v) for c, v in ledger.items()})


def _journal_commission(report: CostReport) -> Money:
    """Commission for the journalled asset only, base and as-charged."""
    return _money(report.journal_commission_base, report.journal_native_by_ccy)


def _journal_per_unit(report: CostReport) -> Row | None:
    """Commission per contract, or None when the scope has no quantity.

    Both halves come from one `Money.per`, so the base and as-charged figures
    are guaranteed to share a denominator. The base used to come from a separate
    `CostReport.journal_per_unit_base` property while the native was divided
    here -- two divisions that had to agree by inspection. That property is gone
    rather than left unread, so there is no second answer to fall back to.
    """
    # float(), because `quantity` is a Decimal to keep fractional share lots
    # exact while they accumulate, and `Money` is the float domain the payload
    # speaks. Coerced here, at the boundary that already owns that conversion
    # (see `_num`), rather than letting a Decimal cross into `Money.per`.
    qty = float(sum(g.quantity for g in report.journal_commissions))
    per = _journal_commission(report).per(qty)
    return None if per is None else per.payload()


def costs_data(report: CostReport) -> Row:
    """Cost report as a JSON-safe structure.

    Hand-built rather than `dataclasses.asdict(report)`, which was the previous
    approach and silently wrong: asdict serialises *fields* only, so every
    computed total -- friction, commission, fees, the AutoFX estimate -- was
    absent from `costs --json`, leaving consumers a payload of raw components
    and no answers. The same trap applies per pair, where `autofx_spread_base`
    and `commission_bps` are properties.
    """
    return {
        "base_currency": report.base_currency,
        "from_date": _iso(report.from_date),
        "to_date": _iso(report.to_date),
        "fx_caveat": report.fx_caveat,
        "fx": [
            {
                "symbol": p.symbol,
                "conversions": p.conversions,
                "notional_base": _num(p.notional_base),
                "commission_base": _num(p.commission_base),
                "commission_bps": _num(p.commission_bps),
                "autofx_conversions": p.autofx_conversions,
                "autofx_notional_base": _num(p.autofx_notional_base),
                "autofx_spread_base": _num(p.autofx_spread_base),
            }
            for p in report.fx
        ],
        "commissions": [
            {
                "asset_category": g.asset_category,
                "fills": g.fills,
                "quantity": _num(g.quantity),
                "commission_base": _num(g.commission_base),
                "taxes_base": _num(g.taxes_base),
                "per_unit_base": _num(g.per_unit_base),
            }
            for g in report.commissions
        ],
        "fees": [
            {
                "name": c.name,
                "count": c.count,
                "total_base": _num(c.total_base),
                "examples": list(c.examples),
            }
            for c in report.fees
        ],
        "withholding": [
            {
                "symbol": w.symbol,
                "currency": w.currency,
                "gross_base": _num(w.gross_base),
                "withheld_base": _num(w.withheld_base),
                "effective_rate": _num(w.effective_rate),
            }
            for w in report.withholding
        ],
        "journal_asset": report.journal_asset,
        "totals": {
            # Each gated figure is one `Money` -- one gate call, one key, and
            # the amount inseparable from the currency it was charged in.
            "commission": _money(
                report.total_commission_base, report.total_native_by_ccy).payload(),
            "fees": _money(
                report.total_fees_base, report.total_fees_native_by_ccy).payload(),
            "taxes": _money(
                report.total_taxes_base, report.total_taxes_native_by_ccy).payload(),
            "autofx_notional_base": _num(report.total_autofx_notional_base),
            "autofx_spread_base": _num(report.total_autofx_spread_base),
            "stated_friction": _money(
                report.total_stated_friction_base,
                report.total_stated_friction_native_by_ccy).payload(),
            "friction_base": _num(report.total_friction_base),
            "fx_notional_base": _num(report.total_fx_notional_base),
            "fx_commission": _money(
                report.total_fx_commission_base,
                report.total_fx_commission_native_by_ccy).payload(),
            # Scope split. `commission` above spans the whole account, so
            # a consumer that wants this journal's cost must read the journal_*
            # keys -- presenting the account figure as the journal's was the
            # defect this split exists to remove.
            "journal_commission": _journal_commission(report).payload(),
            "journal_taxes": _money(
                report.journal_taxes_base,
                report.journal_taxes_native_by_ccy).payload(),
            "journal_friction": _money(
                report.journal_friction_base,
                report.journal_friction_native_by_ccy).payload(),
            # Divided by the same quantity the base figure uses, so the two
            # halves differ only in the currency of the numerator. `Money.per`
            # divides both at once, so an as-charged numerator can never end up
            # over a restated denominator.
            "journal_per_unit": _journal_per_unit(report),
            # Account-level figures take the same gate. On a multi-currency
            # account it almost always withholds -- these deliberately span
            # asset categories -- but "withheld because the scope is mixed" is
            # a different statement from "never considered", and only one of
            # them survives a currency becoming uniform later.
            #
            # account_friction and friction are flat base-only floats on
            # purpose: both include the estimated AutoFX markup, which was never
            # billed as a line item in any currency, so no as-charged figure
            # exists and the shape should not imply one could.
            "other_commission": _money(
                report.other_commission_base, report.other_native_by_ccy).payload(),
            "other_taxes": _money(
                report.other_taxes_base, report.other_taxes_native_by_ccy).payload(),
            "account_friction_base": _num(report.account_friction_base),
            "credit_fills": sum(g.credit_fills for g in report.commissions),
        },
    }


def broker_costs_data(report: DbCostReport) -> Row:
    """The DB-backed cost report as a JSON-safe structure.

    Distinct from `costs_data`, which serialises the statement-derived
    `analysis.CostReport`. Both exist on purpose: the CLI reports one statement's
    own costs and the page reports the journal's, and a single serializer would
    have to blur two different scopes into one shape.

    Every cost is a `Charge`, so each figure carries the ledger it was billed in
    alongside the gated single figure -- `{base, native, ccy, charged}`. A
    consumer already reading a money-shaped payload needs no new branch; one that
    wants to dissect a mixed-currency total reads `charged`.
    """
    return {
        "base_currency": report.base_currency,
        "from_date": report.from_date,
        "to_date": report.to_date,
        # What the reader selected, echoed back so the page labels its own scope
        # from the payload rather than from its local state -- the two drifting is
        # how a total comes to be captioned with the wrong scope.
        "scope": {
            "categories": sorted(report.scope.categories),
            "is_everything": report.scope.is_everything,
            "subset": report.scope.subset,
            "is_subset": report.scope.is_subset,
        },
        "by_category": [
            {
                "category": c.category,
                "fills": c.fills,
                "orders": c.orders,
                "quantity": _num(c.quantity),
                "commission": c.commission.payload(),
                "taxes": c.taxes.payload(),
                "total": c.total.payload(),
                # None where a per-unit figure is not a rate anyone charges --
                # a conversion's quantity is an amount of money.
                "per_unit": None if c.per_unit is None else c.per_unit.payload(),
                "credit_fills": c.credit_fills,
            }
            for c in report.by_category
        ],
        "fx": [
            {
                "symbol": p.symbol,
                "conversions": p.conversions,
                "notional_base": _num(p.notional),
                "commission": p.commission.payload(),
                "commission_bps": _num(p.commission_bps),
                "auto": {
                    "conversions": p.auto.conversions,
                    "notional_base": _num(p.auto.notional),
                    # A bare float, not a Charge: no currency was ever billed.
                    "markup_base": _num(p.markup_at(AUTOFX_MARKUP_BPS)),
                    "markup_high_base": _num(
                        p.markup_at(AUTOFX_MARKUP_MEASURED_BPS)
                    ),
                },
                "manual": {
                    "conversions": p.manual.conversions,
                    "notional_base": _num(p.manual.notional),
                    "commission": p.manual.commission.payload(),
                },
            }
            for p in report.fx
        ],
        "fees": [
            {
                "name": f.name,
                "count": f.count,
                "total": f.total.payload(),
                "examples": list(f.examples),
            }
            for f in report.fees
        ],
        "withholding": [
            {
                "symbol": w.symbol,
                "currency": w.currency,
                "gross": w.gross.payload(),
                "withheld": w.withheld.payload(),
                "effective_rate": _num(w.effective_rate),
            }
            for w in report.withholding
        ],
        "totals": {
            # The three-way split the tab is built on: what narrows with the
            # scope, what cannot be attributed at all, and what was never billed.
            "attributable": report.attributable.payload(),
            "unattributable": report.unattributable.payload(),
            "fills": report.fills,
            "credit_fills": report.credit_fills,
            "autofx": {
                "conversions": report.autofx_conversions,
                "notional_base": _num(report.autofx_notional),
                "bps": AUTOFX_MARKUP_BPS,
                "measured_bps": AUTOFX_MARKUP_MEASURED_BPS,
            },
            "friction": _friction_payload(report.friction),
        },
    }


def _friction_payload(friction: Friction) -> Row:
    """Measured and estimated, kept apart in the payload as they are in the type.

    `stated` is a Charge -- billed, per currency. The estimate is a RANGE of bare
    floats, because IBKR publishes the markup as "typically" 3 bps and a year of
    real conversions implied 3.2: around a quarter of account friction is this
    figure, so sending only a point estimate would invite the page to render it
    as a measurement. The midpoint is sent too, because a headline has to print
    one number -- but it arrives beside the ends it came from, never instead of
    them. `total_*` are precomputed so no consumer has to know the estimate is
    additive.
    """
    return {
        "stated": friction.stated.payload(),
        "estimated_low_base": _num(friction.estimated_low),
        "estimated_mid_base": _num(friction.estimated_mid),
        "estimated_high_base": _num(friction.estimated_high),
        "total_low_base": _num(friction.total_low),
        # What the headline prints. Sent rather than derived in the page so the
        # figure on screen and the figure in `--json` cannot drift, and so the
        # midpoint rule lives in one place.
        "total_mid_base": _num(friction.total_mid),
        "total_high_base": _num(friction.total_high),
        "is_estimated": friction.is_estimated,
    }


def history_data(report: HistoryReport) -> Row:
    """Closed-position history as a JSON-safe structure."""
    def one(e) -> Row:
        return {
            "symbol": e.symbol,
            "conid": e.conid,
            "underlying_symbol": e.underlying_symbol,
            "put_call": e.put_call,
            "strike": e.strike,
            "expiry": e.expiry,
            "status": e.status,
            "entry_outside_window": e.entry_outside_window,
            "snapshot_only": e.snapshot_only,
            "cost_basis": e.cost_basis,
            "unrealized": e.unrealized,
            "opened_at": e.opened_at,
            "closed_at": e.closed_at,
            "holding_days": e.holding_days,
            # Classified here rather than in the page: the comparison needs
            # date parsing (opened_at carries a time, expiry does not) and is
            # not the same question as holding_days == 0. See Episode.is_odte.
            "is_odte": e.is_odte,
            "contracts": e.contracts,
            "open_fills": e.open_fills,
            "close_fills": e.close_fills,
            "net_qty": e.net_qty,
            # Both readings are known for one episode, and an episode is
            # single-currency by construction, so the plain constructor applies
            # -- there is no gate to ask.
            "realized_pnl": Money(
                base=e.realized_pnl_base, native=e.realized_pnl,
                currency=e.currency).payload(),
            "realized_is_net_of_commission": e.net_of_commission,
            "commission": Money(
                base=e.commission_base, native=e.commission,
                currency=e.currency).payload(),
            "proceeds": Money(
                base=e.proceeds_base, native=e.proceeds,
                currency=e.currency).payload(),
            "currency": e.currency,
            "notes": e.notes,
        }

    return {
        "asset_category": report.asset_category,
        "base_currency": report.base_currency,
        "snapshot_date": report.snapshot_date,
        "partial_record": report.partial_record,
        "closed": [one(e) for e in report.closed],
        "open": [one(e) for e in report.open],
        "totals": {
            "closed_episodes": len(report.closed),
            "open_episodes": len(report.open),
            "realized_base": _num(report.total_realized_base),
            "commission_base": _num(report.total_commission_base),
            "wins": report.wins,
            "losses": report.losses,
            "win_rate": report.win_rate,
        },
    }

def statements_data(
    archive_dir: Path, conn: sqlite3.Connection | None = None
) -> list[Row]:
    """List archived statements, flagging which have been ingested."""
    ingested: dict[str, Row] = {}
    if conn is not None:
        try:
            ingested = {
                r["source_file"]: dict(r)
                for r in conn.execute("SELECT * FROM statements")
            }
        except sqlite3.OperationalError:
            ingested = {}

    out: list[Row] = []
    for path in sorted(archive_dir.glob("activity-*.xml")):
        meta = ingested.get(path.name)
        out.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "ingested": meta is not None,
                "from_date": meta["from_date"] if meta else None,
                "to_date": meta["to_date"] if meta else None,
                "asset_filter": meta["asset_filter"] if meta else None,
            }
        )
    return out


def market_data(
    conn: sqlite3.Connection, *, now: datetime, days: int = 7
) -> Row:
    """The economic calendar as the Market tab draws it: a week, and its events.

    Two shapes rather than one list, because the page needs both and deriving the
    week strip in JavaScript would put a second calendar in a second language.
    `week` is always seven days even when empty -- a strip with gaps in it would
    read as missing data rather than as a quiet Tuesday.

    The week is Monday-anchored (`weekday()`), which is what the mockup shows and
    what an economic calendar means by a week; `days` extends the EVENT list past
    it without stretching the strip.

    Rendered in `MARKET_TZ` from stored UTC. One timeline, like every other stamp
    here -- the feed sends offsets, the table holds instants, and the page never
    parses a zone.

    `impact` travels as the feed stated it, and `impact_source` names whose
    judgement it is. Same rule as the AutoFX markup: an assessment presented
    without attribution reads as a measurement.

    TWO INDEPENDENT AXES, not one flag. This used to send `key` per event --
    a single boolean meaning "USD and High" -- and the page could only offer that
    or everything. Two axes are what the reader actually wants ("USD, but all
    impacts") and, more to the point, an event carries its `country` and `impact`
    already: a server-side boolean that ANDs them throws away which half failed,
    so no finer question can be asked of the payload without a new field per
    question. The page filters on the two values instead, and `countries` /
    `impacts` here carry the VOCABULARY (what is present in this window, with
    counts) plus the journal's defaults -- so the buttons are built from what was
    stored rather than from a hard-coded list that would drift from the feed.

    Per-day counts are NOT precomputed per axis combination: there are 2^n of
    them, and the page already holds every event. `MarketDay.events` is the day's
    total and the page counts its own filtered subset, which is the only way the
    strip and the rows cannot disagree.
    """
    monday = (now.astimezone(MARKET_TZ)
              .replace(hour=0, minute=0, second=0, microsecond=0)
              - timedelta(days=now.astimezone(MARKET_TZ).weekday()))
    strip_end = monday + timedelta(days=7)
    # The event list runs from the strip's start to whichever is later, so a
    # `days` beyond this week still returns its events.
    end = max(strip_end, monday + timedelta(days=days))

    rows = upcoming(conn, start=int(monday.timestamp()), end=int(end.timestamp()))
    events: list[Row] = []
    for row in rows:
        when = datetime.fromtimestamp(row["starts_at"], MARKET_TZ)
        events.append({
            "event_id": row["event_id"],
            "source": row["source"],
            "day": when.date().isoformat(),
            "at": when.strftime("%H:%M"),
            "starts_at": row["starts_at"],
            "country": row["country"],
            "title": row["title"],
            "impact": row["impact"],
            "forecast": row["forecast"],
            "previous": row["previous"],
        })

    by_day: dict[str, int] = {}
    for event in events:
        by_day[event["day"]] = by_day.get(event["day"], 0) + 1

    today = now.astimezone(MARKET_TZ).date().isoformat()
    week = []
    for offset in range(7):
        day = (monday + timedelta(days=offset)).date().isoformat()
        week.append({
            "day": day,
            "label": (monday + timedelta(days=offset)).strftime("%a"),
            "dom": (monday + timedelta(days=offset)).day,
            #: The day's TOTAL. The page counts the filtered subset itself from
            #: `events`, so the strip cannot disagree with the rows it opens --
            #: which a precomputed per-filter count could, and did.
            "events": by_day.get(day, 0),
            "today": day == today,
        })

    #: The axis vocabularies, each ordered for the buttons that render them and
    #: carrying the count so a choice states what it costs before it is pressed.
    #: Built from what is STORED in the window rather than from the feed's full
    #: alphabet: a country with no events this week is a button that does nothing.
    countries = [
        {"value": value,
         "events": sum(1 for event in events if event["country"] == value),
         "default": value in DEFAULT_COUNTRIES}
        # Alphabetical, because no severity order exists for a currency and
        # ordering by count would reshuffle the row as the week filled up.
        # CASE-INSENSITIVELY: the feed's global rows use the country `All`, and a
        # plain `sorted` puts it after every all-caps code (`AUD` < `All` by
        # codepoint), so the one non-currency chip landed in the middle of the row.
        for value in sorted({event["country"] for event in events},
                            key=lambda value: value.casefold())
    ]
    impacts = [
        {"value": value,
         "events": sum(1 for event in events if event["impact"] == value),
         "default": value in DEFAULT_IMPACTS}
        # Severity order, from `events.IMPACT_ORDER`, so the page holds no copy
        # of what "more important" means. Intersected with what is present.
        for value in IMPACT_ORDER
        if any(event["impact"] == value for event in events)
    ]

    return {
        "week": week,
        "events": events,
        "from_day": week[0]["day"],
        "to_day": week[-1]["day"],
        "today": today,
        #: Whose judgement `impact` is. The page shows this rather than implying
        #: the journal graded the event itself.
        "impact_source": SOURCE,
        "zone": str(MARKET_TZ),
        "countries": countries,
        "impacts": impacts,
        #: What the DEFAULT view narrows to, in words, so the page can say it
        #: rather than assembling the sentence from the two lists itself. From
        #: `events.default_scope` so the CLI and the page cannot describe the
        #: default differently -- the drift that shipped once already.
        "default_scope": default_scope(),
        "total_events": len(events),
        #: How many rows the DEFAULT filter shows, so the page can report what
        #: resetting would cost without recomputing the server's own defaults.
        "default_events": sum(
            1 for event in events
            if event["country"] in DEFAULT_COUNTRIES
            and event["impact"] in DEFAULT_IMPACTS
        ),
    }


def odte_context_data(conn: sqlite3.Connection, *, now: datetime) -> Row | None:
    """The 0DTE planner's pre-open reading, or None when the feed has not landed.

    Three parts, and each is an ABSENCE the planner renders rather than a zero:
    the S&P 500's last completed session close, the current VIX, and the
    expected-range bands `zdte.plan` derives from the two. None when either close
    is missing -- a fresh clone, or a `bars` fetch that has not run -- because a
    planner drawn from no data is worse than one that says "run `optjournal
    bars`". The band maths lives in `zdte.py`; this only reads the two numbers
    and pairs them with the day's events.

    Closes come from `price_bars` under the index's own symbol as conid (see
    `bars.CONTEXT_SYMBOLS`). The S&P figure is the last COMPLETED session's
    close, which is not the newest row: on a trading day the newest daily bar is
    TODAY's, still moving, and printing it as "prior close" would label a live
    quote as a settled one and shift every rail under the reader mid-session.
    Measured against the reference implementation on 2026-08-31, which read
    7711.76 (Friday's close) while the newest row held 7686.14 (Monday, in
    progress).

    The VIX is deliberately NOT excluded the same way: the plan wants the CURRENT
    level of volatility against the prior close, which is the live reading, and a
    seller sizing a strangle at 09:40 wants today's VIX rather than Friday's. The
    two dates therefore differ by design during a session, and both are carried so
    the page can say so rather than printing one date over two figures.

    Today's events are the same rows the Market tab holds, narrowed to the ET
    session date -- an economic print at 08:30 is exactly the "big day" warning a
    same-day seller wants beside the bands. Filtered here rather than in the page
    so the two surfaces cannot disagree about which day "today" is.
    """
    spx = bars_close_series(conn, "^GSPC", bar_size="1d")
    vix = bars_close_series(conn, "^VIX", bar_size="1d")
    if not spx or not vix:
        return None

    today = now.astimezone(MARKET_TZ).date().isoformat()
    # The last close from a session that is NOT today. Falls back to the newest
    # row only when every row predates today, which is the pre-open and
    # weekend case -- there the newest row IS the prior close.
    settled = [(ts, close) for ts, close in spx if et_day(ts) != today]
    if not settled:
        return None
    spx_ts, spx_close = settled[-1]
    # The VIX is the live level, so the newest row stands. See the docstring.
    vix_ts, vix_close = vix[-1]
    result = zdte_plan(spx_prev_close=spx_close, vix=vix_close)
    if result is None:
        return None

    day_start = now.astimezone(MARKET_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0)
    rows = upcoming(conn, start=int(day_start.timestamp()),
                    end=int((day_start + timedelta(days=1)).timestamp()))
    events = [
        {
            "at": datetime.fromtimestamp(r["starts_at"], MARKET_TZ).strftime("%H:%M"),
            "country": r["country"],
            "title": r["title"],
            "impact": r["impact"],
        }
        for r in rows
    ]

    payload = result.payload()
    payload["spx_date"] = et_day(spx_ts)
    payload["vix_date"] = et_day(vix_ts)
    payload["events_today"] = events
    payload["today"] = today
    return payload


def _days_until(recorded: str | None, *, today: date) -> int | None:
    """Whole days from `today` to a recorded YYYY-MM-DD day. Signed, or None.

    Derived on every read rather than stored beside the date, and that is the whole
    reason it is a function here instead of a column in `watchlist`: a stored "14
    days" is wrong tomorrow, silently, while the date it was counted from still
    reads correctly beside it. There is no drift available to a figure recomputed
    from its own input.

    SIGNED, so a date that has passed reports a negative count rather than being
    clamped to zero or swallowed. A recorded date stands until the reader records
    the next one, and the surfaces render the past case as the date plus "recorded,
    now past" -- which says what is true (this is what you typed, and it has gone
    by) where a bare "-14d" invites reading it as a countdown that ran backwards
    and a clamp to 0 would claim the company reports today.

    Zero means today. None means either nothing recorded or a stored value that is
    not a day at all; both writers validate through `clock.parse_day`, so the
    second only happens to a hand-edited journal, and reporting None there is what
    keeps one bad cell from taking the payload -- and the page -- down with it.
    """
    day = parse_day(recorded)
    return None if day is None else (day - today).days


def watchlist_data(
    conn: sqlite3.Connection, *, sessions: int = 21, history: int = 520,
    now: datetime | None = None,
) -> list[Row]:
    """Watched symbols with price, realised vol, and this journal's own context.

    The context is the part a broker app cannot show: whether YOU hold it, and
    which option positions are open against it. That is the reason the watchlist
    lives here rather than being a second quote screen.

    `realised_vol` is named for what it is. It is NOT implied vol, which is not
    reachable for a symbol this journal does not hold -- see `vol.py` for the
    measurement. A column headed IV showing realised vol would be a well-formed
    number under a label that does not describe it, which is the defect shape this
    project keeps finding.

    A symbol with too little history reports None rather than 0.0, so a row added
    yesterday reads as "not enough data" instead of "a stock that never moved".
    Measured on the real journal: GOOG had 5 daily closes and PLTR 4, which is
    why `bars_manifest` has to learn about watched symbols (task 14d) before this
    tab is useful for anything just added.

    TWO WINDOWS, deliberately separate. `history` is how many SESSIONS are read
    back, wide because everything derived from these closes needs more of them
    than one column does; `sessions` is the realised vol window and stays at 21,
    so widening the read cannot silently move a figure the whole watchlist story
    is built on. The parameter was called `lookback` while it meant both, which is
    the shape that lets one change do two things.

    The read goes through `bars.watch_closes` rather than a SELECT here, because
    what a row needs is SESSIONS and `price_bars` stores ROWS: a symbol that is
    both watched and traded holds two conids covering the same ET days, and 21
    rows of it spanned 12 sessions. That is a `bars.py` rule (journal shape plus
    the clock), and having the reader own it is what keeps this serializer from
    holding a second, drifting copy of it.

    FOUR DERIVED FIGURES RIDE HERE, and each one travels with the count or the
    bounds that explain its absence. `bx_daily` and `bx_weekly` are `trend`'s two
    arms, `rv_rank` is `vol`'s position of today's realised vol inside its own
    year, and every one of them is None below its own gate -- never 0.0, which on a
    signed oscillator would read as a neutral measurement and on a rank as the
    quiet end of the year. What makes a dash explainable rather than mute is the
    count beside it: `closes` for the daily arm, `weeks` for the weekly one,
    `rv_rank_windows` for the rank. Measured on the real journal while the read
    window was still 60 days, five of its six watched symbols held 45 or 46
    sessions and 10 ISO weeks, so the dash IS the normal state until `optjournal
    bars` has run against the widened window.

    This layer composes and names; it computes nothing. The arithmetic is in two
    leaves (`vol`, `trend`) that import only `math`, and the two server-side
    labels, `bx_bucket` and `rv_rank_band`, come from those modules' own functions
    rather than from comparisons written here -- so a cut point cannot drift from
    the constant a caption quotes.

    THE WEEKLY ARM IS READ IN FULL, not through `history`. 120 ISO weeks is about
    600 sessions, more than `history`'s 520, and a cap counted in sessions cannot
    express a week count -- so `bars.weekly_closes` reads everything stored and the
    session cap governs the daily figures only. Both still come off one
    deduplicated series, which is why the weekly reader is built on the daily one.

    TWO OF THE ROW'S FACTS ARE TYPED, not measured: `note` and `earnings_on`. They
    are the only ones stored on the watchlist table, because they are the only ones
    with nowhere else to come from -- nothing this repo can reach publishes an
    earnings date (see `db.py`'s column comment for what was probed). So the row
    carries the date VERBATIM, and `earnings_in_days` beside it is derived here on
    every read rather than stored; the surfaces label the pair as recorded rather
    than fetched, which is the whole reason the column is honest.

    `now` exists so a countdown can be asserted without asserting the calendar. It
    is optional rather than required because every caller wants the same answer --
    today -- and a required argument that every call site fills in identically is
    how two call sites end up filling it in differently. The ET day is taken
    through `clock.et_day`, the same conversion the closes are bucketed by, so
    "today" on this tab means the trading day the rest of the tab is stated in.
    """
    rows = conn.execute(
        "SELECT symbol, note, earnings_on, added_at FROM watchlist ORDER BY symbol"
    ).fetchall()
    # One ET day for the whole payload, so two rows of one response cannot land on
    # opposite sides of midnight and report countdowns a day apart.
    today = date.fromisoformat(et_day(int((now or datetime.now(UTC)).timestamp())))

    held: dict[str, list[Row]] = {}
    for position in conn.execute(
        "SELECT underlying_symbol, symbol, position, put_call, strike, expiry"
        " FROM current_option_positions WHERE position != 0"
    ):
        key = str(position["underlying_symbol"] or "").upper()
        held.setdefault(key, []).append({
            "symbol": position["symbol"],
            "quantity": position["position"],
            "put_call": position["put_call"],
            "strike": position["strike"],
            "expiry": position["expiry"],
        })

    out: list[Row] = []
    for row in rows:
        symbol = str(row["symbol"]).upper()
        series = watch_closes(conn, symbol, sessions=history)
        closes = [close for _, close in series]
        # The vol's own slice, taken here rather than by reading less: the whole
        # series is what says how much history exists, and `closes` reports that.
        window = closes[:sessions]
        last = window[0] if window else None
        # Change over one session and one week, from the same series. None rather
        # than 0.0 when the history is not there, for the same reason as the vol.
        prev = window[1] if len(window) > 1 else None
        week = window[5] if len(window) > 5 else None
        realised = realised_vol(window)
        # The daily arm, and the same arm one session back. Two calls rather than a
        # series because `bxtrender_short` returns the newest value: dropping the
        # NEWEST close (the series is newest first) is what "one session ago" means,
        # and both calls are gated, so a symbol holding exactly `MIN_SETTLED`
        # sessions gets a value and no delta rather than a delta against nothing.
        bx_daily = bxtrender_short(closes)
        bx_previous = bxtrender_short(closes[1:])
        weekly = weekly_closes(conn, symbol)
        # Oldest first out of `bars`, newest first into `trend`. The reversal is
        # here, once, at the seam between the two conventions.
        bx_weekly = bxtrender_short([close for _, close, _ in reversed(weekly)])
        vol_series = realised_vol_series(closes, window=sessions)
        rv_rank = rank(vol_series)
        out.append({
            "symbol": symbol,
            "note": row["note"],
            "added_at": row["added_at"],
            "last": last,
            #: SESSIONS held, not rows stored. The two differed by a factor of two
            #: on a symbol covered by two conids, and this count is what the page
            #: prints to say WHY a figure is missing -- so it has to count the same
            #: thing the figure was computed over.
            "closes": len(series),
            #: The ET trading day of the newest close used. A stored close with no
            #: date is the shape `marketdata.parse_quote` refuses outright, and the
            #: page's stale marker had to say "stored close" with no idea which
            #: session it came from.
            "closes_through": series[0][0] if series else None,
            "change_1d": None if (last is None or prev is None)
                         else (last - prev) / prev * 100,
            "change_5d": None if (last is None or week is None)
                         else (last - week) / week * 100,
            #: REALISED, not implied. See vol.py.
            "realised_vol": realised,
            "expected_move_5d": expected_move(last, realised, days=5),
            #: B-Xtrender's short arm over daily closes, and its one-session
            #: change. The delta carries the published indicator's SECOND state
            #: (rising or falling), which the surfaces render as a glyph rather
            #: than as a second shade of the sign's colour.
            "bx_daily": bx_daily,
            "bx_daily_delta": None if (bx_daily is None or bx_previous is None)
                              else bx_daily - bx_previous,
            #: Which band the daily arm sits in, from `trend.bucket` -- so the cut
            #: points live once, beside the measured share of sessions each band
            #: holds, instead of being retyped per surface. Deliberately not
            #: "oversold"/"overbought": those are claims about what a share is
            #: worth, and this is a statement about the shape of recent closes.
            "bx_bucket": bucket(bx_daily),
            #: The same arm over ISO weeks, with the newest bucket named and
            #: counted. A weekly value over an incomplete week repaints every
            #: session (measured on TSLA: -19.02, -18.63, -19.71 across one week's
            #: three sessions), so it may never be shown as a bare figure.
            "bx_weekly": bx_weekly,
            #: The ISO week of the newest close, and how many of its sessions are
            #: stored. Both are facts about the stored series rather than figures,
            #: like `closes_through`, so they answer even when `bx_weekly` does not.
            "bx_weekly_week": weekly[-1][0] if weekly else None,
            "bx_weekly_sessions": weekly[-1][2] if weekly else None,
            #: ISO weeks held, which is what explains the weekly arm's dash. In
            #: weeks and not sessions because that is the arm's own unit.
            "weeks": len(weekly),
            #: Today's realised vol as a MIN-MAX position inside its own trailing
            #: year, 0 to 100, with both bounds and the window count beside it. A
            #: rank, not a percentile, and not an IV rank: implied vol is
            #: unreachable for a symbol this journal does not hold. See vol.py.
            "rv_rank": rv_rank,
            #: The bounds the rank was measured against, and they answer exactly
            #: when it does. A low and a high off a dozen windows would read as a
            #: year's extremes while being two adjacent readings from one fortnight
            #: -- the count is what says a range exists, and below the gate it says
            #: one does not.
            "rv_rank_low": min(vol_series) if rv_rank is not None else None,
            "rv_rank_high": max(vol_series) if rv_rank is not None else None,
            #: Windows actually measured. Reported below the gate too, because it
            #: is the number that turns the rank's dash into a sentence.
            "rv_rank_windows": len(vol_series),
            #: Which side of `vol.RANK_MIDPOINT`, from `vol.rank_band`. The midpoint
            #: is the middle of THIS symbol's own year, never IV rank's 30.
            "rv_rank_band": rank_band(rv_rank),
            #: The next earnings date, TYPED. Verbatim from the column, because the
            #: only claim being made about it is that this is what the reader
            #: recorded -- there is no source to reconcile it against.
            "earnings_on": row["earnings_on"],
            #: Days from the ET trading day to that date, derived here and never
            #: stored. Negative for a date that has gone by, 0 for today, None when
            #: nothing is recorded. See `_days_until` for why it is signed.
            "earnings_in_days": _days_until(row["earnings_on"], today=today),
            "options": held.get(symbol, []),
            "held": bool(held.get(symbol)),
        })
    return out


#: How stale the scheduler's heartbeat may be before the page calls it dead. The
#: tick is 60s, so three missed ticks is unambiguous while a single slow one is
#: not -- and a laptop waking from sleep writes a heartbeat within one tick.
HEARTBEAT_STALE_S = 300


def jobs_data(conn: sqlite3.Connection, *, now: datetime) -> Row:
    """What the scheduler has done, and whether it is alive at all.

    TWO SEPARATE QUESTIONS, and conflating them is the failure this exists for.
    "Did the last run succeed" is `last_status`; "is anything running the
    schedule" is the heartbeat. The MeshClaw crons answered the first with `ok`
    for two days while the answer to the second was no -- three bars jobs green
    while `price_bars` gained nothing, including the audit job whose whole purpose
    was to notice. A row can therefore be green on outcome and red on freshness at
    the same time, and the page must be able to say so.

    Reads only. Writing is the runner's job; this is what the page renders.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT job, last_fired_for, last_status, consecutive_failures,"
        " heartbeat_at FROM job_state ORDER BY job")]

    latest: dict[str, Row] = {}
    for run in conn.execute(
        # The newest run per job: id DESC is the insertion order, which is also
        # the completion order for a single worker.
        "SELECT job, id, fired_for, started_at, finished_at, status, detail,"
        " done, total, note, slept FROM job_runs ORDER BY id DESC"
    ):
        latest.setdefault(str(run["job"]), dict(run))

    # EVERY REGISTERED JOB, not only those with a `job_state` row. Reading the
    # table alone meant a job that had never run did not appear -- no row, no Run
    # button, no way to start it from the page. That is the `crons.json` failure
    # wearing a new hat: `market` has never run anywhere, so it would have been
    # invisible on the one surface built to make it runnable. The registry is the
    # list of what EXISTS; the table only says what has happened.
    #
    # Imported here rather than at module scope: `jobs` imports `bars` and `sync`,
    # and `serialize` is imported by `render` for terminal output that needs
    # neither. The graph stays acyclic either way (`jobs` does not import
    # `serialize`), so this is about import cost, not direction.
    from optjournal.jobs import JOBS  # noqa: PLC0415 - see above

    state_by_job = {str(r["job"]): r for r in rows}
    jobs: list[Row] = []
    for spec in JOBS:
        state = state_by_job.get(spec.name)
        jobs.append({
            "job": spec.name,
            "last_status": None if state is None else state["last_status"],
            "last_fired_for": None if state is None else state["last_fired_for"],
            "consecutive_failures": (
                0 if state is None else (state["consecutive_failures"] or 0)),
            "last_run": latest.get(spec.name),
            #: Whether running this spends one of IBKR's rate-limited requests.
            #: Decided by the registry, so the page holds no copy of which jobs
            #: touch the broker -- the same rule that keeps "USD high-impact" out
            #: of the calendar's markup.
            "spends_request": spec.spends_broker_request,
        })
    # A `job_state` row for a job no longer in the registry: kept, and marked, so a
    # renamed job's history does not silently vanish from the page that reports on
    # collection health.
    for name, state in sorted(state_by_job.items()):
        if any(spec.name == name for spec in JOBS):
            continue
        jobs.append({
            "job": name,
            "last_status": state["last_status"],
            "last_fired_for": state["last_fired_for"],
            "consecutive_failures": state["consecutive_failures"] or 0,
            "last_run": latest.get(name),
            #: Unregistered, so it cannot be run from the page and cannot spend
            #: anything.
            "spends_request": False,
            "retired": True,
        })

    # The heartbeat is one clock for the whole loop, not per job: the tick writes
    # it, so the freshest value across jobs is the loop's own liveness. Absent
    # entirely means the scheduler has never run here, which is NOT the same as
    # dead and must not render as an alarm on a journal that has only ever used
    # the CLI.
    beats = [r["heartbeat_at"] for r in rows if r["heartbeat_at"] is not None]
    beat = max(beats) if beats else None
    age = None if beat is None else int(now.timestamp()) - int(beat)
    return {
        "jobs": jobs,
        "heartbeat_at": beat,
        "heartbeat_age_s": age,
        "running": age is not None and age <= HEARTBEAT_STALE_S,
        #: None when no scheduler has ever written a heartbeat. The page says
        #: "not running" rather than "stale", because they need different actions.
        "ever_ran": beat is not None,
        "stale_after_s": HEARTBEAT_STALE_S,
    }


def audit_data(conn: sqlite3.Connection, *, now: datetime) -> Row:
    """Did the last session's perishable option bars actually land?

    Computed on EVERY page load rather than by a scheduled job, and that is the
    point rather than a shortcut. The audit was a cron, which meant the watchdog
    and the thing it watched could stop together -- and did. Measured at 2.95 ms
    on the real journal (the plan predicted 0.76 ms; either way it is noise
    against a payload that already issues ~142 statements), so there is no reason
    to make it conditional.

    Answers the one question no later run can fix: an option's intraday series
    exists only while its own session runs.

    `audit.ok` IS NOT ENOUGH, AND THE PAGE MUST NOT RENDER IT ALONE. `ok` is
    `not market_traded or not missing` (bars.SessionAudit.ok), and `market_traded`
    is answered by "does any UNDERLYING have hourly bars for that day" -- a
    deliberately calendar-free holiday oracle. That oracle shares a failure mode
    with the thing it certifies. Reproduced on three copies of the real journal:

        healthy          traded=True  covered=5  missing=0  ok=True
        option poll dead traded=True  covered=0  missing=5  ok=False   <- caught
        TOTAL blackout   traded=False covered=0  missing=0  ok=True    <- MISSED

    Delete every hourly bar, as a fully dead collector would, and `ok` goes GREEN
    because "no bars for anyone" reads as a market holiday. That is the same
    watchdog-and-watched-stop-together shape that moved this audit out of a cron in
    the first place, one level down.

    So two extra fields travel, and the page reads THEM rather than `ok`:

    * `witnesses` -- how many contracts were actually checked. Falls to zero in a
      blackout while `ok` is green, so a count is honest where a boolean is not.
    * `blackout` -- no underlying traded across `last_traded_day`'s whole 10-day
      lookback. Nine US market holidays a year do not fall in a ten-day row, so
      this cannot be a quiet December: it means collection itself has stopped.
    * `ever_collected` -- whether this journal holds ANY bar at all. Without it
      `blackout` cannot tell "collection stopped" from "collection never started",
      and those need opposite responses. MEASURED, not assumed: with `price_bars`
      emptied out of a copy of the real journal and again on a fresh one, the two
      payloads were byte-identical (`market_traded` False, `witnesses` 0,
      `blackout` True, `ok` True). So the demo journal, and any journal before its
      first `optjournal bars`, rendered a red collection alarm for the absence of
      a thing that had never been there. Exactly the distinction the heartbeat
      already draws with `ever_ran`, which is what makes its omission here an
      oversight rather than a judgement.

    `ok` is kept, unchanged, because `optjournal bars --audit` and the cron's
    delivery policy both key on it and this is not the commit to move that.
    """
    audit = audit_perishable(conn, now=now)
    # `day` is None only when last_traded_day found no session in ten days, which
    # is the blackout: the audit could not even choose a day to examine.
    blackout = not audit.market_traded and not audit.covered and not audit.missing
    # LIMIT 1, not COUNT(*): the question is existence, and price_bars holds
    # thousands of rows on a working journal.
    ever = conn.execute("SELECT 1 FROM price_bars LIMIT 1").fetchone() is not None
    return {
        "day": audit.day,
        "market_traded": audit.market_traded,
        "covered": list(audit.covered),
        "missing": list(audit.missing),
        "ok": audit.ok,
        #: What the check actually looked at. Zero means it proved nothing.
        "witnesses": len(audit.covered) + len(audit.missing),
        #: Nothing traded anywhere in the lookback -- not a holiday, a dead poll.
        #: Read WITH `ever_collected`: a blackout on a journal that never collected
        #: is not a fault, and the page must not tint it as one.
        "blackout": blackout,
        #: Whether any bar has ever been stored here. Distinguishes a stopped
        #: collector from one that was never started.
        "ever_collected": ever,
    }


def logbook_data(conn: sqlite3.Connection, *, today: date) -> Row:
    """The header's dateline: how long this log has been kept, and since when.

    A *bitácora* is a ship's log -- a dated, sequential record. The header used
    to spend its most prominent small slot on a fixed string naming the journal's
    contents ("Strikes · Fills · Round trips"), which is identical for every
    reader on every load and is already said by the tab strip directly beneath
    it. This is the same slot answering something only this journal can: it is
    day N of a log opened on a specific date.

    `day` counts INCLUSIVELY from first activity, so the day the account opened
    is day 1 and not day 0. A log's first page is page one.

    No count of open positions travels with it, deliberately. An episode is per
    CONTRACT, so a strangle is two of them -- `strategies.open_position_count`
    exists precisely because a naive tally read a two-leg strangle as two bets
    and disagreed with the Positions tab on screen. The page derives the header's
    names from `state.positions`, the same snapshot rows that tab renders, so the
    two cannot drift apart. What is period-invariant and unambiguous travels
    here; what needs grouping is left to the layer that already groups it.
    """
    opened = first_activity(conn)
    if not opened:
        # A journal with no fills and no cash rows has no first day, so there is
        # no day to be. The page falls back to the title alone rather than
        # rendering "day 1" for an account that has not started -- an honest
        # absence beats a figure counting from nothing.
        return {"opened": None, "day": None}
    try:
        start = date.fromisoformat(opened)
    except ValueError:  # pragma: no cover - defensive
        # `_day_of` normalises both stored forms, so this is unreachable for data
        # this package wrote. It must not blank the page if a hand-edited row
        # ever gets past it.
        return {"opened": None, "day": None}
    return {
        "opened": opened,
        #: Inclusive, so the opening day is day 1. Clamped at 1 because a
        #: statement timestamped ahead of the reader's clock (a timezone away
        #: from UTC, or a laptop with a wrong date) would otherwise render day 0
        #: or a negative day -- nonsense a reader cannot interpret, where "day 1"
        #: is merely uninteresting.
        "day": max(1, (today - start).days + 1),
    }
