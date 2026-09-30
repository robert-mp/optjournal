"""Modelled numbers for the replay panel: implied vol, the band, and the marks.

THE ONLY MODULE THAT PRICES ANYTHING. Every figure the rest of this journal
reports is broker-stated -- a fill's price, a commission, a realised P&L, all
read from a statement. The replay panel is the one surface that shows numbers
nobody was charged: an expected-move envelope, a modelled mark-to-market series,
an effective delta. Those come from a model, the panel captions them as modelled,
and `tests/test_layering.py` holds the quarantine that keeps them here.

That quarantine is why this module exists as a module. It previously lived inside
`bars.py`, whose real job is storage -- which contract over which window, the
idempotent write, the series a chart reads. Nothing about a manifest or an upsert
needs Black-Scholes, so `bars.py` was in the pricing allowlist for the half of
itself that did. Split apart, the allowlist entry names a module whose whole
purpose is pricing, and `bars.py` imports no pricing at all.

WHAT IS SHARED, AND WHY IT IS ONE SOLVE. The band and the marks are two halves of
one picture: an envelope, and the P&L series drawn inside it. They each used to
solve implied vol independently -- 20 solves for 10 replays over the demo
journal, 60% of `build_state` with half of that exact recomputation. `replay_model`
solves once and hands the result to both, which is the stronger correctness
statement as well as the faster one: they cannot disagree about what the market
charged for a contract when there is one answer rather than two that happen to
match.

THE ONE THING THIS STILL NEEDS FROM STORAGE is `bars.close_series` (and
`bars.replay_bars` for the underlying series): a vol solve reads an option's own
daily closes and the underlying's, so pricing depends on the layer that keeps
them. That direction is correct and one-way -- storage never reaches back for a
model. The one read beside it is the underlying's session opens (`_path`),
because a fill in a session's first hour happened next to the open.

EVERY PRICE IS READ AT ITS CLOSE. A stored bar is stamped at its open and carries
its close, so the model places it where it was printed (`_closed_at`): the vol a
bar sees, the fills it holds and the time it has left all run from its close,
and an option's midnight-stamped daily close is not known until 16:00.

TWO HALVES, ONE CONCERN. Below the arithmetic sits the ASSEMBLY: turning
lifecycles and position rows into the `replays` map the panel reads -- the strike
segments, the fill stamps, the event cards. That half was `web.py`'s, where it was
285 lines of one concern inside an HTTP server, reachable only through a function
that mutated the payload dict in place. It is here because it is the same concern
as the arithmetic: a replay. `attach` is the whole interface, and `build_state`
calls it once, last.

Keeping them together is what lets the seam be narrow. The assembly needs the
marks to build its event cards (`_annotations` reads the delta a roll removed
straight out of them), so splitting the two would mean either handing the marks
back across a module boundary or solving vol twice -- and solving twice is the
exact defect `replay_model` exists to prevent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, time
from functools import cache
from typing import Any

from optjournal.bars import close_series, replay_bars
from optjournal.blackscholes import bs_delta, bs_price, expected_move, implied_vol
from optjournal.clock import MARKET_TZ, epoch_et, et_day, expiry_epoch

__all__ = [
    "BandContract",
    "ReplayLeg",
    "attach",
    "band_contracts",
    "delta_around",
    "expected_move_band",
    "modelled_marks",
    "replay_model",
]


@dataclass(frozen=True, slots=True)
class ReplayLeg:
    """One leg, with everything the modelled P&L needs to follow it over time."""

    conid: str
    strike: float
    right: str
    expiry: str
    multiplier: float = 100.0
    #: (epoch, signed quantity delta, price per unit) per fill, oldest first.
    fills: tuple[tuple[int, float, float], ...] = ()
    #: For a contract with no fills anywhere: the position held throughout the
    #: window and the price it was acquired at, from the snapshot's cost basis.
    seed_quantity: float = 0.0
    seed_price: float = 0.0


@dataclass(frozen=True, slots=True)
class BandContract:
    """One leg, reduced to what a vol solve needs and nothing else."""

    conid: str
    strike: float
    right: str
    expiry: str
    #: (epoch, price per unit) from the trade's OWN fills. A fill is an option
    #: price the market actually charged, so it is a vol observation in exactly
    #: the way a daily close is -- and the only one available for a session the
    #: price source has no history for. See _vol_series.
    anchors: tuple[tuple[int, float], ...] = ()


#: How long each stored bar spans. A bar is stamped at its OPEN and carries its
#: CLOSE (see `marketdata.Bar`), so where its price belongs in time is the stamp
#: plus this, and never later than the session's close.
_BAR_SECONDS = {"1h": 3600, "1d": 86400}

#: The regular session's close in ET: when a daily bar's price was set, and the
#: latest an hourly bar can close.
_SESSION_CLOSE = time(16, 0)

_YEAR = 365.0 * 86400


def _session_at(stamp: int, moment: time) -> int:
    """``moment`` ET on the trading day ``stamp`` belongs to, as an epoch."""
    day = datetime.fromtimestamp(stamp, MARKET_TZ).date()
    return int(datetime.combine(day, moment, MARKET_TZ).timestamp())


#: Cached for `clock.et_day`'s reason: a pure function of a bar stamp, called
#: from inside every per-bar loop, over a key space of the stored bars.
@cache
def _closed_at(stamp: int, bar_size: str) -> int:
    """When a stored bar's price was observed: at its close, not at its stamp.

    Every price here is a CLOSE, and the source stamps a bar at its OPEN, so the
    10:30 hourly bar's price is the one printed at 11:30 and the 15:30 bar's the
    one at 16:00. A daily bar closes at 16:00 whatever it is stamped: 09:30 for
    an underlying, midnight ET for an option. Reading a price at its stamp gave
    every bar an hour (or a session) more to expiry than its price had, and read
    an option's close as known from midnight, sixteen hours before it happened.
    """
    return min(stamp + _BAR_SECONDS[bar_size], _session_at(stamp, _SESSION_CLOSE))


def _path(
    conn: sqlite3.Connection,
    conid: str,
    points: list[tuple[int, float]],
    bar_size: str,
) -> list[tuple[int, float]]:
    """The underlying's price at each instant one was printed, oldest first.

    Every bar's close at its close (see `_closed_at`), and a bar's OPEN at its
    stamp wherever that comes after the last close: the start of each session,
    which is the price a fill in the first hour happened next to. The night
    between a 16:00 close and the next open is not trading time, and the gap
    across it is often the largest move in the series. Measured on the real S&P
    on 2026-09-03: 7667.45 at the close, 7686.71 at the open, 7703.52 at 10:30.
    A 09:34 fill interpolated across the night from the prior close solved 22.4%
    for the 7755C, where the price around it (7688.02) solves 18.4%.

    Read here rather than through `bars.close_series` because this is the one
    use of an open: the chart draws closes.
    """
    opens = {
        int(row["ts"]): float(row["open"])
        for row in conn.execute(
            "SELECT ts, open FROM price_bars WHERE conid = ? AND bar_size = ?"
            " AND ts >= ? AND ts <= ? AND open IS NOT NULL",
            (conid, bar_size, points[0][0], points[-1][0]),
        )
    }
    path: list[tuple[int, float]] = []
    for stamp, close in points:
        if stamp in opens and (not path or stamp > path[-1][0]):
            path.append((stamp, opens[stamp]))
        path.append((_closed_at(stamp, bar_size), close))
    return path


def _spot_at(path: list[tuple[int, float]], ts: int) -> float | None:
    """Underlying price at ``ts``, between the two prints either side of it.

    ``path`` is stamped at the instant of each print (see `_path`), so the two
    prices bracketing a fill really do surround it.
    """
    if not path or ts < path[0][0] or ts > path[-1][0]:
        return None
    for index in range(1, len(path)):
        if path[index][0] >= ts:
            prev_ts, prev_px = path[index - 1]
            next_ts, next_px = path[index]
            width = (next_ts - prev_ts) or 1
            return prev_px + ((ts - prev_ts) / width) * (next_px - prev_px)
    return path[-1][1]


def _vol_series(
    conn: sqlite3.Connection,
    contracts: list[BandContract],
    points: list[tuple[int, float]],
    underlying_conid: str | None,
    bar_size: str,
) -> dict[str, list[tuple[int, float]]]:
    """Implied vol per leg, as ``(observed_at, vol)``, from each option's OWN prices.

    The reference implementation this feature imitates labels its band
    "IV ref: VIX" -- S&P 500 implied vol applied to a gold ETF -- because the
    underlying is all it has. We hold the contract's prices, so we can ask what
    the market charged for THIS contract.

    An option's daily close is paired with the underlying's daily close for the
    same session, matched on the ET trading DAY (see et_day for why the raw
    timestamps cannot be compared), and both are the 16:00 close whatever the
    source stamped them. So the vol is solved at 16:00, with the time to expiry
    left at 16:00, and it is observed from 16:00: the source's midnight stamp
    had it known across the whole session it closed, which on a 0DTE is the
    settlement read from the open. Pairing against the CHART series instead
    looks equivalent and is not: a chart may be hourly, and interpolating a
    daily close into it prices the option against a spot the market never
    showed and books the discrepancy as volatility.

    Per LEG rather than averaged, because pricing a leg wants its own vol; the
    band averages afterwards, since expected move is a property of the underlying
    and a strangle's two legs are two observations of it.
    """
    if not points or not contracts or not underlying_conid:
        return {}
    low, high = points[0][0], points[-1][0]
    path = _path(conn, str(underlying_conid), points, bar_size)
    daily = {
        et_day(stamp): close
        for stamp, close in close_series(conn, str(underlying_conid), bar_size="1d")
    }
    out: dict[str, list[tuple[int, float]]] = {}
    for contract in contracts:
        expiry = expiry_epoch(contract.expiry)
        if expiry is None:
            continue
        # A day of slack either side because the join is BY DAY and the source
        # stamps the two series differently: an option's bar for the same session
        # lands at 04:00Z, hours BEFORE a chart window that starts at the 13:30Z
        # open, so clipping to the exact window drops the first day's vol.
        closes = close_series(
            conn, contract.conid, bar_size="1d",
            start=low - 86400, end=high + 86400,
        )
        found: list[tuple[int, float]] = []
        for stamp, close in closes:
            spot = daily.get(et_day(stamp))
            if spot is None:
                continue
            at = _closed_at(stamp, "1d")
            years = (expiry - at) / _YEAR
            vol = implied_vol(close, spot, contract.strike, years, contract.right)
            if vol is not None:
                found.append((at, vol))
        found.extend(_anchor_vols(contract, path, expiry))
        if found:
            out[contract.conid] = sorted(found)
    return out


def _anchor_vols(
    contract: BandContract, path: list[tuple[int, float]], expiry: int
) -> list[tuple[int, float]]:
    """Implied vol from the trade's own fills.

    This exists because a price source's history can start AFTER a trade did.
    Measured on this journal: the TSLA 270P was sold on 2026-07-24, and the
    source's first bar for that contract is 2026-07-27 -- so the band, the
    effective delta and the modelled P&L were all absent across the entry
    session, which is the one part of a replay a reader most wants. Asking the
    source for an earlier window returns nothing; the data does not exist. The
    fill does: the market charged 5.24 for that contract at 10:35, which is an
    option price as real as any close.

    Spot comes from _spot_at, between the two prints that surround the fill's
    own instant (see `_path`), and this is the one place that pairing is
    correct, where pairing a daily close that way is not: a fill carries a
    genuine intraday instant that the series brackets, while a daily close is a
    16:00 price the hourly series only reaches at its last bar.

    Only fills inside the chart window are used, since a spot outside it cannot
    be read; anything the solve rejects (a price below intrinsic, a fill after
    expiry) is dropped rather than defaulted, so a gap stays a gap.
    """
    found: list[tuple[int, float]] = []
    for stamp, price in contract.anchors:
        spot = _spot_at(path, stamp)
        if spot is None:
            continue
        years = (expiry - stamp) / _YEAR
        vol = implied_vol(price, spot, contract.strike, years, contract.right)
        if vol is not None:
            found.append((stamp, vol))
    return found


def _held_forward(series: list[tuple[int, float]], stamp: int) -> float | None:
    """The last observed value at or before ``stamp``, or None before the first.

    Held forward rather than interpolated toward the next observation: an
    interpolated vol asserts a value between two closes that nothing was priced
    at, while holding forward says only "this is the last thing the market told
    us". None before the first observation, so a gap reads as absent information
    rather than as a narrow range or a zero P&L.
    """
    found: float | None = None
    for observed_at, value in series:
        if observed_at > stamp:
            break
        found = value
    return found


def _position(leg: ReplayLeg, at: int) -> tuple[float, float]:
    """A leg's signed quantity and the cash it has moved by ``at``: ``(quantity, cash)``.

    A fill counts once it has happened, one at exactly ``at`` included. A
    snapshot-only contract has no fills anywhere -- that is what makes it
    snapshot-only -- so it is seeded from its cost basis and held flat across the
    window. Cash is money received, so a sale is positive.
    """
    quantity = leg.seed_quantity
    cash = -leg.seed_quantity * leg.seed_price * leg.multiplier
    for fill_at, delta_qty, price in leg.fills:
        if fill_at > at:
            break
        quantity += delta_qty
        cash -= delta_qty * price * leg.multiplier
    return quantity, cash


def _basis(legs: list[ReplayLeg], at: int) -> list[tuple[ReplayLeg, int]]:
    """The legs a bar is modelled from, each with its expiry epoch.

    The legs HELD at ``at``, or every leg when nothing is held (the context either
    side of the trade), less any that have expired by then: an expired contract
    has no horizon left to measure. Held rather than ever-held, because a roll
    closes one contract and opens another, and the position after it is the new
    one's. Empty when everything held has expired, which is a settled position.
    """
    held = [leg for leg in legs if _position(leg, at)[0]]
    return [
        (leg, expiry)
        for leg in (held or legs)
        if (expiry := expiry_epoch(leg.expiry)) is not None and expiry >= at
    ]


def expected_move_band(
    conn: sqlite3.Connection,
    legs: list[ReplayLeg],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
    bar_size: str,
    vols: dict[str, list[tuple[int, float]]] | None = None,
) -> list[list[float]]:
    """A one-standard-deviation envelope, per underlying bar, keyed by its stamp.

    Spot and time-to-expiry are per BAR, so the envelope moves and tapers hourly;
    the vol input steps daily, because the source serves no intraday option
    history and there is nothing finer to solve against. Each bar is read at its
    CLOSE (see `_closed_at`): its spot is the close, the vol is the latest one
    observed by then, and the time left runs from then. So the last bar of an
    expiry session, which closes AT the expiry, is drawn with no width: nothing
    is left to move.

    Each bar is measured from its `_basis`, the legs held at that bar: the vol is
    their average and the horizon the NEAREST of their expiries, which is the one
    that dominates the risk. That is what makes the envelope narrow as a trade
    ages and step outward when a roll pushes expiry further out. It was once the
    nearest expiry over every leg the trade ever held, so after a roll it kept
    measuring to the contract just closed, and vanished at that contract's
    expiry while the new one was still open.
    """
    # `is None`, not `vols or ...`: an empty dict is the legitimate answer when
    # nothing solved, and the truthiness spelling would re-run the whole solve
    # for exactly that case.
    if vols is None:
        vols = _vol_series(conn, band_contracts(legs), points, underlying_conid, bar_size)
    if not vols:
        return []
    band: list[list[float]] = []
    for stamp, spot in points:
        at = _closed_at(stamp, bar_size)
        basis = _basis(legs, at)
        observed = [
            vol
            for vol in (_held_forward(vols.get(leg.conid, []), at) for leg, _ in basis)
            if vol is not None
        ]
        if not observed:
            continue
        horizon = min(expiry for _, expiry in basis)
        average = sum(observed) / len(observed)
        move = 0.0 if at == horizon else expected_move(spot, average, (horizon - at) / _YEAR)
        if move is None:
            continue
        band.append([stamp, round(spot - move, 4), round(spot + move, 4)])
    return band


def band_contracts(legs: list[ReplayLeg]) -> list[BandContract]:
    """The vol-solve view of a replay's legs: one `BandContract` each.

    The single projection, used by BOTH consumers. The band and the modelled
    marks are drawn on one chart and must agree about what the market charged for
    each contract, so deriving them from separately-built contract lists made
    "one vol series per leg" a property of two functions happening to match --
    they did match, verified across both journals, but nothing held them there.

    A leg's own fills become its anchors, so a session the price source has no
    history for still prices from what the market really charged. A snapshot-only
    leg has no fills to anchor with -- that is what makes it snapshot-only -- and
    is seeded from its cost basis instead, which carries no timestamp.

    Deduplication is the caller's: `_replay_legs` already groups by conid, which
    is what makes a contract sold to open and bought to close ONE leg with two
    anchors rather than two legs weighting that strike double in the average.
    """
    return [
        BandContract(
            conid=leg.conid,
            strike=leg.strike,
            right=leg.right,
            expiry=leg.expiry,
            anchors=tuple((stamp, price) for stamp, _qty, price in leg.fills),
        )
        for leg in legs
    ]


def modelled_marks(
    conn: sqlite3.Connection,
    legs: list[ReplayLeg],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
    bar_size: str,
    vols: dict[str, list[tuple[int, float]]] | None = None,
) -> list[list[float | None]]:
    """Modelled P&L and effective delta per bar: ``[ts, pnl, delta]``.

    P&L is cash flow to date plus the mark-to-market of whatever is still open --
    the standard formulation, and the reason it behaves correctly through a
    partial close or a roll rather than only for a clean open-then-close. A
    closing fill moves value out of the open leg and into realised cash, so once
    a position is flat the figure FREEZES at what the trade made instead of
    continuing to mark a contract nobody holds.

    Each bar is read at its CLOSE (see `_closed_at`), the instant its price was
    printed: a fill inside the bar is already in the position, the vol is the
    latest one observed by then, and the time to expiry runs from then.

    The PRICING is exact: repricing an option's own close at the vol solved from
    it returns that close to 1e-14, so nothing is approximated in the model
    itself. The chart's mark is not a restatement of a broker figure though --
    it applies that vol to the BAR's spot and the BAR's time to expiry. So the
    series TRACKS the broker's mark rather than matching it: the LEAP lands $75
    from the snapshot's unrealised on a $3,000 position, measured while option
    closes were still read at their midnight stamps, which is that drift plus the
    snapshot being a day older than the latest bar. Close enough to trust the
    shape, not close enough to quote as the position's value -- which is why the
    card beside the chart still states the broker's figure.

    Gross of commission, unlike every accounting figure in this journal. A
    per-bar commission would have to invent when the cost was incurred, and the
    card beside the chart already states the net figure the broker billed.

    Effective delta is ``sum(signed quantity * delta)``, without the multiplier,
    so a delta-neutral strangle reads 0.0 and a short put reads a positive
    fraction -- the scale the reference chart uses.

    It is ``None`` on any bar where nothing is HELD: before the opening fill, and
    after a close takes the position flat. 0.0 cannot serve there, because on a
    symmetric axis 0.0 means delta-neutral -- a real state, and the one a strangle
    is opened in -- so reporting it for an empty position states a position that
    was never held. P&L keeps reporting through the same bars; see the note at
    the append.
    """
    if vols is None:
        vols = _vol_series(
            conn, band_contracts(legs), points, underlying_conid, bar_size
        )
    if not vols:
        return []
    marks: list[list[float | None]] = []
    for stamp, spot in points:
        at = _closed_at(stamp, bar_size)
        cash = 0.0
        value = 0.0
        delta = 0.0
        priced = False
        # Separate from `priced`, and that distinction is the whole point. A bar
        # can be PRICEABLE (its contract has a solvable vol) while nothing is
        # HELD -- before the opening fill, and after a close takes the position
        # flat. Sharing one flag made those bars report `delta` as the sum of
        # `0 * bs_delta(...)`, an exact 0.0, and on a SYMMETRIC axis 0.0 is not
        # an absence: it is the centre line, the state a strangle is opened in.
        # So a closed trade drew twelve bars of "we were delta-neutral" when the
        # truth was "we were not in the trade" -- the same fabrication
        # `delta_around` already refuses to make for an opening event, and the
        # same rule `markAt` follows in returning null rather than a neighbour's
        # figure. Measured on this journal: the TSLA short put reported +0.3171
        # then 0.0000 for twelve bars after its buyback, and every replay with
        # context before entry did the mirror image.
        held = False
        for leg in legs:
            expiry = expiry_epoch(leg.expiry)
            series = vols.get(leg.conid)
            if expiry is None or not series:
                continue
            # Position and cash as of this bar. A snapshot-only contract has no
            # fills anywhere -- that is what makes it snapshot-only -- so it is
            # seeded from its cost basis and held flat across the window.
            quantity = leg.seed_quantity
            cash -= leg.seed_quantity * leg.seed_price * leg.multiplier
            for fill_at, delta_qty, price in leg.fills:
                if fill_at > at:
                    break
                quantity += delta_qty
                cash -= delta_qty * price * leg.multiplier
            vol = _held_forward(series, at)
            if vol is None:
                continue
            years = (expiry - at) / _YEAR
            if years < 0:
                # The contract is gone. Pricing past expiry is not merely
                # imprecise, it is confidently wrong: bs_price clamps to
                # intrinsic and bs_delta to 1.0, so a held position draws a P&L
                # that keeps swinging with spot and a delta pinned at the top of
                # its axis for as long as the window runs. Measured on the demo's
                # snapshot-only call, that was two months of tail on a contract
                # that had settled. Skipping means the series ENDS at expiry,
                # which is where the band already ends.
                continue
            unit = bs_price(spot, leg.strike, years, vol, leg.right)
            value += quantity * unit * leg.multiplier
            delta += quantity * bs_delta(spot, leg.strike, years, vol, leg.right)
            priced = True
            if quantity:
                held = True
        if not priced:
            continue
        # P&L is still reported when nothing is held, and that asymmetry is
        # deliberate rather than an oversight. Cash flow to date with no open leg
        # left to mark IS the trade's result: it is frozen because the trade
        # finished, so the figure is true. Exposure has no such post-close value
        # -- there is nothing to be exposed by -- so it is absent instead.
        marks.append([
            stamp, round(cash + value, 2), round(delta, 4) if held else None,
        ])
    return marks


def replay_model(
    conn: sqlite3.Connection,
    legs: list[ReplayLeg],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
    bar_size: str | None,
) -> tuple[list[list[float]], list[list[float | None]]]:
    """The band and the modelled marks for one replay, as ``(band, marks)``.

    ``bar_size`` is the grid ``points`` are on, which is what places each price
    at its close. None only when no bars are stored, and then there is nothing
    to model.

    Solves the vol series ONCE and hands it to both. They are the two halves of
    one picture -- an envelope and the P&L series drawn inside it -- and they were
    each solving it independently: measured over the demo journal, that was 20
    solves for 10 replays, with `_vol_series` accounting for 60% of the whole
    `build_state` and half of that being exact recomputation.

    Sharing the solve is also the stronger correctness statement, not just the
    faster one. The band and the marks now cannot disagree about what the market
    charged for a contract, because there is one answer rather than two that
    happen to match -- the same reason `band_contracts` is a single projection.

    Composed here rather than in `web.py` so the caller cannot get the sharing
    half-right: passing the solve to one function and not the other would look
    correct and silently keep the cost.
    """
    if not points or bar_size is None:
        return [], []
    vols = _vol_series(conn, band_contracts(legs), points, underlying_conid, bar_size)
    band = expected_move_band(
        conn, legs, points, underlying_conid=underlying_conid,
        bar_size=bar_size, vols=vols,
    )
    marks = modelled_marks(
        conn, legs, points, underlying_conid=underlying_conid,
        bar_size=bar_size, vols=vols,
    )
    return band, marks


def delta_around(
    marks: list[list[float | None]], stamp: int, *, bar_size: str
) -> tuple[float | None, float | None]:
    """Effective delta immediately before and after an event, as ``(before, after)``.

    The pair a roll is judged by: the reference implementation this feature
    imitates puts "Eff Delta 17 -> 10" on its roll card, because the number that
    matters about a roll is how much exposure it removed.

    ``before`` is the last mark whose bar CLOSED strictly before the event and
    ``after`` the first that closed at or after it, so an opening fill correctly
    reports ``None -> 0.56``: there was no position to have a delta, and inventing
    0.0 there would read as "we were delta-neutral" rather than "we were not in
    the trade". Compared at the close because that is when a mark is read (see
    `modelled_marks`): the bar a 10:35 fill falls in closes at 11:30 already
    holding it, so by its stamp it would be "before" a fill it contains.

    Either side may be None at a window edge, when no bar in the window had a
    solvable vol, or because the neighbouring bar HELD nothing -- `modelled_marks`
    reports delta as None off-position, and this carries that through rather than
    flattening it to 0.0. A closing event therefore reads ``0.32 -> None``, which
    is the honest shape: it had exposure, and now there is none to have.
    """
    before: float | None = None
    after: float | None = None
    for row in marks:
        row_stamp = row[0]
        if row_stamp is None:
            continue
        if _closed_at(int(row_stamp), bar_size) < stamp:
            before = row[2]
        else:
            after = row[2]
            break
    return before, after




# --------------------------------------------------------------------------
# Assembly: the payload the replay panel reads.
#
# This half was `web.py`'s. It is here because it is the same concern as the
# arithmetic above -- a replay -- and `web.py` is an HTTP server that happened to
# host it. `attach` is the whole interface: `build_state` calls it once, last.
# --------------------------------------------------------------------------


def _strikes_of(legs: list[ReplayLeg]) -> list[dict[str, Any]]:
    """One entry per contract, sided by how it was OPENED and spanning when held.

    The window is what makes a strike a SEGMENT rather than a full-width rule.
    Drawn edge to edge, a strike claims to have existed for the whole chart --
    including the session of context before entry, and every bar after a roll
    moved it somewhere else. The reference implementation draws segments for
    exactly this reason: the picture should say which levels were live when.

    Side comes from the opening fill because a closed contract holds the same
    strike twice, sold to open and bought to close. A strike you sold is a level
    you are defending and one you bought is a level you paid for; the chart
    encodes that difference, so reading it off the wrong fill inverts the meaning
    of every line.

    ``to`` is None while the contract is still held, which the page draws to the
    right edge. ``frm`` is None only for a snapshot-only contract, whose entry
    date is unknown -- drawn full width, because the honest statement there is
    "held throughout" rather than a guessed start.
    """
    out: list[dict[str, Any]] = []
    for leg in legs:
        # Running position, so the segment ends where the contract went flat
        # rather than at whichever fill happened to be last.
        quantity = leg.seed_quantity
        opened_at: int | None = None
        closed_at: int | None = None
        sold = leg.seed_quantity < 0
        for index, (stamp, delta_qty, _price) in enumerate(leg.fills):
            if index == 0:
                opened_at, sold = stamp, delta_qty < 0
            quantity += delta_qty
            if quantity == 0:
                closed_at = stamp
                break
        out.append({
            "strike": leg.strike,
            "put_call": leg.right,
            "side": "short" if sold else "long",
            "frm": opened_at,
            "to": closed_at,
        })
    return sorted(out, key=lambda row: row["strike"])


def _replay_legs(rows: list[dict[str, Any]]) -> list[ReplayLeg]:
    """One ReplayLeg per distinct contract, carrying its whole fill schedule.

    Grouped by conid rather than one leg per fill, because the mark-to-market
    walks a running position: a contract sold to open and bought to close is ONE
    leg with two fills, and treating it as two legs would hold both at once and
    double the position.

    Quantities are signed as the fill states them -- IBKR sends -3 for a sale --
    so cash flow and position both fall out of the same number without a
    buy_sell branch.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        conid = str(row.get("conid") or "")
        strike, expiry = row.get("strike"), row.get("expiry")
        if not conid or strike is None or not expiry:
            continue
        stamp = epoch_et(row.get("first_fill_at"))
        quantity, price = row.get("quantity"), row.get("avg_price")
        entry = grouped.setdefault(conid, {
            "conid": conid,
            "strike": float(strike),
            "right": str(row.get("put_call") or ""),
            "expiry": str(expiry),
            "multiplier": float(row.get("multiplier") or 100.0),
            "fills": [],
        })
        if stamp is not None and quantity is not None and price is not None:
            entry["fills"].append((stamp, float(quantity), float(price)))
    return [
        ReplayLeg(
            conid=entry["conid"], strike=entry["strike"], right=entry["right"],
            expiry=entry["expiry"], multiplier=entry["multiplier"],
            fills=tuple(sorted(entry["fills"])),
        )
        for entry in grouped.values()
    ]


def _snapshot_leg(row: dict[str, Any]) -> ReplayLeg:
    """The single leg a position snapshot row implies.

    One constructor because the strikes and the modelled marks need the SAME
    leg: built twice, the two could disagree about the seed quantity or price
    and the chart would draw a strike segment for a position the P&L series was
    not following.

    Seeded rather than filled: a row reaching this path has no fills anywhere --
    that is what makes it snapshot-only -- so the position is held flat across
    the window at the basis the snapshot states.
    """
    return ReplayLeg(
        conid=str(row.get("conid") or ""),
        strike=float(row.get("strike") or 0.0),
        right=str(row.get("put_call") or ""),
        expiry=str(row.get("expiry") or ""),
        multiplier=float(row.get("multiplier") or 100.0),
        seed_quantity=float(row.get("position") or 0.0),
        seed_price=float(row.get("cost_basis_price") or 0.0),
    )


def _annotations(
    lifecycle: dict[str, Any], marks: list[list[float | None]], bar_size: str | None
) -> list[dict[str, Any]]:
    """One card per EVENT on the timeline: what was done, what it cost, what it changed.

    An event, not a fill. A four-leg iron condor opened in one order is one
    decision and belongs on one card; splitting it per fill would turn a single
    act into four annotations that each look like a separate trade. The grouping
    is strategies.py's, already computed -- which is also how a roll arrives
    labelled as one: an order with both opening and closing legs classifies as
    "Roll" there, so the card needs no detection of its own and cannot disagree
    with the event caption shown elsewhere on the same page.

    ``kind`` is derived from the legs' open/close markers rather than parsed out
    of the label, because the label is prose meant for a human ("Short put
    close") and matching on it would break the moment classify() rewords.

    Delta before and after come from the modelled marks, so a roll states the
    exposure it removed. An OPENING event reports ``None -> x``: there was no
    position to have a delta. ``bar_size`` is the grid the marks are on, which
    `delta_around` needs to read each at its close; None only with no marks.
    """
    out: list[dict[str, Any]] = []
    for event in lifecycle.get("events") or []:
        stamp = epoch_et(event.get("first_fill_at"))
        if stamp is None:
            continue
        legs = [
            leg
            for order in (event.get("orders") or [])
            for leg in (order.get("legs") or [])
        ]
        markers = {str(leg.get("open_close") or "").upper() for leg in legs}
        kind = (
            "roll" if len(markers) > 1
            else "close" if markers == {"C"}
            else "open"
        )
        before, after = (
            delta_around(marks, stamp, bar_size=bar_size) if bar_size else (None, None)
        )
        out.append({
            "ts": stamp,
            "at": str(event.get("first_fill_at") or "")[:16],
            "label": event.get("label"),
            "kind": kind,
            # Only what a card shows. Passing the whole leg would ship account
            # ids and both currencies' worth of every figure into a tooltip.
            "legs": [
                {
                    "strike": leg.get("strike"),
                    "put_call": leg.get("put_call"),
                    "buy_sell": leg.get("buy_sell"),
                    "open_close": leg.get("open_close"),
                    "quantity": leg.get("quantity"),
                    "avg_price": leg.get("avg_price"),
                }
                for leg in legs
            ],
            "cash": event.get("proceeds"),
            "commission": event.get("commission"),
            # Realised P&L is meaningless on an opening event -- the episode layer
            # reports 0.0 there, and a card reading "realised $0.00" beside an
            # opening credit invites the reader to think the trade made nothing.
            "realized": event.get("realized_pnl") if kind != "open" else None,
            "delta_before": before,
            "delta_after": after,
        })
    return sorted(out, key=lambda row: row["ts"])


def attach(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """Build one replay per trade and point rows at it by key.

    Replays live in a shared ``replays`` map rather than inline on each row,
    because a strangle's two position rows are ONE trade: a copy each would ship
    the underlying series twice and let a reader open two charts of the same
    position and wonder why they differ.

    Only an OPEN lifecycle claims a position row. Close a strike and reopen it
    and both lifecycles share a conid, so claiming by conid alone would aim a
    live position at the chart of a trade that already ended.

    A row no open lifecycle claims gets its own single-strike replay. That is the
    snapshot-only case -- the LEAP, which has no fills anywhere and is otherwise
    the position with the most history and no way to see it.

    Position ROWS gain no key. The page resolves a row to its lifecycle's replay
    through the same open-lifecycle-by-conid map it already builds to group the
    table, so a strangle's two legs open one chart showing both strikes. Writing
    the key onto the row instead would make the row scope-dependent -- the trade
    type filter changes which lifecycles exist, so the same position would carry
    a different key under a different filter, and the open book is the open book
    whatever subset of trades you are looking at.
    """
    replays: dict[str, dict[str, Any]] = {}

    for lifecycle in state["lifecycles"]:
        conids = [str(c) for c in (lifecycle.get("conids") or [])]
        opened, closed = lifecycle.get("opened_at"), lifecycle.get("closed_at")
        key = "lc:" + "-".join(conids) + "@" + str(opened or "")[:10]
        legs = [
            leg
            for event in (lifecycle.get("events") or [])
            for order in (event.get("orders") or [])
            for leg in (order.get("legs") or [])
        ]
        bars = replay_bars(
            conn, str(lifecycle.get("underlying") or ""),
            opened_at=opened, closed_at=closed,
        )
        # Grouped once and shared: the strikes and the marks must follow the same
        # legs, or a segment could be drawn for a position the P&L series was not
        # walking. Same reason `_snapshot_leg` is one constructor.
        replay_legs = _replay_legs(legs)
        # One vol solve behind both, via replay.replay_model. Solving per consumer
        # meant 20 solves for 10 replays and 60% of build_state inside them.
        band, marks = replay_model(
            conn, replay_legs, bars.points, underlying_conid=bars.conid,
            bar_size=bars.bar_size,
        )
        replays[key] = {
            "key": key,
            "underlying": lifecycle.get("underlying"),
            "label": lifecycle.get("label"),
            "bar_size": bars.bar_size,
            "points": [[ts, close] for ts, close in bars.points],
            "strikes": _strikes_of(replay_legs),
            "opened_at": opened,
            "closed_at": closed,
            # Epochs, so the page never parses a timezone. Every journal stamp is
            # US Eastern (clock.epoch_et carries the evidence) and the chart
            # labels the same zone, so fills and bars share one timeline.
            "opened_ts": epoch_et(opened),
            "closed_ts": epoch_et(closed),
            "fills": sorted(
                {ts for ts in (epoch_et(leg.get("first_fill_at")) for leg in legs)
                 if ts is not None}
            ),
            # From the same solve the marks walk, so the band and the P&L series
            # cannot disagree about what the market charged for a contract.
            "band": band,
            "marks": marks,
            "events": _annotations(lifecycle, marks, bars.bar_size),
        }
        lifecycle["replay_key"] = key

    claimed = {
        str(conid): lifecycle["replay_key"]
        for lifecycle in state["lifecycles"]
        if lifecycle.get("status") == "open"
        for conid in (lifecycle.get("conids") or [])
    }
    for row in state["positions"]:
        conid = str(row.get("conid") or "")
        if conid in claimed:
            continue
        key = "pos:" + conid
        opened = row.get("open_date_time")
        bars = replay_bars(
            conn, str(row.get("underlying_symbol") or ""),
            opened_at=opened, closed_at=None,
        )
        # One leg, shared by the strikes and the marks below: built twice they
        # could disagree, and the chart would draw a segment for a position the
        # P&L series was not following.
        legs = [_snapshot_leg(row)]
        band, marks = replay_model(
            conn, legs, bars.points, underlying_conid=bars.conid,
            bar_size=bars.bar_size,
        )
        replays[key] = {
            "key": key,
            "underlying": row.get("underlying_symbol"),
            "label": "Open position",
            "bar_size": bars.bar_size,
            "points": [[ts, close] for ts, close in bars.points],
            # A snapshot row has no fills, so its side comes from the signed
            # position and its window stays unknown -- drawn full width.
            "strikes": _strikes_of(legs),
            "opened_at": opened,
            "closed_at": None,
            # A snapshot row has no fills anywhere -- that is what makes it
            # snapshot-only -- so there is nothing to mark and no entry to mark
            # it from. Empty rather than guessed.
            "opened_ts": epoch_et(opened),
            "closed_ts": None,
            "fills": [],
            "band": band,
            "marks": marks,
            # No fills means no events to annotate. Explicitly empty rather than
            # absent, so the page reads one shape for every replay.
            "events": [],
        }

    state["replays"] = replays
