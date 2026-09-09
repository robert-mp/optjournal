"""Historical price bars, fetched and parsed. No database, no journal.

A leaf module like ``money``: it knows how to ask a market-data source for
OHLCV bars and how to read the answer, and nothing about episodes, lifecycles
or SQLite. ``bars`` owns the journal-shaped questions -- which contract, which
window -- and the persistence.

Three facts about the data drove the design, each verified against the live
endpoint rather than assumed:

* Bars are requested by an explicit epoch window (``period1``/``period2``)
  rather than a coarse range bucket, so one request maps exactly onto a
  position's holding span. A 2026-07-22 -> 2026-08-05 hourly request for TSLA
  returns 70 bars from 13:30Z to 19:30Z: session-aligned to 09:30-15:30 ET.

* The last hourly bar of a US session is a HALF bar. The session ends at
  16:00 ET, so the 15:30 bar spans thirty minutes, giving **seven** bars per
  session rather than six or eight. Nothing may assume uniform bar width --
  not the chart's x-axis, not any aggregation.

* Option contracts are served at DAILY granularity only. An intraday request
  for an OCC symbol returns a well-formed response with an *empty* timestamp
  array. That is neither an error nor a symbol-format problem, so
  :func:`fetch_bars` returns an empty list for it and callers can distinguish
  "no bars here" from "the request failed".

One correctness rule outranks the rest: ``close`` and friends arrive as nulls
on sessions with no print -- roughly one day in five on a quiet option strike.
They stay ``None`` all the way into the database. Coercing them to ``0.0``
would draw an option's value collapsing to nothing, which reads as a
catastrophic loss rather than as a gap.

A note for whoever later "fixes" the timestamps: a bar's UTC calendar date
already equals its ET session date, for both granularities. Daily bars land at
04:00Z/05:00Z (ET midnight) and the US session spans 13:30Z-20:00Z, so both
sit inside the same UTC day. No timezone conversion is needed to recover the
trading date, and adding one would be a change in behaviour, not a fix.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BAR_SIZES",
    "SOURCE_RANK",
    "Bar",
    "BarFetchError",
    "BarNotFound",
    "Quote",
    "fetch_bars",
    "fetch_quote",
    "occ_symbol",
    "parse_chart",
    "parse_quote",
]

#: Bar sizes this journal stores. Hourly for a position held days, daily for
#: one held months -- ``bars.bar_size_for`` picks per position. Not an
#: exhaustive list of what the source offers: it is what the chart renders.
BAR_SIZES = ("1h", "1d")

#: Trust order for the same ``(conid, bar_size, ts)``. A fetch from a
#: higher-ranked source overwrites a stored row; a re-fetch from a lower-ranked
#: one leaves it alone. Broker marks beat a public endpoint, so adding an IBKR
#: source later upgrades history in place with no migration and no re-fetch of
#: what is already good.
#:
#: ``synthetic`` is the demo's computed bars and sits below everything real, so a
#: genuine fetch always displaces one and never the reverse. It is listed rather
#: than left to the ELSE-0 default so the ordering is stated in one place.
SOURCE_RANK: dict[str, int] = {"synthetic": 0, "yahoo": 10, "ibkr": 20}

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_TIMEOUT_S = 25
_USER_AGENT = "Mozilla/5.0"


class BarFetchError(RuntimeError):
    """The source could not be asked, or answered with something unusable.

    Deliberately not raised for an empty result: a known symbol with no bars
    at the requested granularity is an answer, not a failure.
    """


class BarNotFound(BarFetchError):
    """The source has no chart endpoint for this symbol.

    Kept distinct from transport and parse failures because an expired option
    can disappear from a public source after its history is no longer
    recoverable. The journal layer decides whether that absence is expected for
    the window it requested; live contracts and underlyings still treat it as a
    failure.
    """


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ``ts`` is the bar's OPEN, epoch seconds, UTC.

    Full OHLC is kept even though the chart draws a line: the endpoint sends
    it for free, a line is derivable from OHLC, and a range is not recoverable
    from a close alone. Any field may be ``None`` -- see the module docstring.
    """

    ts: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None


@dataclass(frozen=True)
class Quote:
    """The last trade the source knows about, and WHEN it was.

    Already in every chart response, in a `meta` block the bar parser reads past
    and drops. So this costs no extra request: `fetch_bars` and `fetch_quote` hit
    the same URL, and the watchlist's stored close and live price come from one
    endpoint. Measured against the live source while writing this -- a quote ten
    seconds old beside a stored close 0.50% away from it, which is the gap that
    made a column headed `last` misleading.

    `at` is NOT decoration. A quote has no meaning without its age: outside
    market hours the source keeps serving Friday's last trade, and a number that
    old presented as "live" is worse than showing yesterday's close honestly. The
    page renders the age, and the age comes from here.

    Deliberately NOT `market_state`. Verified rather than assumed: the chart
    response carries no `marketState` field for either a stock or an ETF, so a
    boolean "market open" here would have to be inferred from the clock -- a
    second calendar, wrong on holidays and half-days. The age is a measurement;
    open-or-closed would be a guess wearing a measurement's clothes.

    `previous_close` comes from the same block, so the change is computed against
    what the source itself considers the prior settle rather than against a stored
    bar that may be a different session.
    """

    symbol: str
    price: float | None
    #: Epoch seconds, UTC, of the last trade. None when the source omits it,
    #: which makes the price unusable rather than merely undated -- see
    #: `parse_quote`.
    at: int | None
    previous_close: float | None
    currency: str | None
    #: The company or fund name, from the same `meta` block, at NO extra request:
    #: this parser already receives it and used to drop it. Nullable because it is
    #: fetched fact and the surface has to work without it -- a watchlist row shows
    #: the bare symbol until a quote has arrived, exactly as the stored close
    #: already carries its stale marker until then.
    #:
    #: Probed live across nine symbols (six stocks, an ETF, a dual-class ticker and
    #: an OCC option): present on every one, in a 25-key block that carries no
    #: implied vol. Not cached anywhere -- see the plan for why `watchlist` (the
    #: user-input table) and `securities.description` (one of six real watched
    #: symbols, and "TESLA INC" against this source's "Tesla, Inc.") are both the
    #: wrong home.
    name: str | None = None


def occ_symbol(symbol: str) -> str:
    """The journal's padded OCC symbol as the price source spells it.

    IBKR writes ``TSLA  260904P00270000``, padding the root to six characters;
    the endpoint wants ``TSLA260904P00270000``. Plain underlying tickers have
    no padding and pass through unchanged, so this is safe to apply to both.
    """
    return "".join(symbol.split())


def fetch_bars(
    symbol: str,
    *,
    bar_size: str,
    start: int,
    end: int,
    source: str = "yahoo",
    timeout: int = _TIMEOUT_S,
) -> list[Bar]:
    """Bars for ``symbol`` over ``[start, end]`` in epoch seconds, oldest first.

    Returns ``[]`` when the source knows the symbol but holds no bars for that
    window and granularity. Raises :class:`BarFetchError` when the request
    itself failed or the response was unreadable.

    A second source (IBKR, for recorded hourly option marks) would branch
    here and reuse :func:`parse_chart` only if it happened to share the wire
    format, which it does not -- it would bring its own parser and land the
    same :class:`Bar` list. The seam is the return type, not the parser.
    """
    if bar_size not in BAR_SIZES:
        raise BarFetchError(f"unsupported bar size {bar_size!r}")
    if source != "yahoo":
        raise BarFetchError(f"no adapter for source {source!r}")

    query = f"?interval={bar_size}&period1={int(start)}&period2={int(end)}"
    payload = _get_chart(symbol, query, what=bar_size, timeout=timeout)
    return parse_chart(payload, symbol=symbol, bar_size=bar_size)


def _get_chart(symbol: str, query: str, *, what: str, timeout: int) -> Any:
    """One chart request, with the error wrapping both callers need.

    Extracted when `fetch_quote` arrived rather than copied: bars and quotes come
    from the SAME url and differ only in the query string, so duplicating this
    would mean two places to fix a timeout, a user agent or an error message. The
    parsers stay separate because they read different parts of the response.
    """
    url = _CHART_URL.format(symbol=occ_symbol(symbol))
    request = urllib.request.Request(url + query, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        error = f"{occ_symbol(symbol)} {what}: HTTPError: {exc}"
        if exc.code == 404:
            raise BarNotFound(error) from exc
        raise BarFetchError(error) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BarFetchError(
            f"{occ_symbol(symbol)} {what}: {type(exc).__name__}: {exc}"
        ) from exc


def fetch_quote(
    symbol: str, *, source: str = "yahoo", timeout: int = _TIMEOUT_S
) -> Quote:
    """The last trade for ``symbol``, from the same endpoint the bars come from.

    `range=1d` is the smallest window that still carries the `meta` block; the
    bars in the reply are discarded. One request per symbol, so a watchlist of
    four costs four -- which is why the page fetches these on demand rather than
    on a timer, and why they are never written to `price_bars`.
    """
    if source != "yahoo":
        raise BarFetchError(f"no adapter for source {source!r}")
    payload = _get_chart(symbol, "?interval=1d&range=1d", what="quote",
                         timeout=timeout)
    return parse_quote(payload, symbol=symbol)


def parse_quote(payload: Any, *, symbol: str) -> Quote:
    """Read a chart response's `meta` block into a :class:`Quote`.

    Separate from the fetch for the same reason as `parse_chart`: the suite
    exercises it against a captured fixture and never touches the network.

    A PRICE WITHOUT A TIME IS DISCARDED. If `regularMarketTime` is missing the
    price goes to None as well, because the page's whole defence against a stale
    quote is showing its age -- an undated price would render as live and could be
    Friday's. Refusing it is the same rule `money.py` applies to a figure whose
    currency cannot be established: drop the number rather than present it
    unqualified.

    THE NAME PREFERS `longName`, and the fallback order is measured rather than
    stylistic. `shortName` is truncated at 31 characters by the source -- SPY reads
    "State Street SPDR S&P 500 ETF T" there against the full "State Street SPDR
    S&P 500 ETF Trust" in `longName` -- and it disagrees outright on a dual-class
    ticker (BRK-B: "Berkshire Hathaway Inc. New"). It is kept as a fallback because
    a truncated name is still worth more than a bare symbol, and because the two
    were identical on seven of the nine symbols probed, so which one answered would
    not be visible in the output. Unlike the price, the name is
    NOT tied to the timestamp: it does not go stale within a session, so an undated
    quote may still carry it.
    """
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise BarFetchError(f"{symbol} quote: response was not a chart payload")
    error = chart.get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else error
        raise BarFetchError(f"{symbol} quote: source reported {code!r}")
    results = chart.get("result") or []
    if not results:
        raise BarFetchError(f"{symbol} quote: response carried no result")

    meta = (results[0] or {}).get("meta") or {}

    def number(name: str) -> float | None:
        value = meta.get(name)
        return float(value) if isinstance(value, int | float) else None

    def text(*names: str) -> str | None:
        """The first of `names` holding a non-empty string.

        A string check rather than a truthiness one, because the source has been
        seen to answer numbers where a name was expected on other keys, and a
        stringified float under a company-name label is the defect shape this
        project hunts.
        """
        for name in names:
            value = meta.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    stamp = meta.get("regularMarketTime")
    at = int(stamp) if isinstance(stamp, int | float) else None
    price = number("regularMarketPrice")
    return Quote(
        symbol=symbol,
        # Undated means unusable. See the docstring.
        price=price if at is not None else None,
        at=at,
        previous_close=number("chartPreviousClose"),
        currency=meta.get("currency") if isinstance(meta.get("currency"), str)
                 else None,
        name=text("longName", "shortName"),
    )


def parse_chart(payload: Any, *, symbol: str, bar_size: str) -> list[Bar]:
    """Read a chart response into bars. Separate from the fetch so the suite
    can exercise it against a captured fixture and never touch the network.
    """
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        raise BarFetchError(f"{symbol} {bar_size}: response was not a chart payload")
    error = chart.get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else error
        raise BarFetchError(f"{symbol} {bar_size}: source reported {code!r}")
    results = chart.get("result") or []
    if not results:
        raise BarFetchError(f"{symbol} {bar_size}: response carried no result")

    result = results[0] or {}
    stamps = result.get("timestamp") or []
    if not stamps:
        return []

    quotes = (result.get("indicators") or {}).get("quote") or []
    quote = quotes[0] if quotes and isinstance(quotes[0], dict) else {}

    def series(name: str) -> list[Any]:
        """One OHLCV column, padded to the timestamp count.

        A short column would otherwise raise IndexError on a response that is
        merely incomplete, which is a gap rather than a fault.
        """
        values = list(quote.get(name) or [])
        if len(values) < len(stamps):
            values += [None] * (len(stamps) - len(values))
        return values

    opens, highs, lows, closes, volumes = (
        series(name) for name in ("open", "high", "low", "close", "volume")
    )
    bars = [
        Bar(
            ts=int(stamp),
            open=_number(opens[index]),
            high=_number(highs[index]),
            low=_number(lows[index]),
            close=_number(closes[index]),
            volume=_whole(volumes[index]),
        )
        for index, stamp in enumerate(stamps)
        if stamp is not None
    ]
    return _on_grid(sorted(bars, key=lambda bar: bar.ts), bar_size)


#: Seconds per intraday bar. Daily bars are deliberately absent: a daily bar for
#: a session in progress is legitimately incomplete and the chart draws it as
#: "where it is now", so grid-filtering it would delete the live point.
_INTRADAY_SECONDS = {"1h": 3600}


def _on_grid(bars: list[Bar], bar_size: str) -> list[Bar]:
    """Intraday bars aligned to the series' own grid, dropping the live stub.

    The source appends a synthetic bar for the moment you asked, stamped at that
    moment rather than on the grid: a 13:17 request returns 09:00, 10:00, 11:00,
    12:00 and then 12:35. Its timestamp is unique per request, so it does not
    upsert over anything -- each poll deposits a fresh phantom bar. Six such rows
    were already in this journal from two backfills during one session, and
    polling hourly through a session would have added seven a day per contract.

    Anchored on the FIRST bar's phase rather than the modal phase or a clock.
    The first bar of a window is always a real session bar, whereas a modal vote
    is ambiguous on a two-bar series and a clock comparison would make the parser
    depend on when it ran -- untestable against a fixture, which is the whole
    reason this function lives beside the reader rather than in the fetch.

    One real bar is dropped by this: the underlying's post-close 16:00 print,
    which is half an hour off a 09:30 grid. That costs nothing, because the
    session's close is what the DAILY series carries -- the hourly series exists
    to show movement WITHIN a session, not to restate its close.
    """
    seconds = _INTRADAY_SECONDS.get(bar_size)
    if seconds is None or not bars:
        return bars
    phase = bars[0].ts % seconds
    return [bar for bar in bars if bar.ts % seconds == phase]


def _number(value: Any) -> float | None:
    """A float, or ``None``. Null prices stay null -- see the module docstring."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _whole(value: Any) -> int | None:
    """An int, or ``None``. Volume is absent on a session with no print."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
