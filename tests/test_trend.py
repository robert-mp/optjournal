"""B-Xtrender, pinned to arithmetic rather than to a previous run.

The risk this file exists for is not that the code crashes. It is that it returns
a well-formed number in the right range which is a DIFFERENT indicator from the
one the column will be headed with -- the defect shape this project keeps hunting.
There are three separate ways to arrive there, and each has a test:

* compose the pieces in the wrong order (`ema(rsi(close, 5) - 50, 3)`, which a
  popular repository really does publish under this name);
* smooth the RSI with an EMA rather than with Wilder's average, which is a
  one-character difference in the constant and a visible difference in the value;
* seed either recursion with the first value rather than with an SMA, which is
  what pandas does by default and is worth more than ten points near the start of
  a series.

None of the three produces a number a reader could tell was wrong, so each is
pinned against arithmetic that can be done on paper. The vectors are built so the
answer has a CLOSED FORM: a ramp plus a period-two wobble drives the smoothed
averages into a two-cycle whose ratio is exactly 15:14, so the expected value is
`100 * 15/29 - 50` and depends on neither the slope nor the wobble's size. Pinning
against the module's own output would have asserted that today equals today.

The fourth way to be wrong is to answer at all when the window is too short, which
is `MIN_SETTLED`'s job and its own test.
"""

from __future__ import annotations

import math

import pytest

from optjournal.trend import (
    BANDS_SOURCE,
    BX_OVERBOUGHT,
    BX_OVERSOLD,
    CENTRE,
    LONG_L1,
    LONG_L2,
    MIN_CLOSES,
    MIN_SETTLED,
    PARAMS_CAPTION,
    SHORT_L1,
    SHORT_L2,
    SHORT_L3,
    bucket,
    bxtrender_long,
    bxtrender_short,
    ema,
    rsi,
)

# --- the vectors --------------------------------------------------------------

#: A ramp of +1.00 a session with a +2.00 wobble on alternate sessions, 400 long.
#: Two properties make it hand-computable; see the vector test for the derivation.
ALTERNATING = [100.0 + t + 2.0 * (t % 2) for t in range(400)]

#: A 24-session sine, 300 long. Chosen by search as a case where the published
#: formula and the popular wrong one disagree in SIGN while neither is pegged at
#: an extreme, so the disagreement cannot be dismissed as a boundary artefact.
SINE_24 = [100.0 + 10.0 * math.sin(2 * math.pi * t / 24.0) for t in range(300)]

#: A falling 24-session sine, 400 long, whose first 44 values are the warm-up zone
#: the seeding test reads. Falling because the two seedings then disagree in sign
#: as well as in magnitude.
WARM_UP = [
    100.0 - 0.5 * t + 3.0 * math.sin(2 * math.pi * t / 24.0) for t in range(400)
]

#: Strictly falling, 40 sessions. Any strictly falling series pegs the long arm at
#: exactly -50.0 the moment it can be computed at all, which is the measurement
#: `MIN_SETTLED` exists for.
FALLING = [200.0 - t for t in range(40)]


def newest_first(oldest_first: list[float]) -> list[float]:
    """These functions take closes newest first, as `ORDER BY ts DESC` gives them.

    The vectors above are written chronologically because that is how a series is
    read on paper, so every call reverses. Spelled as a named helper rather than
    inline so no test can accidentally be the one that forgot.
    """
    return list(reversed(oldest_first))


def raw_short(oldest_first: list[float]) -> float | None:
    """The short arm with NO settled-window gate, straight from the primitives.

    Needed because the gate is the thing under test in one place: to show what
    `MIN_SETTLED` is standing in front of, something has to be able to look past
    it. Deliberately spelled out here rather than exposed from the module, so the
    ungated value has no import path a serializer could reach for.
    """
    fast, slow = ema(oldest_first, SHORT_L1), ema(oldest_first, SHORT_L2)
    offset = len(fast) - len(slow)
    difference = [f - s for f, s in zip(fast[offset:], slow, strict=True)]
    series = rsi(difference, SHORT_L3)
    return series[-1] - CENTRE if series else None


def raw_long(oldest_first: list[float]) -> float | None:
    """The long arm with no gate, for the same reason as `raw_short`."""
    series = rsi(ema(oldest_first, LONG_L1), LONG_L2)
    return series[-1] - CENTRE if series else None


# --- the arithmetic -----------------------------------------------------------


def test_the_short_arm_matches_a_hand_computed_vector():
    """+1.7241 on `ALTERNATING`, derived on paper and not read off a run.

    The derivation, which is why this series and not a random walk:

    1. `ALTERNATING` is a ramp of slope 1 plus a period-two wobble. An EMA is a
       linear filter, so each output is the ramp lagged plus a period-two
       response. An SMA-seeded EMA's lag on a ramp is EXACT rather than
       asymptotic: the mean of the first `L` values is the ramp's value at that
       window's centre, which is `b*(L-1)/2` behind its last value, and
       `b*(1-alpha)/alpha` with `alpha = 2/(L+1)` is the same distance. So the
       ramp contributes a CONSTANT to `ema(c,5) - ema(c,20)`: `1 * (19-4)/2 =
       7.5`. Measured on the difference series' tail: it alternates 7.35, 7.65,
       7.35, 7.65 -- a constant 7.5 plus a wobble of +/-0.15.
    2. A constant contributes nothing to the CHANGES the RSI reads, so the RSI
       sees a strictly alternating +0.30, -0.30, +0.30 ... series.
    3. Wilder's average of a one-on-one-off sequence settles into a two-cycle.
       With `r = (15-1)/15 = 14/15`, the peak solves `g = m/15 + r^2 * g`, giving
       `g = 15m/29`; on that same step the average of the down moves is `r` times
       its own peak, `14m/29`. The wobble size cancels in the ratio.
    4. `rsi = 100 * gain / (gain + loss) = 100 * 15/29 = 51.7241...`, so the arm
       reads `+1.7241379310` -- independent of the slope AND of the wobble.

    Measured: 1.7241379310062 against the closed form's 1.7241379310345, agreeing
    to eleven decimals at 400 closes. At 120 it is 1.71725, which is the same
    convergence story `MIN_SETTLED` is sized by.

    This vector also separates Wilder's smoothing from an EMA of the same period,
    which is the difference between `ta.rma` and `ta.ema` and a one-character
    difference in a constant. Smoothing the same series with `2/(15+1)` gives the
    two-cycle `8m/15` against `7m/15`, so `100 * 8/15 - 50 = +3.3333` -- verified
    numerically at +3.3333333333336 while the module answers +1.7241379310062.
    """
    closed_form = 100.0 * 15.0 / 29.0 - CENTRE
    got = bxtrender_short(newest_first(ALTERNATING))
    assert got == pytest.approx(closed_form, abs=1e-9)
    assert round(got, 4) == 1.7241

    # The long arm on the same rising series is pegged at the top, which is a real
    # reading rather than a warm-up artefact: an EMA(20) of a ramp rises at every
    # single step, so there are no down moves for Wilder's average to hold.
    assert bxtrender_long(newest_first(ALTERNATING)) == pytest.approx(50.0)


def test_the_rsi_reads_the_ema_difference_not_the_price():
    """The published formula and the popular wrong one disagree in SIGN here.

    A widely-copied Python implementation computes `ema(rsi(close, 5) - 50, 3)`:
    an RSI of PRICE, smoothed, landing in the same [-50, +50] range with a
    plausible sign. Nothing about the output says which one produced it, so the
    only way to hold the composition is to run both on a series where they
    disagree about the answer to the question the column asks -- is this symbol
    positive or negative.

    `SINE_24` was found by search under exactly that condition, with the extra
    requirement that NEITHER reading is pegged at +/-50: a disagreement between
    two saturated values proves nothing, since almost any formula saturates on a
    contrived series. Measured: the published formula reads +1.9650 and the wrong
    one -8.9169 on the same 300 closes.
    """
    correct = bxtrender_short(newest_first(SINE_24))

    # The wrong composition, written out so the difference is visible: same
    # primitives, same periods' spirit, RSI applied to the PRICES.
    smoothed = ema([v - CENTRE for v in rsi(SINE_24, SHORT_L1)], 3)
    wrong = smoothed[-1]

    assert correct == pytest.approx(1.9649835, abs=1e-6)
    assert wrong == pytest.approx(-8.9168625, abs=1e-6)
    assert correct > 0 > wrong, (
        "the two compositions must disagree in sign on this vector, or the test "
        "has stopped distinguishing them"
    )
    # And neither is pegged, so the disagreement is about the indicator rather
    # than about a boundary.
    assert abs(correct) < 45.0 and abs(wrong) < 45.0


def test_the_ema_is_seeded_with_an_sma_not_the_first_value():
    """TradingView seeds with an SMA; pandas seeds with the first value.

    Both converge, and near the start of a series the choice is worth more than
    the whole band width. MEASURED on TSLA's 755 fetched daily closes: a 44-close
    window reads **-1.3487** with SMA seeding against **+9.4472** with recursive
    seeding, a **10.80** point gap that flips the sign and therefore the bucket;
    the same comparison at 200 closes is 0.0003 and at 755 is 0.0000.

    Since the figure's whole meaning is "what the published indicator says about
    this symbol", the published seeding is the correct one and the other is a bug
    that looks like a number. Asserted here twice: directly, that the first EMA
    value IS the SMA of the first `length` closes, and end to end, that the two
    seedings disagree materially in the warm-up zone and agree once settled --
    which is the same convergence `MIN_SETTLED` is sized by, on a hermetic series
    so the assertion needs no network.
    """
    # Directly: 2, 4, 9 seeds at their mean, 5.0, not at 2.0. Then one step of
    # alpha = 2/(3+1) = 0.5 on the next value: 0.5*10 + 0.5*5 = 7.5.
    assert ema([2.0, 4.0, 9.0, 10.0], 3) == pytest.approx([5.0, 7.5])
    assert ema([2.0, 4.0, 9.0, 10.0], 3)[0] != 2.0, "seeded with the first value"
    # The series begins where the seed completes: n - length + 1 values, and
    # nothing before it, because a smoothed mean of fewer bars than its own period
    # is a different statistic under the same name.
    assert ema([1.0, 2.0], 3) == []

    published = raw_short(WARM_UP[:44])
    recursive = _recursively_seeded_short(WARM_UP[:44])
    assert published == pytest.approx(1.1126245, abs=1e-6)
    assert recursive == pytest.approx(-25.4266196, abs=1e-6)
    assert abs(published - recursive) > 20.0, (
        "the two seedings must still disagree in the warm-up zone, or this test "
        "has stopped measuring the choice"
    )
    assert published > 0 > recursive, "and the disagreement reaches the sign"

    # Settled, the choice stops mattering, which is why the fix is a window gate
    # rather than a warning: 6.4e-10 apart at 400 closes.
    assert raw_short(WARM_UP) == pytest.approx(
        _recursively_seeded_short(WARM_UP), abs=1e-6
    )


def _recursively_seeded_short(oldest_first: list[float]) -> float:
    """The short arm as pandas' `ewm(adjust=False)` would compute it.

    Exists only for the comparison above: every recursion starts from its first
    observation instead of from an SMA. Written out rather than imported because
    pandas is not a dependency of this project and the point is the seeding, not
    the library.
    """

    def ema_recursive(values: list[float], length: int) -> list[float]:
        alpha = 2.0 / (length + 1)
        current = values[0]
        out = [current]
        for value in values[1:]:
            current = alpha * value + (1.0 - alpha) * current
            out.append(current)
        return out

    def rsi_recursive(values: list[float], length: int) -> list[float]:
        changes = [values[i + 1] - values[i] for i in range(len(values) - 1)]
        gains = [c if c > 0.0 else 0.0 for c in changes]
        losses = [-c if c < 0.0 else 0.0 for c in changes]
        gain, loss = gains[0], losses[0]
        out = [100.0 * gain / (gain + loss) if gain + loss else float(CENTRE)]
        for i in range(1, len(changes)):
            gain = (gain * (length - 1) + gains[i]) / length
            loss = (loss * (length - 1) + losses[i]) / length
            out.append(100.0 * gain / (gain + loss) if gain + loss else float(CENTRE))
        return out

    fast = ema_recursive(oldest_first, SHORT_L1)
    slow = ema_recursive(oldest_first, SHORT_L2)
    difference = [f - s for f, s in zip(fast, slow, strict=True)]
    return rsi_recursive(difference, SHORT_L3)[-1] - CENTRE


def test_reading_the_closes_backwards_flips_the_sign():
    """The input order is a convention, and getting it wrong is silent.

    `vol.log_returns` documents newest-first because that is what `ORDER BY ts
    DESC LIMIT n` gives, and this module follows it so the watchlist panel hands
    one list of closes to both leaves. A reversed read costs nothing visible: the
    value stays inside [-50, +50] and stays plausible, and only the SIGN is wrong
    -- which is the indicator's primary published state and the thing the column's
    colour is driven by.

    `ALTERNATING` makes the failure exact rather than approximate: read the wrong
    way round it is the same magnitude with the opposite sign, +1.7241 against
    -1.7241, because the two-cycle is symmetric. On the long arm the same
    reversal turns a series pegged at +50.0 into one pegged at -50.0, the two
    strongest opposite readings the indicator can produce.
    """
    forwards = bxtrender_short(newest_first(ALTERNATING))
    backwards = bxtrender_short(ALTERNATING)          # deliberately wrong order
    assert forwards == pytest.approx(-backwards, abs=1e-9)
    assert forwards > 0 > backwards

    assert bxtrender_long(newest_first(ALTERNATING)) == pytest.approx(50.0)
    assert bxtrender_long(ALTERNATING) == pytest.approx(-50.0)


# --- the gate -----------------------------------------------------------------


def test_fewer_than_the_floor_returns_none_rather_than_a_pegged_extreme():
    """Below the settled window the answer is None, and this is what it hides.

    Two constants, two jobs. `MIN_CLOSES` is where the arithmetic first produces
    anything (an SMA-seeded EMA(20) over n closes yields n-19 values, and Wilder's
    RSI(15) needs 16 of them, so 35), and `MIN_SETTLED` is where the answer is
    worth showing. A single number would have answered the second question with
    the first.

    What it hides is measured here rather than described: on any strictly falling
    series the long arm reports **exactly -50.0000** as soon as it can be computed
    at all, because a 16-value EMA(20) is still falling out of its own seed, so
    every change is a down move, Wilder's average of up moves is zero and the RSI
    is zero. That is the strongest signal anything on this page can render, from
    35 closes, with nothing wrong with the arithmetic. On TSLA's real closes it
    reads -50.0000 from 35 closes through 43.

    So the gated functions must answer None at 36 closes -- not the pegged value,
    and not 0.0, which would be a claim that the indicator is neutral.

    The behavioural assertions come FIRST and the relationship between the two
    constants comes last, deliberately: `mutate.py`'s `bx-gate` collapses the gate
    onto the floor, and a test that reported that as "35 is not less than 35" would
    be catching a mutant on an inequality rather than on what a reader would see.
    """
    # The gate refuses a warm-up window, in the arm whose warm-up value is the
    # loudest. 36 closes is one past the arithmetic floor, and the honest answer
    # there is nothing at all.
    assert bxtrender_long(newest_first(FALLING[: MIN_CLOSES + 1])) is None
    assert bxtrender_short(newest_first(FALLING[: MIN_CLOSES + 1])) is None
    assert bxtrender_long(newest_first(FALLING[: MIN_CLOSES - 1])) is None

    # And this is what it refused to publish: the pegged extreme, well-formed.
    assert raw_long(FALLING[:MIN_CLOSES]) == -50.0
    assert raw_long(FALLING[: MIN_CLOSES + 1]) == -50.0

    # 45 sessions is the state five of the six real watched symbols were in when
    # this was written, so it is the common case rather than an edge.
    ramp = newest_first(ALTERNATING)
    assert bxtrender_short(ramp[:45]) is None
    # One close short of the gate is refused; the gate itself answers.
    assert bxtrender_short(ramp[: MIN_SETTLED - 1]) is None
    assert bxtrender_short(ramp[:MIN_SETTLED]) is not None

    # The gate is a count of USABLE closes, so a series of the right length made
    # of holes is refused too -- a null close is dropped rather than crashing the
    # recursion, following `vol.log_returns`.
    punctured = [None if i % 2 else 100.0 + i for i in range(MIN_SETTLED)]
    assert bxtrender_short(newest_first(punctured)) is None

    # The arithmetic floor is where it says it is: one value at MIN_CLOSES, none
    # below it. This is what makes MIN_CLOSES a measurement rather than a comment.
    assert len(rsi(ema(FALLING[:MIN_CLOSES], LONG_L1), LONG_L2)) == 1
    assert rsi(ema(FALLING[: MIN_CLOSES - 1], LONG_L1), LONG_L2) == []
    assert raw_long(FALLING[: MIN_CLOSES - 1]) is None
    assert MIN_CLOSES < MIN_SETTLED, "the floor cannot be the gate"


# --- the bands, and what they may be called -----------------------------------


def test_the_bucket_boundaries_are_inclusive_and_the_bands_are_named(monkeypatch):
    """Three buckets that partition the range, cut at the named constants.

    Inclusive edges because the three buckets have to cover everything: with
    exclusive ones, exactly -20.0 belongs to no bucket and the row falls out of
    every filter, which is the sort of hole that shows up as a missing row rather
    than as an error.

    Read from the constants rather than from literals, asserted by moving one:
    with the band at -30 a reading of -25 stops being "low". The failure this
    prevents is a cut point inlined at a second site (a filter chip, a server-side
    bucket) drifting from the constant the caption prints.

    None in, None out. A symbol with no reading is NOT in the middle band: a
    filter that bucketed it as "mid" would put an unmeasured row inside a set the
    reader believes was measured, which is this project's defect shape wearing a
    filter's clothes.
    """
    assert bucket(BX_OVERSOLD) == "low"
    assert bucket(BX_OVERSOLD - 0.01) == "low"
    assert bucket(BX_OVERSOLD + 0.01) == "mid"
    assert bucket(0.0) == "mid"
    assert bucket(BX_OVERBOUGHT - 0.01) == "mid"
    assert bucket(BX_OVERBOUGHT) == "high"
    assert bucket(BX_OVERBOUGHT + 0.01) == "high"
    assert bucket(None) is None
    assert bucket(float("nan")) is None
    # The whole range is covered, both saturation points included.
    assert {bucket(v) for v in (-50.0, -20.0, 0.0, 20.0, 50.0)} == {
        "low", "mid", "high"
    }

    monkeypatch.setattr("optjournal.trend.BX_OVERSOLD", -30.0)
    assert bucket(-25.0) == "mid", "the cut point is inlined, not read"


def test_the_band_vocabulary_is_attributed_in_one_string():
    """Where the band came from, and what its words are not claiming.

    The published indicator states NO oversold or overbought level: no hline, no
    level input, only the sign crossed with rising-or-falling. So +/-20 is imported
    from Wilder's RSI 70/30 re-centred by the indicator's own -50, and an imported
    threshold that does not say where it came from is a figure its reader cannot
    argue with. `impact_source` closes the same gap for the calendar feed.

    The two valuation words survive HERE and only here. "Oversold" and
    "overbought" are claims that a share is cheaper or dearer than it should be;
    an RSI of an EMA difference re-centred by 50 is a statement about the shape of
    recent closes. `bucket`'s keys carry none of that vocabulary, which is the
    other half of the same decision.

    The numbers in the sentence are derived from the constants, so a retuned band
    cannot leave the caption claiming the old one. If a band is ever chosen on its
    own evidence rather than imported, this sentence has to be rewritten rather
    than reformatted -- it would no longer be Wilder's.
    """
    assert "Wilder" in BANDS_SOURCE
    assert f"{CENTRE + BX_OVERBOUGHT:.0f}/{CENTRE + BX_OVERSOLD:.0f}" in BANDS_SOURCE
    assert "70/30" in BANDS_SOURCE, "the RSI convention the band is imported from"
    assert "oversold" in BANDS_SOURCE and "overbought" in BANDS_SOURCE
    assert "not this journal's view of what a share is worth" in BANDS_SOURCE

    # The vocabulary is confined to the attributed string: no payload key, and no
    # bucket name, may carry a claim about worth.
    for value in (-50.0, -20.0, 0.0, 20.0, 50.0):
        assert bucket(value) in {"low", "mid", "high"}
        assert "sold" not in bucket(value) and "bought" not in bucket(value)

    # The tile's caption is generated from the periods for the same reason: at
    # other settings B-Xtrender is a different number, so a figure printed without
    # them cannot be reproduced.
    from_the_constants = (
        f"B-Xtrender, short arm {SHORT_L1}/{SHORT_L2}/{SHORT_L3}, "
        f"centred on {CENTRE}; Jhunjhunwala 2019"
    )
    assert from_the_constants == PARAMS_CAPTION
    assert "5/20/15" in PARAMS_CAPTION and "Jhunjhunwala" in PARAMS_CAPTION
