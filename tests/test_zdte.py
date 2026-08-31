"""Tests for the 0DTE planner's arithmetic.

Literals throughout, no database and no fixture: `zdte.py` is a leaf, and the
whole point of it being one is that its numbers can be checked against a hand
computation rather than against a rendered page.
"""

from __future__ import annotations

import math

import pytest

from optjournal import zdte


def test_the_vix_band_uses_the_desk_divisor_of_sixteen():
    """VIX de-annualises by 16, the desk convention, not by sqrt(252) = 15.87.

    The one figure a reader might expect to be wrong: VIX is an annual number and
    the band is a daily one, so the conversion is the claim -- and WHICH
    de-annualisation is a second claim on top of it. Pinned against the reference
    implementation's own published figures, because a number 0.8% off is invisible
    in a band and still the wrong number beside a platform quoting the other one.
    """
    p = zdte.plan(spx_prev_close=5000.0, vix=16.0)
    assert p is not None
    assert p.vix_daily_move_pct == pytest.approx(1.0), "VIX 16 is a 1% session"
    assert p.vix_daily_move_pct != pytest.approx(16.0 / math.sqrt(252)), (
        "sqrt(252) is the textbook divisor and NOT the one in use here"
    )


@pytest.mark.parametrize("spx, vix, want_pct, want_points, want_low, want_high", [
    # Both rows read from the reference implementation's own stored snapshots, so
    # this is a cross-check against a second program rather than against itself.
    (7711.76, 15.25, 0.9531, 73.5, 7638.26, 7785.26),
    (7711.76, 14.43, 0.9019, 69.55, 7642.21, 7781.31),
])
def test_the_plan_reproduces_the_reference_figures_to_the_cent(
    spx, vix, want_pct, want_points, want_low, want_high
):
    """The whole point of matching the divisor: the rails agree to the cent.

    `points` is rounded to one decimal and the band is built FROM it, which is
    what makes the low and high land exactly on the reference's own numbers --
    banding off the raw percentage gives 7638.2592 and a rail that is always
    slightly out.
    """
    p = zdte.plan(spx_prev_close=spx, vix=vix)
    assert round(p.vix_daily_move_pct, 4) == want_pct
    assert p.points == want_points
    band = next(b for b in p.bands if b.label == "VIX 1σ")
    assert round(band.low, 2) == want_low
    assert round(band.high, 2) == want_high


def test_the_fixed_bands_are_exactly_two_and_three_percent():
    """The 2% and 3% rails are fixed, not derived from VIX.

    They are a desk's habit, quoted whatever the VIX is doing, and the read is
    where the VIX band sits relative to them. So they must not move with VIX.
    """
    p = zdte.plan(spx_prev_close=6000.0, vix=40.0)
    two = next(b for b in p.bands if b.label == "2%")
    three = next(b for b in p.bands if b.label == "3%")
    assert (two.low, two.high) == (6000.0 * 0.98, 6000.0 * 1.02)
    assert (three.low, three.high) == (6000.0 * 0.97, 6000.0 * 1.03)
    assert two.pct == 2.0 and three.pct == 3.0


def test_the_band_carries_its_own_percentage_so_a_caption_need_not_re_derive_it():
    """`pct` is stored, not recomputed from the edges.

    Recovering 2.0 from a rounded low and high would sometimes give 1.999...,
    and a caption reading "2.0%" beside edges that no longer divide to exactly
    that is the small lie this avoids.
    """
    band = next(b for b in zdte.plan(spx_prev_close=5000.0, vix=16.0).bands
                if b.label == "2%")
    assert band.pct == 2.0
    assert band.payload() == {"label": "2%", "pct": 2.0,
                              "low": 4900.0, "high": 5100.0}


@pytest.mark.parametrize("prev_close, vix", [
    (None, 16.0),      # no index close fetched yet
    (5000.0, None),    # no VIX fetched yet
    (0.0, 16.0),       # a bad index row
    (-1.0, 16.0),      # a bad index row
    (5000.0, -1.0),    # impossible VIX
])
def test_an_unusable_input_is_an_absence_not_a_zero_width_range(prev_close, vix):
    """The planner rides on top of the cohort comparison, so a missing feed is
    "not available yet", not a range of zero width -- which would render as a
    market that cannot move, a false statement rather than a blank.
    """
    assert zdte.plan(spx_prev_close=prev_close, vix=vix) is None


def test_a_zero_vix_is_a_flat_band_not_an_absence():
    """VIX can legitimately be reported low; zero is the degenerate but valid
    edge, and it means "no expected move", which is a real (if never-seen)
    reading rather than a missing feed. It is `>= 0`, unlike the prior close.
    """
    p = zdte.plan(spx_prev_close=5000.0, vix=0.0)
    assert p is not None
    assert p.vix_daily_move_pct == 0.0
    vix_band = next(b for b in p.bands if b.label == "VIX 1σ")
    assert vix_band.low == vix_band.high == 5000.0
