"""Output rendering for the CLI.

Split into `*_data()` and `render_*()` pairs so every command can emit either
a human table or `--json` from one source of truth. `cli.py` stays pure
wiring and does no formatting.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = [
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


def _charged(mo: Any, places: int = 2) -> str:
    """The as-charged amount of a `Money` payload key.

    Withheld -- rendered "-" -- whenever no single currency accounts for the
    figure, which an order spanning currencies legitimately does. The `_base`
    column beside it always answers, so the pair reads as "exactly this, or
    the translation" rather than as a missing number.
    """
    return _money((mo or {}).get("native"), places)


def _base(mo: Any, places: int = 2) -> str:
    """The base-currency translation of a `Money` payload key. Always present."""
    return _money((mo or {}).get("base"), places)


# ------------------------------------------------------------------ statement


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
            f"  {o['first_fill_at']}   proceeds {_charged(o['proceeds'])}"
            f"   commission {_charged(o['commission'], 4)}"
            f"   base {_base(o['proceeds'])}"
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
                        _charged(e["realized_pnl"]),
                        _base(e["realized_pnl"]),
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
                        _base(e["commission"], 4),
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
            "        statement, so entry price and holding period are unknown."
        )
        out.append(
            f"        Open/closed for those was decided from the "
            f"{data['snapshot_date']} snapshot."
        )
    return "\n".join(out)


# ----------------------------------------------------------------- statements


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


