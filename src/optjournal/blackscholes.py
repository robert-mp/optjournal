"""Black-Scholes pricing and the implied vol backed out of a market price.

A leaf: pure functions over floats, no journal shapes, no I/O, no dependencies.
``math.erf`` supplies the normal CDF, so this needs neither numpy nor scipy and
the project's runtime dependency rule survives intact.

This module exists for ONE purpose: the expected-move band needs a volatility,
and the honest source for it is the option's own market price. The reference
implementation this feature imitates labels its band ``IV ref: VIX`` -- S&P 500
implied vol applied to a gold ETF -- because the underlying is all it has. We
hold the contract's own closes, so we can ask what vol the market was actually
charging for THIS contract instead of borrowing an index's.

Nothing here feeds the accounting layer. A modelled number is a different kind
of claim from a broker-stated one, and the journal's credibility rests on not
mixing them: `stats.py` never reads this module.
"""

from __future__ import annotations

import math

__all__ = ["bs_delta", "bs_price", "expected_move", "implied_vol"]

#: Continuously-compounded risk-free rate, as an ASSUMPTION rather than a
#: measurement. A term structure would be more precise, but the sensitivity is
#: small over the holding periods this journal sees: on a 45-day option a full
#: percentage point of rate error moves the implied vol by well under a vol
#: point. Named and documented rather than buried as a literal 0.04, so the
#: assumption is auditable.
RISK_FREE = 0.04

#: Dividend yield, likewise assumed. Every underlying this journal has traded
#: options on (TSLA, META, GOOG) pays nothing or nearly nothing, so zero is
#: right today. A dividend payer would need this per underlying, and the band
#: would be wrong until it got one.
DIVIDEND_YIELD = 0.0

#: Search bracket for the vol solver. The upper bound is deliberately generous:
#: a 0DTE contract minutes from expiry can genuinely imply several hundred
#: percent, and clamping it would silently report a wrong number instead of
#: reporting none.
_VOL_LO, _VOL_HI = 1e-4, 8.0

#: Bisection steps. Sixty halvings of an 8.0 bracket resolve far beyond the
#: precision of the price that seeded it, so this is cheap certainty rather than
#: a tuning knob.
_ITERATIONS = 60


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf, since the stdlib has no Phi."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float,
    strike: float,
    years: float,
    sigma: float,
    right: str,
    *,
    rate: float = RISK_FREE,
    yield_: float = DIVIDEND_YIELD,
) -> float:
    """Black-Scholes value of one option, per unit of underlying.

    ``right`` is "P" or "C". At or past expiry, and at zero vol, the value is
    intrinsic -- returned rather than raising, because a journal legitimately
    holds expired contracts and asking for their value is not an error.
    """
    is_put = str(right).upper().startswith("P")
    if years <= 0 or sigma <= 0:
        edge = (strike - spot) if is_put else (spot - strike)
        return max(0.0, edge)
    sqrt_t = math.sqrt(years)
    d1 = (
        math.log(spot / strike) + (rate - yield_ + 0.5 * sigma * sigma) * years
    ) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discount, carry = math.exp(-rate * years), math.exp(-yield_ * years)
    if is_put:
        return strike * discount * _norm_cdf(-d2) - spot * carry * _norm_cdf(-d1)
    return spot * carry * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)


def bs_delta(
    spot: float,
    strike: float,
    years: float,
    sigma: float,
    right: str,
    *,
    rate: float = RISK_FREE,
    yield_: float = DIVIDEND_YIELD,
) -> float:
    """Rate of change of value per unit of underlying, per contract unit.

    Signed by the RIGHT only, not by the position: a call is positive and a put
    negative, and multiplying by a signed quantity is the caller's job. A short
    put therefore ends up positive, which is the whole point of selling one.

    At or past expiry delta is the step function -- fully in or fully out -- which
    is what a held-to-expiry contract actually behaves like on its last day.
    """
    is_put = str(right).upper().startswith("P")
    if years <= 0 or sigma <= 0:
        if is_put:
            return -1.0 if spot < strike else 0.0
        return 1.0 if spot > strike else 0.0
    d1 = (
        math.log(spot / strike) + (rate - yield_ + 0.5 * sigma * sigma) * years
    ) / (sigma * math.sqrt(years))
    carry = math.exp(-yield_ * years)
    return -carry * _norm_cdf(-d1) if is_put else carry * _norm_cdf(d1)


def implied_vol(
    price: float,
    spot: float,
    strike: float,
    years: float,
    right: str,
    *,
    rate: float = RISK_FREE,
) -> float | None:
    """The vol that reprices ``price``, or None when no vol can.

    Bisection rather than Newton-Raphson: vega collapses toward zero for deep
    out-of-the-money and near-expiry contracts, which is exactly the population
    a premium seller's journal is full of, and a Newton step divided by a
    near-zero vega diverges. Bisection cannot -- it is slower and it always
    lands.

    None is returned rather than a clamped guess whenever the price sits outside
    what ANY vol can produce: below intrinsic (a stale or crossed quote), above
    the no-arbitrage ceiling, or at an expiry that has passed. A gap in the band
    is honest; a fabricated vol is not.
    """
    if price is None or price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None
    low = bs_price(spot, strike, years, _VOL_LO, right, rate=rate)
    high = bs_price(spot, strike, years, _VOL_HI, right, rate=rate)
    if not (low <= price <= high):
        return None
    lo, hi = _VOL_LO, _VOL_HI
    for _ in range(_ITERATIONS):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, years, mid, right, rate=rate) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def expected_move(spot: float, sigma: float, years: float) -> float | None:
    """One standard deviation of expected move, in price units.

    ``spot * sigma * sqrt(years)`` -- the formula tastylive states for expected
    move, and the same one the reference chart uses. Measured to the position's
    EXPIRY rather than over a fixed horizon, which is why the envelope narrows
    as a trade ages and steps outward when a roll pushes expiry further out.
    """
    if spot <= 0 or sigma is None or sigma <= 0 or years <= 0:
        return None
    return spot * sigma * math.sqrt(years)
