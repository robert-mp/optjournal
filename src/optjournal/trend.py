"""B-Xtrender: an RSI of an EMA difference, and the window it needs to mean anything.

A leaf: imports nothing internal, no database, no journal shapes, exactly like
`vol.py`. It is a separate module from `vol.py` rather than two more functions in
it because `vol.py` is titled for realised volatility and its whole body is one
argument about realised against implied -- an oscillator in there would make the
module name and the docstring a lie -- and because `MIN_RETURNS = 5` is not this
indicator's floor. This one's is 35 by arithmetic and about 120 by honesty, which
is the same discipline applied to a different number.

ATTRIBUTION. The algorithm is Bharat Jhunjhunwala's, published in the IFTA
Journal (2019); the implementation everyone actually reads is QuantTherapy's
TradingView (Pine) port. Both arms, both seedings and every default here are that
implementation's, because the whole meaning of the figure is "what the published
indicator says about this symbol":

    short arm   rsi(ema(close, 5) - ema(close, 20), 15) - 50
    long arm    rsi(ema(close, 20), 15) - 50

THE RSI READS THE EMA DIFFERENCE, NOT THE PRICE. That is the one step every wrong
copy drops. A popular Python repository computes `ema(rsi(close, 5) - 50, 3)`
instead: an RSI of price, smoothed, landing in the same [-50, +50] range, signed
plausibly, and a DIFFERENT indicator. It is exactly the defect shape this project
keeps hunting, a well-formed number under a label that does not describe it, so
`tests/test_trend.py` pins a series on which the two disagree in SIGN rather than
merely in magnitude, and a hand-computed vector rather than a shape.

WHY THIS IS NOT A MODELLED NUMBER. Every value here is arithmetic over
broker-stated closes: two exponential means, their difference, and Wilder's
average of up moves against down moves. No option model, no assumed
distribution, nothing solved. So the watchlist stays OUTSIDE the modelled-number
quarantine and `tests/test_layering.py`'s `MAY_MODEL` allowlist stays at two
modules -- which is what `vol.py` names as the expensive kind of small decision to
grow, and this module is one of the two additions that could have been used to
argue for growing it.

WHAT THE NUMBER IS NOT A STATEMENT ABOUT. The sign, and the bands below, describe
the SHAPE OF RECENT CLOSES. They say nothing about whether a share is cheaper or
dearer than it should be, which is why `bucket` returns "low", "mid" and "high"
and why the RSI convention's own valuation words survive only inside
`BANDS_SOURCE`, attributed. The precedent is `market_events.impact`: the feed's
judgement, stored verbatim and printed with whose judgement it is beside it.

INPUT ORDER. NEWEST FIRST, the same convention `vol.log_returns` documents,
because that is what `ORDER BY ts DESC LIMIT n` gives and because the watchlist
panel hands the same list of closes to both leaves. Two functions over one list
disagreeing about its direction is the trap worth spending a `reversed()` to
avoid: an EMA read backwards is still a well-formed number, still in range, and
carries the wrong sign -- and the sign is this indicator's primary published
state. `test_reading_the_closes_backwards_flips_the_sign` is what would catch it.
"""

from __future__ import annotations

import math

__all__ = [
    "BANDS_SOURCE",
    "BX_OVERBOUGHT",
    "BX_OVERSOLD",
    "CENTRE",
    "LONG_L1",
    "LONG_L2",
    "MIN_CLOSES",
    "MIN_SETTLED",
    "PARAMS_CAPTION",
    "SHORT_L1",
    "SHORT_L2",
    "SHORT_L3",
    "bucket",
    "bxtrender_long",
    "bxtrender_short",
    "ema",
    "rsi",
]

#: The short arm's three periods: fast EMA, slow EMA, RSI. QuantTherapy's Pine
#: defaults, unchanged. B-Xtrender at 5/20/15 and B-Xtrender at other settings
#: are different numbers, so a column headed BXTRENDER showing +22.4 with no
#: periods stated is a figure its reader cannot reproduce -- which is why
#: `PARAMS_CAPTION` exists and is generated from these three names.
SHORT_L1 = 5
SHORT_L2 = 20
SHORT_L3 = 15

#: The long arm's two periods: the same slow EMA, read by the same RSI. It shares
#: `SHORT_L2`'s value at 20 and is spelled separately anyway, because the two are
#: independent inputs in the published indicator and collapsing them would make a
#: retune of one silently retune the other.
LONG_L1 = 20
LONG_L2 = 15

#: An RSI runs 0 to 100; the indicator subtracts this so its zero is the RSI's
#: midpoint and the sign carries the state. It also bounds the output to
#: [-50, +50] BY CONSTRUCTION, which is what lets the meter in the panel carry a
#: fixed scale instead of auto-scaling to whatever rows are visible.
CENTRE = 50

#: Where the arithmetic first produces a number at all, and therefore the floor
#: below which there is nothing to gate. An SMA-seeded EMA(20) over n closes
#: yields n-19 values; Wilder's RSI(15) needs 16 inputs to produce its first; so
#: both arms need 20 + 15 = 35 closes for one value, and 35 is not a choice.
#:
#: It is NOT the gate. `MIN_SETTLED` is. The two are separate constants because
#: they answer different questions -- "can this be computed" and "is the answer
#: worth showing" -- and a single number would quietly answer the second with the
#: first, which is how a pegged extreme reaches a reader.
MIN_CLOSES = 35

#: The gate the functions actually enforce, in the arm's own unit (sessions for a
#: daily series, ISO weeks for a weekly one). Below this they return None and the
#: surface renders an em dash titled with the count held.
#:
#: MEASURED, over 755 fetched daily closes each for TSLA, GOOG, PLTR and SPY,
#: which is why 35 is not the answer:
#:
#: * at 45 closes the newest short-arm value is 2.2 to 7.5 points away from what
#:   the SAME session reads once the series is long (SPY 2.22, PLTR 2.78, GOOG
#:   5.28, TSLA 7.50), against a range 100 points wide and a band at 20;
#: * at 44 closes TSLA's short arm reads -1.35 where the settled value is +8.24,
#:   so the SIGN flips, and the sign is the indicator's primary published state;
#: * from 35 closes through 43, TSLA's long arm reports exactly -50.0000, the
#:   pegged extreme, which renders as the strongest signal anything on the page
#:   can show. It is pegged because a 16-value EMA(20) is still falling out of its
#:   own seed, so every change is a down move, every up move is zero, and the RSI
#:   is 0 with nothing wrong with the arithmetic.
#: * by 120 closes the same four symbols are within 0.01 to 0.05 points of their
#:   settled values, which is under the last digit anything prints.
#:
#: This is `vol.MIN_RETURNS`' discipline per indicator: a warm-up value is noise
#: presented as a measurement. 120 is where the warm-up error has fallen into the
#: last decimal (see `ema`'s convergence measurement), and the realised-vol rank
#: the same panel will carry is deliberately sized on the same 120, so the tab's
#: derived figures appear together rather than lighting up in stages.
MIN_SETTLED = 120

#: The bucket edges, and the only imported numbers in this module.
#:
#: The published indicator states no oversold or overbought level AT ALL -- no
#: hline, no level input, only the sign crossed with rising-or-falling -- so any
#: band here comes from somewhere else and the somewhere has to be named. These
#: are Wilder's RSI 70/30 re-centred by the indicator's own -50; `BANDS_SOURCE`
#: says so on screen.
#:
#: MEASURED over TSLA's 721 daily short-arm values (from 755 fetched closes):
#: +/-20 cuts the outer 12.1% and 9.2%, so each band names a genuinely unusual
#: session. The same band over the LONG arm cuts 29.1% and 37.0% of the same
#: sessions, and over four symbols the long arm sits at or above +20 on 37.0%
#: (TSLA), 51.5% (PLTR), 56.2% (GOOG) and 67.4% (SPY) of them. That is why one
#: band set cannot serve both arms and why the panel states which arm it is
#: banding: a bucket holding half of all sessions has stopped meaning anything.
#: Only the daily short arm is banded, and the shares wobble a point or two with
#: the window, which is why the series each figure came from is stated.
BX_OVERSOLD = -20.0
BX_OVERBOUGHT = 20.0

#: Where the band came from and what its words do and do not claim, as one string
#: so the caption on the page and the constants above cannot drift apart.
#:
#: The numbers are interpolated rather than typed for the same reason: the band is
#: not an independent choice, it IS the RSI convention re-centred, so 70/30 and
#: +/-20 are one decision spelled twice. If a later band is ever chosen on its own
#: evidence instead of imported, this sentence must be rewritten rather than
#: reformatted, because it would no longer be Wilder's.
BANDS_SOURCE = (
    f"Wilder's RSI {CENTRE + BX_OVERBOUGHT:.0f}/{CENTRE + BX_OVERSOLD:.0f}, "
    f"re-centred by the indicator's own -{CENTRE}; 'oversold' and 'overbought' "
    "are that convention's words for below and above the band, not this "
    "journal's view of what a share is worth"
)

#: The caption a tile prints, generated from the periods above so a retune cannot
#: leave the screen claiming the old ones. Names the author for the same reason
#: `impact_source` names the feed: a reader who cannot reproduce a figure has to
#: be able to look it up.
PARAMS_CAPTION = (
    f"B-Xtrender, short arm {SHORT_L1}/{SHORT_L2}/{SHORT_L3}, "
    f"centred on {CENTRE}; Jhunjhunwala 2019"
)


def _chronological(closes: list[float | None]) -> list[float]:
    """Usable closes, OLDEST FIRST, from a newest-first list.

    Non-positive and missing closes are dropped rather than crashing the
    recursion, following `vol.log_returns`: a bar's close is nullable by design
    (a quiet strike has no print on roughly one session in five). Dropping
    shortens the window instead of manufacturing a level, and the shortened
    window is what the gate then counts -- so a symbol whose stored closes are
    mostly holes reports a dash rather than an indicator over a punctured series.
    """
    return [c for c in reversed(closes) if c is not None and c > 0]


def ema(values: list[float], length: int) -> list[float]:
    """Exponential moving average, OLDEST FIRST in and out, SMA-seeded.

    `n` values give `n - length + 1` results: the series begins where the seed
    completes, and carries no values before it. Absent rather than back-filled,
    because a smoothed average of fewer bars than its own period is a different
    statistic wearing the same name.

    SEEDING IS NOT TASTE, and this is the decision most re-implementations get
    wrong. TradingView's `ta.ema` starts from an SMA of the first `length` values;
    pandas' `ewm(adjust=False)` starts from the first value alone. Both converge,
    and near the start they disagree by a lot: MEASURED over TSLA's 755 fetched
    daily closes, the short arm over a 44-close window reads **-1.3487** with both
    recursions SMA-seeded against **+9.4472** with both seeded from their first
    observation, a 10.80 point gap that flips the bucket AND the sign; the same
    comparison at 120 closes is 0.0104, at 200 closes 0.0003, and over the whole
    series 0.0000.

    Since the number's whole meaning is "what the published indicator says", the
    published seeding is the correct one and the other is a bug that looks like a
    number. That convergence measurement is also where `MIN_SETTLED` comes from.
    """
    if length < 1:
        raise ValueError(f"ema length must be positive, got {length}")
    if len(values) < length:
        return []
    alpha = 2.0 / (length + 1)
    current = sum(values[:length]) / length
    out = [current]
    for value in values[length:]:
        current = alpha * value + (1.0 - alpha) * current
        out.append(current)
    return out


def _rsi_of(avg_gain: float, avg_loss: float) -> float:
    """One RSI reading from Wilder's two averages.

    Written as `100 * gain / (gain + loss)` rather than as `100 - 100/(1+rs)` so
    the two one-sided cases need no special case: nothing but down moves gives
    exactly 0, and nothing but up moves exactly 100 where the ratio form would
    divide by a zero average loss. Only a perfectly flat input reaches the branch
    below, and it answers `CENTRE` -- no movement, which is a real measurement and
    distinct from the None that means no history. `vol.py` draws the same line when
    it lets a flat series report a genuine zero vol.
    """
    total = avg_gain + avg_loss
    if total == 0.0:
        return float(CENTRE)
    return 100.0 * avg_gain / total


def rsi(values: list[float], length: int) -> list[float]:
    """Wilder's RSI, OLDEST FIRST in and out, over whatever series is passed.

    `n` values give `n - length` results, because the first result needs `length`
    CHANGES and so `length + 1` values.

    Wilder's smoothing (`ta.rma` in Pine, not `ta.ema`) is a different constant
    from an EMA of the same period: `1/length` against `2/(length+1)`. Using the
    EMA form here would produce a plausible oscillator that is not an RSI, and
    nothing on screen would say which one it was.

    Seeded with an SMA of the first `length` gains and losses, for the reason in
    `ema`: it is what the published implementation does, and near the start of a
    series the choice is worth more than ten points of the output.

    This reads whatever it is given. The short arm gives it an EMA DIFFERENCE and
    the long arm an EMA -- neither gives it prices, which is the step the wrong
    copies drop.
    """
    if length < 1:
        raise ValueError(f"rsi length must be positive, got {length}")
    if len(values) < length + 1:
        return []
    changes = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    gains = [c if c > 0.0 else 0.0 for c in changes]
    losses = [-c if c < 0.0 else 0.0 for c in changes]

    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    out = [_rsi_of(avg_gain, avg_loss)]
    for i in range(length, len(changes)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        out.append(_rsi_of(avg_gain, avg_loss))
    return out


def bxtrender_short(closes: list[float | None]) -> float | None:
    """The short arm's newest value, or None below `MIN_SETTLED`.

    `rsi(ema(c, 5) - ema(c, 20), 15) - 50`. The two EMAs are aligned on the SLOW
    one before subtracting: the fast series begins 15 values earlier, and in Pine
    those bars have `na` for the slow leg, so their difference does not exist. The
    alignment is the fiddly part and it is load-bearing -- pairing the two series
    from their own starts instead would subtract sessions that are 15 apart,
    producing a number in the right range for a stock nobody traded.

    None rather than a warm-up value: see `MIN_SETTLED` for what those look like.
    """
    usable = _chronological(closes)
    if len(usable) < MIN_SETTLED:
        return None
    fast = ema(usable, SHORT_L1)
    slow = ema(usable, SHORT_L2)
    offset = len(fast) - len(slow)
    difference = [f - s for f, s in zip(fast[offset:], slow, strict=True)]
    series = rsi(difference, SHORT_L3)
    return series[-1] - CENTRE if series else None


def bxtrender_long(closes: list[float | None]) -> float | None:
    """The long arm's newest value, or None below `MIN_SETTLED`.

    `rsi(ema(c, 20), 15) - 50`: the RSI reads the slow EMA itself rather than a
    difference of two, so this arm is a statement about the trend of the trend.

    It shares the floor with the short arm (both are 20 + 15 = 35 closes), and it
    is the arm that misbehaves loudest below it: MEASURED on TSLA's real closes it
    reports exactly -50.0000 from 35 closes through 43, and ANY strictly falling
    series pegs it there the moment it can be computed at all. That is why the gate
    is a shared constant rather than one guess per arm.
    """
    usable = _chronological(closes)
    if len(usable) < MIN_SETTLED:
        return None
    series = rsi(ema(usable, LONG_L1), LONG_L2)
    return series[-1] - CENTRE if series else None


def bucket(value: float | None) -> str | None:
    """Which band `value` sits in: "low", "mid", "high", or None for no value.

    The keys carry no valuation vocabulary ON PURPOSE. "Oversold" and
    "overbought" are claims that a share is cheaper or dearer than it should be;
    an RSI of an EMA difference re-centred by 50 is a statement about the shape of
    recent closes. Those two words survive in `BANDS_SOURCE`, attributed to the
    convention they belong to, and nowhere else -- the same treatment
    `market_events.impact` gets.

    An edge belongs to the OUTER bucket, so the three buckets partition the whole
    range and no value can fall through: a reading of exactly `BX_OVERSOLD` is
    "low" and exactly `BX_OVERBOUGHT` is "high". With exclusive edges a row landing
    on the band would belong to no bucket and drop out of every filter, which shows
    up as a missing row rather than as an error.

    None in, None out. A symbol with no value is not in the middle band; a filter
    that quietly bucketed it as "mid" would put a row with no measurement inside a
    set the reader believes was measured.
    """
    if value is None or math.isnan(value):
        return None
    if value <= BX_OVERSOLD:
        return "low"
    if value >= BX_OVERBOUGHT:
        return "high"
    return "mid"
