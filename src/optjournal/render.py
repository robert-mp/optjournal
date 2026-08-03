"""Output rendering for the CLI.

Split into `*_data()` and `render_*()` pairs so every command can emit either
a human table or `--json` from one source of truth. `cli.py` stays pure
wiring and does no formatting.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

from optjournal.analysis import CostReport
from optjournal.history import HistoryReport
from optjournal.sections import raw_sections

__all__ = [
    "costs_data",
    "history_data",
    "orders_data",
    "positions_data",
    "statements_data",
    "summary_data",
    "render_history",
    "render_orders",
    "render_positions",
    "render_statements",
    "render_summary",
    "table",
]

Row = dict[str, Any]

# ---------------------------------------------------------------- formatting


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    align: str = "",
    indent: str = "  ",
) -> str:
    """Render a fixed-width table, sizing columns to their content.

    `align` is one character per column: '<' left, '>' right. Missing entries
    default to left for the first column and right for the rest, which is the
    convention for label-then-numbers tables.
    """
    if not rows:
        return f"{indent}(none)"

    cells = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [
        max(len(str(headers[i])), *(len(r[i]) for r in cells))
        for i in range(len(headers))
    ]

    def a(i: int) -> str:
        if i < len(align) and align[i] in "<>":
            return align[i]
        return "<" if i == 0 else ">"

    def line(values: Sequence[str]) -> str:
        return indent + "  ".join(
            f"{v:{a(i)}{widths[i]}}" for i, v in enumerate(values)
        )

    out = [line([str(h) for h in headers])]
    out.append(indent + "  ".join("-" * w for w in widths))
    out.extend(line(r) for r in cells)
    return "\n".join(out)


def _money(value: Any, places: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{places}f}"


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


# ------------------------------------------------------------------ statement


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


def render_summary(data: Row) -> str:
    out: list[str] = []
    for s in data["statements"]:
        out.append(f"account {s['account_id']}  {s['from_date']} .. {s['to_date']}")
        out.append(
            table(
                ["", "count"],
                [
                    ["trades", s["trades"]],
                    ["distinct orders", s["distinct_orders"]],
                    ["underlyings", s["underlyings"]],
                    ["cash transactions", s["cash_transactions"]],
                ],
            )
        )
        for label, key in (
            ("asset class", "by_asset"),
            ("open/close", "by_open_close"),
            ("cash types", "cash_by_type"),
        ):
            if s[key]:
                pretty = ", ".join(f"{k}={v}" for k, v in sorted(s[key].items()))
                out.append(f"  {label:<18} {pretty}")

    if "unmodelled_sections" in data:
        sections = data["unmodelled_sections"]
        pretty = ", ".join(f"{k}={v}" for k, v in sorted(sections.items())) or "none"
        out.append(f"  {'unmodelled':<18} {pretty}")
    return "\n".join(out)


# --------------------------------------------------------------------- orders


def orders_data(conn: sqlite3.Connection) -> list[Row]:
    orders = conn.execute(
        "SELECT * FROM option_orders ORDER BY first_fill_at DESC"
    ).fetchall()
    out: list[Row] = []
    for o in orders:
        legs = conn.execute(
            "SELECT * FROM option_legs WHERE ib_order_id = ? ORDER BY expiry, strike",
            (o["ib_order_id"],),
        ).fetchall()
        row = dict(o)
        row["legs"] = [dict(lg) for lg in legs]
        out.append(row)
    return out


def render_orders(data: list[Row]) -> str:
    if not data:
        return "No option orders. Run `optjournal ingest` first."

    out = [f"{len(data)} option order(s)"]
    for o in data:
        tag = "  [multi-leg]" if (o["leg_count"] or 0) > 1 else ""
        out.append("")
        out.append(
            f"order {o['ib_order_id']}  {o['underlyings']}  "
            f"{o['leg_count']} leg(s) / {o['fills']} fill(s){tag}"
        )
        out.append(
            f"  {o['first_fill_at']}   proceeds {_money(o['proceeds'])}"
            f"   commission {_money(o['commission'], 4)}"
            f"   base {_money(o['proceeds_base'])}"
        )
        out.append(
            table(
                ["symbol", "o/c", "side", "qty", "avg price", "fills"],
                [
                    [
                        lg["symbol"], lg["open_close"] or "-", lg["buy_sell"] or "-",
                        lg["quantity"], _money(lg["avg_price"], 4), lg["fills"],
                    ]
                    for lg in o["legs"]
                ],
                align="<<<>>>",
                indent="    ",
            )
        )
    return "\n".join(out)


# ------------------------------------------------------------------ positions


def positions_data(conn: sqlite3.Connection) -> list[Row]:
    rows = conn.execute(
        "SELECT * FROM current_option_positions ORDER BY expiry, strike"
    ).fetchall()
    return [dict(r) for r in rows]


def render_positions(data: list[Row]) -> str:
    if not data:
        return "No option positions. Run `optjournal ingest` first."

    total_base = sum(r["position_value_base"] or 0.0 for r in data)
    total_unreal = sum(r["fifo_pnl_unrealized"] or 0.0 for r in data)
    out = [f"Option book as of {data[0]['report_date']}"]
    out.append(
        table(
            ["symbol", "qty", "mark", "value", "unrealised", "ccy"],
            [
                [
                    r["symbol"], r["position"], _money(r["mark_price"]),
                    _money(r["position_value"]), _money(r["fifo_pnl_unrealized"]),
                    r["currency"],
                ]
                for r in data
            ],
            align="<>>>><",
        )
    )
    out.append("")
    out.append(f"  unrealised (instrument ccy) {_money(total_unreal):>14}")
    out.append(f"  position value (base ccy)   {_money(total_base):>14}")
    return "\n".join(out)


def _day(value: Any, placeholder: str = "-") -> str:
    """Date portion of an IBKR timestamp, leaving placeholders intact."""
    if not value:
        return placeholder
    return str(value)[:10]


# -------------------------------------------------------------------- history


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


def render_history(data: Row) -> str:
    closed, still_open = data["closed"], data["open"]
    cur = data["base_currency"]
    scope = data["asset_category"]

    if not closed and not still_open:
        return (
            f"No {scope} position history. Run `optjournal ingest` first, or "
            f"widen scope with `--assets ALL`."
        )

    out = [f"Position history ({scope}, base {cur})"]

    out.append("")
    out.append(f"Closed  {len(closed)} episode(s)")
    if closed:
        out.append(
            table(
                ["symbol", "opened", "closed", "days", "qty",
                 "realized", f"realized {cur}", "how"],
                [
                    [
                        e["symbol"],
                        _day(e["opened_at"], "pre-archive"),
                        _day(e["closed_at"]),
                        e["holding_days"] if e["holding_days"] is not None else "-",
                        e["contracts"],
                        _money(e["realized_pnl"]),
                        _money(e["realized_pnl_base"]),
                        e["status"],
                    ]
                    for e in closed
                ],
                align="<<<>>>><",
            )
        )
        t = data["totals"]
        rate = f"{t['win_rate']:.0f}%" if t["win_rate"] is not None else "-"
        out.append("")
        out.append(f"  realized total ({cur}){_money(t['realized_base']):>18}")
        out.append(f"  commission paid ({cur}){_money(t['commission_base']):>17}")
        out.append(f"  win / loss{t['wins']:>21} / {t['losses']}   win rate {rate}")
        out.append("  realized P&L is already net of commission, so the two")
        out.append("  lines above must not be added together.")
    else:
        out.append("  (none)")

    out.append("")
    out.append(f"Open  {len(still_open)} episode(s)")
    if still_open:
        out.append(
            table(
                ["symbol", "opened", "qty", "fills", f"commission {cur}",
                 "cost basis", "record"],
                [
                    [
                        e["symbol"],
                        _day(e["opened_at"], "pre-archive"),
                        e["net_qty"],
                        e["open_fills"] + e["close_fills"],
                        _money(e["commission_base"], 4),
                        _money(e["cost_basis"]),
                        "snapshot only" if e["snapshot_only"]
                        else ("entry missing" if e["entry_outside_window"]
                              else "complete"),
                    ]
                    for e in still_open
                ],
                align="<<>>>><",
            )
        )
    else:
        out.append("  (none)")

    if data["partial_record"]:
        out.append("")
        out.append(
            f"  note: {data['partial_record']} episode(s) opened before the "
            f"earliest archived"
        )
        out.append(
            f"        statement, so entry price and holding period are unknown."
        )
        out.append(
            f"        Open/closed for those was decided from the "
            f"{data['snapshot_date']} snapshot."
        )
    return "\n".join(out)


# ----------------------------------------------------------------- statements


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


def render_statements(data: list[Row], *, limit: int = 0) -> str:
    if not data:
        return "No archived statements. Run `optjournal fetch <query-id>` first."

    # Newest last in the underlying list, so the tail is the recent window.
    shown = data[-limit:] if limit and len(data) > limit else data
    hidden = len(data) - len(shown)

    header = f"{len(data)} archived statement(s)"
    if hidden:
        header += f" — showing newest {len(shown)}, {hidden} older hidden (--all)"

    return "\n".join(
        [
            header,
            table(
                ["file", "size", "period", "ingested", "assets"],
                [
                    [
                        d["file"],
                        f"{d['bytes']:,}",
                        f"{d['from_date']} .. {d['to_date']}" if d["from_date"] else "-",
                        "yes" if d["ingested"] else "no",
                        d["asset_filter"] or "-",
                    ]
                    for d in shown
                ],
                align="<><<<",
            ),
        ]
    )


def newest_statement(archive_dir: Path) -> Path | None:
    """Most recently archived statement, or None if the archive is empty."""
    files = sorted(archive_dir.glob("activity-*.xml"))
    return files[-1] if files else None
