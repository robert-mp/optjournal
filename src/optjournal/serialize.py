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

def orders_data(
    conn: sqlite3.Connection, order_ids: frozenset[str] | None = None
) -> list[Row]:
    """Option orders with their legs, optionally restricted to a set of ids.

    Filtered in Python rather than in SQL: `option_orders` is a view that
    aggregates fills, and the caller's id set comes from episode membership,
    which no column on the view carries. The order count here is small enough
    that the difference is not measurable.
    """
    orders = conn.execute(
        "SELECT * FROM option_orders ORDER BY first_fill_at DESC"
    ).fetchall()
    out: list[Row] = []
    for o in orders:
        if order_ids is not None and str(o["ib_order_id"]) not in order_ids:
            continue
        legs = conn.execute(
            "SELECT * FROM option_legs WHERE ib_order_id = ? ORDER BY expiry, strike",
            (o["ib_order_id"],),
        ).fetchall()
        row = dict(o)
        row["legs"] = [dict(lg) for lg in legs]
        out.append(row)
    return out

def positions_data(conn: sqlite3.Connection) -> list[Row]:
    rows = conn.execute(
        "SELECT * FROM current_option_positions ORDER BY expiry, strike"
    ).fetchall()
    return [dict(r) for r in rows]

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
            "commission_base": _num(report.total_commission_base),
            "fees_base": _num(report.total_fees_base),
            "taxes_base": _num(report.total_taxes_base),
            "autofx_notional_base": _num(report.total_autofx_notional_base),
            "autofx_spread_base": _num(report.total_autofx_spread_base),
            "stated_friction_base": _num(report.total_stated_friction_base),
            "friction_base": _num(report.total_friction_base),
            "fx_notional_base": _num(report.total_fx_notional_base),
            "fx_commission_base": _num(report.total_fx_commission_base),
            # Scope split. `commission_base` above spans the whole account, so
            # a consumer that wants this journal's cost must read the journal_*
            # keys -- presenting the account figure as the journal's was the
            # defect this split exists to remove.
            "journal_commission_base": _num(report.journal_commission_base),
            "journal_taxes_base": _num(report.journal_taxes_base),
            "journal_friction_base": _num(report.journal_friction_base),
            "journal_per_unit_base": _num(report.journal_per_unit_base),
            "other_commission_base": _num(report.other_commission_base),
            "other_taxes_base": _num(report.other_taxes_base),
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
            "realized_pnl": e.realized_pnl,
            "realized_pnl_base": e.realized_pnl_base,
            "realized_is_net_of_commission": e.net_of_commission,
            "commission": e.commission,
            "commission_base": e.commission_base,
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
