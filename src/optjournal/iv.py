"""Implied volatility rank: a FETCHED implied vol, ranked inside its own year.

A leaf like ``marketdata`` and ``vol``: it knows how to ask CBOE for an implied
volatility and its trailing-year bounds, and how to read the answer. No database,
no journal shapes, no page.

WHY THIS IS NOT A MODELLED NUMBER, and why the watchlist can carry it at last.
``vol.py`` records the measurement that implied vol is unreachable for a symbol
this journal does not hold: it is solved from an OPTION's own closes, and a watched
name has none. That is still true of any IV this journal would COMPUTE. It is not
true of an IV this journal is TOLD. CBOE publishes ``iv30`` -- a 30-day implied
volatility it computes itself -- and publishes the high and low of its own iv30
over the trailing year. Reading three numbers off an endpoint needs no option
model, no assumed distribution and nothing solved, so ``blackscholes`` stays out of
the watchlist path and ``tests/test_layering.py``'s ``MAY_MODEL`` stays at
``{"replay", "demo"}``. The quarantine was never a rule against implied vol; it is
a rule against MODELLING, and this module does none.

What that costs instead is PROVENANCE, and the cost is real. iv30 is CBOE's
computation over CBOE's own series, so every figure here is attributed to CBOE
rather than presented as the journal's. The precedent is ``market_events.impact``,
stored verbatim as "the FEED's judgement, not the journal's" and printed with that
sentence beside it.

THE FORMULA IS TASTYTRADE'S, and it was checked against theirs rather than
assumed. Their published definition is a min-max position of the current IV inside
the previous 52 weeks::

    ivr = (iv30 - iv30_annual_low) / (iv30_annual_high - iv30_annual_low) * 100

Measured against a ten-row screenshot of their platform's numbers: all ten came
within 15 points on a different session, and -- the check that actually
distinguishes a rank from a level -- the INVERSION reproduced. LOW had the
second-lowest absolute iv30 of the ten (33.58%) and the highest rank (78.7), while
COHR had nearly the highest iv30 (80.26%) and one of the lowest ranks (54.3). Only
a rank behaves that way, and their screenshot shows the same inversion.

WHAT THIS IS NOT: tastytrade's own number. Their API returns TWO ranks per symbol
(``tos_implied_volatility_index_rank`` and ``tw_implied_volatility_index_rank``)
plus an ``implied_volatility_index_rank_source`` field naming which one their
platform used. A choice that is not observable cannot be reproduced, and they do
not publish which IV series feeds the rank either. So this is their FORMULA over
CBOE's series, which is a different number by construction, and it says CBOE on
screen. Matching their figure exactly would mean asking their API for it, which
needs an account and OAuth credentials; this module deliberately needs neither.

THE RANK IS CLAMPED, and the clamp is a measurement rather than defensive habit.
CBOE's annual bounds are computed on a slower cadence than its live iv30, so the
live figure can sit OUTSIDE its own published year. Measured on TSLA while writing
this: iv30 37.12 against an annual low of 37.18, which is a raw rank of -0.2. A
rank is defined as a position between two bounds, so a number outside 0 to 100 is
not a rank at all -- it is the bounds being stale. Clamping states that the
position is at the edge, which is true; publishing -0.2 would state a position
that does not exist.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

__all__ = [
    "IVR_HIGH",
    "IvFetchError",
    "IvRank",
    "band",
    "fetch_iv_rank",
    "parse_bounds",
    "parse_iv30",
    "rank",
]

#: The cut point the two filter chips are worded around: `IVR > 30` and
#: `IVR <= 30`. THIRTY IS A CHOSEN THRESHOLD, NOT A MEASURED ONE, and it is not
#: tastytrade's: their published guidance leans on 50 for selling premium, calls 80
#: and above extreme and under 20 depressed. 30 is the level this journal's reader
#: asked to filter at, twice, so it is what the chips cut at -- and because it is a
#: preference rather than a finding, the page attributes it as the reader's own
#: line rather than dressing it as anyone's research. The edge belongs to the LOWER
#: chip so the pair partitions the range and a rank landing exactly on 30 is in one
#: of them rather than in neither, which is `vol.rank_band`'s rule for the same
#: reason.
IVR_HIGH = 30.0

_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
_HIST_URL = (
    "https://cdn.cboe.com/api/global/delayed_quotes/historical_data/{symbol}.json"
)
_TIMEOUT_S = 25
_USER_AGENT = "Mozilla/5.0"

#: What CBOE answers for a symbol it does not carry. Measured rather than guessed:
#: both `ZZZDEMO` and `NOTATICKER` came back 403, NOT 404, so "this symbol has no
#: options here" and "the request failed" are distinguishable and the page can say
#: which. A synthetic demo symbol hitting this is the normal case, not an error.
_NOT_CARRIED = 403


class IvFetchError(RuntimeError):
    """CBOE could not be asked, or answered with something unusable.

    Deliberately NOT raised for a symbol CBOE does not carry: that is an answer.
    `fetch_iv_rank` returns None for it, so a caller can tell "no options here"
    from "the network is down" and the page can render the difference.
    """


@dataclass(frozen=True)
class IvRank:
    """One symbol's implied vol and its position inside its own trailing year.

    Every term of the rank travels WITH the rank, which is the same discipline
    `Quote` applies to a price and its age. A bare 76.7 is unreproducible and
    unfalsifiable: the reader cannot tell whether the year was wide or narrow, and
    a rank inside a two-point range is a different fact from the same rank inside a
    seventy-point one. So `iv30`, `low` and `high` are all here and all rendered.

    `as_of` is CBOE's own timestamp, not the moment of the fetch. Outside market
    hours the endpoint keeps serving the last session's figure, and a rank that old
    presented as current is the defect `Quote.at` exists to prevent -- so the age
    comes from the source and the page states it.
    """

    symbol: str
    #: CBOE's 30-day implied volatility, in percent (83.625, not 0.83625).
    iv30: float
    #: The trailing-year low and high of that same series, CBOE's own computation.
    low: float
    high: float
    #: The rank, 0 to 100, clamped. None is impossible here: a row with no rank is
    #: not constructed at all, so a reader of this type never has to check.
    rank: float
    #: CBOE's timestamp for the quote, verbatim. A string rather than an epoch
    #: because that is what the endpoint sends and re-formatting it here would put
    #: a second date parser between the source and the reader.
    as_of: str


def rank(iv30: float | None, low: float | None, high: float | None) -> float | None:
    """Where `iv30` sits between `low` and `high`, 0 to 100, clamped.

    tastytrade's published formula. See the module docstring for the check against
    their platform and for why the result is clamped.

    None when any term is missing or when the year is DEGENERATE (`high == low`).
    That last case is `vol.rank`'s rule and it is deliberate: the position would be
    0/0, and 50.0 would be the arithmetic's worst available lie, rendering
    mid-gauge inside a range that does not exist. A symbol whose implied vol never
    moved has no answer to give rather than a middling one.
    """
    if iv30 is None or low is None or high is None:
        return None
    if high == low:
        return None
    return max(0.0, min(100.0, (iv30 - low) / (high - low) * 100))


def band(value: float | None) -> str | None:
    """Which side of `IVR_HIGH` a rank sits on: "upper", "lower", or None.

    Computed once here rather than at each surface, for `trend.bucket`'s reason:
    the cut point lives beside the constant and the comment that justify it, so a
    chip label, a filter and a column cannot end up disagreeing about where 30 is.

    None in, None out. A symbol with no rank is not on the low side of anything,
    and a filter that quietly banded it would put an unmeasured row inside a set
    the reader believes was measured -- which is the whole failure mode a filter on
    a fetched number has.
    """
    if value is None:
        return None
    return "upper" if value > IVR_HIGH else "lower"


def parse_iv30(payload: Any, *, symbol: str) -> tuple[float | None, str | None]:
    """Read `iv30` and CBOE's timestamp out of the delayed-quotes reply.

    Separate from the fetch for `parse_chart`'s reason: the suite exercises it
    against a captured fixture and never touches the network.

    A ZERO iv30 IS TREATED AS ABSENT. Measured in the live reply: 224 of DELL's
    3138 contracts carry `iv` exactly 0.0, which is the endpoint's way of saying it
    has no volatility for that instrument rather than a claim that volatility is
    nil. The same convention has to hold for the underlying's iv30, because a rank
    computed from a zero would place the symbol at the very bottom of its year on
    the strength of a missing number.
    """
    data = _data(payload, symbol=symbol, what="quote")
    iv30 = _number(data.get("iv30"))
    stamp = payload.get("timestamp") if isinstance(payload, dict) else None
    return (iv30 or None), (stamp if isinstance(stamp, str) else None)


def parse_bounds(
    payload: Any, *, symbol: str
) -> tuple[float | None, float | None]:
    """Read the trailing-year low and high of iv30 out of the historical reply.

    The endpoint also carries iv60, iv90 and the three matching realised-vol
    ranges. Only the iv30 pair is read, because the rank has to be computed from
    the SAME series as the current reading beside it -- ranking a 30-day IV inside
    a 60-day IV's year would be a well-formed number describing nothing.
    """
    data = _data(payload, symbol=symbol, what="history")
    return _number(data.get("iv30_annual_low")), _number(data.get("iv30_annual_high"))


def fetch_iv_rank(
    symbol: str, *, timeout: int = _TIMEOUT_S
) -> IvRank | None:
    """One symbol's IV rank, or None when CBOE does not carry it.

    TWO requests, because CBOE serves the current figure and the year's bounds from
    two different paths. That is double what a quote costs, so this is on the same
    on-demand footing as `marketdata.fetch_quote` rather than in the page's shared
    payload -- see `web` for where it is spent.

    Returns None, rather than raising, when the symbol is not carried (HTTP 403,
    measured) or when any term of the rank is missing. Raises `IvFetchError` when
    the request itself failed, so a caller can tell an absent symbol from an absent
    network and the page can say which.
    """
    quote = _get(_QUOTE_URL, symbol, what="quote", timeout=timeout)
    if quote is None:
        return None
    iv30, as_of = parse_iv30(quote, symbol=symbol)
    if iv30 is None:
        return None

    history = _get(_HIST_URL, symbol, what="history", timeout=timeout)
    if history is None:
        return None
    low, high = parse_bounds(history, symbol=symbol)

    value = rank(iv30, low, high)
    if value is None or as_of is None:
        # An undated rank is unusable for the same reason an undated price is:
        # the page's only defence against a stale figure is showing its age.
        return None
    return IvRank(symbol=symbol.upper(), iv30=iv30, low=low, high=high,  # type: ignore[arg-type]
                  rank=value, as_of=as_of)


def _get(template: str, symbol: str, *, what: str, timeout: int) -> Any | None:
    """One CBOE request. None for a symbol it does not carry, raise for the rest."""
    url = template.format(symbol=symbol.upper())
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == _NOT_CARRIED:
            return None
        raise IvFetchError(f"{symbol} {what}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError,
            http.client.HTTPException) as exc:
        # `HTTPException` for a body cut short (IncompleteRead): see marketdata.
        raise IvFetchError(f"{symbol} {what}: {type(exc).__name__}: {exc}") from exc


def _data(payload: Any, *, symbol: str, what: str) -> dict[str, Any]:
    """The `data` block, or a loud failure naming which reply was malformed."""
    if not isinstance(payload, dict):
        raise IvFetchError(f"{symbol} {what}: response was not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise IvFetchError(f"{symbol} {what}: response carried no data block")
    return data


def _number(value: Any) -> float | None:
    """A float, or None. Booleans are not numbers here, for `bool` being an `int`."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
