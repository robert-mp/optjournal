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
  are marked `entry_outside_window`. The position snapshots say how much each
  account holds, so a gap between one and the fills up to its date is quantity
  the archive cannot account for; a gap that is the SAME on every snapshot date
  is a holding from before the archive (`_pre_archive`), and the walk starts
  from it. A gap that appears part way through is a change no trade made (a
  split, a transfer), and those keep the older reading: a close-only run whose
  open/closed verdict is the snapshot's. This is the second place snapshots
  carry information trades cannot.

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
    "FillPart",
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


@dataclass(frozen=True, slots=True)
class FillPart:
    """What one episode took of one fill, in the terms an order leg is summed in.

    The whole fill, or for a reversal (`C;O`) the half `_through_zero` gave this
    episode. Named like `db.trade_legs`' columns, so a caller holding order LEGS
    can sum these per (order, contract) into the share of a leg one position
    took. See `campaigns.Campaign.leg_parts`.
    """

    quantity: float
    #: IBKR's marker, or for a half of a reversal that half's own: `C` or `O`.
    open_close: str
    date_time: str | None
    trade_price: float | None
    proceeds: float
    proceeds_base: float
    commission: float
    commission_base: float
    realized_pnl: float
    realized_pnl_base: float


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
    #: What was held before the first fill on record, when the position snapshot
    #: says: the snapshot's quantity less the fills up to its date. Non-zero only
    #: on an `entry_outside_window` episode whose size is therefore known, so
    #: trades decide when it goes flat. Zero on one the archive cannot size.
    pre_archive_qty: float = 0
    #: True when the archive holds no fills for this contract at all, so
    #: everything known about it comes from a position snapshot.
    snapshot_only: bool = False

    opened_at: str | None = None
    closed_at: str | None = None
    #: IBKR's trade date of the last closing fill: the day its statement books
    #: the realised P&L on. Usually `closed_at`'s day, but `closed_at` is the ET
    #: stamp (see `clock.epoch_et`), so a Korean sale at 20:03 ET on 31 August
    #: closed on 1 September. The scoreboard dates the outcome by it, the day
    #: its last P&L is booked.
    closed_on: str | None = None

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
    #: What this episode took of each of its fills, keyed by trade id (the keys
    #: of `trade_ids`): the whole fill, or the half of a reversal `_through_zero`
    #: gave it. Carried because an order LEG is per (order, contract), and one
    #: order's fills can end one position and begin the next, so a caller holding
    #: legs divides one by what each position actually took rather than by which
    #: orders it lists. See `campaigns.Campaign.leg_parts`.
    fill_parts: dict[str, FillPart] = field(default_factory=dict)

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


def _known_size(ep: Episode) -> bool:
    """Whether the episode's quantity is known, so trades can prove it flat.

    False only for an entry that predates the archive with no snapshot to say
    how large it was: that one defers to the snapshot instead.
    """
    return not ep.entry_outside_window or bool(ep.pre_archive_qty)


def _reverses(open_close: str | None) -> bool:
    """Whether the fill closed one side and OPENED the other: IBKR's `C;O`.

    A bare `C` closes only, so the quantity past flat in one is not a new
    position. Splitting every overshoot invented one out of a holding the archive
    had simply never seen the entry of.
    """
    tokens = split_notes((open_close or "").upper())
    return "C" in tokens and "O" in tokens


def _past_flat(ep: Episode | None, row: Any) -> bool:
    """Whether this closing fill takes the position beyond flat.

    Only for an episode whose quantity is known (`_known_size`): one whose entry
    predates the archive with nothing saying how large it was has no zero to
    pass.
    """
    if ep is None or not _known_size(ep) or _flat(ep.net_qty):
        return False
    qty = row["quantity"] or 0
    if not _is_close(row["open_close"]) or (qty > 0) == (ep.net_qty > 0):
        return False
    return abs(qty) > abs(ep.net_qty) and not _flat(abs(qty) - abs(ep.net_qty))


def _through_zero(ep: Episode, row: Any) -> tuple[dict, dict]:
    """A reversing fill past flat, split at zero. See `_past_flat`, which gates it.

    Long 2 calls then SELL 3 in one fill (IBKR marks it `C;O`): the first 2
    close the long and the last 1 opens a short. Returned as the closing part
    and the opening part.

    The closing part takes all of IBKR's realised P&L, which is what it is: the
    opening part realises nothing. Commission and proceeds divide by quantity.
    """
    qty = row["quantity"] or 0
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
        ep.closed_on = row["trade_date"] or row["date_time"]
    else:
        ep.open_fills += 1
        ep.opened_qty += qty
        # An entry before the archive has no opening fill on record, so a later
        # add-on is not when the position opened.
        if ep.opened_at is None and not ep.entry_outside_window:
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
        trade_id = str(row["trade_id"])
        ep.trade_ids.append(trade_id)
        ep.fill_parts[trade_id] = FillPart(
            quantity=qty,
            open_close=str(row["open_close"] or ""),
            date_time=row["date_time"],
            trade_price=row["trade_price"],
            proceeds=row["proceeds"] or 0.0,
            proceeds_base=row["proceeds_base"] or 0.0,
            commission=row["ib_commission"] or 0.0,
            commission_base=row["ib_commission_base"] or 0.0,
            realized_pnl=row["fifo_pnl_realized"] or 0.0,
            realized_pnl_base=row["fifo_pnl_realized_base"] or 0.0,
        )
    for tok in split_notes(row["notes"]):
        if tok not in ep.notes:
            ep.notes.append(tok)


def _finalise(ep: Episode, still_held: bool) -> None:
    """Decide an episode's terminal status.

    `still_held` is whether the contract appears in the newest position
    snapshot. It is the deciding signal only when trades alone are
    inconclusive, which happens when the opening fill predates the archive and
    no snapshot said how much was held (see `_known_size`).
    """
    disposition = disposition_of(";".join(ep.notes)) if ep.close_fills else None

    if not _known_size(ep):
        # Trades cannot prove flatness without the entry, so defer to the
        # snapshot: absent means the position is gone.
        ep.status = STATUS_OPEN if still_held else (disposition or STATUS_CLOSED)
        return

    if _flat(ep.net_qty) and ep.close_fills:
        ep.status = disposition or STATUS_CLOSED
    else:
        ep.status = STATUS_OPEN


#: The equity-summary column that prices each position-bearing category, so a
#: zero in it says the book in that category is empty. IBKR's NAV breakdown
#: prices stock and options separately and under their own names, and nothing
#: else a journal can hold: a fund, a bond or a future has no column here, so no
#: NAV row ever calls one of those flat.
NAV_VALUE_BY_CATEGORY: dict[str, str] = {
    "STK": "stock_base",
    "OPT": "options_base",
}


def _nav_flat_where(asset_category: str) -> str:
    """The equity-summary predicate saying this category holds nothing, or "".

    Per category, because each column speaks only for its own: the option book is
    flat when `options_base` is 0 whatever the stock figure. Reading the two
    together left a journal ingested with `--assets OPT` on a stale option book
    for good, since the day its options go flat has no position row at all and its
    NAV still prices the stock that journal does not track.

    A category the NAV cannot price gets "": no NAV row may empty it.
    """
    column = NAV_VALUE_BY_CATEGORY.get(asset_category.upper())
    return f"{column} = 0" if column else ""


def _book_dates(flat: str) -> str:
    """`book_dates_sql` for one category, whose NAV predicate is `flat`."""
    nav = (
        " UNION ALL SELECT broker, account_id, report_date FROM equity_summaries"
        f"  WHERE {flat}"
    ) if flat else ""
    return (
        "SELECT broker, account_id, MAX(d) AS book_date FROM ("
        " SELECT broker, account_id, report_date AS d FROM position_snapshots"
        f"{nav})"
        " GROUP BY broker, account_id"
    )


def _book_of(column: str) -> str:
    """Which of the mixed scope's books a row with this `asset_category` reads:
    its own category's if the NAV prices it, else the one no NAV row moves."""
    priced = ", ".join(f"'{category}'" for category in sorted(NAV_VALUE_BY_CATEGORY))
    return f"CASE WHEN {column} IN ({priced}) THEN {column} ELSE '' END"


def book_dates_sql(asset_category: str | None = None) -> str:
    """The date each account's position book is as of, as a derived table.

    The newest day the account reported a position in ANY category, or on which
    its NAV breakdown priced the category at nothing.

    `None`, every category at once, is each category read at its own book: one
    row per account for each category the NAV prices, tagged with it in
    `category`, and one tagged '' for every category it does not. So the whole is
    the union of the parts. Taking the NAV flat only where it priced stock AND
    options at nothing kept a position both halves called gone: the options of an
    `--assets OPT` journal, whose NAV still prices stock, or the stock of an
    account whose statement that day had no OpenPositions. `book_join_sql`
    matches each row to its own.

    Any category, because IBKR's OpenPositions lists only what is held and every
    row of one statement carries the same reportDate (checked across the real
    archive). The day the option book goes flat there is no OPT row at all, so the
    newest date that had an option is a stale book: a sold LEAP stayed OPEN with
    its realised P&L missing. A Trade Confirmation carries no positions, so it
    never moves this date.

    The NAV clause is the one case no position row can speak for: everything in
    the scope sold, and no row for it in any category. A statement whose query
    lacks the OpenPositions section leaves no row either, but the account still
    holds things and its NAV says so, so that silence keeps the older book rather
    than reading as flat. `_nav_flat_where` says which column answers.

    Per `(broker, account_id)`, so an account whose statements lag is read at its
    own date rather than against another's. `db.current_option_positions` spells
    this again for the Positions tab, at `asset_category='OPT'`;
    `tests/test_history.py` holds the two equal. GROUPED, not one subquery per
    row: spelled as a correlated scalar subquery it ran the UNION once for every
    snapshot row it filtered, which is quadratic in the snapshot count and reached
    `/api/state` three times over (measured 0.14s to 0.34s on 398 rows, and 0.2s
    to 21.5s on the 4,558 rows two more years of daily statements bring). As a
    derived table joined on `(broker, account_id)` it is one pass: 2.2s back to
    1ms at that size.
    """
    if asset_category:
        return _book_dates(_nav_flat_where(asset_category))
    books = [(category, _book_dates(_nav_flat_where(category)))
             for category in sorted(NAV_VALUE_BY_CATEGORY)]
    books.append(("", _book_dates("")))
    return " UNION ALL ".join(
        f"SELECT broker, account_id, '{category}' AS category, book_date FROM ({sql})"
        for category, sql in books
    )


def book_join_sql(asset_category: str | None = None) -> str:
    """The join narrowing a `position_snapshots p` to each account's current book:
    for every category at once, each row to its own category's (`book_dates_sql`)."""
    own = "" if asset_category else f"   AND b.category = {_book_of('p.asset_category')}"
    return (
        f" JOIN ({book_dates_sql(asset_category)}) b"
        "  ON b.broker = p.broker AND b.account_id = p.account_id"
        "   AND b.book_date = p.report_date"
        f"{own}"
    )


def book_date(
    conn: sqlite3.Connection, asset_category: str | None = None
) -> str | None:
    """The newest account's book date, for labelling a book "as of".

    The newest across accounts because a label for a mixed-date book should name
    its most recent statement. Deciding what is held stays per account.
    """
    row = conn.execute(
        f"SELECT MAX(book_date) FROM ({book_dates_sql(asset_category)})"
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

    "Current" is `book_dates_sql`, for this scope and per account. This dict
    decides open versus closed, so a stale book here is not cosmetic: a contract
    read from an older date than its account's newest is judged still held after
    it was sold.

    The date is returned even when nothing in `asset_category` is held: a flat
    book is still a book as of that day.
    """
    where, params = _position_scope_where(asset_category)
    held = {
        (str(r["broker"] or ""), str(r["account_id"] or ""), str(r["conid"])): dict(r)
        for r in conn.execute(
            "SELECT p.* FROM position_snapshots p"
            f"{book_join_sql(asset_category)} {where}"
            "  AND p.position != 0",
            params,
        )
    }
    return held, book_date(conn, asset_category)


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


def _day_key(value: Any) -> str:
    """YYYYMMDD from either stored date shape, so the two compare as text.

    Snapshot dates arrive as IBKR's compact `20260929`; fills as ISO or, from a
    Trade Confirmation, compact with a time (`20260924;101659`).
    """
    return "".join(ch for ch in str(value or "")[:10] if ch.isdigit())[:8]


def _pre_archive(
    rows: list[Any],
    conn: sqlite3.Connection,
) -> dict[tuple[str, str, str], float]:
    """What each contract held before its first fill on record, per the snapshots.

    A gap between what a snapshot says an account holds and what its fills up to
    that date add up to is quantity the archive cannot account for. It is seeded
    as a pre-archive holding only when it is the SAME on every snapshot date the
    account has, which is the property a real one has: every later change to a
    pre-archive holding is a trade, the sale of the pre-archive shares included,
    so the gap never moves. A gap that appears part way through is a quantity
    change no trade made -- a share split, which arrives as a corporate action, or
    a transfer between brokers -- and seeding one started the walk with shares
    nothing had bought: the closed round trip before it fused with the position
    after it into a single open episode, and its realised P&L left the closed
    totals. Those keep the close-only reading.

    Only fills up to each date, because a Trade Confirmation fill from today
    postdates the newest statement and no snapshot can know it. Nothing for an
    account with no snapshot at all, since there is then no book to reconcile
    against.

    Real data: TSLA stock read 96 shares where the account held 206 (110 bought
    before the archive), and IBKR 0.0019 where it held 0.8615. Those two are the
    only disagreements anywhere in the archive, and each is the same quantity on
    all 28 of its snapshot dates.
    """
    days: dict[tuple[str, str], set[str]] = {}
    positions: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in conn.execute(
        "SELECT broker, account_id, conid, report_date, position"
        " FROM position_snapshots"
    ):
        account = (str(row["broker"] or ""), str(row["account_id"] or ""))
        day = _day_key(row["report_date"])
        days.setdefault(account, set()).add(day)
        key = (*account, str(row["conid"] or ""))
        positions.setdefault(key, {})[day] = row["position"] or 0
    dates = {account: sorted(seen) for account, seen in days.items()}

    fills: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
    for row in rows:
        key = (str(row["broker"] or ""), str(row["account_id"] or ""),
               str(row["conid"] or ""))
        if key[:2] in dates:
            fills.setdefault(key, []).append(
                (_day_key(row["trade_date"] or row["date_time"]),
                 row["quantity"] or 0))

    out: dict[tuple[str, str, str], float] = {}
    for key, entries in fills.items():
        snapshot = positions.get(key, {})
        entries.sort()
        before: float | None = None
        net: float = 0
        index = 0
        for day in dates[key[:2]]:
            while index < len(entries) and entries[index][0] <= day:
                net += entries[index][1]
                index += 1
            gap = (snapshot.get(day) or 0) - net
            if before is None:
                before = gap
            # Settled by the first date whose gap is flat, or the first that moves
            # off it: walking on to the end made this contracts times dates, and
            # nearly every contract is decided on the first date.
            if _flat(before) or not _flat(gap - before):
                break
        else:
            if before is not None:
                out[key] = before
    return out


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

    pre_archive = _pre_archive(rows, conn)

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
            before = pre_archive.pop(key, 0)
            if before:
                # The snapshot says what was held before this first fill, so the
                # walk starts there and the fills alone decide when it is flat.
                # Without it, scaling in or out before closing walked from zero:
                # flat too early, or never (a phantom open episode).
                current.entry_outside_window = True
                current.pre_archive_qty = before
                current.net_qty = before
                current.peak_qty = abs(before)
            else:
                # No opening fill on record means the entry predates the archive.
                current.entry_outside_window = closing
        elif not _known_size(current) and not closing:
            # An opening fill after a close-only run is a fresh entry, so the
            # unresolvable episode ends here and a clean one begins.
            flush(closed_by_reentry=True)
            current = _new_episode(row)

        past_flat = _past_flat(current, row)
        if past_flat and _reverses(row["open_close"]):
            # The fill finished one position and began the opposite one, so it
            # belongs to both episodes: the closing part ends this one, the
            # leftover opens the next. Each records its own half (`fill_parts`).
            close_part, open_part = _through_zero(current, row)
            _absorb(current, close_part)
            flush()
            current = _new_episode(row)
            _absorb(current, open_part)
            continue

        peak = current.peak_qty
        _absorb(current, row)

        if past_flat:
            # A bare `C` opens nothing, so closing more than the walk knew was
            # held says the rest was held before the archive began rather than
            # that a position opened. The close took it flat, and its size is now
            # known: the whole fill went out of it. Held all along, so every
            # reading the walk took was short of it, the largest included, and
            # the position opened before the archive rather than at the first
            # fill the walk saw.
            unseen = -current.net_qty
            current.pre_archive_qty += unseen
            current.entry_outside_window = True
            current.peak_qty = peak + abs(unseen)
            current.opened_at = None
            current.net_qty = 0

        if _known_size(current) and _flat(current.net_qty):
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
