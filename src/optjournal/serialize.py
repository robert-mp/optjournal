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
from pathlib import Path
from typing import Any

from optjournal.analysis import CostReport
from optjournal.history import HistoryReport
from optjournal.money import FILL_MONEY_FIELDS, Money
from optjournal.sections import raw_sections

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

def positions_data(
    conn: sqlite3.Connection, cost_basis_fallback: dict[str, float] | None = None
) -> list[Row]:
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
    """
    rows = conn.execute(
        "SELECT * FROM current_option_positions ORDER BY expiry, strike"
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        ccy, rate = row.get("currency"), row.get("fx_rate_to_base")
        row["value"] = Money.at_rate(row.get("position_value"), rate, ccy).payload()
        # A snapshot row does not always carry a basis; the open episode for the
        # same conid does. Resolved here rather than in the page, which used to
        # pick the fallback and then convert it with its own rate arithmetic --
        # a second converter is what this change exists to remove.
        basis = row.get("cost_basis_money")
        if basis is None and cost_basis_fallback:
            basis = cost_basis_fallback.get(str(row.get("conid")))
        row["cost_basis"] = Money.at_rate(basis, rate, ccy).payload()
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
    are guaranteed to share a denominator. Previously the base came from
    `report.journal_per_unit_base` and the native was divided separately here,
    which is two divisions that had to agree by inspection.
    """
    qty = sum(g.quantity for g in report.journal_commissions)
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
                "quantity": g.quantity,
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

def newest_statement(archive_dir: Path) -> Path | None:
    """Most recently archived statement, or None if the archive is empty."""
    files = sorted(archive_dir.glob("activity-*.xml"))
    return files[-1] if files else None
