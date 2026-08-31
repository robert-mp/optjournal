"""The 0DTE planner's arithmetic: the expected day's range, before the open.

A leaf, like `vol.py` and `trend.py`: it imports only `math`, takes two numbers
an index feed stated -- the prior session's S&P 500 close and the current VIX --
and returns the ranges a same-day options seller reads before deciding whether
to sell, and how far out. It stores nothing and knows no journal shape.

Nothing here is modelled in the pricing sense (see the pricing quarantine in
`replay.py`): every figure is `prev_close` times a percentage. The percentages
are either FIXED -- the 2% and 3% lines a 0DTE desk quotes as its "normal day"
and "big day" rails -- or VIX read as exactly what it already is.

WHY `sqrt(252)`. VIX is an annualised standard deviation quoted in percent.
Volatility scales with the square root of time, so one trading session is
`VIX / sqrt(252)`, the 252 being the trading days in a year that every desk
uses to de-annualise. The exact count drifts a day or two a year and does not
move the band at two decimals, so it is a constant rather than a calendar
lookup. VIX 16 gives ~1.008% expected for the session, which is the figure a
seller sizes a one-day strangle against. This is a ONE-SIGMA move: about a 68%
chance the close lands inside it, which is the reading, not a guarantee, and
the page says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["TRADING_DAYS", "Band", "ZdtePlan", "plan"]

#: Trading days in a year, for de-annualising VIX. A constant, not a calendar
#: count: see the module docstring on why the drift does not matter here.
TRADING_DAYS = 252


@dataclass(frozen=True, slots=True)
class Band:
    """One expected range around the prior close: a label and its two edges."""

    label: str
    #: The percentage this band is drawn at, so the page can caption it without
    #: re-deriving the number from the edges (which rounding would make lie).
    pct: float
    low: float
    high: float

    def payload(self) -> dict[str, float | str]:
        return {"label": self.label, "pct": self.pct, "low": self.low,
                "high": self.high}


@dataclass(frozen=True, slots=True)
class ZdtePlan:
    """The planner's numbers for one session.

    `vix_daily_move_pct` is the one-sigma expected move as a percent; the `vix`
    band in `bands` is that same figure turned into price rails. The two fixed
    bands (2%, 3%) travel beside it deliberately: a desk quotes fixed rails as
    habit, and seeing the VIX band sit inside or outside them is the read.
    """

    spx_prev_close: float
    vix: float
    vix_daily_move_pct: float
    bands: list[Band]

    def payload(self) -> dict:
        return {
            "spx_prev_close": self.spx_prev_close,
            "vix": self.vix,
            "vix_daily_move_pct": self.vix_daily_move_pct,
            "bands": [b.payload() for b in self.bands],
        }


def _band(label: str, prev_close: float, pct: float) -> Band:
    delta = prev_close * pct / 100.0
    return Band(label=label, pct=pct,
                low=prev_close - delta, high=prev_close + delta)


def plan(spx_prev_close: float, vix: float) -> ZdtePlan | None:
    """The expected range for the session, or None when an input is unusable.

    None rather than a raise or a zero: the planner is a courtesy on top of the
    cohort comparison, and a session before the index feed has been fetched, or
    a feed that returned a non-positive close, is an ABSENCE the page renders as
    "not available yet" -- not a range of zero width, which would read as a
    market that cannot move. A negative or zero VIX is impossible and a
    non-positive prior close is a bad row, so both fail closed the same way.
    """
    if spx_prev_close is None or vix is None:
        return None
    if spx_prev_close <= 0 or vix < 0:
        return None

    vix_daily_move_pct = vix / math.sqrt(TRADING_DAYS)
    bands = [
        _band("VIX 1σ", spx_prev_close, vix_daily_move_pct),
        _band("2%", spx_prev_close, 2.0),
        _band("3%", spx_prev_close, 3.0),
    ]
    return ZdtePlan(
        spx_prev_close=spx_prev_close,
        vix=vix,
        vix_daily_move_pct=vix_daily_move_pct,
        bands=bands,
    )
