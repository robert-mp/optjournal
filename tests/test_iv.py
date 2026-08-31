"""The IV rank leaf: the formula, the clamp, and the two answers that are not errors.

Every fixture here is a REDUCED capture of a real CBOE reply, taken while writing
`iv.py` on 2026-08-13. Reduced rather than invented, and reduced rather than whole:
DELL's quote reply carries 3138 contracts and 1.4MB, none of which the parser reads,
so the fixture keeps the fields under test and the shape around them.

Nothing here touches the network. The two fetch tests monkeypatch `urlopen`, which
is `test_events.py`'s idiom for the same job.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from optjournal import iv as iv_module
from optjournal.iv import (
    IVR_HIGH,
    IvFetchError,
    band,
    fetch_iv_rank,
    parse_bounds,
    parse_iv30,
    rank,
)

# DELL's delayed-quotes reply, reduced. `iv30` and the top-level `timestamp` are
# what the parser reads; `current_price` and one contract stay so the fixture still
# looks like what arrives.
QUOTE = {
    "timestamp": "2026-08-13 16:50:25",
    "symbol": "DELL",
    "data": {
        "symbol": "DELL",
        "current_price": 496.21,
        "iv30": 83.625,
        "iv30_change": -1.851,
        "options": [{"option": "DELL260814C00205000", "iv": 0.0, "delta": 0.9999}],
    },
}

# DELL's historical-data reply, whole: it is 617 bytes and every sibling field
# matters to one assertion below, which is that they are NOT read.
BOUNDS = {
    "timestamp": "2026-08-13 11:06:57",
    "symbol": "DELL",
    "data": {
        "symbol": "DELL",
        "annual_high": 485.70001220703125,
        "hv30_annual_high": 116.7969970703125,
        "iv30_annual_high": 99.30899810791016,
        "iv60_annual_high": 91.65299987792969,
        "iv90_annual_high": 88.35199737548828,
        "annual_low": 110.22000122070312,
        "hv30_annual_low": 33.84960174560547,
        "iv30_annual_low": 31.79199981689453,
        "iv60_annual_low": 33.474998474121094,
        "iv90_annual_low": 36.374000549316406,
    },
}


def _reply(payload: dict):
    """A context-manager stand-in for what `urlopen` yields."""
    def opened(*_a, **_k):
        return io.BytesIO(json.dumps(payload).encode())
    return opened


# --------------------------------------------------------------------------
# The formula.


def test_the_rank_is_tastytrades_formula_on_a_hand_computed_vector():
    """(now - low) / (high - low) * 100, checked against arithmetic done by hand.

    A hand vector rather than a property: the whole value of this figure is that it
    is the published definition, so the test has to know the answer independently
    rather than re-derive it from the code under test. 40 inside 20..70 is 40% of
    the way up a 50-point range, which is 40.0 and is checkable without a machine.
    """
    assert rank(40.0, 20.0, 70.0) == pytest.approx(40.0)
    # tastytrade's own worked example: a 20-70 year with IV at 60 gives 80.
    assert rank(60.0, 20.0, 70.0) == pytest.approx(80.0)
    # And the two ends are exactly 0 and 100, not merely near them.
    assert rank(20.0, 20.0, 70.0) == 0.0
    assert rank(70.0, 20.0, 70.0) == 100.0


def test_a_reading_outside_its_own_year_clamps_rather_than_leaving_the_range():
    """MEASURED ON TSLA, and the reason the clamp exists at all.

    CBOE's annual bounds move on a slower cadence than its live iv30, so the live
    figure can sit outside the year the same endpoint publishes: TSLA read iv30
    37.12 against an annual low of 37.18, a raw rank of -0.2. A rank is defined as a
    position between two bounds, so -0.2 is not a rank -- it is the bounds being
    stale -- and rendering it would put a gauge knob off the end of its own track.

    Both directions, because nothing says the high cannot go stale the same way.
    """
    assert rank(37.12, 37.18, 64.53) == 0.0
    assert rank(101.0, 20.0, 70.0) == 100.0
    # Without the clamp the first of those is negative, which is the ablation.
    raw = (37.12 - 37.18) / (64.53 - 37.18) * 100
    assert raw < 0


def test_a_degenerate_year_has_no_rank_and_certainly_not_fifty():
    """`high == low` is 0/0. 50.0 would be the worst available answer.

    `vol.rank` draws this line for the same reason and it is worth drawing twice: a
    symbol whose implied vol never moved would render mid-gauge, in the middle of a
    range that does not exist, and read as ordinary.
    """
    assert rank(30.0, 30.0, 30.0) is None
    assert rank(45.0, 45.0, 45.0) is None


@pytest.mark.parametrize("args", [
    (None, 20.0, 70.0),
    (40.0, None, 70.0),
    (40.0, 20.0, None),
    (None, None, None),
])
def test_a_missing_term_has_no_rank(args):
    """Three numbers or nothing. A rank from two of them would be a guess."""
    assert rank(*args) is None


# --------------------------------------------------------------------------
# The band, which is what the filter actually reads.


def test_the_band_cuts_at_thirty_with_the_edge_on_the_low_side():
    """The chips read `> 30` and `<= 30`, so the pair must partition the range.

    A rank landing exactly on 30 belongs to one of them rather than to neither,
    which is the defect a `>= / <=` pair would ship: 30.0 in both, or in neither,
    depending on which chip was tested first.
    """
    assert band(30.01) == "upper"
    assert band(78.7) == "upper"
    assert band(30.0) == "lower"
    assert band(0.0) == "lower"
    assert band(9.6) == "lower"
    # And the constant is the one the chips are worded from.
    assert IVR_HIGH == 30.0


def test_an_unranked_symbol_is_not_on_the_low_side_of_anything():
    """None in, None out.

    The failure this prevents is specific and silent: a filter that banded an
    unmeasured row as "lower" would exclude it from `> 30` on the strength of a
    number that was never fetched, and the reader would believe the set they are
    looking at was measured.
    """
    assert band(None) is None


# --------------------------------------------------------------------------
# The parsers.


def test_the_quote_parser_reads_iv30_and_cboes_own_timestamp():
    """The age comes from the SOURCE, not from the moment of the fetch.

    Outside market hours the endpoint keeps serving the last session's figure, so a
    rank stamped with "now" would present a day-old reading as current -- the defect
    `marketdata.parse_quote` refuses a price for.
    """
    iv30, as_of = parse_iv30(QUOTE, symbol="DELL")
    assert iv30 == pytest.approx(83.625)
    assert as_of == "2026-08-13 16:50:25"


def test_a_zero_iv30_is_absent_rather_than_a_claim_that_vol_is_nil():
    """MEASURED: 224 of DELL's 3138 contracts carry `iv` exactly 0.0.

    That is the endpoint saying it has no volatility for an instrument, not that
    volatility is zero. Carried through to the underlying's iv30, a zero would rank
    the symbol at the very bottom of its year on the strength of a missing number,
    which is a well-formed 0.0 under a label that does not describe it.
    """
    payload = {"timestamp": "2026-08-13 16:50:25", "data": {"iv30": 0.0}}
    iv30, _ = parse_iv30(payload, symbol="DELL")
    assert iv30 is None
    assert rank(iv30, 31.79, 99.31) is None


def test_the_bounds_parser_reads_the_iv30_pair_and_ignores_its_siblings():
    """The rank must come from ONE series.

    The reply carries iv60, iv90 and three realised-vol ranges beside the pair
    under test. Ranking a 30-day implied vol inside a 60-day one's year would be a
    well-formed number describing nothing, so the assertion is not just that the
    right pair is read but that the near-miss values are not.
    """
    low, high = parse_bounds(BOUNDS, symbol="DELL")
    assert low == pytest.approx(31.79199981689453)
    assert high == pytest.approx(99.30899810791016)
    # The siblings that a fat-fingered key would have picked up instead.
    for wrong in (33.474998474121094, 36.374000549316406,   # iv60/iv90 lows
                  91.65299987792969, 88.35199737548828,     # iv60/iv90 highs
                  33.84960174560547, 116.7969970703125,     # realised vol
                  110.22000122070312, 485.70001220703125):  # the PRICE range
        assert low != pytest.approx(wrong)
        assert high != pytest.approx(wrong)


@pytest.mark.parametrize("payload", ["not an object", 42, None, {"data": "nope"}, {}])
def test_a_malformed_reply_fails_loudly_naming_which_one(payload):
    """A parse failure is a defect, not a missing symbol, so it raises.

    The distinction matters to the cron: an absent symbol is routine and an endpoint
    that changed shape needs a human.
    """
    with pytest.raises(IvFetchError):
        parse_iv30(payload, symbol="DELL")
    with pytest.raises(IvFetchError):
        parse_bounds(payload, symbol="DELL")


# --------------------------------------------------------------------------
# The fetch, and the two answers that are not errors.


def test_a_symbol_cboe_does_not_carry_is_an_answer_rather_than_a_failure(monkeypatch):
    """403, MEASURED: both ZZZDEMO and NOTATICKER answer 403 and not 404.

    So "this symbol has no options here" is distinguishable from "the request
    failed", and the page can render the difference. The demo's synthetic symbol
    hits this on every refresh, which makes it the normal path rather than an edge.
    """
    def forbidden(*_a, **_k):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(iv_module.urllib.request, "urlopen", forbidden)
    assert fetch_iv_rank("ZZZDEMO") is None


def test_any_other_http_status_is_a_failure_that_raises(monkeypatch):
    """A 500 is not "no options here", and returning None for it would be a lie.

    The caller would cache an absence, the cell would dash with "CBOE does not
    carry this symbol", and the reader would be told something false about their
    own watchlist because a server was briefly unwell.
    """
    def broken(*_a, **_k):
        raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)

    monkeypatch.setattr(iv_module.urllib.request, "urlopen", broken)
    with pytest.raises(IvFetchError):
        fetch_iv_rank("DELL")


def test_a_whole_fetch_assembles_the_rank_and_carries_every_term_of_it(monkeypatch):
    """TWO requests, and the result keeps the bounds it was computed from.

    A bare 76.7 is unfalsifiable: the reader cannot tell whether the year was wide
    or narrow, and the same rank inside a two-point range is a different fact from
    one inside a seventy-point range. So the assertion is on all four numbers, not
    just the rank.
    """
    calls: list[str] = []

    def routed(request, *_a, **_k):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        calls.append(url)
        return _reply(BOUNDS if "historical_data" in url else QUOTE)()

    monkeypatch.setattr(iv_module.urllib.request, "urlopen", routed)
    got = fetch_iv_rank("dell")

    assert got is not None
    assert got.symbol == "DELL", "the symbol is upper-cased, matching the watchlist"
    assert got.iv30 == pytest.approx(83.625)
    assert got.low == pytest.approx(31.79199981689453)
    assert got.high == pytest.approx(99.30899810791016)
    assert got.rank == pytest.approx(76.77, abs=0.01)
    assert got.as_of == "2026-08-13 16:50:25"
    # Two paths, and the quote is asked for first: it is the request that can make
    # the second one unnecessary, when iv30 is absent.
    assert len(calls) == 2
    assert "delayed_quotes/options/DELL.json" in calls[0]
    assert "historical_data/DELL.json" in calls[1]


def test_an_absent_iv30_skips_the_second_request_entirely(monkeypatch):
    """No current reading means no rank, so the year's bounds are not worth asking for.

    One request saved per unrankable symbol, on a path that already costs double a
    quote. The count is the assertion, because the return value would be None either
    way and a wasted request is invisible without it.
    """
    calls: list[str] = []

    def routed(request, *_a, **_k):
        calls.append(request.full_url)
        return _reply({"timestamp": "2026-08-13 16:50:25", "data": {"iv30": 0.0}})()

    monkeypatch.setattr(iv_module.urllib.request, "urlopen", routed)
    assert fetch_iv_rank("DELL") is None
    assert len(calls) == 1, "the bounds were fetched for a symbol that cannot rank"


def test_an_undated_rank_is_discarded_like_an_undated_price(monkeypatch):
    """`marketdata.parse_quote`'s rule, applied to the same class of figure.

    The page's only defence against a stale rank is showing its age, so a rank with
    no timestamp would render as current and could be a week old.
    """
    def routed(request, *_a, **_k):
        if "historical_data" in request.full_url:
            return _reply(BOUNDS)()
        return _reply({"data": {"iv30": 83.625}})()   # no `timestamp`

    monkeypatch.setattr(iv_module.urllib.request, "urlopen", routed)
    assert fetch_iv_rank("DELL") is None
