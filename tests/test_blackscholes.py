"""Black-Scholes: the identities that must hold, and the refusals that must.

No fixture prices are copied from a textbook. Each test asserts a property the
model itself guarantees -- parity, monotonicity, a round trip -- so a wrong
formula fails rather than a wrong constant.
"""

from __future__ import annotations

import math

import pytest

from optjournal.blackscholes import (
    RISK_FREE,
    bs_price,
    expected_move,
    implied_vol,
)


def test_put_call_parity_holds():
    """C - P == S - K*exp(-rT). The single strongest check on the formula: a
    misplaced discount factor or a swapped d1/d2 breaks it immediately.
    """
    spot, strike, years, vol = 100.0, 95.0, 0.75, 0.35
    call = bs_price(spot, strike, years, vol, "C")
    put = bs_price(spot, strike, years, vol, "P")
    expected = spot - strike * math.exp(-RISK_FREE * years)
    assert call - put == pytest.approx(expected, abs=1e-9)


def test_value_rises_with_volatility():
    """Both rights are long vega. A sign slip inside the normal CDF would still
    produce plausible-looking numbers while inverting this.
    """
    prices = [bs_price(100.0, 100.0, 0.5, vol, "P") for vol in (0.1, 0.2, 0.4, 0.8)]
    assert prices == sorted(prices)
    calls = [bs_price(100.0, 100.0, 0.5, vol, "C") for vol in (0.1, 0.2, 0.4, 0.8)]
    assert calls == sorted(calls)


def test_at_expiry_the_value_is_intrinsic():
    """A journal legitimately holds expired contracts, so asking is not an error."""
    assert bs_price(100.0, 90.0, 0.0, 0.5, "C") == pytest.approx(10.0)
    assert bs_price(100.0, 90.0, 0.0, 0.5, "P") == pytest.approx(0.0)
    assert bs_price(80.0, 90.0, 0.0, 0.5, "P") == pytest.approx(10.0)


def test_implied_vol_round_trips():
    """Price at a known vol, solve it back. Exact to the solver's precision."""
    for vol in (0.12, 0.35, 0.80, 2.5):
        for right in ("P", "C"):
            price = bs_price(100.0, 105.0, 0.4, vol, right)
            assert implied_vol(price, 100.0, 105.0, 0.4, right) == pytest.approx(
                vol, abs=1e-6
            )


def test_implied_vol_solves_deep_out_of_the_money():
    """The population a premium seller's journal is full of, and exactly where
    Newton-Raphson diverges: vega collapses toward zero, so a step divided by it
    runs away. Bisection has to land here.
    """
    price = bs_price(100.0, 55.0, 0.02, 1.4, "P")
    assert implied_vol(price, 100.0, 55.0, 0.02, "P") == pytest.approx(1.4, abs=1e-5)


@pytest.mark.parametrize(
    "price,spot,strike,years,right,why",
    [
        (1.0, 100.0, 130.0, 0.5, "P", "below intrinsic -- a stale or crossed quote"),
        (200.0, 100.0, 100.0, 0.5, "C", "above the no-arbitrage ceiling"),
        (5.0, 100.0, 100.0, 0.0, "P", "expiry has passed"),
        (0.0, 100.0, 100.0, 0.5, "P", "a zero price implies no vol"),
        (-1.0, 100.0, 100.0, 0.5, "P", "a negative price is not a price"),
    ],
)
def test_an_unsolvable_price_returns_none(price, spot, strike, years, right, why):
    """None rather than a clamped guess. A gap in the band is honest; a
    fabricated vol is a number the reader would trust.
    """
    assert implied_vol(price, spot, strike, years, right) is None, why


def test_expected_move_is_spot_times_vol_times_root_time():
    """The formula tastylive states, and the one the reference chart uses."""
    assert expected_move(400.0, 0.5, 0.25) == pytest.approx(400.0 * 0.5 * 0.5)
    assert expected_move(400.0, 0.0, 0.25) is None
    assert expected_move(400.0, 0.5, 0.0) is None


def test_expected_move_narrows_as_expiry_approaches():
    """Why the envelope tapers across a held trade, and steps back out when a
    roll pushes expiry further away.
    """
    moves = [expected_move(100.0, 0.4, years) for years in (0.5, 0.25, 0.1, 0.01)]
    assert moves == sorted(moves, reverse=True)
