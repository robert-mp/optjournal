"""Realised volatility, and the line between measuring and modelling.

The arithmetic is short enough that "does stdev work" is not worth a test. What
IS worth testing is the three claims the watchlist makes on top of it:

* that too little history reports NOTHING rather than a plausible number;
* that the input order is the one the caller actually has (`ORDER BY ts DESC`);
* that this stays out of the modelled-number quarantine, because the moment it
  imports `blackscholes` the watchlist inherits a caption requirement.

Hand-computable throughout: a constant-return series has an exact vol, so the
annualisation can be checked against arithmetic rather than against itself.
"""

from __future__ import annotations

import math

import pytest

from optjournal.vol import (
    MIN_RETURNS,
    RANK_BANDS_SOURCE,
    RANK_MIDPOINT,
    RANK_MIN_WINDOWS,
    TRADING_DAYS,
    expected_move,
    log_returns,
    rank,
    rank_band,
    realised_vol,
    realised_vol_series,
)


def test_closes_arrive_newest_first_because_that_is_what_sql_gives():
    """`ORDER BY ts DESC LIMIT n` is how the caller gets a recent window.

    So the reversal belongs here, once, rather than at every call site. Asserted
    on a rising series: read newest-first and reversed correctly, every return is
    POSITIVE. Read in the wrong order they would all be negative -- a sign error
    that would invert nothing in the vol (stdev is sign-blind) and everything in
    the change columns beside it, which is exactly the kind of defect that hides.
    """
    newest_first = [110.0, 105.0, 100.0]     # rose 100 -> 105 -> 110
    rets = log_returns(newest_first)
    assert len(rets) == 2
    assert all(r > 0 for r in rets), "the series was read backwards"
    assert rets[0] == pytest.approx(math.log(105 / 100))


def test_a_missing_close_is_skipped_not_treated_as_zero():
    """A bar's close is nullable by design -- a quiet strike has no print.

    `log(0)` is not a number this can report, and treating a gap as zero would
    manufacture a -100% return. Skipped, so the series shortens rather than
    acquiring a catastrophe.
    """
    assert log_returns([110.0, None, 100.0]) == pytest.approx(
        [math.log(110 / 100)]
    )
    assert log_returns([110.0, 0.0, 100.0]) == pytest.approx([math.log(110 / 100)])
    assert log_returns([]) == []
    assert log_returns([100.0]) == [], "one close is no return"


def test_too_little_history_reports_none_not_zero():
    """Zero vol is a claim; a symbol added yesterday has not made it.

    The distinction is the whole reason the watchlist can show a dash: a row with
    four closes reads as "not enough data" rather than as a stock that never
    moved. Measured on the real journal -- GOOG had 5 daily closes and PLTR 4 --
    so this is the common case for anything just added, not an edge.
    """
    rising = [100.0 + n for n in range(MIN_RETURNS)]      # one return short
    assert realised_vol(rising) is None
    assert realised_vol([]) is None
    assert realised_vol([100.0]) is None
    # One more close crosses the threshold and produces a figure.
    assert realised_vol([100.0 + n for n in range(MIN_RETURNS + 1)]) is not None


def test_the_annualisation_is_checkable_by_hand():
    """A known daily deviation must give a known annual figure.

    Two alternating returns of +/-r have sample stdev exactly r*sqrt(2)... which
    is why the series here is built to have a stdev this test can state. Pinned
    against the arithmetic rather than against a previous run, so a changed
    convention (365 instead of 252, population instead of sample) fails loudly.
    """
    # Closes alternating by a fixed log step: returns are +s, -s, +s, -s, ...
    step = 0.01
    closes = []
    price = 100.0
    for i in range(12):
        closes.append(price)
        price *= math.exp(step if i % 2 == 0 else -step)
    got = realised_vol(list(reversed(closes)))    # caller passes newest first

    rets = log_returns(list(reversed(closes)))
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    expected = math.sqrt(var) * math.sqrt(TRADING_DAYS) * 100
    assert got == pytest.approx(expected)
    # And it IS annualised: the daily figure is smaller by exactly sqrt(252).
    daily = realised_vol(list(reversed(closes)), annualise=False)
    assert got == pytest.approx(daily * math.sqrt(TRADING_DAYS))


def test_a_flat_series_really_is_zero_vol():
    """A stock that did not move has zero realised vol, which is a real answer.

    Distinct from None: this series HAS enough history and made the claim. If the
    two collapsed, the watchlist could not tell "no data" from "no movement".
    """
    assert realised_vol([100.0] * 10) == pytest.approx(0.0)


def test_expected_move_scales_with_the_square_root_of_time():
    """Four times the horizon is twice the move, which is the whole shape of it.

    Checked as a RATIO rather than against two absolute figures, so the test says
    what the rule is instead of restating the output.
    """
    one = expected_move(100.0, 20.0, days=TRADING_DAYS // 4)
    four = expected_move(100.0, 20.0, days=TRADING_DAYS)
    assert four == pytest.approx(one * 2)
    # A full year at 20% vol is 20 points on a 100 stock, by definition.
    assert four == pytest.approx(20.0)


def test_expected_move_refuses_what_it_cannot_answer():
    """No price or no vol means no move -- not zero.

    Zero would render as "±0.00", which reads as a stock the market expects to be
    motionless rather than as a symbol with no bars stored yet.
    """
    assert expected_move(None, 20.0, days=5) is None
    assert expected_move(100.0, None, days=5) is None
    assert expected_move(0.0, 20.0, days=5) is None
    assert expected_move(100.0, 20.0, days=0) is None


# ---------------------------------------------------------------- the rank
#
# The gauge the mockup drew as IV RANK, filled with the only rank this journal can
# measure. Three of these tests are about a number NOT being produced, which is the
# balance of risk here: the formula is one line and a hand vector settles it, while
# every way of getting a plausible 0-to-100 figure out of too little history looks
# exactly like a working gauge.


def _series(now: float, low: float, high: float, *, count: int) -> list[float]:
    """A vol series, NEWEST FIRST, whose low, high and newest reading are known.

    Padded with a value strictly inside the range so the bounds stay where the test
    put them however long the series is.
    """
    inside = (low + high) / 2
    body = [low, high] + [inside] * max(0, count - 3)
    return [now, *body]


def test_the_rank_is_a_min_max_position_in_the_symbols_own_year():
    """`(now - lo) / (hi - lo) * 100`, which is what the word rank means here.

    Hand computed rather than compared against a second implementation: 35 sitting
    between a low of 20 and a high of 60 is 15/40 of the way up, so 37.5. That is
    also the arithmetic a reader can check against the two bounds printed beside the
    gauge, which is the whole reason both bounds are on the wire.

    A percentile over the same series would answer something else entirely (the
    share of readings below 35, which here is dominated by the padding), and the two
    are not substitutes -- see the module docstring.
    """
    series = _series(35.0, 20.0, 60.0, count=RANK_MIN_WINDOWS)
    assert rank(series) == pytest.approx(37.5)
    # The ends are the ends, not a scaled interior: today AT the year's low reads 0
    # and at its high reads 100.
    assert rank(_series(20.0, 20.0, 60.0, count=RANK_MIN_WINDOWS)) == pytest.approx(0.0)
    assert rank(
        _series(60.0, 20.0, 60.0, count=RANK_MIN_WINDOWS)
    ) == pytest.approx(100.0)


def test_a_flat_year_ranks_nothing_rather_than_fifty():
    """`hi == lo` is 0/0, and 50.0 would be the worst available answer.

    It would render mid-gauge -- the middle of a range that does not exist -- on the
    one symbol whose realised vol genuinely never moved, and mid-gauge is a
    perfectly ordinary reading that no reader would question. This is the same line
    `realised_vol` draws between "no data" and "no movement", except that here even
    the flat case has no position to report. `mutate.py`'s `rank-degenerate` is this
    branch answering `RANK_MIDPOINT` instead.
    """
    flat = [22.5] * RANK_MIN_WINDOWS
    assert rank(flat) is None
    assert rank(flat) != RANK_MIDPOINT, "a flat year cannot have a position"
    # And the band inherits the refusal rather than inventing a side to be on.
    assert rank_band(rank(flat)) is None


def test_a_quarter_of_a_year_of_windows_ranks_nothing():
    """Below the gate the bounds are not bounds, so there is no position.

    A min-max rank rests on two SINGLE observations, and neither gains precision
    from the windows around it -- unlike a mean or a standard deviation. Over a
    handful of windows today's vol usually IS the extreme, so the figure prints 0 or
    100 with nothing wrong with the arithmetic, which is `trend.MIN_SETTLED`'s
    failure mode in a different statistic.

    Asserted at the boundary in both directions, because a gate written `>` instead
    of `>=` passes any test that only checks a number far below it.
    """
    quarter = _series(35.0, 20.0, 60.0, count=RANK_MIN_WINDOWS // 4)
    assert len(quarter) == RANK_MIN_WINDOWS // 4
    assert rank(quarter) is None
    exact = _series(35.0, 20.0, 60.0, count=RANK_MIN_WINDOWS)
    assert rank(exact) is not None, (
        "the gate counts windows, and exactly RANK_MIN_WINDOWS of them is enough"
    )
    one_short = _series(35.0, 20.0, 60.0, count=RANK_MIN_WINDOWS - 1)
    assert len(one_short) == RANK_MIN_WINDOWS - 1
    assert rank(one_short) is None, "one window fewer must not answer"


def test_the_rank_band_source_refuses_iv_ranks_thirty():
    """The constant says why 30 was not carried over, so nobody re-imports it.

    IV rank's conventional 30 is calibrated on IMPLIED vol across a population of
    symbols; this figure is one symbol's own realised vol against its own year.
    Carrying the threshold over would be this project's recurring defect one level
    up -- in a threshold rather than in a value -- and the refusal has to live in a
    string the surfaces print, because a comment cannot be read from the page.
    """
    assert "30" in RANK_BANDS_SOURCE and "not carried over" in RANK_BANDS_SOURCE
    assert "implied vol" in RANK_BANDS_SOURCE, (
        "the sentence has to name the statistic 30 belongs to, or it reads as "
        "an arbitrary preference"
    )
    assert RANK_MIDPOINT == 50.0
    # The cut point is the middle of the range and the edge belongs to "lower",
    # which is exactly how the two filter chips are worded (`> 50`, `<= 50`) -- so
    # the pair partitions the range instead of leaving the midpoint in neither.
    assert rank_band(RANK_MIDPOINT + 0.1) == "upper"
    assert rank_band(RANK_MIDPOINT) == "lower"
    assert rank_band(RANK_MIDPOINT - 0.1) == "lower"


def test_the_vol_series_reads_newest_first_like_everything_else_here():
    """One convention for both leaves, and a reversal here is invisible.

    A reversed series has the SAME low and the SAME high, so the bounds beside the
    gauge would look right while the rank reported where the year STARTED as where
    it is now. Asserted on a series whose two halves have deliberately different
    volatility, so the newest window and the oldest cannot coincide.

    The window is asserted in the same breath: it is the 21 sessions the watchlist's
    realised-vol column already uses, so the reading being ranked is the reading on
    screen rather than a second figure that resembles it.
    """
    quiet = [100.0 * (1.002 if i % 2 else 1.0) for i in range(40)]
    lively = [100.0 * (1.03 if i % 2 else 1.0) for i in range(40)]
    newest_first = list(reversed(quiet + lively))     # the lively half is NEWEST

    series = realised_vol_series(newest_first, window=21)
    assert series[0] == pytest.approx(realised_vol(newest_first[:21]))
    assert series[0] > series[-1], "the series was built from the wrong end"
    assert len(series) == len(newest_first) - 20, (
        "one window per session that has 21 closes behind it"
    )
    # `history` caps the count without moving what the newest window is.
    capped = realised_vol_series(newest_first, window=21, history=10)
    assert len(capped) == 10 and capped[0] == pytest.approx(series[0])


def test_this_module_imports_no_option_model():
    """The reason `vol.py` exists rather than a helper inside `bars.py`.

    Realised vol is arithmetic over broker-stated prices -- no option model, no
    distribution assumed, nothing solved. That keeps the watchlist OUTSIDE the
    modelled-number quarantine and `test_layering`'s MAY_MODEL allowlist at two
    modules, which is the point: that allowlist IS the quarantine.

    Asserted here as well as in test_layering because the two say different
    things. There it is "a leaf imports nothing"; here it is "this specific
    figure is not modelled", which is the claim the watchlist's header makes to a
    reader.
    """
    import ast

    from conftest import ROOT

    source = (ROOT / "src" / "optjournal" / "vol.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    # `__future__` is not a dependency, it is a compiler directive every module
    # here carries.
    assert imported - {"__future__"} == {"math"}, (
        f"vol.py imports {sorted(imported)}. It must stay pure arithmetic: the "
        f"moment it reaches for an option model, the watchlist's realised-vol "
        f"column becomes a modelled number and needs a caption saying so."
    )
