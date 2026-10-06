"""Serializer arithmetic that a hand-seeded journal can settle on its own.

Most of `serialize.py` is exercised through `web.build_state` in
`tests/test_web.py`, against the real archive -- which is the right way round for
a payload contract, and which means those tests SKIP wherever the archive is not
present (a fresh clone, a git worktree, every `optjournal mutate` run). So a rule
that lives entirely inside one serializer, over tables a test can seed, belongs
here: it then runs everywhere the suite does, including inside the mutation
harness where a payload guard is skipped.

`watchlist_data` is the first such rule. It reads bars by SYMBOL, and `price_bars`
is keyed on conid, so what it gets back is rows where it wants sessions.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import connect_migrated

from optjournal.bars import upsert_bars
from optjournal.clock import MARKET_TZ
from optjournal.marketdata import Bar
from optjournal.serialize import allocation_data, portfolio_data, watchlist_data
from optjournal.trend import bucket, bxtrender_short
from optjournal.vol import rank, rank_band, realised_vol, realised_vol_series


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    return connect_migrated(tmp_path / "serialize.db")


def _sessions(count: int, *, ending: date = date(2026, 8, 7)) -> list[str]:
    """`count` ET weekdays, oldest first, ending on `ending`.

    Weekends are skipped so the series is a plausible run of sessions rather than
    a calendar. Nothing in `watch_closes` knows about holidays -- the sessions
    present in the data are the definition, which is the same principle the
    perishable audit's holiday oracle uses.
    """
    days: list[str] = []
    day = ending
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(days))


def _bar(day: str, hour: int, close: float) -> Bar:
    stamp = int(
        datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=MARKET_TZ)
        .timestamp()
    )
    return Bar(ts=stamp, open=close, high=close, low=close, close=close, volume=1)


def _seed_watched_symbol(
    conn: sqlite3.Connection, symbol: str, days: list[str], prices: list[float],
    *, duplicated: bool = True,
) -> None:
    """One watched symbol whose sessions are stored under two conids.

    The real shape rather than an invented one: a symbol that is watched AND
    traded has its closes written under the synthetic `watch:SYMBOL` key by the
    watch window and under its real underlying conid by the position windows,
    stamped at different hours of the same ET session.
    """
    conn.execute(
        "INSERT INTO watchlist (symbol, note, added_at) VALUES (?, NULL, ?)",
        (symbol, days[0]),
    )
    conn.commit()
    upsert_bars(
        conn, conid=f"watch:{symbol}", symbol=symbol, bar_size="1d", source="yahoo",
        bars=[_bar(d, 0, p) for d, p in zip(days, prices, strict=True)],
    )
    if duplicated:
        upsert_bars(
            conn, conid="4815747", symbol=symbol, bar_size="1d", source="yahoo",
            bars=[_bar(d, 9, p) for d, p in zip(days, prices, strict=True)],
        )


#: A path that alternates +2% / -2% every session, so a duplicated session is an
#: unmistakable zero-return day sitting between two real moves. Annualises to
#: about 31%, which is the order of magnitude a real watched name reports.
def _zigzag(count: int) -> list[float]:
    return [100.0 * (1.02 if i % 2 else 1.0) for i in range(count)]


def _wobble(count: int) -> list[float]:
    """A path whose VOLATILITY itself varies, oldest first.

    `_zigzag`'s constant step is right for the duplicate-session tests and useless
    for the rank: every 21-session window of it has the same standard deviation, so
    the year's low and high would be one number and the rank would correctly refuse
    to answer. Here the step size cycles slowly, which is what real realised vol
    does -- it drifts in regimes -- so the series of windows has a range for today's
    reading to sit inside.
    """
    prices: list[float] = []
    price = 100.0
    for index in range(count):
        prices.append(price)
        step = 0.004 + 0.016 * (1 + math.sin(index / 17.0)) / 2
        price *= math.exp(step if index % 2 == 0 else -step)
    return prices


def test_a_duplicated_session_no_longer_halves_the_vol(conn):
    """End to end: the payload's realised vol counts sessions, not rows.

    This is the measured defect. NVDA's closes were stored under two conids, so
    the serializer's own `ORDER BY ts DESC LIMIT 21` returned 21 rows spanning 12
    sessions with a zero-return day between every real one, and realised vol read
    30.15% where the 21 real sessions say 40.71%. It always understates, because
    a duplicate can only add a zero to the returns.

    Both readings are computed here from the same seeded prices -- the honest one
    over deduplicated sessions and the old one over the raw rows -- so the test
    fails whichever way the reader stops collapsing.
    """
    days = _sessions(30)
    prices = _zigzag(30)
    _seed_watched_symbol(conn, "NVDA", days, prices)

    row = watchlist_data(conn)[0]

    newest_first = list(reversed(prices))
    assert row["realised_vol"] == pytest.approx(realised_vol(newest_first[:21]))
    assert row["closes"] == 30, (
        "`closes` is what the page prints to explain a missing figure, so it has "
        "to count the same thing the figure was computed over: sessions"
    )
    assert row["closes_through"] == days[-1]

    # The old reading, over the rows a symbol-keyed LIMIT actually returns: each
    # session twice, so half the returns are zero.
    duplicated_rows = [price for price in newest_first for _ in (0, 1)][:21]
    assert row["realised_vol"] != pytest.approx(realised_vol(duplicated_rows)), (
        "the vol is still being computed over duplicated rows"
    )
    assert row["change_1d"] != pytest.approx(0.0), (
        "a 1d change of exactly zero is the duplicate showing through: the two "
        "newest ROWS are one session"
    )
    assert row["last"] == pytest.approx(prices[-1])


def test_widening_the_read_does_not_widen_the_vol_window(conn):
    """`history` is how much is READ; `sessions` is what the vol is computed over.

    They were one parameter called `lookback`, which is the shape that lets one
    change do two things: fetching a year of closes for an indicator would have
    silently moved a figure the README's whole watchlist story is built on, and
    nothing in the suite would have reported it as a change.

    Asserted against a 60-session series whose two halves have deliberately
    different volatility, so a 21-session vol and a 60-session vol cannot
    coincide.
    """
    days = _sessions(60)
    quiet = [100.0 * (1.002 if i % 2 else 1.0) for i in range(39)]
    lively = [100.0 * (1.03 if i % 2 else 1.0) for i in range(21)]
    _seed_watched_symbol(conn, "NVDA", days, quiet + lively, duplicated=False)

    row = watchlist_data(conn)[0]
    newest_first = list(reversed(quiet + lively))

    assert row["closes"] == 60, "the read is wide; that is what `history` is for"
    assert row["realised_vol"] == pytest.approx(realised_vol(newest_first[:21]))
    assert row["realised_vol"] != pytest.approx(realised_vol(newest_first)), (
        "the vol widened with the read -- the two windows are one again"
    )


# --------------------------------------------------------- the derived figures
#
# Four figures with four gates, and the interesting assertion is the same one in
# both directions: a symbol past its gates carries numbers, and a symbol short of
# them carries None BESIDE THE COUNT that says why. A zero here would be a claim --
# a neutral oscillator reading, or a vol sitting at the quiet end of its year -- so
# the two cases are what the panel's dash-with-a-reason rests on.


def test_a_settled_symbol_carries_every_derived_figure(conn):
    """200 sessions: past `trend.MIN_SETTLED` and past `vol.RANK_MIN_WINDOWS`.

    The figures are checked against the leaves' own functions over the same closes
    rather than against transcribed numbers, so this test says "the serializer
    composes these correctly" and leaves "is the arithmetic right" to
    `test_trend.py`'s hand-computed vector and `test_vol.py`'s hand-computed rank.
    That split matters: a transcribed figure here would have to be rewritten on any
    honest change to a leaf, and would be rewritten from the output.

    The seeded path alternates, so realised vol moves window to window and the rank
    has a range to sit inside -- a straight line would be the degenerate year, which
    has its own test.
    """
    days = _sessions(200)
    prices = _wobble(200)
    _seed_watched_symbol(conn, "NVDA", days, prices, duplicated=False)

    row = watchlist_data(conn)[0]
    newest_first = list(reversed(prices))

    assert row["closes"] == 200
    assert row["bx_daily"] == pytest.approx(bxtrender_short(newest_first))
    assert row["bx_daily_delta"] == pytest.approx(
        bxtrender_short(newest_first) - bxtrender_short(newest_first[1:])
    ), "the delta is the same arm one session back, not a change in the price"
    assert row["bx_bucket"] == bucket(row["bx_daily"])
    assert row["bx_bucket"] in ("low", "mid", "high")

    # The weekly arm over the same sessions: 200 sessions is ~40 ISO weeks, which is
    # well short of the gate, so the value is absent and the WEEK is still named.
    assert row["weeks"] == 40
    assert row["bx_weekly"] is None
    assert row["bx_weekly_week"] == "2026-W32"
    assert row["bx_weekly_sessions"] == 5

    series = realised_vol_series(newest_first, window=21)
    assert row["rv_rank_windows"] == len(series) == 180
    assert row["rv_rank"] == pytest.approx(rank(series))
    assert row["rv_rank_low"] == pytest.approx(min(series))
    assert row["rv_rank_high"] == pytest.approx(max(series))
    assert row["rv_rank_low"] < row["rv_rank_high"], (
        "a rank needs a range; identical bounds are the degenerate case"
    )
    assert row["rv_rank_band"] == rank_band(row["rv_rank"])
    assert 0.0 <= row["rv_rank"] <= 100.0


def test_a_thin_symbol_reports_none_with_the_count_that_says_why(conn):
    """45 sessions: the honest state of five of the six real watched symbols today.

    Every derived figure is None and every count is a real number, which is the pair
    the surfaces need -- a dash alone cannot say whether a symbol is new, thin or
    quiet. The realised vol itself DOES answer at 45 sessions (its window is 21), and
    that asymmetry is the point of separate gates: the row is not uniformly empty,
    it is empty exactly where the window is not full.
    """
    days = _sessions(45)
    _seed_watched_symbol(conn, "GOOG", days, _wobble(45), duplicated=False)

    row = watchlist_data(conn)[0]

    assert row["closes"] == 45
    assert row["realised_vol"] is not None, "21 sessions is enough for the vol"
    for key in (
        "bx_daily", "bx_daily_delta", "bx_bucket", "bx_weekly",
        "rv_rank", "rv_rank_low", "rv_rank_high", "rv_rank_band",
    ):
        assert row[key] is None, f"{key} answered from inside its own warm-up window"
    # The counts that turn each dash into a sentence.
    assert (row["weeks"], row["rv_rank_windows"]) == (9, 25)
    assert row["bx_weekly_week"] == "2026-W32", (
        "the newest week is a fact about the stored series, like closes_through, so "
        "it answers even where the weekly figure cannot"
    )


# ------------------------------------------------------- the one typed figure
#
# `earnings_on` is the only cell on this tab whose input is the reader, and
# `earnings_in_days` is the only figure derived from something other than a close.
# It is derived rather than stored precisely so it cannot drift from the date beside
# it, which is what these assert: the same stored date reads differently on two
# different days, and nothing in the table changed.


def _watch(conn: sqlite3.Connection, symbol: str, earnings: str | None) -> None:
    """One watched row with a typed earnings date and no bars at all.

    No bars on purpose: the countdown has nothing to do with stored closes, and a
    seeded price series would only make the test slower and its subject less clear.
    """
    conn.execute(
        "INSERT INTO watchlist (symbol, note, earnings_on, added_at)"
        " VALUES (?, NULL, ?, '2026-08-01')",
        (symbol, earnings),
    )
    conn.commit()


def _at(day: str) -> datetime:
    """Midday ET on `day`, as an aware datetime.

    Midday rather than midnight because the ET day is what the countdown is stated
    against: a UTC midnight instant belongs to the PREVIOUS ET day, so a fixture
    stamped at 00:00 would be asserting the timezone conversion by accident.
    """
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=MARKET_TZ)


def test_the_countdown_is_derived_from_the_date_and_the_et_day(conn):
    """One stored date, four different ET days, four different countdowns.

    The date is never touched; only the day it is counted from moves. That is the
    whole argument for deriving it: a stored "14 days" is wrong tomorrow, silently,
    while the date it was computed from still reads correctly beside it.

    The PAST case is the one with a decision in it. It reports a NEGATIVE count
    rather than being clamped or dropped, because a recorded date stands until the
    reader records the next one -- clamping to 0 would claim the company reports
    today, and dropping the date would delete what the reader typed. The surfaces
    turn the negative into "recorded, now past"; the payload carries the number.
    """
    _watch(conn, "DELL", "2026-08-27")

    def days(on: str) -> int | None:
        return watchlist_data(conn, now=_at(on))[0]["earnings_in_days"]

    assert days("2026-08-13") == 14
    assert days("2026-08-26") == 1
    assert days("2026-08-27") == 0, "the day itself is zero, not one"
    assert days("2026-09-03") == -7, (
        "a date that has gone by must report a negative count: it is what the "
        "reader typed, and only they can replace it"
    )
    # The date itself is verbatim on every one of those days.
    assert watchlist_data(conn, now=_at("2026-09-03"))[0]["earnings_on"] == "2026-08-27"


def test_no_recorded_date_means_no_countdown_rather_than_a_zero(conn):
    """Null in, null out -- and 0 would read as "reports today".

    The normal state of this column for a long time: it is typed, so most rows carry
    nothing. A zero here is the same defect class as a 0.0 realised vol on a symbol
    added yesterday, except worse -- it would put an earnings date on every symbol
    the reader has not touched.
    """
    _watch(conn, "PLTR", None)
    row = watchlist_data(conn, now=_at("2026-08-13"))[0]
    assert row["earnings_on"] is None
    assert row["earnings_in_days"] is None


def test_a_stored_value_that_is_not_a_day_reports_no_countdown(conn):
    """A hand-edited journal cannot take the payload down with it.

    Both writers validate through `clock.parse_day`, so this is reachable only by
    editing the SQLite file -- which is a thing a single-user local journal invites.
    A raise here would blank every tab on the page rather than one cell, so the
    countdown withholds and the date is still carried verbatim for the one person who
    can correct it.
    """
    _watch(conn, "COHR", "next thursday")
    row = watchlist_data(conn, now=_at("2026-08-13"))[0]
    assert row["earnings_on"] == "next thursday"
    assert row["earnings_in_days"] is None


def test_the_countdown_is_counted_from_the_et_day_not_the_utc_one(conn):
    """The tab is stated in one clock, and it is the market's.

    20:00 ET on the 13th is 00:00 UTC on the 14th, so a countdown taken off a UTC
    date is a day short for four or five hours every evening -- the same class of
    defect `clock.et_day` exists for, where two daily series stamped 04:00Z and
    13:30Z had to be joined on the trading day rather than the timestamp.
    """
    _watch(conn, "DELL", "2026-08-27")
    evening = datetime.strptime("2026-08-13", "%Y-%m-%d").replace(
        hour=20, tzinfo=MARKET_TZ)
    assert evening.astimezone(UTC).date().isoformat() == "2026-08-14", (
        "the fixture is meant to sit on an instant where the two calendars disagree"
    )
    assert watchlist_data(conn, now=evening)[0]["earnings_in_days"] == 14


def test_a_symbol_with_no_bars_reports_nothing_rather_than_zero(conn):
    """A watched row with no stored close is a row of Nones, never of zeroes.

    `closes_through` is the one added by this change and it carries the same rule:
    an em dash the page can explain, not a date it invented. A zero vol is a claim
    that a stock never moved, and a symbol added this morning has made no claim.
    """
    conn.execute(
        "INSERT INTO watchlist (symbol, note, added_at) VALUES ('PLTR', NULL, ?)",
        ("2026-08-07",),
    )
    conn.commit()

    row = watchlist_data(conn)[0]
    assert row["closes"] == 0
    assert row["closes_through"] is None
    assert row["last"] is None
    assert row["realised_vol"] is None
    assert row["change_1d"] is None


def _seed_index(conn, symbol: str, day: str, close: float) -> None:
    """One daily index close under the symbol's own conid, as the manifest stores it.

    `^GSPC` and `^VIX` are not contracts, so the symbol IS the conid -- see
    `bars.CONTEXT_SYMBOLS`. Daily, because that is what the calculator reads and what
    the manifest fetches for them.
    """
    upsert_bars(conn, conid=symbol, symbol=symbol, bar_size="1d", source="yahoo",
                bars=[_bar(day, 16, close)])


def test_the_calculator_pairs_the_latest_index_closes_with_the_days_events(conn):
    """`odte_context_data` reads the newest S&P and VIX closes, and nothing else.

    The whole reading in one assertion: the two levels come back as stored and the
    dates ride along so a reader can see which session each figure is from. NO
    derived level is here to check -- the strike ladder is built in
    `static/zdte.js` from a reading the tab lets you retype, and is checked
    against the reference implementation's own screen in
    tests/frontend/zdte.test.mjs.
    """
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-08-27", 6000.0)
    _seed_index(conn, "^GSPC", "2026-08-28", 6120.0)   # newer, so this one wins
    _seed_index(conn, "^VIX", "2026-08-28", 16.0)
    now = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)

    ctx = odte_context_data(conn, now=now)
    assert ctx is not None
    assert ctx["spx_prev_close"] == 6120.0, "the newest close is the reading"
    assert ctx["vix"] == 16.0
    assert ctx["spx_date"] == "2026-08-28" and ctx["vix_date"] == "2026-08-28"
    assert set(ctx) == {
        "spx_prev_close", "spx_date", "vix", "vix_date", "events_today", "today",
        "spx_fetched_at", "vix_fetched_at", "live", "fresh", "stale_reason",
    }, "a derived level in the payload is a second copy of the ladder"


@pytest.mark.parametrize("spx_close, vix_close", [
    (0.0, 16.0),      # a bad index row
    (-1.0, 16.0),     # a bad index row
    (6000.0, -1.0),   # an impossible VIX
])
def test_an_unusable_index_row_is_an_absence(conn, spx_close, vix_close):
    """A reading the calculator cannot draw from is None, not a ladder of zeros.

    The same bound `static/zdte.js` holds against a typed reading, kept here too
    because the two inputs arrive by different routes and only one of them passes
    through the page.
    """
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-08-28", spx_close)
    _seed_index(conn, "^VIX", "2026-08-28", vix_close)
    assert odte_context_data(conn, now=datetime(2026, 8, 31, 13, 0, tzinfo=UTC)) is None


def test_a_zero_vix_is_a_reading_not_an_absence(conn):
    """Zero means "no expected move", which is a figure; the feed missing is not.

    `>= 0` for the VIX and `> 0` for the close, and the asymmetry is the point.
    """
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-08-28", 6120.0)
    _seed_index(conn, "^VIX", "2026-08-28", 0.0)
    ctx = odte_context_data(conn, now=datetime(2026, 8, 31, 13, 0, tzinfo=UTC))
    assert ctx is not None and ctx["vix"] == 0.0


def test_the_calculator_is_absent_until_both_feeds_have_landed(conn):
    """One index without the other is not half a reading, it is none.

    A ladder needs the S&P close and the VIX together, so a fetch that got one and
    not the other is an absence the tab renders as "run bars", not a partial
    reading that implies a range it cannot compute.
    """
    from optjournal.serialize import odte_context_data
    now = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)
    assert odte_context_data(conn, now=now) is None, "nothing fetched yet"

    _seed_index(conn, "^GSPC", "2026-08-28", 6120.0)
    assert odte_context_data(conn, now=now) is None, "S&P alone is not enough"

    _seed_index(conn, "^VIX", "2026-08-28", 16.0)
    assert odte_context_data(conn, now=now) is not None, "both present now"


def test_the_prior_close_is_the_last_settled_session_not_todays_moving_bar(conn):
    """"Prior close" must exclude today, and on a trading day that is the bug.

    The newest daily bar during a session is TODAY's, still moving. Printing it
    as the prior close labels a live quote as a settled one AND shifts every rail
    under the reader mid-session, which is the opposite of what a pre-open
    calculator is for. Caught by comparing against the reference implementation,
    which read Friday's 7711.76 while the newest row here held Monday's 7686.14.

    The VIX is deliberately the newest row: the plan wants CURRENT volatility
    against the prior close, so the two dates differ during a session by design.
    """
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-08-28", 7711.76)   # Friday, settled
    _seed_index(conn, "^GSPC", "2026-08-31", 7686.14)   # Monday, in progress
    _seed_index(conn, "^VIX", "2026-08-28", 14.43)
    _seed_index(conn, "^VIX", "2026-08-31", 15.25)
    now = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)      # Monday, mid-session ET

    ctx = odte_context_data(conn, now=now)
    assert ctx["spx_prev_close"] == 7711.76, "Monday's moving bar is not a close"
    assert ctx["spx_date"] == "2026-08-28"
    assert ctx["vix"] == 15.25, "the VIX is the live level, so today's row stands"
    assert ctx["vix_date"] == "2026-08-31"
    # That the rails then follow the settled close is checked where they are built,
    # in tests/frontend/zdte.test.mjs -- against this same session's reference
    # figures (7638.26 and 7785.26 around this close).


def test_before_the_open_the_newest_row_is_itself_the_prior_close(conn):
    """On a weekend or pre-open, every stored row predates today.

    The exclusion must not empty the series in that case -- it is the ordinary
    state for a calculator read on Sunday evening, and the newest row genuinely IS
    the last completed session.
    """
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-08-28", 7711.76)
    _seed_index(conn, "^VIX", "2026-08-28", 14.43)
    ctx = odte_context_data(conn, now=datetime(2026, 8, 30, 18, 0, tzinfo=UTC))
    assert ctx is not None and ctx["spx_prev_close"] == 7711.76
    assert ctx["spx_date"] == "2026-08-28"


def test_scoring_pairs_each_settled_session_with_the_close_before_it(conn):
    """`odte_scoring_data` walks the stored series into band-and-outcome pairs.

    The whole point of the function in one assertion: a session's row carries the
    close a band would have been drawn FROM, the VIX it would have been drawn AT
    (the prior session's, the newest settled reading before the open), and the close
    the index actually printed. NO level is here, for the same reason
    `odte_context_data` carries none -- the rails are `railScores`' to draw from the
    ladder's own `RAIL_PCTS`, and a stored rail is one that can disagree with the
    drawn one.

    This is the half the reference implementation keeps a whole table for
    (`zdte_snapshots`, ten derived columns a session) and can only fill going
    forward. Here it falls out of `price_bars`, which already holds both series.
    """
    from optjournal.serialize import odte_scoring_data
    for day, spx, vix in [
        ("2026-08-26", 7600.0, 15.0),
        ("2026-08-27", 7650.0, 16.0),
        ("2026-08-28", 7711.76, 14.43),
    ]:
        _seed_index(conn, "^GSPC", day, spx)
        _seed_index(conn, "^VIX", day, vix)

    rows = odte_scoring_data(conn, now=datetime(2026, 8, 31, 13, 0, tzinfo=UTC))
    assert rows == [
        {"date": "2026-08-27", "prev_close": 7600.0, "vix": 15.0, "close": 7650.0},
        {"date": "2026-08-28", "prev_close": 7650.0, "vix": 16.0, "close": 7711.76},
    ], "each row is the band's inputs and the outcome, oldest first"
    # The first stored session is not scorable and must not appear: there is no
    # close before it to have drawn a band from.


def test_scoring_excludes_today_because_its_close_is_still_moving(conn):
    """A band scored against an unsettled price reports a hit the afternoon revokes.

    The same exclusion `odte_context_data` makes for the prior close, needed here
    for the outcome instead of the input.
    """
    from optjournal.serialize import odte_scoring_data
    _seed_index(conn, "^GSPC", "2026-08-28", 7711.76)
    _seed_index(conn, "^VIX", "2026-08-28", 14.43)
    _seed_index(conn, "^GSPC", "2026-08-31", 7686.14)   # today, in progress
    _seed_index(conn, "^VIX", "2026-08-31", 15.25)
    now = datetime(2026, 8, 31, 17, 0, tzinfo=UTC)      # Monday, mid-session ET

    assert odte_scoring_data(conn, now=now) == [], "today is not a settled outcome"


def test_scoring_skips_a_session_whose_vix_never_landed(conn):
    """A missing reading is an absence, not a band that broke.

    Counting it would make every rail's hit rate read worse than the history it is
    supposed to be measuring, which is the one failure a score must not have.
    """
    from optjournal.serialize import odte_scoring_data
    for day, spx in [("2026-08-26", 7600.0), ("2026-08-27", 7650.0),
                     ("2026-08-28", 7711.76)]:
        _seed_index(conn, "^GSPC", day, spx)
    _seed_index(conn, "^VIX", "2026-08-26", 15.0)
    # Nothing for the 27th, so the session it would have drawn the 28th's band from
    # is unusable.
    rows = odte_scoring_data(conn, now=datetime(2026, 8, 31, 13, 0, tzinfo=UTC))
    assert [row["date"] for row in rows] == ["2026-08-27"]


def test_scoring_skips_a_pair_the_stored_series_has_a_hole_between(conn):
    """Two stored sessions are only a band and its outcome if they are ADJACENT.

    `price_bars` keeps the index series from a 60-day window, so a journal left
    unopened for longer than that stores June and then August. Pairing across the
    hole scored an eight-week move against a one-session band and booked it as a
    broken rail. A single weekday between two sessions is still adjacent, because
    that is what an exchange holiday looks like: the real series runs 2026-09-04
    to 2026-09-08 across Labor Day.
    """
    from optjournal.serialize import odte_scoring_data
    for day, spx in [("2026-06-01", 7000.0), ("2026-06-02", 7010.0),
                     ("2026-08-03", 7600.0), ("2026-08-04", 7605.0),
                     ("2026-09-04", 7700.0), ("2026-09-08", 7710.0)]:
        _seed_index(conn, "^GSPC", day, spx)
        _seed_index(conn, "^VIX", day, 16.0)
    rows = odte_scoring_data(conn, now=datetime(2026, 9, 10, 13, 0, tzinfo=UTC))
    assert [row["date"] for row in rows] == ["2026-06-02", "2026-08-04", "2026-09-08"], (
        "the session after the hole was scored against a close from before it, or "
        "the holiday weekend was mistaken for a hole"
    )


def test_scoring_is_empty_until_both_series_have_landed(conn):
    """One index without the other scores nothing, the same as it reads nothing."""
    from optjournal.serialize import odte_scoring_data
    now = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)
    assert odte_scoring_data(conn, now=now) == []
    _seed_index(conn, "^GSPC", "2026-08-27", 7650.0)
    _seed_index(conn, "^GSPC", "2026-08-28", 7711.76)
    assert odte_scoring_data(conn, now=now) == [], "the S&P alone scores nothing"


# ------------------------------------------------------------------ allocation


def _alloc_fixture(conn):
    conn.execute(
        "INSERT INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES ('t.xml','x','U1','20260901','20260924','EUR','ALL','now')")

    def snap(day, conid, symbol, under, cat, value, rate):
        conn.execute(
            "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
            " underlying_symbol, asset_category, position, position_value,"
            " currency, fx_rate_to_base, raw, source_file, ingested_at)"
            " VALUES (?,?,'U1',?,?,?,1,?,'USD',?,'{}','t.xml','now')",
            (day, conid, symbol, under, cat, value, rate))

    snap("20260924", "1", "TSLA", None, "STK", 1000.0, 0.5)
    snap("20260924", "2", "TSLA  260918P00300000", "TSLA", "OPT", -100.0, 0.5)
    snap("20260924", "3", "MRVL  260918P00070000", "MRVL", "OPT", -40.0, 0.5)
    # An older option snapshot that must not be read: only the book's newest
    # date counts.
    snap("20260901", "4", "OLD", "OLD", "OPT", -999.0, 0.5)
    conn.execute(
        "INSERT INTO equity_summaries (report_date, account_id, currency,"
        " cash_base, stock_base, options_base, total_base, raw, source_file,"
        " ingested_at) VALUES ('20260924','U1','EUR',80,500,-70,510,'{}','t.xml','now')")
    conn.commit()
    return snap


def test_allocation_folds_a_stock_and_its_options_into_one_holding(tmp_path):
    """TSLA shares and a TSLA put are one line, read from the newest snapshot,
    and rows plus cash sum to the broker's net liquidation."""
    conn = connect_migrated(tmp_path / "journal.db")
    _alloc_fixture(conn)
    al = allocation_data(conn)
    by = {r["holding"]: r for r in al["rows"]}
    assert set(by) == {"TSLA", "MRVL"}, "a stale snapshot date was read"
    assert (by["TSLA"]["stock"], by["TSLA"]["options"], by["TSLA"]["net"]) == (
        500.0, -50.0, 450.0)
    assert by["MRVL"]["share"] == pytest.approx(-20 / 510)
    assert sum(r["net"] for r in al["rows"]) + al["cash"] == pytest.approx(al["nav"])
    assert [r["holding"] for r in al["rows"]] == ["TSLA", "MRVL"]


def test_an_option_book_gone_flat_leaves_no_options_in_the_allocation(tmp_path):
    """Every row of one statement carries the same reportDate, so options absent
    from the newest date were sold, not reported late. Each category from its own
    latest snapshot kept a sold put in the allocation (`pnl/s_empty_book.py`)."""
    conn = connect_migrated(tmp_path / "journal.db")
    snap = _alloc_fixture(conn)
    snap("20260925", "1", "TSLA", None, "STK", 1000.0, 0.5)
    conn.commit()
    al = allocation_data(conn)
    assert al["as_of"] == "20260925"
    assert [(r["holding"], r["stock"], r["options"]) for r in al["rows"]] == [
        ("TSLA", 500.0, 0.0)]


def test_allocation_drops_an_option_book_the_nav_prices_at_zero(tmp_path):
    """A journal ingested with `--assets OPT` holds no stock position row, so the
    day its options go flat has no position row at all and only the NAV says the
    book is empty. Requiring the stock figure to be zero too kept every sold
    option in the allocation, because this account does hold stock and its NAV
    prices it."""
    conn = connect_migrated(tmp_path / "journal.db")
    _alloc_fixture(conn)
    conn.execute("DELETE FROM position_snapshots WHERE asset_category = 'STK'")
    conn.execute(
        "INSERT INTO equity_summaries (report_date, account_id, currency,"
        " cash_base, stock_base, options_base, total_base, raw, source_file,"
        " ingested_at) VALUES ('20260925','U1','EUR',80,500,0,580,'{}','t.xml','now')")
    conn.commit()
    al = allocation_data(conn)
    assert al["as_of"] == "20260925"
    assert al["rows"] == []


def test_allocation_sums_every_accounts_net_liquidation(tmp_path):
    """Two accounts, each with its own NAV. Reading one row made the other
    account's holdings a share of a total that excluded them. Each account's
    newest summary counts, so an account whose statements lag still does."""
    conn = connect_migrated(tmp_path / "journal.db")
    _alloc_fixture(conn)
    conn.execute(
        "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
        " underlying_symbol, asset_category, position, position_value, currency,"
        " fx_rate_to_base, raw, source_file, ingested_at) VALUES ('20260923','9',"
        " 'U2','MRVL','MRVL','STK',1,400.0,'USD',0.5,'{}','t.xml','now')")
    conn.execute(
        "INSERT INTO equity_summaries (report_date, account_id, currency,"
        " cash_base, stock_base, options_base, total_base, raw, source_file,"
        " ingested_at) VALUES ('20260923','U2','EUR',20,200,0,220,'{}','t.xml','now')")
    conn.commit()
    al = allocation_data(conn)
    assert (al["nav"], al["cash"], al["nav_date"]) == (730, 100, "20260924")
    by = {r["holding"]: r for r in al["rows"]}
    assert (by["MRVL"]["stock"], by["MRVL"]["options"]) == (200.0, -20.0)
    assert sum(r["net"] for r in al["rows"]) + al["cash"] == pytest.approx(al["nav"])


def test_allocation_without_a_net_liquidation_figure_has_no_shares(tmp_path):
    """A share of some other total would be a different number wearing the same
    label, so without an equity summary there are none."""
    conn = connect_migrated(tmp_path / "journal.db")
    _alloc_fixture(conn)
    conn.execute("DELETE FROM equity_summaries")
    al = allocation_data(conn)
    assert al["nav"] is None and al["cash"] is None
    assert all(r["share"] is None for r in al["rows"])


# ------------------------------------------------------------------ portfolio


def _portfolio_fixture(conn, *, navs, deposits=(), broker="ibkr"):
    """Reported values per session and cash moved in, as the ingest stores them.

    `navs` is (YYYYMMDD, cash, stock, options, total); `deposits` is
    (YYYY-MM-DD, amount_base). Real IBKR shapes: summary dates are compact and a
    deposit's `date_time` carries a clock, so the reader's day normalisation is
    exercised rather than assumed.
    """
    conn.execute(
        "INSERT OR IGNORE INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES ('t.xml','x','U1','20250801','20260930','EUR','ALL','now')")
    for day, cash, stock, options, total in navs:
        conn.execute(
            "INSERT INTO equity_summaries (broker, report_date, account_id, currency,"
            " cash_base, stock_base, options_base, total_base, raw, source_file,"
            " ingested_at) VALUES (?,?,'U1','EUR',?,?,?,?,'{}','t.xml','now')",
            (broker, day, cash, stock, options, total))
    for i, (day, amount) in enumerate(deposits):
        conn.execute(
            "INSERT INTO cash_transactions (broker, transaction_id, account_id,"
            " date_time, type, amount, currency, fx_rate_to_base, amount_base, raw,"
            " source_file, first_seen_at) VALUES"
            " (?,?,'U1',?,'Deposits & Withdrawals',?,'EUR',1,?,'{}','t.xml','now')",
            (broker, f"{broker}-d{i}", f"{day} 00:00:00", amount, amount))
    conn.commit()


def test_portfolio_separates_what_was_earned_from_what_was_deposited(tmp_path):
    """A deposit raises net liquidation and the result by nothing.

    The whole reason the tab draws two lines: 1,000 grows to 1,600, and 500 of
    that was sent in, so the account earned 100 -- not 600.
    """
    conn = connect_migrated(tmp_path / "journal.db")
    _portfolio_fixture(conn, navs=[
        ("20260130", 100, 900, 0, 1000),
        ("20260227", 600, 950, 0, 1550),    # +500 deposited, +50 earned
        ("20260331", 600, 1000, 0, 1600),   # +50 earned
    ], deposits=[("2026-02-10", 500)])

    pf = portfolio_data(conn)
    assert pf["total"] == {
        "start": "2026-01-30", "start_nav": 1000, "end": "2026-03-31",
        "nav": 1600, "deposits": 500, "put_in": 1500, "gain": 100,
    }
    assert [(d["day"], d["put_in"]) for d in pf["days"]] == [
        ("2026-01-30", 1000), ("2026-02-27", 1500), ("2026-03-31", 1500)]
    assert [(m["month"], m["deposits"], m["gain"]) for m in pf["months"]] == [
        ("2026-01", 0, 0), ("2026-02", 500, 50), ("2026-03", 0, 50)]
    assert sum(m["gain"] for m in pf["months"]) == pf["total"]["gain"], (
        "the months must account for the headline, or one of them is wrong")


def test_a_deposit_outside_the_reported_span_is_not_counted_twice(tmp_path):
    """One before the first session is already inside its value; one after the
    last has not reached any value yet. Counting either would put a gap between
    the lines that the account did not earn."""
    conn = connect_migrated(tmp_path / "journal.db")
    _portfolio_fixture(conn, navs=[
        ("20260130", 1000, 0, 0, 1000),
        ("20260227", 1000, 0, 0, 1000),
    ], deposits=[("2026-01-30", 999), ("2026-01-02", 999), ("2026-03-02", 999)])
    total = portfolio_data(conn)["total"]
    assert (total["deposits"], total["gain"]) == (0, 0)


def test_a_withdrawal_lowers_what_was_put_in(tmp_path):
    """Money taken out is the same flow with the other sign, not a loss."""
    conn = connect_migrated(tmp_path / "journal.db")
    _portfolio_fixture(conn, navs=[
        ("20260130", 1000, 0, 0, 1000),
        ("20260227", 700, 0, 0, 700),
    ], deposits=[("2026-02-05", -300)])
    total = portfolio_data(conn)["total"]
    assert (total["put_in"], total["gain"]) == (700, 0)


def test_the_monthly_split_sums_to_the_value_it_splits(tmp_path):
    """Short options are a negative share, and whatever the broker's total holds
    beyond the three named classes lands in `other` rather than going missing."""
    conn = connect_migrated(tmp_path / "journal.db")
    _portfolio_fixture(conn, navs=[("20260130", 200, 900, -150, 1000)])
    (month,) = portfolio_data(conn)["months"]
    assert (month["cash"], month["stock"], month["options"], month["other"]) == (
        200, 900, -150, 50)


def test_a_session_counts_only_where_every_broker_reported(tmp_path):
    """Summing a day one broker skipped prints the other account's value as the
    whole, and reads the missing half as a loss."""
    conn = connect_migrated(tmp_path / "journal.db")
    _portfolio_fixture(conn, navs=[
        ("20260130", 0, 1000, 0, 1000), ("20260227", 0, 1100, 0, 1100)])
    _portfolio_fixture(conn, broker="schwab", navs=[("20260227", 0, 400, 0, 400)])
    pf = portfolio_data(conn)
    assert [(d["day"], d["nav"]) for d in pf["days"]] == [("2026-02-27", 1500)]


def test_no_equity_summary_is_an_absence_not_a_zero_account(tmp_path):
    conn = connect_migrated(tmp_path / "journal.db")
    assert portfolio_data(conn) == {"days": [], "months": [], "total": None}


# ------------------------------------------------------------ journal review


def _card(anchor, pnl, status="closed"):
    return {"anchor": anchor, "status": status,
            "realized_pnl": {"base": pnl} if status == "closed" else None}


def test_the_review_counts_held_broken_and_silent_plans_apart():
    """Silence is not discipline: a card with only `na` answers, or none, is
    unreviewed rather than held. Either half answered `no` breaks the plan even
    when the other said `yes`. Open cards have no outcome and are left out."""
    from optjournal.serialize import journal_review  # noqa: PLC0415

    cards = [_card("1", 100.0), _card("2", -40.0), _card("3", 10.0),
             _card("4", 5.0), _card("5", 0.0, status="open")]
    entries = {
        "1": {"plan_target": "50%", "followed_target": "yes", "exit_trigger": "target"},
        "2": {"followed_target": "yes", "followed_invalidation": "no",
              "exit_trigger": "max_loss"},
        "3": {"followed_target": "na", "followed_invalidation": "na"},
        "5": {"followed_target": "no"},
    }
    rv = journal_review(cards, entries)
    assert (rv["closed"], rv["written"], rv["planned"], rv["reviewed"]) == (4, 3, 1, 3)
    assert rv["plan"]["held"] == {"count": 1, "wins": 1, "pnl": 100.0}
    assert rv["plan"]["broken"] == {"count": 1, "wins": 0, "pnl": -40.0}
    assert rv["plan"]["unreviewed"]["count"] == 2
    assert rv["adherence"]["target"] == {"yes": 2, "no": 0, "na": 1}


def test_the_review_lists_only_used_triggers_in_the_journals_order():
    from optjournal.journal import TRIGGERS  # noqa: PLC0415
    from optjournal.serialize import journal_review  # noqa: PLC0415

    cards = [_card("1", 1.0), _card("2", 2.0), _card("3", -3.0)]
    # `max_loss` sorts before `target` alphabetically and after it in TRIGGERS,
    # so this pins the journal's order rather than an accident of spelling.
    entries = {"1": {"exit_trigger": "max_loss"}, "2": {"exit_trigger": "target"},
               "3": {"exit_trigger": "max_loss"}}
    rv = journal_review(cards, entries)
    keys = [t["key"] for t in rv["triggers"]]
    assert keys == [k for k in TRIGGERS if k in {"max_loss", "target"}]
    assert keys == ["target", "max_loss"]
    stop = next(t for t in rv["triggers"] if t["key"] == "max_loss")
    assert (stop["count"], stop["wins"], stop["pnl"]) == (2, 1, -2.0)


# ------------------------------------------------------ 0DTE freshness

_ET = ZoneInfo("America/New_York")


def _fetched(conn, symbol: str, at: datetime) -> None:
    """Stamp every stored bar of one index as fetched at `at`."""
    conn.execute("UPDATE price_bars SET fetched_at = ? WHERE conid = ?",
                 (at.astimezone(UTC).isoformat(timespec="seconds"), symbol))
    conn.commit()


def test_the_last_settle_skips_the_weekend_and_waits_for_the_close():
    from optjournal.serialize import last_settle
    mon_10 = datetime(2026, 9, 28, 10, 0, tzinfo=_ET)
    assert last_settle(mon_10) == datetime(2026, 9, 25, 16, 15, tzinfo=_ET)
    mon_17 = datetime(2026, 9, 28, 17, 0, tzinfo=_ET)
    assert last_settle(mon_17) == datetime(2026, 9, 28, 16, 15, tzinfo=_ET)
    sat = datetime(2026, 9, 26, 12, 0, tzinfo=_ET)
    assert last_settle(sat) == datetime(2026, 9, 25, 16, 15, tzinfo=_ET)


def test_a_close_fetched_before_the_last_settle_is_stale(conn):
    """The case that shipped: Friday's fetch failed offline, and on Monday the
    calculator opened on Thursday's close with nothing on screen saying so."""
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-09-24", 7704.13)
    _seed_index(conn, "^VIX", "2026-09-25", 15.11)
    _fetched(conn, "^GSPC", datetime(2026, 9, 25, 7, 0, tzinfo=_ET))
    _fetched(conn, "^VIX", datetime(2026, 9, 25, 15, 0, tzinfo=_ET))
    ctx = odte_context_data(conn, now=datetime(2026, 9, 28, 10, 0, tzinfo=_ET))
    assert ctx is not None and ctx["fresh"] is False
    assert "S&P" in ctx["stale_reason"] and "Fri 25 Sep" in ctx["stale_reason"]
    # L46: the page prints this after "Not current, so not shown.", so it is a
    # sentence: "the S&P close…" there read as a typo.
    assert ctx["stale_reason"].startswith("The S&P close was last fetched")


def test_a_close_fetched_after_the_settle_with_a_recent_vix_is_fresh(conn):
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-09-25", 7710.0)
    _seed_index(conn, "^GSPC", "2026-09-28", 7690.0)   # today, still moving
    _seed_index(conn, "^VIX", "2026-09-28", 16.2)
    now = datetime(2026, 9, 28, 10, 0, tzinfo=_ET)
    _fetched(conn, "^GSPC", now - timedelta(minutes=1))
    _fetched(conn, "^VIX", now - timedelta(minutes=1))
    ctx = odte_context_data(conn, now=now)
    assert ctx["fresh"] is True and ctx["live"] is True
    assert (ctx["spx_date"], ctx["spx_prev_close"]) == ("2026-09-25", 7710.0), \
        "an open session's bar is not the prior close"


def test_a_live_vix_older_than_ten_minutes_in_session_is_stale(conn):
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-09-25", 7710.0)
    _seed_index(conn, "^VIX", "2026-09-28", 16.2)
    now = datetime(2026, 9, 28, 11, 0, tzinfo=_ET)
    _fetched(conn, "^GSPC", now - timedelta(minutes=1))
    _fetched(conn, "^VIX", now - timedelta(minutes=11))
    ctx = odte_context_data(conn, now=now)
    assert ctx["fresh"] is False and ctx["stale_reason"].startswith("The VIX was")


def test_after_the_settle_todays_close_is_the_prior_close(conn):
    """Planning tomorrow in the evening means today's settled close, not
    yesterday's."""
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-09-25", 7710.0)
    _seed_index(conn, "^GSPC", "2026-09-28", 7690.0)
    _seed_index(conn, "^VIX", "2026-09-28", 16.2)
    now = datetime(2026, 9, 28, 17, 0, tzinfo=_ET)
    _fetched(conn, "^GSPC", datetime(2026, 9, 28, 16, 30, tzinfo=_ET))
    _fetched(conn, "^VIX", datetime(2026, 9, 28, 16, 30, tzinfo=_ET))
    ctx = odte_context_data(conn, now=now)
    assert ctx["fresh"] is True and ctx["live"] is False
    assert (ctx["spx_date"], ctx["spx_prev_close"]) == ("2026-09-28", 7690.0)


@pytest.mark.parametrize("when, session", [
    (datetime(2026, 9, 28, 10, 0, tzinfo=_ET), "2026-09-28"),   # Monday, open
    (datetime(2026, 9, 28, 17, 0, tzinfo=_ET), "2026-09-29"),   # Monday, settled
    (datetime(2026, 9, 26, 12, 0, tzinfo=_ET), "2026-09-28"),   # Saturday
    (datetime(2026, 9, 25, 17, 0, tzinfo=_ET), "2026-09-28"),   # Friday, settled
])
def test_the_ladder_is_for_the_next_session_once_today_has_settled(conn, when, session):
    from optjournal.serialize import odte_context_data
    _seed_index(conn, "^GSPC", "2026-09-24", 7700.0)
    _seed_index(conn, "^VIX", "2026-09-24", 16.0)
    assert odte_context_data(conn, now=when)["today"] == session


def test_the_row_histogram_reads_each_session_as_it_closed(conn):
    """`bx_recent` is the daily arm at the close of each of the last five sessions,
    OLDEST FIRST: its first bar drops the four newest closes, its last is today's
    reading. Pinned against `trend` directly, because the newest bar and `bx_daily`
    share one source and cannot catch an order flip between themselves.
    """
    from optjournal.trend import bxtrender_short  # noqa: PLC0415
    days = _sessions(160)
    prices = _wobble(160)
    _seed_watched_symbol(conn, "WOB", days, prices, duplicated=False)
    row = watchlist_data(conn)[0]
    newest_first = list(reversed(prices))
    assert row["bx_recent"][0] == pytest.approx(bxtrender_short(newest_first[4:]))
    assert row["bx_recent"][-1] == pytest.approx(bxtrender_short(newest_first))
    assert row["bx_recent"][0] != pytest.approx(row["bx_recent"][-1])


# ---------------------------------------------------- earnings, typed and fetched


def _watch_earnings(conn, symbol: str, **cols) -> None:
    conn.execute("INSERT INTO watchlist (symbol, added_at) VALUES (?, '2026-01-01')",
                 (symbol,))
    if cols:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE watchlist SET {sets} WHERE symbol = ?",
                     (*cols.values(), symbol))
    conn.commit()


def test_a_typed_earnings_date_outranks_the_fetched_one(conn):
    """Yours is yours: the feed's date is stored beside it, never over it, and the
    countdown follows the one on screen."""
    _watch_earnings(conn, "AAA", earnings_on="2026-11-05",
                    earnings_next="2026-10-28", earnings_confirmed=0)
    row = watchlist_data(conn, now=datetime(2026, 10, 1, 16, 0, tzinfo=UTC))[0]
    assert (row["earnings_date"], row["earnings_source"]) == ("2026-11-05", "typed")
    assert row["earnings_in_days"] == 35, "the countdown follows the typed date"
    assert row["earnings_on"] == "2026-11-05"


@pytest.mark.parametrize("confirmed, source", [(1, "confirmed"), (0, "estimated")])
def test_the_fetched_date_says_whether_it_was_announced(conn, confirmed, source):
    """An estimate and an announced date are different claims, so the page is told
    which it is rather than being handed a bare date."""
    _watch_earnings(conn, "BBB", earnings_next="2026-10-28",
                    earnings_confirmed=confirmed, earnings_timing="after close")
    row = watchlist_data(conn, now=datetime(2026, 10, 1, 16, 0, tzinfo=UTC))[0]
    assert (row["earnings_date"], row["earnings_source"]) == ("2026-10-28", source)
    assert row["earnings_timing"] == "after close"
    assert row["earnings_in_days"] == 27


def test_no_date_anywhere_is_a_null_source(conn):
    """A fund. Absent rather than 'estimated for never'."""
    _watch_earnings(conn, "CCC")
    row = watchlist_data(conn, now=datetime(2026, 10, 1, 16, 0, tzinfo=UTC))[0]
    assert (row["earnings_date"], row["earnings_source"]) == (None, None)
    assert row["earnings_in_days"] is None


# ------------------------------------------------------------ journal orphans


def test_a_note_no_decision_claims_is_listed_rather_than_lost(tmp_path):
    """`journal.orphans` had no caller, so a note whose anchor stopped naming a
    campaign matched no card and vanished from the page without a word. The
    payload lists it; a note on a live decision, options or equities, is not,
    and `entries` holds what the cards show."""
    from conftest import add_statement

    from optjournal import journal
    from optjournal.serialize import journal_data

    conn = connect_migrated(tmp_path / "journal.db")
    add_statement(conn)
    for tid, order, cat, conid in (("1", "100", "OPT", "C1"), ("2", "200", "STK", "S1")):
        conn.execute(
            "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, ib_order_id,"
            " account_id, trade_date, date_time, asset_category, symbol, conid,"
            " underlying_symbol, open_close, quantity, currency, fx_rate_to_base,"
            " raw, source_file, first_seen_at) VALUES (?,?,?,?,'U1','2026-09-01',"
            " '2026-09-01 10:00:00',?,?,?,'SPY','O',1,'USD',1.0,'{}','t.xml','now')",
            (tid, tid, tid, order, cat, conid, conid),
        )
    for anchor in ("100", "200", "999"):
        journal.save(conn, anchor, account_id="U1", underlying_symbol="SPY",
                     opened_on="2026-09-01", values={"entry_note": f"note {anchor}"})
    data = journal_data(conn)
    assert [o["anchor"] for o in data["orphans"]] == ["999"]
    assert data["orphans"][0]["entry_note"] == "note 999"
    assert set(data["entries"]) == {"100", "200"}


def test_no_writing_means_no_orphan_check(conn):
    from optjournal.serialize import journal_data

    assert journal_data(conn)["orphans"] == []


# ---------------------------------------------------------------- collection status


def _scheduler(*, running: bool = True, ever_ran: bool = True,
               sync: dict | None = None, history: dict | None = None) -> dict:
    """The `scheduler` block `collection_status` reads: the heartbeat's two flags
    and each job's newest run."""
    jobs = [{"job": name, "last_run": run}
            for name, run in (("sync", sync), ("history", history)) if run is not None]
    return {"running": running, "ever_ran": ever_ran, "jobs": jobs}


def _failed_sync(exc: Exception) -> dict:
    """A failed sync run, with the detail the real job writes for `exc`."""
    from optjournal.jobs import sync_outcome
    return {"status": "failed", "detail": sync_outcome(exc).detail}


#: A Tuesday, so "behind" counts the weekdays since the newest statement.
_TODAY = date(2026, 10, 6)


@pytest.mark.parametrize(("kwargs", "state", "fix"), [
    ({"demo": True, "configured": False}, "demo", None),
    ({"configured": False}, "setup", "settings"),
    ({"sync": {"status": "running"}}, "running", None),
    ({"history": {"status": "running"}}, "running", None),
    ({"sync": "rejected"}, "attention", "settings"),
    ({"sync": "missing"}, "attention", "settings"),
    ({"running": False}, "stalled", None),
    ({"running": False, "ever_ran": False}, "off", None),
    ({"newest": None}, "waiting", None),
    ({"newest": date(2026, 9, 30)}, "behind", None),
    ({"newest": date(2026, 10, 2)}, "ok", None),
])
def test_collection_status_names_one_state(kwargs, state, fix):
    """Every state the header can show, from the inputs that produce it. Only
    setup and attention point at Settings: a failure that clears by itself is the
    schedule's to retry, and saying "fix this" over it would send the reader
    looking for something that is not broken."""
    from optjournal.flex import TokenMissing, TokenRejected
    from optjournal.serialize import collection_status
    made = {"rejected": _failed_sync(TokenRejected("IBKR error 1012")),
            "missing": _failed_sync(TokenMissing("no token for 'me'"))}
    sync = kwargs.get("sync")
    got = collection_status(
        _scheduler(running=kwargs.get("running", True),
                   ever_ran=kwargs.get("ever_ran", True),
                   sync=made.get(sync, sync) if isinstance(sync, str) else sync,
                   history=kwargs.get("history")),
        configured=kwargs.get("configured", True), demo=kwargs.get("demo", False),
        newest=kwargs.get("newest", date(2026, 10, 5)), today=_TODAY)
    assert (got["state"], got["fix"]) == (state, fix), got
    assert got["message"]


def test_a_journal_behind_says_why_and_that_it_keeps_trying():
    """Behind is the one state a transient failure reaches, and only once three
    weekdays have gone by. It names the last day the statements cover and what the
    last attempt said, so the reader can tell a holiday from IBKR being down."""
    from optjournal.flex import FlexUnreachable
    from optjournal.serialize import collection_status
    down = _failed_sync(FlexUnreachable("could not reach IBKR: timed out"))
    got = collection_status(_scheduler(sync=down), configured=True, demo=False,
                            newest=date(2026, 9, 30), today=_TODAY)
    assert got["state"] == "behind" and got["through"] == "2026-09-30"
    assert "keeps trying" in got["message"] and "timed out" in got["message"]
    assert got["fix"] is None, "a failure that clears by itself is not the reader's"
    # Two weekdays behind is not yet worth a banner: a morning before the sync,
    # or a holiday, looks exactly like this.
    calm = collection_status(_scheduler(sync=down), configured=True, demo=False,
                             newest=date(2026, 10, 1), today=_TODAY)
    assert calm["state"] == "ok"


def test_a_long_first_collection_reads_running_not_stalled():
    """The loop stamps its heartbeat once per tick, and a first collection (a
    year's statement, the prices, then four years of history) holds one tick for
    minutes. Read off the heartbeat alone, the page said "stalled, restart" in
    the middle of the import, and a reader who did as told interrupted it."""
    from optjournal.serialize import collection_status
    stale = {"running": False, "ever_ran": True,
             "jobs": [{"job": "sync", "last_run": {"status": "ok"}},
                      {"job": "bars_daily", "last_run": {"status": "running"}}]}
    got = collection_status(stale, configured=True, demo=False, newest=None, today=_TODAY)
    assert got["state"] == "running", got
    # And it says which: the import is the long one, worth naming.
    importing = collection_status(_scheduler(history={"status": "running"}),
                                  configured=True, demo=False, newest=_TODAY, today=_TODAY)
    assert "earlier years" in importing["message"], importing


@pytest.mark.parametrize(("detail", "state", "fix"), [
    # IBKR refused the query: no retry changes that, and the fix is in Settings.
    ("FlexError: Flex API Error 1014: Query is invalid.", "attention", "settings"),
    # Something else broke: said, but not sent to Settings for it.
    ("KeyError: 'FlexStatements'", "attention", None),
    # IBKR down at the first try: the schedule retries, so this still waits.
    ("FlexUnreachable (retried by itself): timed out after 30s", "waiting", None),
])
def test_a_first_sync_that_cannot_succeed_says_why(detail, state, fix):
    """A mistyped query id read "Waiting for the first statement" for ever, with
    no dot and no banner, while every run failed with IBKR's 1014."""
    from optjournal.serialize import collection_status
    got = collection_status(_scheduler(sync={"status": "failed", "detail": detail}),
                            configured=True, demo=False, newest=None, today=_TODAY)
    assert (got["state"], got["fix"]) == (state, fix), got
    if state == "attention":
        assert detail[:40] in got["message"]


def test_a_transient_detail_is_read_back_as_one():
    """`clears_by_itself` reads the ledger's detail, so it has to match what
    `sync_outcome` writes for exactly the failures it marks transient."""
    from optjournal.flex import FlexBusy, FlexUnreachable, TokenRejected
    from optjournal.jobs import clears_by_itself, sync_outcome
    for exc in (FlexUnreachable("timed out"), FlexBusy("generating", "1019")):
        out = sync_outcome(exc)
        assert out.transient and clears_by_itself(out.detail), out
    assert not clears_by_itself(sync_outcome(TokenRejected("1012")).detail)
    assert not clears_by_itself("FlexError: Flex API Error 1014: Query is invalid.")


def test_a_server_without_a_scheduler_is_off_not_stalled():
    """`serve --no-scheduler` on a journal a scheduler once fed: the old heartbeat
    is stale, and "stopped answering, restart" would be advice that changes
    nothing. The server knows it has none."""
    from optjournal.serialize import collection_status
    old = _scheduler(running=False, ever_ran=True)
    got = collection_status(old, configured=True, demo=False,
                            newest=date(2026, 10, 5), today=_TODAY, scheduled=False)
    assert got["state"] == "off", got
    assert collection_status(old, configured=True, demo=False, newest=date(2026, 10, 5),
                             today=_TODAY)["state"] == "stalled"


def test_an_old_credentials_row_gets_advice_that_fits_it():
    """Rows written before the class went into the detail read "credentials: IBKR
    says your Flex token is expired..." and got "No IBKR Flex token is stored",
    which is the wrong fix for an expired one."""
    from optjournal.jobs import needs_reader
    fix = needs_reader("credentials: IBKR says your Flex token is expired. Nothing...")
    assert fix and "stored" not in fix and "Settings" in fix


def test_a_weekend_does_not_count_toward_behind():
    """Friday's statement read on Monday is up to date, and so is Thursday's read
    on Tuesday: Saturday and Sunday have no session to report."""
    from optjournal.serialize import collection_status
    monday = collection_status(_scheduler(), configured=True, demo=False,
                               newest=date(2026, 10, 2), today=date(2026, 10, 5))
    assert monday["state"] == "ok" and monday["through"] == "2026-10-02"
    tuesday = collection_status(_scheduler(), configured=True, demo=False,
                                newest=date(2026, 10, 1), today=_TODAY)
    assert tuesday["state"] == "ok"
    # The edge: three weekdays missing (Wed..Fri), with the weekend between.
    late = collection_status(_scheduler(), configured=True, demo=False,
                             newest=date(2026, 9, 29), today=date(2026, 10, 5))
    assert late["state"] == "behind"
