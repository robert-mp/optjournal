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

Note codes are read through `notes.py`, which owns that rule for both readers of
them: tokens are split and matched exactly, because substring matching would read
'AFx' (AutoFX) as 'A' (Assignment). 'SL' is a tax lot-matching method, not a
closure type. `split_notes` is re-exported here, where its callers already look.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from optjournal.money import win_rate
from optjournal.notes import split_notes

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
    #: Which broker and account this position belongs to. Part of the episode's
    #: identity, since the same contract held in two accounts is two positions --
    #: defaulted so the many hand-built Episodes in the suite need no change, and
    #: so a snapshot row missing either still produces an episode.
    broker: str = "ibkr"
    account_id: str = ""
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
    #: The largest absolute net quantity the position reached. What `contracts`
    #: reports, and not derivable from the two sums above: short 2, buy 1 back,
    #: sell 1 again, buy 2 back opens 3 and closes 3 while never holding more
    #: than 2.
    peak_qty: float = 0

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
        size = self.peak_qty
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


def _position_scope_where(asset_category: str | None) -> tuple[str, tuple[Any, ...]]:
    """The category predicate the snapshot and the trade query must agree on.

    `None` means every POSITION-BEARING category, which is not the same as no
    filter: currency conversions never form a position, so the clause becomes a
    `NOT IN` rather than an empty string. Deliberately unlike
    `stats._category_where`, which has no exclusion and may return "" -- an
    episode built from a scope the snapshot query did not share would decide
    open-versus-closed against the wrong book.

    One helper because this was written out twice, and the two copies had already
    diverged in spelling (an inline `','.join(...)` against a named
    `placeholders`), which is how the exclusion ends up applied in one query and
    not the other.
    """
    if asset_category:
        return "WHERE asset_category = ?", (asset_category,)
    excluded = sorted(NON_POSITION_CATEGORIES)
    placeholders = ",".join("?" for _ in excluded)
    return f"WHERE asset_category NOT IN ({placeholders})", tuple(excluded)


def _new_episode(row: Any) -> Episode:
    return Episode(
        conid=str(row["conid"] or ""),
        symbol=str(row["symbol"] or ""),
        broker=str(row["broker"] or ""),
        account_id=str(row["account_id"] or ""),
        asset_category=str(row["asset_category"] or ""),
        currency=str(row["currency"] or ""),
        underlying_symbol=row["underlying_symbol"],
        put_call=row["put_call"],
        strike=row["strike"],
        expiry=row["expiry"],
        multiplier=row["multiplier"],
    )


def _is_close(open_close: str | None) -> bool:
    """Whether a fill closed anything: `C`, or IBKR's `C;O` for a fill that
    closed one side and opened the other. Only a bare `C` used to count, so a
    fill through zero was read as an opening fill and its position never went
    flat."""
    return "C" in split_notes((open_close or "").upper())


def _through_zero(ep: Episode | None, row: Any) -> tuple[dict, dict] | None:
    """A closing fill that takes the position past flat, split at zero.

    Long 2 calls then SELL 3 in one fill (IBKR marks it `C;O`): the first 2
    close the long and the last 1 opens a short. Returned as the closing part
    and the opening part, or None when the fill stops at or before zero.

    The closing part takes all of IBKR's realised P&L, which is what it is: the
    opening part realises nothing. Commission and proceeds divide by quantity.

    Only for an episode whose quantity is known. One whose entry predates the
    archive with nothing saying how large it was (`entry_outside_window` without
    a snapshot) has no zero to split at.
    """
    if ep is None or ep.entry_outside_window or _flat(ep.net_qty):
        return None
    qty = row["quantity"] or 0
    if not _is_close(row["open_close"]) or (qty > 0) == (ep.net_qty > 0):
        return None
    if abs(qty) <= abs(ep.net_qty) or _flat(abs(qty) - abs(ep.net_qty)):
        return None
    closing = -ep.net_qty
    share = closing / qty
    close_part, open_part = dict(row), dict(row)
    close_part.update(quantity=closing, open_close="C")
    open_part.update(quantity=qty - closing, open_close="O",
                     fifo_pnl_realized=0.0, fifo_pnl_realized_base=0.0)
    for name in ("ib_commission", "ib_commission_base", "proceeds", "proceeds_base"):
        whole = row[name] or 0.0
        close_part[name] = whole * share
        open_part[name] = whole - close_part[name]
    return close_part, open_part


def _absorb(ep: Episode, row: Any) -> None:
    """Fold one fill into an episode."""
    qty = row["quantity"] or 0
    closing = _is_close(row["open_close"])

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
    ep.peak_qty = max(ep.peak_qty, abs(ep.net_qty))
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


#: The date an account's position book is as of, for a `position_snapshots p`
#: row in the enclosing query: the newest day that account reported a position
#: in ANY category, or on which its NAV breakdown held no stock and no options.
#:
#: Any category, because IBKR's OpenPositions lists only what is held and every
#: row of one statement carries the same reportDate (checked across the real
#: archive). The day the option book goes flat there is no OPT row at all, so the
#: newest date that had an option is a stale book: a sold LEAP stayed OPEN with
#: its realised P&L missing. A Trade Confirmation carries no positions, so it
#: never moves this date.
#:
#: The NAV clause is the one case no position row can speak for: everything sold,
#: an empty OpenPositions section, no row in any category. A statement whose
#: query lacks the section leaves no row either, but the account still holds
#: things and its NAV says so, so that silence keeps the older book rather than
#: reading as flat.
#:
#: Per `(broker, account_id)`, so an account whose statements lag is read at its
#: own date rather than against another's. `db.current_option_positions` spells
#: the same rule for the Positions tab; `tests/test_history.py` holds them equal.
BOOK_DATE_SQL = (
    "SELECT MAX(d) FROM ("
    " SELECT report_date AS d FROM position_snapshots"
    "  WHERE broker = p.broker AND account_id = p.account_id"
    " UNION ALL SELECT report_date FROM equity_summaries"
    "  WHERE broker = p.broker AND account_id = p.account_id"
    "   AND stock_base = 0 AND options_base = 0)"
)


def book_date(conn: sqlite3.Connection) -> str | None:
    """The newest account's book date, for labelling a book "as of".

    The newest across accounts because a label for a mixed-date book should name
    its most recent statement. Deciding what is held stays per account.
    """
    row = conn.execute(
        f"SELECT MAX(({BOOK_DATE_SQL})) FROM position_snapshots p"
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _held(
    conn: sqlite3.Connection, asset_category: str | None
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], str | None]:
    """Open positions in each account's current book, and the book's date.

    Keyed by `(broker, account_id, conid)` -- the same identity the episode walk
    uses, and for the same reason: the same contract held in two accounts is two
    positions, so a conid-only key would let one account's holding answer the
    open/closed question for another's.

    "Current" is `BOOK_DATE_SQL`, per account. This dict decides open versus
    closed, so a stale book here is not cosmetic: a contract read from an older
    date than its account's newest is judged still held after it was sold.

    The date is returned even when nothing in `asset_category` is held: a flat
    book is still a book as of that day.
    """
    where, params = _position_scope_where(asset_category)
    held = {
        (str(r["broker"] or ""), str(r["account_id"] or ""), str(r["conid"])): dict(r)
        for r in conn.execute(
            f"SELECT p.* FROM position_snapshots p {where}"
            f"  AND p.position != 0 AND p.report_date = ({BOOK_DATE_SQL})",
            params,
        )
    }
    return held, book_date(conn)


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
        broker=str(row.get("broker") or ""),
        account_id=str(row.get("account_id") or ""),
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
    ep.peak_qty = abs(ep.net_qty)
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

    where, params = _position_scope_where(asset_category)
    # Grouped by (broker, account_id, conid), not by conid alone. A position
    # exists WITHIN an account: the same contract held in two accounts is two
    # positions, and the same conid at two brokers need not even be the same
    # instrument. Ordering by conid alone fused them into one episode -- verified
    # on hand-built rows, where a +200 round trip in one account and a -150 one in
    # another became a single fabricated +50 CLOSED episode, with the position
    # netting to flat because the quantities cancelled.
    rows = conn.execute(
        f"SELECT * FROM trades {where} "
        "ORDER BY broker, account_id, conid, COALESCE(date_time, trade_date), trade_id",
        params,
    ).fetchall()

    episodes: list[Episode] = []
    current: Episode | None = None
    #: (broker, account_id, conid) -- the position's identity. See the query.
    current_key: tuple[str, str, str] | None = None

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
            still_held = (
                False if closed_by_reentry
                else (current.broker, current.account_id, current.conid) in held
            )
            _finalise(current, still_held=still_held)
            episodes.append(current)
            current = None

    for row in rows:
        conid = str(row["conid"] or "")
        key = (str(row["broker"] or ""), str(row["account_id"] or ""), conid)
        closing = _is_close(row["open_close"])

        if key != current_key:
            flush()
            current_key = key

        if current is None:
            current = _new_episode(row)
            # No opening fill on record means the entry predates the archive.
            current.entry_outside_window = closing
        elif current.entry_outside_window and not closing:
            # An opening fill after a close-only run is a fresh entry, so the
            # unresolvable episode ends here and a clean one begins.
            flush(closed_by_reentry=True)
            current = _new_episode(row)

        split = _through_zero(current, row)
        if split is not None:
            # The fill finished one position and began the opposite one, so it
            # belongs to both episodes: the closing part ends this one, the
            # leftover opens the next.
            _absorb(current, split[0])
            flush()
            current = _new_episode(row)
            _absorb(current, split[1])
            continue

        _absorb(current, row)

        if not current.entry_outside_window and _flat(current.net_qty):
            flush()

    flush()

    # A held contract with no fills anywhere in the archive produces no
    # episode above, so it would silently vanish from the open book.
    seen = {(e.broker, e.account_id, e.conid) for e in episodes}
    episodes.extend(
        _from_snapshot(row) for key, row in held.items() if key not in seen
    )

    episodes.sort(key=lambda e: (e.closed_at or "", e.opened_at or ""), reverse=True)
    return HistoryReport(
        asset_category=asset_category or "ALL",
        base_currency=base_currency,
        episodes=episodes,
        snapshot_date=snapshot_date,
        partial_record=sum(1 for e in episodes if e.entry_outside_window),
    )
