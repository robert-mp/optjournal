"""Closed-position P&L history, reconstructed as position episodes.

An *episode* is one round trip in a single contract: the span from the
position leaving flat to returning to flat. Episodes, not contracts, are the
right unit -- verified against real data, where SIVE was bought twice, fully
sold, then bought again three days later. Grouping by conid alone would fuse
that closed round trip with the still-open re-entry and report a single
incoherent row.

Three properties of IBKR's data drive the design:

* `fifoPnlRealized` is already net of both opening and closing commission.
  Verified arithmetically: cost 62,642.46 (= 62,592.00 price basis + 50.46
  opening commission) against netCash 131,326.14 (= 131,400 proceeds - 73.86
  closing commission) gives exactly IBKR's reported 68,683.68. So commission
  must never be subtracted from realized P&L again -- it is reported here for
  visibility only, and `net_of_commission` records that fact.

* Option quantities are integral, so their flat test is exact. Stock lots
  are legitimately fractional (dividend reinvestment buys 1.79 shares), so
  the flat test is `_flat`: exact zero for integer quantities, a dust
  epsilon for fractional ones -- a residual under a millionth of a share is
  a rounding artefact, not a position.

* A position opened before the earliest statement has no opening fill on
  record, so its episode can never balance to zero from trades alone. Those
  are marked `entry_outside_window` and resolved against the latest position
  snapshot: absent from the snapshot means flat, present means still open.
  This is the second place snapshots carry information trades cannot.

Note codes arrive semicolon-joined ('AFx;P'), so tokens are split and matched
exactly. Substring matching would read 'AFx' (AutoFX) as 'A' (Assignment),
and 'SL' is a tax lot-matching method, not a closure type.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from optjournal.money import win_rate

__all__ = [
    "Episode",
    "HistoryReport",
    "build_history",
    "disposition_of",
    "split_notes",
]

#: Closure dispositions keyed by exact IBKR note-code token. Codes not listed
#: here (tax lot methods, partial-execution markers, solicitation flags) say
#: nothing about how a position ended.
DISPOSITION_BY_CODE: dict[str, str] = {
    "Ep": "EXPIRED",     # Resulted from an Expired Position
    "A": "ASSIGNED",     # Assignment
    "Ex": "EXERCISED",   # Exercise
    "AEx": "EXERCISED",  # Automatic exercise
    "MEx": "EXERCISED",  # Manual exercise
}

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"

#: Residual quantity small enough to call flat. Options quantities are ints,
#: so for them this is exactness by another name; fractional stock lots can
#: leave float dust that is not a position.
_FLAT_EPS = 1e-6


def _flat(qty: int | float) -> bool:
    return qty == 0 if isinstance(qty, int) else abs(qty) < _FLAT_EPS

#: Ordering of dispositions when a multi-fill close carries several codes.
#: Assignment and exercise are more specific outcomes than expiry.
_DISPOSITION_RANK = {"ASSIGNED": 3, "EXERCISED": 2, "EXPIRED": 1}


def split_notes(notes: str | None) -> tuple[str, ...]:
    """Split a stored notes field into exact code tokens."""
    if not notes:
        return ()
    return tuple(tok for tok in (p.strip() for p in notes.split(";")) if tok)


def disposition_of(notes: str | None) -> str | None:
    """Map note codes to a closure disposition, or None if uninformative."""
    found = [
        DISPOSITION_BY_CODE[tok]
        for tok in split_notes(notes)
        if tok in DISPOSITION_BY_CODE
    ]
    if not found:
        return None
    return max(found, key=lambda d: _DISPOSITION_RANK.get(d, 0))


def _parse_dt(value: str | None) -> datetime | None:
    """Parse the datetime forms IBKR emits, tolerating all of them."""
    if not value:
        return None
    text = str(value).strip().replace(";", " ").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H%M%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass(slots=True)
class Episode:
    """One round trip in a single contract."""

    conid: str
    symbol: str
    asset_category: str
    currency: str
    underlying_symbol: str | None = None
    put_call: str | None = None
    strike: float | None = None
    expiry: str | None = None
    multiplier: float | None = None

    status: str = STATUS_OPEN
    entry_outside_window: bool = False
    #: True when the archive holds no fills for this contract at all, so
    #: everything known about it comes from a position snapshot.
    snapshot_only: bool = False

    opened_at: str | None = None
    closed_at: str | None = None

    open_fills: int = 0
    close_fills: int = 0
    opened_qty: float = 0    #: int in practice for options; stock can fract
    closed_qty: float = 0
    net_qty: float = 0

    #: IBKR's realized P&L, already net of opening and closing commission.
    realized_pnl: float = 0.0
    realized_pnl_base: float = 0.0
    #: Reported for visibility. Already inside realized_pnl -- do not subtract.
    commission: float = 0.0
    commission_base: float = 0.0
    net_of_commission: bool = True

    proceeds: float = 0.0
    proceeds_base: float = 0.0
    #: Snapshot-sourced, so only populated for snapshot_only episodes. For
    #: trade-driven episodes the basis is implied by the opening fills.
    cost_basis: float | None = None
    unrealized: float | None = None
    trade_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.status != STATUS_OPEN

    @property
    def holding_days(self) -> int | None:
        """Calendar days from first open fill to last close fill."""
        start, end = _parse_dt(self.opened_at), _parse_dt(self.closed_at)
        if start is None or end is None:
            return None
        return (end.date() - start.date()).days

    @property
    def is_odte(self) -> bool | None:
        """Whether the contract had zero days to expiration when it was opened.

        Deliberately *not* `holding_days == 0`. That is a day trade, which is a
        different thing: a 45-day option bought and sold in one afternoon is a
        day trade at 45 DTE, and a 0DTE contract held from the open to the bell
        is 0DTE with a holding period of zero -- the two coincide often enough
        to be mistaken for one definition. What makes 0DTE its own category is
        the expiry, not the holding period: gamma and time decay behave unlike
        anything else on the day a contract dies, which is the whole reason to
        look at these trades separately.

        Compares parsed dates, not strings. `opened_at` carries a time of day
        while `expiry` does not, so `opened_at == expiry` is False even on a
        genuine 0DTE trade; and expiry reaches the database in IBKR's compact
        `YYYYMMDD` form, so the two are not even the same shape. Both sides go
        through `_parse_dt`, which normalises either form.

        None when the contract has no expiry -- a stock has no DTE, and that is
        unknowable rather than false.
        """
        if not self.expiry:
            return None
        opened, expires = _parse_dt(self.opened_at), _parse_dt(self.expiry)
        if opened is None or expires is None:
            return None
        return opened.date() == expires.date()

    @property
    def contracts(self) -> int | float:
        """Position size at its largest, in contracts or shares."""
        size = max(abs(self.opened_qty), abs(self.closed_qty))
        return int(size) if float(size).is_integer() else size


@dataclass(slots=True)
class HistoryReport:
    asset_category: str
    base_currency: str
    episodes: list[Episode]
    snapshot_date: str | None
    #: Episodes whose opening fill predates the archive, so entry data is absent.
    partial_record: int = 0

    @property
    def closed(self) -> list[Episode]:
        return [e for e in self.episodes if e.is_closed]

    @property
    def open(self) -> list[Episode]:
        return [e for e in self.episodes if not e.is_closed]

    @property
    def total_realized_base(self) -> float:
        return sum(e.realized_pnl_base for e in self.closed)

    @property
    def total_commission_base(self) -> float:
        return sum(abs(e.commission_base) for e in self.closed)

    @property
    def wins(self) -> int:
        return sum(1 for e in self.closed if e.realized_pnl_base > 0)

    @property
    def losses(self) -> int:
        return sum(1 for e in self.closed if e.realized_pnl_base < 0)

    @property
    def win_rate(self) -> float | None:
        """See `money.win_rate`: None when nothing was decided, not zero."""
        return win_rate(self.wins, self.losses)


#: Currency conversions are never position round trips. IBKR emits no
#: openCloseIndicator on them at all -- verified, 126 of 126 blank -- so every
#: conversion would read as an opening fill and the whole year's EUR.USD
#: activity would fuse into one endless episode. Excluded by construction
#: rather than filtered in the renderer, because the concept does not apply.
NON_POSITION_CATEGORIES = frozenset({"CASH"})


def _new_episode(row: Any) -> Episode:
    return Episode(
        conid=str(row["conid"] or ""),
        symbol=str(row["symbol"] or ""),
        asset_category=str(row["asset_category"] or ""),
        currency=str(row["currency"] or ""),
        underlying_symbol=row["underlying_symbol"],
        put_call=row["put_call"],
        strike=row["strike"],
        expiry=row["expiry"],
        multiplier=row["multiplier"],
    )


def _absorb(ep: Episode, row: Any) -> None:
    """Fold one fill into an episode."""
    qty = row["quantity"] or 0
    closing = (row["open_close"] or "").upper() == "C"

    if closing:
        ep.close_fills += 1
        ep.closed_qty += qty
        ep.closed_at = row["date_time"] or row["trade_date"]
    else:
        ep.open_fills += 1
        ep.opened_qty += qty
        if ep.opened_at is None:
            ep.opened_at = row["date_time"] or row["trade_date"]

    ep.net_qty += qty
    ep.realized_pnl += row["fifo_pnl_realized"] or 0.0
    ep.realized_pnl_base += row["fifo_pnl_realized_base"] or 0.0
    ep.commission += row["ib_commission"] or 0.0
    ep.commission_base += row["ib_commission_base"] or 0.0
    ep.proceeds += row["proceeds"] or 0.0
    ep.proceeds_base += row["proceeds_base"] or 0.0
    if row["trade_id"]:
        ep.trade_ids.append(str(row["trade_id"]))
    for tok in split_notes(row["notes"]):
        if tok not in ep.notes:
            ep.notes.append(tok)


def _finalise(ep: Episode, still_held: bool) -> None:
    """Decide an episode's terminal status.

    `still_held` is whether the contract appears in the newest position
    snapshot. It is the deciding signal only when trades alone are
    inconclusive, which happens when the opening fill predates the archive.
    """
    disposition = disposition_of(";".join(ep.notes)) if ep.close_fills else None

    if ep.entry_outside_window:
        # Trades cannot prove flatness without the entry, so defer to the
        # snapshot: absent means the position is gone.
        ep.status = STATUS_OPEN if still_held else (disposition or STATUS_CLOSED)
        return

    if _flat(ep.net_qty) and ep.close_fills:
        ep.status = disposition or STATUS_CLOSED
    else:
        ep.status = STATUS_OPEN


def _held(
    conn: sqlite3.Connection, asset_category: str | None
) -> tuple[dict[str, Any], str | None]:
    """Open positions from the newest snapshot, keyed by conid, and its date."""
    if asset_category:
        where, params = "WHERE asset_category = ?", (asset_category,)
    else:
        excluded = sorted(NON_POSITION_CATEGORIES)
        where = f"WHERE asset_category NOT IN ({','.join('?' for _ in excluded)})"
        params = tuple(excluded)

    row = conn.execute(
        f"SELECT MAX(report_date) AS d FROM position_snapshots {where}", params
    ).fetchone()
    latest = row["d"] if row else None
    if latest is None:
        return {}, None

    held = {
        str(r["conid"]): dict(r)
        for r in conn.execute(
            f"SELECT * FROM position_snapshots {where} AND report_date = ?"
            " AND position != 0",
            (*params, latest),
        )
    }
    return held, str(latest)


def _from_snapshot(row: dict[str, Any]) -> Episode:
    """An open position with no fills on record at all.

    Distinct from `entry_outside_window` on a closing fill: here the archive
    contains nothing about the contract except the snapshot. Verified real --
    a long TSLA June-2027 call opened before the earliest statement, whose
    only trace anywhere is `cost_basis_money` in the snapshot. Without this,
    `history` would under-report the open book against `positions`.
    """
    ep = Episode(
        conid=str(row.get("conid") or ""),
        symbol=str(row.get("symbol") or ""),
        asset_category=str(row.get("asset_category") or ""),
        currency=str(row.get("currency") or ""),
        underlying_symbol=row.get("underlying_symbol"),
        put_call=row.get("put_call"),
        strike=row.get("strike"),
        expiry=row.get("expiry"),
        multiplier=row.get("multiplier"),
    )
    ep.status = STATUS_OPEN
    ep.entry_outside_window = True
    ep.snapshot_only = True
    ep.opened_at = row.get("open_date_time") or None
    ep.net_qty = row.get("position") or 0
    ep.opened_qty = ep.net_qty
    ep.cost_basis = row.get("cost_basis_money")
    ep.unrealized = row.get("fifo_pnl_unrealized")
    return ep


def build_history(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
) -> HistoryReport:
    """Reconstruct position episodes from the accumulated trade ledger.

    `asset_category=None` covers every position-bearing category. Currency
    conversions are excluded either way; see NON_POSITION_CATEGORIES.
    """
    held, snapshot_date = _held(conn, asset_category)

    if asset_category:
        where = "WHERE asset_category = ?"
        params: tuple[Any, ...] = (asset_category,)
    else:
        excluded = sorted(NON_POSITION_CATEGORIES)
        placeholders = ",".join("?" for _ in excluded)
        where = f"WHERE asset_category NOT IN ({placeholders})"
        params = tuple(excluded)

    rows = conn.execute(
        f"SELECT * FROM trades {where} "
        "ORDER BY conid, COALESCE(date_time, trade_date), trade_id",
        params,
    ).fetchall()

    episodes: list[Episode] = []
    current: Episode | None = None
    current_conid: str | None = None

    def flush(*, closed_by_reentry: bool = False) -> None:
        """Finalise and store the episode in progress.

        `closed_by_reentry` is the case the snapshot cannot decide. The
        snapshot is keyed by conid, not by episode, so a contract that was
        opened before the archive, closed inside it, and then re-entered
        appears held -- because of the *re-entry*. Consulting it there would
        mark the completed round trip OPEN, drop its realised P&L from the
        totals, and count the contract twice in the open book. The re-entry
        itself is the proof the earlier position went flat, so it overrides.
        """
        nonlocal current
        if current is not None:
            still_held = False if closed_by_reentry else current.conid in held
            _finalise(current, still_held=still_held)
            episodes.append(current)
            current = None

    for row in rows:
        conid = str(row["conid"] or "")
        closing = (row["open_close"] or "").upper() == "C"

        if conid != current_conid:
            flush()
            current_conid = conid

        if current is None:
            current = _new_episode(row)
            # No opening fill on record means the entry predates the archive.
            current.entry_outside_window = closing
        elif current.entry_outside_window and not closing:
            # An opening fill after a close-only run is a fresh entry, so the
            # unresolvable episode ends here and a clean one begins.
            flush(closed_by_reentry=True)
            current = _new_episode(row)

        _absorb(current, row)

        if not current.entry_outside_window and _flat(current.net_qty):
            flush()

    flush()

    # A held contract with no fills anywhere in the archive produces no
    # episode above, so it would silently vanish from the open book.
    seen = {e.conid for e in episodes}
    episodes.extend(
        _from_snapshot(row) for conid, row in held.items() if conid not in seen
    )

    episodes.sort(key=lambda e: (e.closed_at or "", e.opened_at or ""), reverse=True)
    return HistoryReport(
        asset_category=asset_category or "ALL",
        base_currency=base_currency,
        episodes=episodes,
        snapshot_date=snapshot_date,
        partial_record=sum(1 for e in episodes if e.entry_outside_window),
    )
