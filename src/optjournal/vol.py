"""Realised volatility and the move it implies, from closes alone.

A leaf: imports nothing internal, no database, no journal shapes. Deliberately
NOT `blackscholes` -- and that is the point of the module existing.

WHY REALISED AND NOT IMPLIED. Implied vol is what the market CHARGES for what a
stock might do; realised is what the stock DID. An options seller reads them
differently, and the two are not substitutes.

True IV is not reachable for a symbol this journal does not hold. `replay._vol_series`
solves IV from an OPTION's own daily closes, so it exists only for contracts
already traded -- measured on the real journal: 461 bars for the held LEAP, 14-15
for the traded legs, and none at all for a symbol merely watched. Yahoo's option
chain endpoint answers `{"error":{"code":"Unauthorized"}}` and the v6 path is
gone, so there is no keyless chain to solve against either.

What IS computable from stored closes is this. So the watchlist reports realised
vol, named `realised_vol` in the payload rather than `iv`, because a column headed
IV showing realised vol would be this project's recurring defect: a well-formed
number under a label that does not describe it.

WHY THIS IS NOT A MODELLED NUMBER. `stdev(log returns) * sqrt(252)` is arithmetic
over broker-stated prices -- no option model, no assumption about a distribution's
tails, nothing solved. So the watchlist stays OUTSIDE the modelled-number
quarantine and `test_layering`'s `MAY_MODEL` allowlist stays at two modules. That
allowlist IS the quarantine; growing it for a convenience column would be the
expensive kind of small decision.

The expected move here follows from the same input and inherits the same
character: one standard deviation of what the stock has ALREADY been doing,
scaled to a horizon. It is not the market's expected move, and `expected_move`'s
docstring says so.

WHY THE RANK LIVES HERE TOO. `realised_vol_series` and `rank` are realised vol's
own DISTRIBUTION -- the same statistic read 253 times over a trailing year and
then located inside its own range -- so they belong beside the figure they are
made of rather than in the serializer that happens to want them. They also keep
this module importing only `math`, which is the property `test_layering` enforces
and the reason the watchlist can grow a 0-to-100 gauge without the
modelled-number quarantine growing with it.

`rank` is a MIN-MAX rank, not a percentile, and the difference is the whole point
of the word. In options vocabulary, which is the vocabulary a 0-to-100 gauge
borrows, IV *Rank* is `(now - 52w min) / (52w max - 52w min)` while IV
*Percentile* is the share of days below now. They are different numbers, they are
differently distributed, and only the first one can print its own meaning in a
sentence a caption has room for: "21-session realised vol is 70% of the way from
this year's low, 28.6%, to its high, 79.6%, over 253 windows". Both bounds ride on
the wire beside it (`rv_rank_low`, `rv_rank_high`) so an outlier is visible rather
than hidden inside a rate.
"""

from __future__ import annotations

import math

__all__ = [
    "RANK_BANDS_SOURCE",
    "RANK_MIDPOINT",
    "RANK_MIN_WINDOWS",
    "TRADING_DAYS",
    "expected_move",
    "log_returns",
    "rank",
    "rank_band",
    "realised_vol",
    "realised_vol_series",
]

#: Trading days in a year. The convention for annualising a daily vol; 365 would
#: annualise weekends the market was closed for.
TRADING_DAYS = 252

#: Fewest returns worth a standard deviation. Six closes give five returns, and a
#: stdev of fewer than that is noise presented as a measurement -- a watchlist row
#: for a symbol added yesterday should say "not enough history", not "8%".
MIN_RETURNS = 5

#: Fewest windows worth a RANK. Below this, `rank` returns None.
#:
#: The denominator of a min-max rank is a maximum minus a minimum, and each of
#: those is a SINGLE observation -- unlike a mean or a standard deviation, neither
#: gains precision from the other 252 windows. So the whole figure rests on two
#: readings being plausible bounds, and over a handful of windows they are not: the
#: chance that the NEWEST of n exchangeable readings is one of the two extremes is
#: 2/n, so a 10-window series pegs the gauge at 0.0 or 100.0 about one time in five
#: against under 2% at 120. And 2/n is the optimistic bound here, because
#: consecutive windows share 20 of their 21 returns, so a reading near an extreme
#: stays near it for weeks. A pegged extreme reads as the strongest signal on the
#: page, which is exactly the failure `trend.MIN_SETTLED` guards in the oscillator.
#:
#: 120 rather than a number of its own, and the shared value is the point: it is
#: `trend.MIN_SETTLED`, so the tab's four derived figures appear TOGETHER on a
#: symbol whose history is filling in, rather than lighting up in stages and
#: inviting a reader to compare a settled figure against a warm-up one. The two
#: constants are not imported into each other (both modules are leaves and hold
#: nothing), so this sentence is what ties them; a change to either wants the
#: other read.
RANK_MIN_WINDOWS = 120

#: The cut point on a 0-to-100 rank: the middle of the symbol's OWN observed
#: range, so it needs no external calibration and no import. Above it means nearer
#: this year's high than its low, which is a statement the arithmetic supports.
#:
#: IV rank's conventional 30 is deliberately NOT carried over -- see
#: `RANK_BANDS_SOURCE` for why -- and this is not the median either, which is the
#: thing a reader is most likely to assume it is. MEASURED over 253 trailing
#: 21-session windows of real fetched closes, the share of windows sitting above
#: this midpoint is 8.3% (PLTR), 15.4% (TSLA), 20.9% (DELL), 39.1% (SPY), 41.9%
#: (GOOG) and 55.7% (NVDA): realised vol spends most of its time near the quiet
#: end of its own year and spikes, so the midpoint of the RANGE is nowhere near
#: the middle of the population. A percentile would have been ~50% on every one of
#: those six by construction, which is exactly why the two statistics cannot share
#: a threshold, or a name.
RANK_MIDPOINT = 50.0

#: Where the cut point came from and what it does not claim, as one string so the
#: page's caption, the CLI's footer and the constant cannot drift apart -- the
#: treatment `trend.BANDS_SOURCE` gets for the same reason.
#:
#: The refusal is stated in the sentence rather than left to a comment because the
#: number it refuses is the one every reader of the mockup arrives with: IV rank's
#: 30 is calibrated on IMPLIED vol across a population of symbols, and this figure
#: is one symbol's own realised vol against its own year. Carrying 30 over would be
#: this project's recurring defect one level up, in a threshold rather than in a
#: value.
RANK_BANDS_SOURCE = (
    "the middle of this symbol's own year of realised vol; IV rank's "
    "conventional 30 is calibrated on implied vol across a different population "
    "and is deliberately not carried over"
)


def log_returns(closes: list[float]) -> list[float]:
    """Log returns from closes, NEWEST FIRST in, oldest-to-newest out.

    Newest first because that is the order a `ORDER BY ts DESC LIMIT n` gives,
    which is how the caller gets the most recent window without reading the whole
    series. Reversing here rather than at the call site keeps the convention in
    one place.

    Log rather than simple returns because they are additive over time and
    symmetric in sign, which is what makes annualising by `sqrt(252)` valid.

    A non-positive close is skipped rather than crashing: a bar's close is
    nullable by design (a quiet strike has no print on roughly one session in
    five) and `log(0)` is not a number this can report.
    """
    usable = [c for c in reversed(closes) if c is not None and c > 0]
    return [
        math.log(usable[i + 1] / usable[i])
        for i in range(len(usable) - 1)
    ]


def realised_vol(closes: list[float], *, annualise: bool = True) -> float | None:
    """Annualised realised volatility as a PERCENTAGE, or None if too little data.

    None rather than 0.0 for a short series: zero vol is a claim about a stock
    that never moved, and a symbol added yesterday has not made that claim. The
    two must stay distinguishable or the watchlist would report a flat row for a
    new one.

    Sample standard deviation (n-1), which is the right estimator for a sample of
    a longer history -- the population form would understate a 20-day window.
    """
    rets = log_returns(closes)
    if len(rets) < MIN_RETURNS:
        return None
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    daily = math.sqrt(variance)
    return daily * math.sqrt(TRADING_DAYS) * 100 if annualise else daily * 100


def realised_vol_series(
    closes: list[float], *, window: int = 21, history: int = 253
) -> list[float]:
    """A trailing year of realised vol readings, NEWEST FIRST, from one series.

    Input newest first (the module's convention, see `log_returns`) and output the
    same way, so `series[0]` is today's reading and the caller never has to know
    which end it is holding. Two functions over one list disagreeing about its
    direction is a silent wrong-answer defect, and this one would be invisible: a
    reversed series has the same low and the same high, so only the RANK moves --
    it would report where the year STARTED as where it is now.

    `window` is the vol window and defaults to the 21 sessions the watchlist's own
    `realised_vol` column uses, so the reading being ranked is the reading on
    screen rather than a second figure that resembles it. `history` is how many
    windows to compute: 253 is a trailing year of them (252 sessions plus the one
    that closes the newest window), which is the span the word "1y" on the gauge
    claims.

    Windows that cannot be measured are DROPPED rather than filled, so the length
    of the result is the honest count of readings and is what the surfaces print to
    explain a dash ("94 of 120 windows"). A window can fail to measure even on a
    long series: `realised_vol` needs `MIN_RETURNS` usable returns, and a bar's
    close is nullable by design, so a stretch of holes shortens the count instead of
    contributing a zero.
    """
    if window < 2 or history < 1:
        return []
    out: list[float] = []
    for offset in range(min(history, max(0, len(closes) - window + 1))):
        value = realised_vol(closes[offset:offset + window])
        if value is not None:
            out.append(value)
    return out


def rank(series: list[float]) -> float | None:
    """Where the newest reading sits between the lowest and the highest, 0 to 100.

    `(now - lo) / (hi - lo) * 100` over `series` NEWEST FIRST, which is what the
    word rank means in the vocabulary this figure borrows. See the module docstring
    for why it is not a percentile.

    None below `RANK_MIN_WINDOWS`, because the denominator is one observation minus
    one observation and a handful of windows cannot make either a plausible bound.

    A DEGENERATE YEAR RETURNS None, NEVER 50.0. If `hi == lo` the position is
    undefined -- 0/0 -- and 50.0 would be the arithmetic's worst possible lie: it
    would render mid-gauge, in the middle of a range that does not exist, on the
    one symbol whose vol genuinely never moved. That is the same line `realised_vol`
    draws between "no data" and "no movement", except here even the flat case has no
    answer to give. `mutate.py`'s `rank-degenerate` is this branch inverted.
    """
    if len(series) < RANK_MIN_WINDOWS:
        return None
    low, high, now = min(series), max(series), series[0]
    if high == low:
        return None
    return (now - low) / (high - low) * 100


def rank_band(value: float | None) -> str | None:
    """Which side of `RANK_MIDPOINT` a rank sits on: "upper", "lower", or None.

    Computed here rather than at each surface for `trend.bucket`'s reason: the cut
    point then lives once, beside the constant and the sentence that justify it, so
    a chip label and a gauge tick cannot end up disagreeing about where 50 is.

    The edge belongs to "lower", exactly as the two filter chips are worded
    (`> 50` and `<= 50`), so the pair partitions the range and a rank landing on
    the midpoint is in one of them rather than in neither. There is no third band:
    a "mid" here would be a range this figure does not define, unlike the
    oscillator's three, whose middle is the whole space between two named bounds.

    None in, None out -- a symbol with no rank is not on the low side of anything,
    and a filter that quietly banded it would put an unmeasured row inside a set
    the reader believes was measured.
    """
    if value is None or math.isnan(value):
        return None
    return "upper" if value > RANK_MIDPOINT else "lower"


def expected_move(
    last: float | None, vol_pct: float | None, *, days: int
) -> float | None:
    """One standard deviation of movement over `days`, in price terms.

    From REALISED vol, so this is what the stock has already been doing scaled to
    a horizon -- not the market's expected move, which would need an option price.
    A reader comparing this against a straddle is comparing two different things,
    which is why the payload key says `realised`.

    `days` in trading days, matching the annualisation.
    """
    if last is None or vol_pct is None or last <= 0 or days <= 0:
        return None
    return last * (vol_pct / 100.0) * math.sqrt(days / TRADING_DAYS)
