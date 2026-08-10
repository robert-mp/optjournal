"""Output rendering for the CLI.

Split into `*_data()` and `render_*()` pairs so every command can emit either
a human table or `--json` from one source of truth. `cli.py` stays pure
wiring and does no formatting.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = [
    "render_friction",
    "render_history",
    "render_orders",
    "render_positions",
    "render_statements",
    "render_summary",
    "render_watchlist",
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




def render_friction(data: Row) -> str:
    """The DB-backed cost report as plain text.

    Same three-part shape the web tab uses, for the same reason: what narrows with
    the scope, what cannot be attributed at all, and what was never billed. The
    estimate is printed as a RANGE rather than a single figure -- a terminal report
    that collapsed it would be the one surface where the uncertainty disappears.
    """
    cur = data["base_currency"]
    scope = data["scope"]
    totals = data["totals"]
    friction = totals["friction"]
    out: list[str] = []

    label = " + ".join(scope["categories"]) if scope["categories"] else "everything"
    if scope["subset"]:
        label += f", {scope['subset']} only"
    span = (
        f"{data['from_date']} .. {data['to_date']}"
        if data["from_date"]
        else "no trades in this selection"
    )
    out.append(f"Broker cost  {span}  ({label}, base {cur})")

    low, high = friction["total_low_base"], friction["total_high_base"]
    if friction["is_estimated"]:
        out.append(
            f"  total{_money(friction['total_mid_base']):>16}"
            f"   range {_money(low)} .. {_money(high)}"
        )
    else:
        out.append(f"  total{_money(low):>16}")
    out.append(f"  charged{_money(friction['stated']['base']):>14}   as billed:")
    for code, amount in sorted(
        (friction["stated"].get("charged") or {}).items(),
        key=lambda kv: -abs(kv[1]),
    ):
        out.append(f"    {code:<5}{_money(amount):>16}")
    if friction["is_estimated"]:
        out.append(
            f"  estimated{_money(friction['estimated_mid_base']):>12}"
            f"   never itemised by IBKR -- see below"
        )

    if data["by_category"]:
        out.append("\nCharged per category")
        out.append(
            f"  {'category':<10}{'fills':>7}{'orders':>7}{'quantity':>13}"
            f"{'commission':>13}{'taxes':>10}{'per unit':>11}"
        )
        for row in data["by_category"]:
            qty = _money(row["quantity"], 4) if row["quantity"] else "-"
            per = "-" if row["per_unit"] is None else _base(row["per_unit"], 4)
            out.append(
                f"  {row['category']:<10}{row['fills']:>7}{row['orders']:>7}"
                f"{qty:>13}{_base(row['commission']):>13}"
                f"{_base(row['taxes']):>10}{per:>11}"
            )

    if data["fx"]:
        out.append("\nCurrency conversions")
        autofx = totals["autofx"]
        markup_head = f"@{autofx['bps']}bps"
        out.append(
            f"  {'pair':<10}{'n':>5}{'notional':>14}{'comm':>9}{'bps':>7}"
            f"{'auto n':>8}{'auto notional':>15}{markup_head:>10}"
        )
        for pair in data["fx"]:
            bps = pair["commission_bps"]
            out.append(
                f"  {pair['symbol']:<10}{pair['conversions']:>5}"
                f"{_money(pair['notional_base']):>14}"
                f"{_base(pair['commission']):>9}"
                f"{('-' if bps is None else f'{bps:,.2f}'):>7}"
                f"{pair['auto']['conversions']:>8}"
                f"{_money(pair['auto']['notional_base']):>15}"
                f"{_money(pair['auto']['markup_base']):>10}"
            )
        if autofx["notional_base"]:
            out.append(
                f"  note: the markup column is an ESTIMATE. IBKR publishes "
                f"{autofx['bps']} bps and this account's own year of conversions "
                f"implied {autofx['measured_bps']}, so the total sits near "
                f"{_money(friction['estimated_low_base'])} .. "
                f"{_money(friction['estimated_high_base'])}."
            )

    if data["fees"]:
        out.append("\nAccount-level -- attributable to nothing")
        out.append(f"  {'fee':<18}{'n':>5}{'total':>12}")
        for fee in data["fees"]:
            out.append(f"  {fee['name']:<18}{fee['count']:>5}{_base(fee['total']):>12}")
        out.append(
            f"  {'TOTAL':<18}{'':>5}{_money(totals['unattributable']['base']):>12}"
        )
        out.append(
            "  note: no fee row carries a contract or trade id, so these cannot be "
            "split across instruments and do not narrow with the selection."
        )

    withheld = [w for w in data["withholding"] if (w["withheld"] or {}).get("base")]
    if withheld:
        out.append("\nDividend withholding")
        out.append(f"  {'symbol':<16}{'ccy':>5}{'gross':>10}{'withheld':>11}{'eff':>8}")
        for line in withheld:
            rate = line["effective_rate"]
            out.append(
                f"  {line['symbol'] or '-':<16}{line['currency']:>5}"
                f"{_base(line['gross']):>10}{_base(line['withheld']):>11}"
                f"{('-' if rate is None else f'{rate:,.1f}%'):>8}"
            )

    if totals["credit_fills"]:
        out.append(
            f"\n  note: {totals['credit_fills']} fill(s) carried a commission "
            "credit, netted off rather than added."
        )
    return "\n".join(out)


def render_watchlist(rows: list[dict]) -> str:
    """The watchlist as a table.

    `rv` and `move` are headed as REALISED on purpose -- see vol.py. Abbreviating
    to `iv` would fit the column better and be wrong, which is the trade this
    project consistently refuses.

    A dash means "no data", never zero: a symbol with fewer than six closes has
    made no claim about its volatility, and printing 0.0 would put a flat row
    beside a real one.
    """
    if not rows:
        return "  (nothing watched -- `optjournal watch AAPL` adds a symbol)"

    def pct(value: Any) -> str:
        return "-" if value is None else f"{value:+.2f}%"

    body = [
        [
            row["symbol"],
            _money(row["last"]),
            pct(row["change_1d"]),
            pct(row["change_5d"]),
            "-" if row["realised_vol"] is None else f"{row['realised_vol']:.1f}%",
            _money(row["expected_move_5d"]),
            # The context a broker screen cannot give: what YOU hold against it.
            ", ".join(
                f"{o['quantity']:+g} {_num_or(o['strike'])}{o['put_call'] or ''}"
                for o in row["options"]
            ) or "-",
        ]
        for row in rows
    ]
    out = [
        table(
            ["symbol", "last", "1d", "5d", "realised vol", "move 5d", "options"],
            body,
            align="<>>>>><",
        ),
        "",
        "  realised vol is what the stock DID, not what the market charges for what",
        "  it might do -- implied vol needs an option chain this journal cannot reach",
    ]
    thin = [r["symbol"] for r in rows if r["realised_vol"] is None]
    if thin:
        out.append(
            f"  no vol yet for {', '.join(thin)}: fewer than 6 daily closes stored."
            f" `optjournal bars` collects them."
        )
    return "\n".join(out)


def _num_or(value: Any, dash: str = "") -> str:
    """A number without trailing zeros, or `dash` when absent.

    For strikes in a compact cell: 130.0 reads as 130, and a missing strike (a
    stock leg) contributes nothing rather than a stray dot.
    """
    if value is None:
        return dash
    text = f"{float(value):g}"
    return text
