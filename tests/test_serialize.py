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

import pytest
from conftest import connect_migrated

from optjournal.bars import upsert_bars
from optjournal.clock import MARKET_TZ
from optjournal.marketdata import Bar
from optjournal.serialize import watchlist_data
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
