"""Realised volatility and the move it implies, from closes alone.

A leaf: imports nothing internal, no database, no journal shapes. Deliberately
NOT `blackscholes` -- and that is the point of the module existing.

WHY REALISED AND NOT IMPLIED. Implied vol is what the market CHARGES for what a
stock might do; realised is what the stock DID. An options seller reads them
differently, and the two are not substitutes.

True IV is not reachable for a symbol this journal does not hold. `bars._vol_series`
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
"""

from __future__ import annotations

import math

__all__ = [
    "TRADING_DAYS",
    "expected_move",
    "log_returns",
    "realised_vol",
]

#: Trading days in a year. The convention for annualising a daily vol; 365 would
#: annualise weekends the market was closed for.
TRADING_DAYS = 252

#: Fewest returns worth a standard deviation. Six closes give five returns, and a
#: stdev of fewer than that is noise presented as a measurement -- a watchlist row
#: for a symbol added yesterday should say "not enough history", not "8%".
MIN_RETURNS = 5


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
