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
    upsert_bars,
    watch_closes,
    weekly_closes,
)
from optjournal.bars import (
    close_series as bars_close_series,
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
from optjournal.history import (
    HistoryReport,
    book_date,
    book_join_sql,
    build_history,
)
from optjournal.journal import ADHERENCE as JOURNAL_ADHERENCE
from optjournal.journal import FIELDS as JOURNAL_FIELDS
from optjournal.journal import TRIGGERS as JOURNAL_TRIGGERS
from optjournal.journal import entries as journal_entries
from optjournal.journal import orphans as journal_orphans
from optjournal.marketdata import BarFetchError, fetch_bars
from optjournal.money import FILL_MONEY_FIELDS, Money
from optjournal.sections import raw_sections
from optjournal.stats import EQUITY_CATEGORY, campaigns_for, first_activity
from optjournal.trend import bucket, bxtrender_short
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
        # By broker as well: an order id is the issuing broker's, and read by id
        # alone each broker's order 5000 took the other's legs too.
        legs = conn.execute(
            "SELECT * FROM trade_legs WHERE broker = ? AND ib_order_id = ?"
            " AND asset_category = ? ORDER BY expiry, strike",
            (o["broker"], o["ib_order_id"], asset_category),
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

def allocation_data(conn: sqlite3.Connection) -> Row:
    """What the account holds, by holding, as a share of net liquidation.

    A HOLDING is the stock, or the underlying an option is written on, so TSLA
    stock and a TSLA put are one line: the question is how much of the account
    rides on one name. Stocks and options are kept apart within the line because
    they are different kinds of exposure, and summed into `net` because that is
    what the name contributes to the account's value.

    Read from each account's current book (`history.book_dates_sql`), the same
    rows the Positions tab and the episode walk treat as held. Each category from
    its own latest snapshot DATE kept an option sold since then: every row of one
    IBKR statement carries the same reportDate, so options missing from the newest
    date were sold, not reported late. The book date is per category all the same,
    because the NAV declares a category flat through its own column, and "as of"
    is the newer of the two books read here.

    The denominator is the broker's own net liquidation (`equity_summaries`), so
    the rows plus `cash` sum to it and a share can be read against the figure the
    statement prints. A short option is a liability and its share is negative,
    which is true: it is money the account owes back. Without an equity summary
    the shares are None rather than a share of some other total.
    """
    holdings: dict[str, Row] = {}
    dates: list[str] = []
    for cat, key in (("STK", "stock"), ("OPT", "options")):
        for r in conn.execute(
            "SELECT COALESCE(underlying_symbol, symbol) AS holding,"
            " SUM(position_value * fx_rate_to_base) AS value, COUNT(*) AS n"
            f" FROM position_snapshots p{book_join_sql(cat)}"
            " WHERE asset_category = ? GROUP BY 1", (cat,),
        ):
            row = holdings.setdefault(r["holding"], {
                "holding": r["holding"], "stock": 0.0, "options": 0.0, "lines": 0})
            row[key] += r["value"] or 0.0
            row["lines"] += r["n"]
        day = book_date(conn, cat)
        if day:
            dates.append(day)
    as_of = max(dates, default=None)
    # Each account's newest NAV, summed, so a second account's holdings are a
    # share of a total that includes them. Per account, like the holdings above,
    # so an account whose statements lag still counts.
    navs = conn.execute(
        "SELECT total_base, cash_base, report_date FROM equity_summaries e"
        " WHERE report_date = (SELECT MAX(report_date) FROM equity_summaries"
        "  WHERE broker = e.broker AND account_id = e.account_id)"
    ).fetchall()
    total = sum(n["total_base"] or 0.0 for n in navs) if navs else None
    cashes = [n["cash_base"] for n in navs if n["cash_base"] is not None]

    def share(value: float | None) -> float | None:
        return value / total if total and value is not None else None

    rows = []
    for row in holdings.values():
        row["net"] = row["stock"] + row["options"]
        row["share"] = share(row["net"])
        rows.append(row)
    rows.sort(key=lambda r: (-abs(r["net"]), r["holding"]))
    cash = sum(cashes) if cashes else None
    return {
        "as_of": as_of,
        "nav": total,
        "nav_date": max(str(n["report_date"]) for n in navs) if navs else None,
        "cash": cash,
        "cash_share": share(cash),
        "rows": rows,
    }


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


#: When a session's daily bar is final, in ET: the 16:00 close plus a margin for
#: the source to publish the settled print. Before it, the day's bar is still
#: moving; after it, that bar IS the prior close for the next session.
SETTLE_H, SETTLE_M = 16, 15
#: The cash open, in ET. Between the open and the settle the VIX is a live
#: reading and has to be recent to be true.
OPEN_H, OPEN_M = 9, 30
#: How old a live VIX may be during the session. Two refresh cycles of the page.
VIX_LIVE_MAX_S = 10 * 60


def last_settle(now: datetime) -> datetime:
    """The most recent weekday settle at or before `now`, in ET.

    NO HOLIDAY CALENDAR, deliberately, and it does not need one: this is the
    moment after which a fetch MUST contain every completed session. On a holiday
    it names a settle nothing happened at, and a fetch after it still returns the
    last real session as newest, which is correct. What it catches is the case
    that shipped: Friday's close never fetched, and Thursday's printed on Monday.
    """
    et = now.astimezone(MARKET_TZ)
    day = et.replace(hour=SETTLE_H, minute=SETTLE_M, second=0, microsecond=0)
    if et < day:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def in_session(now: datetime) -> bool:
    """Weekday, between the cash open and the settle, ET."""
    et = now.astimezone(MARKET_TZ)
    if et.weekday() >= 5:
        return False
    return (et.replace(hour=OPEN_H, minute=OPEN_M, second=0, microsecond=0) <= et
            < et.replace(hour=SETTLE_H, minute=SETTLE_M, second=0, microsecond=0))


def _fetched_at(conn: sqlite3.Connection, conid: str) -> datetime | None:
    """When the newest fetch of one index's daily series landed."""
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM price_bars WHERE conid = ? AND bar_size = '1d'",
        (conid,),
    ).fetchone()
    try:
        return datetime.fromisoformat(row[0]) if row and row[0] else None
    except ValueError:
        return None


def odte_context_data(conn: sqlite3.Connection, *, now: datetime) -> Row | None:
    """The 0DTE calculator's opening reading, or None when the feed has not landed.

    Two numbers and the day around them: the S&P 500's last completed session
    close, the current VIX, and today's events. None when either close is missing
    -- a fresh clone, or a `bars` fetch that has not run -- because a calculator
    drawn from no data is worse than one that says "run `optjournal bars`".

    NO LEVELS ARE DERIVED HERE, and that is a decision rather than a gap. The
    strike ladder used to be computed in Python (`zdte.py`, deleted with this
    change) and sent ready-made, which worked only while the two readings were the
    feed's. They are now TYPED: the tab lets a reader override either one against
    a moving tape, so the ladder is recomputed on a keystroke and lives in
    `static/zdte.js`, where `node --test` runs it. Sending a second, server-built
    copy of the same levels would be a copy that disagrees with the one on screen
    the moment a digit is typed.

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
    same-day seller wants beside the ladder. Filtered here rather than in the page
    so the two surfaces cannot disagree about which day "today" is.
    """
    spx = bars_close_series(conn, "^GSPC", bar_size="1d")
    vix = bars_close_series(conn, "^VIX", bar_size="1d")
    if not spx or not vix:
        return None

    today = now.astimezone(MARKET_TZ).date().isoformat()
    # The last close from a SETTLED session. Today's bar is excluded only until
    # the settle: after 16:15 ET it is final, and it is the prior close for the
    # next session -- excluding it all evening planned tomorrow off yesterday.
    settle = last_settle(now)
    open_day = None if settle.date().isoformat() == today else today
    settled = [(ts, close) for ts, close in spx if et_day(ts) != open_day]
    if not settled:
        return None
    spx_ts, spx_close = settled[-1]
    # The VIX is the live level, so the newest row stands. See the docstring.
    vix_ts, vix_close = vix[-1]
    # A non-positive close is a bad row and a negative VIX is impossible: both are
    # an absence the tab renders as "not available yet" rather than a ladder drawn
    # around a level nothing traded at. A zero VIX is USABLE -- it reads as "no
    # expected move", which is a real if never-seen figure -- and the same bound
    # is checked again in `static/zdte.js`, which has to hold it against a TYPED
    # reading this function never sees.
    if spx_close <= 0 or vix_close < 0:
        return None

    # THE SESSION THE LADDER IS FOR: today until its settle, then the next
    # weekday. After Monday's close the reader is planning Tuesday, and Monday's
    # 08:15 release beside Tuesday's ladder was a stale warning.
    et = now.astimezone(MARKET_TZ)
    session = et.date()
    if settle.date() == session or session.weekday() >= 5:
        session += timedelta(days=1)
        while session.weekday() >= 5:
            session += timedelta(days=1)
    day_start = datetime(session.year, session.month, session.day, tzinfo=MARKET_TZ)
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

    # FRESHNESS, judged against the clock rather than assumed from the rows. A
    # stored close is only the prior close if it was fetched after the last
    # settle; the VIX is only live if it was fetched in the last few minutes of an
    # open session. A reading that fails either is reported, and the page will not
    # pre-fill the calculator with it.
    spx_at, vix_at = _fetched_at(conn, "^GSPC"), _fetched_at(conn, "^VIX")
    live = in_session(now)
    problems = []
    if spx_at is None or spx_at < settle:
        problems.append(
            f"the S&P close was last fetched "
            f"{spx_at.astimezone(MARKET_TZ).strftime('%a %d %b %H:%M ET') if spx_at else 'never'},"
            f" before the {settle.strftime('%a %d %b')} settle")
    if vix_at is None or (live and (now - vix_at).total_seconds() > VIX_LIVE_MAX_S) \
            or (not live and vix_at < settle):
        problems.append(
            f"the VIX was last fetched "
            f"{vix_at.astimezone(MARKET_TZ).strftime('%a %d %b %H:%M ET') if vix_at else 'never'}")
    # A sentence of its own: the page prints it after "Not current, so not
    # shown.", where a lower-case "the S&P close" read as a typo.
    reason = "; ".join(problems)

    return {
        "spx_prev_close": spx_close,
        "spx_date": et_day(spx_ts),
        "vix": vix_close,
        "vix_date": et_day(vix_ts),
        "spx_fetched_at": spx_at.isoformat() if spx_at else None,
        "vix_fetched_at": vix_at.isoformat() if vix_at else None,
        "live": live,
        "fresh": not problems,
        "stale_reason": reason[:1].upper() + reason[1:] if reason else None,
        "events_today": events,
        "today": session.isoformat(),
    }


def refresh_odte_bars(conn: sqlite3.Connection, *, now: datetime) -> str | None:
    """Fetch the S&P and VIX daily series now. The error text, or None.

    What the 0DTE tab calls when it opens and while it stays open, so the reading
    on screen is the feed's as of now rather than as of the last scheduled job --
    which is how Thursday's close came to be printed on a Monday after a Saturday
    fetch failed offline. Two requests; the daily bar of an open session carries
    the current level, so this is also the live VIX.
    """
    start = int((now - timedelta(days=10)).timestamp())
    end = int(now.timestamp())
    errors = []
    for symbol in ("^GSPC", "^VIX"):
        try:
            bars = fetch_bars(symbol, bar_size="1d", start=start, end=end, timeout=10)
        except BarFetchError as exc:
            errors.append(str(exc))
            continue
        upsert_bars(conn, conid=symbol, symbol=symbol, bar_size="1d",
                    source="yahoo", bars=bars)
    return "; ".join(errors) or None


def odte_scoring_data(conn: sqlite3.Connection, *, now: datetime) -> list[Row]:
    """Settled sessions the 0DTE rails can be scored against, oldest first.

    One row per session that has CLOSED: `{date, prev_close, vix, close}` -- the
    close a band would have been drawn from, the VIX it would have been drawn at,
    and what the index actually did that day. Empty when either series is missing,
    the same absence `odte_context_data` reports.

    NO LEVELS HERE EITHER, for the reason given there and one more. The rails are
    `static/zdte.railScores`' to draw, from the same `RAIL_PCTS` the ladder uses, so
    a rail cannot be scored against a definition of itself that the tab no longer
    holds. This function's whole job is to pair two stored series by trading day.

    WHY THERE IS NO SNAPSHOT TABLE BEHIND THIS. The reference implementation keeps
    `zdte_snapshots`, a row per session carrying all ten of its derived levels, and
    so begins counting the day its calculator was first opened. Everything a band
    needs is ALREADY stored here: `price_bars` holds the daily ^GSPC and ^VIX closes
    and is a cache that only ever grows (see `db.py`), so the entire bar history is
    scorable the first time this runs, and a level that is recomputed cannot drift
    from the one on screen. A new table would have bought nothing but the drift.

    THE VIX IS THE PRIOR SESSION'S CLOSE, WHICH THE LIVE TAB'S IS NOT. That tab
    reads the CURRENT level against the last completed close, and a daily series
    cannot reproduce an intraday reading: the newest settled VIX before a session
    opens is the one before it. So these are the rails as the last settled readings
    would have drawn them, which is close to what the tab showed that morning
    without being it. The difference is real -- the reference's own row for
    2026-09-24 carries VIX 16.33 against the 15.67 the tab was read at on the same
    close -- so `railScores` says so rather than implying the two are one thing.

    TODAY IS EXCLUDED. Its close is still moving, and scoring a band against a
    price that has not settled would report a hit that the afternoon can take back.

    SO IS A PAIR ACROSS A HOLE. The index series is fetched over a 60-day window,
    so a journal left unopened for longer stores June and then August, and the
    first August session would be scored against a June close: weeks of movement
    against a one-session band. Two stored sessions pair only when at most ONE
    weekday lies between them, which is what an exchange holiday looks like (the
    real series runs 2026-09-04 to 2026-09-08 across Labor Day). No US holiday
    closes two weekdays in a row outside an emergency closure, so this needs no
    holiday calendar.
    """
    spx = bars_close_series(conn, "^GSPC", bar_size="1d")
    vix = bars_close_series(conn, "^VIX", bar_size="1d")
    if not spx or not vix:
        return []

    today = now.astimezone(MARKET_TZ).date().isoformat()
    # Keyed by trading day rather than by timestamp, because the two series are
    # not stamped alike even when they come from one provider -- see `clock.et_day`.
    vix_by_day = {et_day(ts): close for ts, close in vix}
    sessions = [(et_day(ts), close) for ts, close in spx if et_day(ts) != today]

    rows: list[Row] = []
    # Sliding pairs, so the shorter tail is the point rather than a mismatch: the
    # oldest stored session has no close before it to have drawn a band from.
    for (before, opened_from), (day, close) in zip(sessions, sessions[1:],
                                                  strict=False):
        level = vix_by_day.get(before)
        # The same bounds `odte_context_data` holds, and for the same reason: a
        # non-positive close is a bad row and a negative VIX is impossible. A
        # session missing either is SKIPPED rather than scored, because counting an
        # absence as a broken band would make every rail read worse than it is.
        if level is None or level < 0 or opened_from <= 0 or close <= 0:
            continue
        start = date.fromisoformat(before)
        skipped = sum(
            1 for offset in range(1, (date.fromisoformat(day) - start).days)
            if (start + timedelta(days=offset)).weekday() < 5
        )
        if skipped > 1:
            continue
        rows.append({
            "date": day,
            "prev_close": opened_from,
            "vix": level,
            "close": close,
        })
    return rows


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
        "SELECT symbol, note, earnings_on, alert_above, alert_below, added_at,"
        " earnings_next, earnings_confirmed, earnings_timing"
        " FROM watchlist ORDER BY symbol"
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
        # The last five sessions of the daily arm, OLDEST FIRST, for the row's
        # histogram. Each is the arm as it read at the close of that session, the
        # same drop-the-newest reading `bx_previous` takes one step further; a
        # session inside the warm-up window is None and draws no bar.
        bx_recent = [bxtrender_short(closes[back:]) for back in range(4, -1, -1)]
        bx_daily = bx_recent[-1]
        bx_previous = bx_recent[-2]
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
            #: recorded.
            "earnings_on": row["earnings_on"],
            #: The date in force: the typed one when there is one, else the feed's
            #: (Nasdaq, from Zacks). `earnings_source` says which, and for the
            #: feed's whether it is the company's announced date or Zacks' estimate
            #: from past reporting dates, so the page never prints a guess as a
            #: date. `earnings_timing` is the feed's "before open" or "after close".
            "earnings_date": row["earnings_on"] or row["earnings_next"],
            "earnings_source": ("typed" if row["earnings_on"]
                                else None if not row["earnings_next"]
                                else "confirmed" if row["earnings_confirmed"]
                                else "estimated"),
            "earnings_timing": None if row["earnings_on"] else row["earnings_timing"],
            #: The reader's price alerts, verbatim. Crossed or not is the page's
            #: to say, against the price it is showing (`watch.alertState`).
            "alert_above": row["alert_above"],
            "alert_below": row["alert_below"],
            "bx_recent": bx_recent,
            #: Days from the ET trading day to that date, derived here and never
            #: stored. Negative for a date that has gone by, 0 for today, None when
            #: nothing is recorded. See `_days_until` for why it is signed.
            "earnings_in_days": _days_until(
                row["earnings_on"] or row["earnings_next"], today=today),
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
    from optjournal.jobs import JOBS, is_backed_off  # noqa: PLC0415 - see above

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
            #: Past `jobs.FAILURE_BACKOFF`: the reconciler has stopped the fast
            #: retries and runs the job only at its healthy cadence until a run
            #: succeeds. Decided by `jobs`, so the page holds no copy of the limit.
            "backed_off": is_backed_off(
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
            #: Unregistered, so nothing schedules it either way.
            "backed_off": False,
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


def journal_review(lifecycles: list[Row], entries: dict[str, Row]) -> Row:
    """What the write-ups say, counted: the reason the journal exists.

    `journal.TRIGGERS` is a fixed list so "how often do I close on a time stop"
    can be answered, and `followed_*` has three answers so the adherence count
    is honest. This is the count. Over CLOSED cards only, because a review is
    written at the close and an open position has no outcome to set a plan
    against.

    A plan HELD when neither half was answered `no` and at least one was
    answered `yes`; BROKEN when either was `no`. Everything else is unreviewed,
    which is not the same as held: counting silence as discipline is the error
    the three-valued answer was built to avoid.

    Read from the payload rows rather than the database, so the review is the
    same reading of the same cards the Trades tab draws, trade-type scope
    included.
    """
    closed = [lc for lc in lifecycles if lc.get("status") == "closed" and lc.get("anchor")]

    def pnl(lc: Row) -> float:
        return float((lc.get("realized_pnl") or {}).get("base") or 0.0)

    def tally(cards: list[Row]) -> Row:
        values = [pnl(lc) for lc in cards]
        return {"count": len(cards), "wins": sum(v > 0 for v in values),
                "pnl": sum(values)}

    written, planned, reviewed = [], [], []
    held, broken, unreviewed = [], [], []
    by_trigger: dict[str, list[Row]] = {}
    answers: dict[str, Counter[str]] = {"target": Counter(), "invalidation": Counter()}
    for lc in closed:
        je = entries.get(str(lc["anchor"])) or {}
        if any(v not in (None, "") for k, v in je.items() if k in JOURNAL_FIELDS):
            written.append(lc)
        if je.get("plan_target") or je.get("plan_invalidation"):
            planned.append(lc)
        target, invalid = je.get("followed_target"), je.get("followed_invalidation")
        for half, answer in (("target", target), ("invalidation", invalid)):
            if answer:
                answers[half][answer] += 1
        if target or invalid or je.get("exit_trigger"):
            reviewed.append(lc)
        if "no" in (target, invalid):
            broken.append(lc)
        elif "yes" in (target, invalid):
            held.append(lc)
        else:
            unreviewed.append(lc)
        if je.get("exit_trigger"):
            by_trigger.setdefault(str(je["exit_trigger"]), []).append(lc)
    return {
        "closed": len(closed),
        "written": len(written),
        "planned": len(planned),
        "reviewed": len(reviewed),
        "adherence": {half: {a: answers[half][a] for a in JOURNAL_ADHERENCE}
                      for half in answers},
        "plan": {"held": tally(held), "broken": tally(broken),
                 "unreviewed": tally(unreviewed)},
        # In `TRIGGERS` order, and only the ones used: an empty row per unused
        # trigger is a table of zeroes that hides the two that matter.
        "triggers": [
            {"key": key, "label": label, **tally(by_trigger[key])}
            for key, label in JOURNAL_TRIGGERS.items() if key in by_trigger
        ],
    }


def journal_data(conn: sqlite3.Connection) -> Row:
    """What the reader wrote, and the vocabulary the form must write it in.

    The two ENUMERATIONS travel with the entries rather than being re-typed in
    the page. The modal renders `triggers` as its options and `adherence` as its
    radio values, so a label spelled twice is a label that will disagree, and a
    VALUE spelled twice is a button whose write the server refuses -- with the
    reader's text in it. A list, not a mapping, because the render order is part
    of the answer: the triggers read from "went to plan" to "taken out of my
    hands", and a reader scanning them should meet them in that order.

    Entries are keyed by the anchor its decision is filed under.

    Keyed by ANCHOR ALONE, dropping the account and broker the table also keys on,
    because an order id names one placement and a placement belongs to one
    account: `(broker, anchor)` already resolves to exactly one row, and with one
    broker configured the anchor does too. So the page can look an entry up from a
    lifecycle card without carrying an account it would only be able to get wrong.

    The whole map in one payload, rather than a lookup per card. The Trades tab
    asks "has this decision been written up" for every card it draws, and a
    request each would put a network round trip inside a render loop.

    `orphans` are the entries no current decision claims: a campaign can change
    membership when a fill lands inside its window, and its anchor with it, and
    a note keyed on the old anchor then matched no card and vanished from the
    page without a word. Listed so the reader sees the writing and what it was
    about. Checked against the decisions the Trades tab can draw, options and
    equities, and not computed at all when nothing has been written.

    An entry filed under an order that is no card's anchor now (the card it was
    written on has since merged into another, or moved its anchor, or the anchor
    was read another way when it was written) shows on the one card that answers
    to that order (`Campaign.answers_to`), which is the card a hand link filed
    under it joins. It shows keyed by the card's anchor, which is what the page
    looks it up by, and saving from the card then files it there. Where that
    card already holds its own entry, the older one is listed with the orphans,
    unless the card's entry already says everything it says.
    """
    written = journal_entries(conn)
    shown: dict[str, Any] = {}
    claimed: set[str] = set()
    if written:
        shown, claimed = _journal_shown(written, [
            c for category in ("OPT", EQUITY_CATEGORY)
            for c in campaigns_for(conn, category,
                                   build_history(conn, asset_category=category).episodes)])
    return {
        "entries": {anchor: entry.payload() for anchor, entry in shown.items()},
        "orphans": [entry.payload() for entry in journal_orphans(conn, claimed)]
        if written else [],
        "triggers": [{"key": key, "label": label}
                     for key, label in JOURNAL_TRIGGERS.items()],
        "adherence": list(JOURNAL_ADHERENCE),
    }


def _journal_shown(
    written: dict[tuple[str, str, str], Any], cards: list[Any],
) -> tuple[dict[str, Any], set[str]]:
    """The entry each card shows, by its anchor, and the anchors claimed.

    One pass over the cards: the entries filed under no card's anchor are found
    once, not once per card, which made a journal of 10,000 written cards take
    seconds to draw.
    """
    by_anchor = {anchor: entry for (_broker, _account, anchor), entry in written.items()}
    shown = dict(by_anchor)
    live = {c.anchor for c in cards if c.anchor}
    claimed = set(live)
    unfiled = by_anchor.keys() - live
    # Every (filed order, card anchor) pair first, then taken in one sorted
    # pass, so which of two older entries a card shows never rests on the
    # order the cards came in.
    pairs = {(filed, str(c.anchor)) for c in cards for filed in c.answers_to & unfiled}
    for filed, anchor in sorted(pairs, key=lambda pair: [(len(x), x) for x in pair]):
        older, own = by_anchor[filed], shown.get(anchor)
        if own is None:
            shown[anchor] = older
            claimed.add(filed)
        elif all(own.values.get(name) == value
                 for name, value in older.values.items()
                 if value not in (None, "")):
            claimed.add(filed)
    return shown, claimed
