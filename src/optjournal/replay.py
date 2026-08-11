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

THE ONE THING THIS STILL NEEDS FROM STORAGE is `bars.close_series`: a vol solve
reads an option's own daily closes and the underlying's, so pricing depends on
the layer that keeps them. That direction is correct and one-way -- storage never
reaches back for a model -- and it is why this is a layering split rather than a
lift: the interface `web.py` already had (`replay_model`, `delta_around`) is
unchanged, and only the address moved.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from optjournal.bars import close_series
from optjournal.blackscholes import bs_delta, bs_price, expected_move, implied_vol
from optjournal.clock import et_day, expiry_epoch

__all__ = [
    "BandContract",
    "ReplayLeg",
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



def _spot_at(points: list[tuple[int, float]], ts: int) -> float | None:
    """Underlying price at ``ts``, interpolated across the bar it falls in.

    Used to place a value on the chart's own grid, NOT to pair with an option
    price -- see expected_move_band for why that pairing has to be daily-to-daily.
    """
    if not points or ts < points[0][0] or ts > points[-1][0]:
        return None
    for index in range(1, len(points)):
        if points[index][0] >= ts:
            prev_ts, prev_px = points[index - 1]
            next_ts, next_px = points[index]
            width = (next_ts - prev_ts) or 1
            return prev_px + ((ts - prev_ts) / width) * (next_px - prev_px)
    return points[-1][1]


def _vol_series(
    conn: sqlite3.Connection,
    contracts: list[BandContract],
    points: list[tuple[int, float]],
    underlying_conid: str | None,
) -> dict[str, list[tuple[int, float]]]:
    """Implied vol per leg per session, from each option's OWN daily closes.

    The reference implementation this feature imitates labels its band
    "IV ref: VIX" -- S&P 500 implied vol applied to a gold ETF -- because the
    underlying is all it has. We hold the contract's prices, so we can ask what
    the market charged for THIS contract.

    An option's daily close is paired with the underlying's daily close for the
    same session, matched on the ET trading DAY (see et_day for why the raw
    timestamps cannot be compared). Pairing against the CHART series instead
    looks equivalent and is not: a chart may be hourly, and an option's daily
    stamp then falls in the overnight gap between two hourly bars, so
    interpolating there prices the option against a spot the market never showed
    and books the discrepancy as volatility.

    Per LEG rather than averaged, because pricing a leg wants its own vol; the
    band averages afterwards, since expected move is a property of the underlying
    and a strangle's two legs are two observations of it.
    """
    if not points or not contracts or not underlying_conid:
        return {}
    low, high = points[0][0], points[-1][0]
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
            years = (expiry - stamp) / (365.0 * 86400)
            vol = implied_vol(close, spot, contract.strike, years, contract.right)
            if vol is not None:
                found.append((stamp, vol))
        found.extend(_anchor_vols(contract, points, expiry))
        if found:
            out[contract.conid] = sorted(found)
    return out


def _anchor_vols(
    contract: BandContract, points: list[tuple[int, float]], expiry: int
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

    Spot comes from _spot_at -- interpolated WITHIN the bar the fill falls in --
    and this is the one place that pairing is correct, where pairing a daily
    close that way is not. A daily option bar is stamped 04:00Z, which on an
    hourly chart lands in the overnight gap, so interpolating there prices the
    option against a spot the market never showed. A fill carries a genuine
    intraday instant that the hourly series brackets, so the interpolation is
    between two prices that really did surround it.

    Only fills inside the chart window are used, since a spot outside it cannot
    be read; anything the solve rejects (a price below intrinsic, a fill after
    expiry) is dropped rather than defaulted, so a gap stays a gap.
    """
    if not points:
        return []
    low, high = points[0][0], points[-1][0]
    found: list[tuple[int, float]] = []
    for stamp, price in contract.anchors:
        if stamp < low or stamp > high:
            continue
        spot = _spot_at(points, stamp)
        if spot is None:
            continue
        years = (expiry - stamp) / (365.0 * 86400)
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


def expected_move_band(
    conn: sqlite3.Connection,
    contracts: list[BandContract],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
    vols: dict[str, list[tuple[int, float]]] | None = None,
) -> list[list[float]]:
    """A one-standard-deviation envelope, per underlying bar.

    Spot and time-to-expiry are per BAR, so the envelope moves and tapers hourly;
    the vol input steps daily, because the source serves no intraday option
    history and there is nothing finer to solve against.

    The horizon is the NEAREST expiry among the legs, which is the one that
    dominates the risk. That is also what makes the envelope narrow as a trade
    ages and step outward when a roll pushes expiry further out.
    """
    # `is None`, not `vols or ...`: an empty dict is the legitimate answer when
    # nothing solved, and the truthiness spelling would re-run the whole solve
    # for exactly that case.
    if vols is None:
        vols = _vol_series(conn, contracts, points, underlying_conid)
    if not vols:
        return []
    expiries = [
        expiry
        for expiry in (expiry_epoch(c.expiry) for c in contracts if c.conid in vols)
        if expiry is not None
    ]
    if not expiries:
        return []
    horizon = min(expiries)
    band: list[list[float]] = []
    for stamp, spot in points:
        observed = [
            vol
            for vol in (_held_forward(series, stamp) for series in vols.values())
            if vol is not None
        ]
        if not observed:
            continue
        average = sum(observed) / len(observed)
        move = expected_move(spot, average, (horizon - stamp) / (365.0 * 86400))
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
    vols: dict[str, list[tuple[int, float]]] | None = None,
) -> list[list[float | None]]:
    """Modelled P&L and effective delta per bar: ``[ts, pnl, delta]``.

    P&L is cash flow to date plus the mark-to-market of whatever is still open --
    the standard formulation, and the reason it behaves correctly through a
    partial close or a roll rather than only for a clean open-then-close. A
    closing fill moves value out of the open leg and into realised cash, so once
    a position is flat the figure FREEZES at what the trade made instead of
    continuing to mark a contract nobody holds.

    The PRICING is exact: repricing an option's own close at the vol solved from
    it returns that close to 1e-14, so nothing is approximated in the model
    itself. The chart's mark is not a restatement of a broker figure though --
    it applies that vol to the BAR's spot and the BAR's time to expiry, and the
    source stamps an option close at midnight ET while the underlying bar for the
    same session carries the session's own time. So the series TRACKS the broker's
    mark rather than matching it: the LEAP lands $75 from the snapshot's
    unrealised on a $3,000 position, which is that drift plus the snapshot being a
    day older than the latest bar. Close enough to trust the shape, not close
    enough to quote as the position's value -- which is why the card beside the
    chart still states the broker's figure.

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
        vols = _vol_series(conn, band_contracts(legs), points, underlying_conid)
    if not vols:
        return []
    marks: list[list[float | None]] = []
    for stamp, spot in points:
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
                if fill_at > stamp:
                    break
                quantity += delta_qty
                cash -= delta_qty * price * leg.multiplier
            vol = _held_forward(series, stamp)
            if vol is None:
                continue
            years = (expiry - stamp) / (365.0 * 86400)
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
) -> tuple[list[list[float]], list[list[float]]]:
    """The band and the modelled marks for one replay, as ``(band, marks)``.

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
    contracts = band_contracts(legs)
    vols = _vol_series(conn, contracts, points, underlying_conid)
    band = expected_move_band(
        conn, contracts, points, underlying_conid=underlying_conid, vols=vols
    )
    marks = modelled_marks(
        conn, legs, points, underlying_conid=underlying_conid, vols=vols
    )
    return band, marks


def delta_around(
    marks: list[list[float | None]], stamp: int
) -> tuple[float | None, float | None]:
    """Effective delta immediately before and after an event, as ``(before, after)``.

    The pair a roll is judged by: the reference implementation this feature
    imitates puts "Eff Delta 17 -> 10" on its roll card, because the number that
    matters about a roll is how much exposure it removed.

    ``before`` is the last mark STRICTLY before the event and ``after`` the first
    at or after it, so an opening fill correctly reports ``None -> 0.56``: there
    was no position to have a delta, and inventing 0.0 there would read as
    "we were delta-neutral" rather than "we were not in the trade".

    Either side may be None at a window edge, when no bar in the window had a
    solvable vol, or because the neighbouring bar HELD nothing -- `modelled_marks`
    reports delta as None off-position, and this carries that through rather than
    flattening it to 0.0. A closing event therefore reads ``0.32 -> None``, which
    is the honest shape: it had exposure, and now there is none to have.
    """
    before: float | None = None
    after: float | None = None
    for row in marks:
        if row[0] < stamp:
            before = row[2]
        else:
            after = row[2]
            break
    return before, after

