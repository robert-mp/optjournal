"""The days ahead of ForexFactory's week, from Nasdaq: placed, graded, and replaced.

No test here touches the network. Nasdaq is a function from the `date` asked for
to a reply, built from rows captured from the live endpoint on 2026-10-15, and it
can be told to answer the way it does today (a day late, New York times) or the
way it would if Nasdaq fixed it.

The defects worth catching are the quiet ones: next week's events filed a day
early once Nasdaq fixes its dates, a release shown twice once ForexFactory serves
its week, a moved release left behind under its old time, and the headline US
prints hidden by the default filter because Nasdaq grades nothing.
"""

from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import connect_migrated

from optjournal import ahead, jobs
from optjournal.ahead import MEASURED, SOURCE, Placing, parse_day, refresh_ahead
from optjournal.bars import upsert_bars
from optjournal.clock import MARKET_TZ
from optjournal.events import (
    DEFAULT_COUNTRIES,
    DEFAULT_IMPACTS,
    EventFetchError,
    EventRateLimited,
    parse_events,
    store_events,
)
from optjournal.marketdata import Bar
from optjournal.serialize import market_data, odte_context_data

#: Thursday 15 Oct 2026, 11:00 New York: ForexFactory serves Sun 11 to Sat 17.
THURSDAY = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
MONDAY = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)


def _ff(stamp: str, country: str, impact: str, title: str, zone: str = "-04:00") -> dict:
    return {"title": title, "country": country, "date": f"{stamp}{zone}",
            "impact": impact, "forecast": "", "previous": ""}


#: ForexFactory's week of Sun 11 Oct, as stored on the real journal.
FF_WEEK = [
    _ff("2026-10-13T10:00:00", "USD", "Low", "Existing Home Sales"),
    _ff("2026-10-13T13:00:00", "USD", "Low", "FOMC Member Barkin Speaks"),
    _ff("2026-10-14T08:30:00", "USD", "High", "CPI m/m"),
    _ff("2026-10-14T08:30:00", "USD", "High", "Core CPI m/m"),
    _ff("2026-10-14T14:00:00", "USD", "Low", "Beige Book"),
    _ff("2026-10-15T08:30:00", "USD", "High", "PPI m/m"),
    _ff("2026-10-15T08:30:00", "USD", "High", "Retail Sales m/m"),
    _ff("2026-10-17T10:15:00", "All", "Low", "IMF Meetings"),
]
#: The week after, once ForexFactory serves it.
FF_NEXT = [
    _ff("2026-10-20T10:00:00", "USD", "Low", "NAHB Housing Market Index"),
    _ff("2026-10-22T08:30:00", "USD", "Medium", "Unemployment Claims"),
]


def _row(gmt: str, name: str, *, country: str = "United States",
         consensus: str = " ", previous: str = "&nbsp;") -> dict:
    """One row as Nasdaq sends it, blanks and all."""
    return {"gmt": gmt, "country": country, "eventName": name, "actual": "&nbsp;",
            "consensus": consensus, "previous": previous, "description": "..."}


def _reply(rows: list[dict], code: int = 200) -> dict:
    return {"data": {"asOf": "Thu, Oct 15, 2026", "headers": {}, "rows": rows},
            "message": None, "status": {"rCode": code}}


#: What really happens on each New York day, in Nasdaq's words.
TRUTH = {
    date(2026, 10, 13): [_row("10:00", "Existing Home Sales"),
                         _row("13:00", "FOMC Member Barkin Speaks")],
    date(2026, 10, 14): [_row("08:30", "CPI", consensus="0.6%"),
                         _row("08:30", "CPI", consensus="3.6%"),
                         _row("08:30", "Core CPI", consensus="0.2%"),
                         _row("14:00", "Beige Book")],
    date(2026, 10, 15): [_row("08:30", "PPI"), _row("08:30", "Retail Sales")],
    date(2026, 10, 18): [_row("22:00", "GDP", country="China", previous="4.3%")],
    date(2026, 10, 20): [_row("10:00", "NAHB Housing Market Index")],
    date(2026, 10, 22): [_row("08:30", "Initial Jobless Claims")],
    date(2026, 10, 23): [_row("10:00", "Michigan Consumer Sentiment",
                              consensus="46.3", previous="48.1")],
}


class Nasdaq:
    """The endpoint: `date=D` answers with the events of `D + shift`.

    `shift=-1` is what it does today, measured. `hours` moves every timed row,
    which is what switching the `gmt` field to real GMT would look like. `down`
    refuses every request, `fail_on` only the one for that `date`.
    """

    def __init__(self, shift: int = -1, *, hours: int = 0, truth: dict | None = None,
                 down: bool = False, fail_on: date | None = None) -> None:
        self.shift, self.hours, self.down, self.fail_on = shift, hours, down, fail_on
        self.truth = TRUTH if truth is None else truth
        self.asked: list[date] = []

    def __call__(self, day: date, **_kw) -> dict:
        self.asked.append(day)
        if self.down or day == self.fail_on:
            raise EventFetchError(f"Nasdaq's calendar for {day}: URLError: down")
        rows = []
        for row in self.truth.get(day + timedelta(days=self.shift), []):
            if ":" in row["gmt"]:
                hh, mm = map(int, row["gmt"].split(":"))
                row = {**row, "gmt": f"{(hh + self.hours) % 24:02d}:{mm:02d}"}
            rows.append(row)
        return _reply(rows)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = connect_migrated(tmp_path / "ahead.db")
    store_events(db, parse_events(FF_WEEK))
    return db


def _listed(conn) -> set[tuple[str, str, str, str]]:
    """The Nasdaq rows held, as (New York day, time, currency, title)."""
    out = set()
    for row in conn.execute(
            "SELECT starts_at, country, title FROM market_events WHERE source = ?",
            (SOURCE,)):
        when = datetime.fromtimestamp(row["starts_at"], MARKET_TZ)
        out.add((when.date().isoformat(), when.strftime("%H:%M"), row["country"],
                 row["title"]))
    return out


def _days(first: date, last: date) -> set[date]:
    return {first + timedelta(days=n) for n in range((last - first).days + 1)}


NEXT_WEEK = {
    ("2026-10-18", "22:00", "CNY", "GDP"),
    ("2026-10-20", "10:00", "USD", "NAHB Housing Market Index"),
    ("2026-10-22", "08:30", "USD", "Initial Jobless Claims"),
    ("2026-10-23", "10:00", "USD", "Michigan Consumer Sentiment"),
}


# ------------------------------------------------------------- placing the days


def test_todays_quirk_is_measured_and_each_day_asked_for_a_day_late(conn, monkeypatch):
    """The calibration day is the Wednesday ForexFactory served; every other
    request is a day the Market tab shows that ForexFactory does not: Sun 18 to
    Sun 25, asked for as 19 to 26 because Nasdaq answers a day late."""
    nasdaq = Nasdaq(shift=-1)
    monkeypatch.setattr(ahead, "_get", nasdaq)

    listed = refresh_ahead(conn, now=THURSDAY)

    assert listed.placing == Placing(shift=-1, clock=-4)
    assert nasdaq.asked[0] == date(2026, 10, 14), "calibrated on the served Wednesday"
    assert set(nasdaq.asked[1:]) == _days(date(2026, 10, 19), date(2026, 10, 26))
    assert len(nasdaq.asked) == 9, "one request per day, and one to calibrate"
    assert _listed(conn) == NEXT_WEEK
    assert (listed.stored, listed.days) == (4, 8)


def test_a_fix_on_nasdaqs_side_is_followed_rather_than_assumed(conn, monkeypatch):
    """The day Nasdaq answers `date=D` with D's own events, the same refresh asks
    for 18 to 25 and files every release on the day it happens. Assuming the
    measured shift would file next week's jobless claims on the Wednesday."""
    nasdaq = Nasdaq(shift=0)
    monkeypatch.setattr(ahead, "_get", nasdaq)

    listed = refresh_ahead(conn, now=THURSDAY)

    assert listed.placing.shift == 0
    assert set(nasdaq.asked[1:]) == _days(date(2026, 10, 18), date(2026, 10, 25))
    assert _listed(conn) == NEXT_WEEK


def test_a_clock_that_no_longer_lines_up_is_refused_and_writes_nothing(
        conn, monkeypatch):
    """Times in real GMT would put every release four hours late, and no day
    shift can place them on ForexFactory's minutes. That raises before a single
    day is asked for, and the listing already held stands."""
    monkeypatch.setattr(ahead, "_get", Nasdaq(shift=-1))
    refresh_ahead(conn, now=THURSDAY)

    moved = Nasdaq(shift=-1, hours=4)
    monkeypatch.setattr(ahead, "_get", moved)
    with pytest.raises(EventFetchError, match="cannot be placed"):
        refresh_ahead(conn, now=THURSDAY)

    assert moved.asked == [date(2026, 10, 14)], "nothing asked past the calibration"
    assert _listed(conn) == NEXT_WEEK


#: ForexFactory's week of Sun 1 Nov, the first on New York's winter clock.
FF_WINTER = [
    _ff("2026-11-03T10:00:00", "USD", "Medium", "JOLTS Job Openings", "-05:00"),
    _ff("2026-11-03T10:00:00", "USD", "Low", "Factory Orders m/m", "-05:00"),
]
#: The day the probe answers with and the next week, at New York's winter times.
WINTER = {
    date(2026, 11, 3): [_row("10:00", "JOLTS Job Openings"),
                        _row("10:00", "Factory Orders")],
    date(2026, 11, 11): [_row("All Day", "United States - Veterans Day")],
    date(2026, 11, 12): [_row("08:30", "Initial Jobless Claims")],
}


@pytest.mark.parametrize("clock", [-4, -5], ids=["utc-4-all-year", "follows-new-york"])
def test_a_winter_week_is_filed_at_new_york_time_on_either_clock(
        conn, monkeypatch, clock):
    """Every winter date asked on 15 Oct read on UTC-4, the claims an hour late
    at 09:30, and once New York is on UTC-5 Nasdaq may follow it instead. Either
    way the probe on ForexFactory's Wednesday finds the clock: the claims are
    filed at 08:30, and Veterans Day on its own day rather than at 23:00 the
    evening before."""
    store_events(conn, parse_events(FF_WINTER))
    monkeypatch.setattr(ahead, "_get", Nasdaq(truth=WINTER, hours=clock + 5))

    listed = refresh_ahead(conn, now=datetime(2026, 11, 5, 16, 0, tzinfo=UTC))

    assert listed.placing == Placing(shift=-1, clock=clock)
    assert _listed(conn) == {
        ("2026-11-11", "00:00", "USD", "United States - Veterans Day"),
        ("2026-11-12", "08:30", "USD", "Initial Jobless Claims"),
    }


def test_with_no_forexfactory_week_the_measured_shift_stands(tmp_path, monkeypatch):
    """A journal whose calendar feed never answered has nothing to calibrate
    against, so it asks no calibration request and lists from today."""
    conn = connect_migrated(tmp_path / "empty.db")
    nasdaq = Nasdaq(shift=MEASURED.shift)
    monkeypatch.setattr(ahead, "_get", nasdaq)

    listed = refresh_ahead(conn, now=THURSDAY)

    assert listed.placing == MEASURED
    assert set(nasdaq.asked) == _days(date(2026, 10, 16), date(2026, 10, 26))
    assert ("2026-10-15", "08:30", "USD", "PPI") in _listed(conn)


# ------------------------------------------------------------ the overlap rule


def test_forexfactory_takes_a_week_over_even_when_nasdaq_is_down(conn, monkeypatch):
    """Once ForexFactory serves the week of Sun 18, its rows are the week's only
    rows. The Nasdaq rows go first, so an unreachable Nasdaq cannot leave
    Thursday's claims listed twice."""
    monkeypatch.setattr(ahead, "_get", Nasdaq())
    refresh_ahead(conn, now=THURSDAY)
    store_events(conn, parse_events(FF_NEXT))

    monkeypatch.setattr(ahead, "_get", Nasdaq(down=True))
    with pytest.raises(EventFetchError):
        refresh_ahead(conn, now=MONDAY)

    assert _listed(conn) == set(), "a week ForexFactory serves kept Nasdaq's rows"
    week = market_data(conn, now=MONDAY)
    claims = [e for e in week["events"] if "Claims" in e["title"]]
    assert [(e["source"], e["day"]) for e in claims] == [("forexfactory", "2026-10-22")]
    assert [d["day"] for d in week["week"] if d["ahead"]] == ["2026-10-25"], (
        "only the Sunday that starts ForexFactory's next week is still ahead")


def test_nasdaq_fills_only_the_days_forexfactory_has_not_served(conn, monkeypatch):
    """With the week of Sun 18 served, Monday's refresh asks for Sun 25 to
    Sun 1 Nov and nothing inside the served week."""
    store_events(conn, parse_events(FF_NEXT))
    nasdaq = Nasdaq()
    monkeypatch.setattr(ahead, "_get", nasdaq)

    listed = refresh_ahead(conn, now=MONDAY)

    assert nasdaq.asked[0] == date(2026, 10, 21), "the newest served Wednesday"
    assert set(nasdaq.asked[1:]) == _days(date(2026, 10, 26), date(2026, 11, 2))
    assert listed.days == 8


def test_a_refresh_replaces_each_days_listing_whole(conn, monkeypatch):
    """Twice is the same as once, and a release Nasdaq moves to 10:30 is held
    at 10:30 only: an upsert keyed on the time would keep both."""
    monkeypatch.setattr(ahead, "_get", Nasdaq())
    refresh_ahead(conn, now=THURSDAY)
    refresh_ahead(conn, now=THURSDAY)
    assert _listed(conn) == NEXT_WEEK

    moved = dict(TRUTH)
    moved[date(2026, 10, 23)] = [_row("10:30", "Michigan Consumer Sentiment")]
    monkeypatch.setattr(ahead, "_get", Nasdaq(truth=moved))
    refresh_ahead(conn, now=THURSDAY)

    michigan = {row for row in _listed(conn) if "Michigan" in row[3]}
    assert michigan == {("2026-10-23", "10:30", "USD", "Michigan Consumer Sentiment")}


# -------------------------------------------------------------- failing safely


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _raises(exc):
    def urlopen(*_a, **_k):
        raise exc
    return urlopen


@pytest.mark.parametrize("failure", [
    "http-503", "url-error", "timeout", "not-json", "rcode-400", "no-data",
    "truncated", "hangup",
])
def test_every_way_a_request_fails_is_an_event_fetch_error(
        conn, monkeypatch, broken_http, failure):
    """Each is what Nasdaq or the link really does: an HTTP error, no route, a
    stall, an HTML error page, HTTP 200 carrying a refusal in `rCode` or no data
    at all, and a body broken off mid-way. The last is
    `http.client.HTTPException`, not an `OSError`, which is the one `earnings`
    does not name. The listing already held stands through every one."""
    real_get = ahead._get
    monkeypatch.setattr(ahead, "_get", Nasdaq())
    refresh_ahead(conn, now=THURSDAY)
    monkeypatch.setattr(ahead, "_get", real_get)
    urlopen = {
        "http-503": _raises(urllib.error.HTTPError("u", 503, "Unavailable", {}, None)),
        "url-error": _raises(urllib.error.URLError("no route")),
        "timeout": _raises(TimeoutError("timed out")),
        "not-json": lambda *_a, **_k: _Body(b"<html>maintenance</html>"),
        "rcode-400": lambda *_a, **_k: _Body(json.dumps(_reply([], 400)).encode()),
        "no-data": lambda *_a, **_k: _Body(
            json.dumps({"data": None, "status": {"rCode": 200}}).encode()),
    }.get(failure)
    if urlopen is None:
        monkeypatch.setattr(ahead, "_URL", broken_http(failure) + "?date={day}")
    else:
        monkeypatch.setattr(ahead.urllib.request, "urlopen", urlopen)

    with pytest.raises(EventFetchError):
        refresh_ahead(conn, now=THURSDAY)
    assert _listed(conn) == NEXT_WEEK


def test_one_day_failing_after_calibration_writes_no_day(conn, monkeypatch):
    """Calibration answered and seven days did, one did not: no day is replaced,
    not even the seven, so the listing never mixes two refreshes."""
    monkeypatch.setattr(ahead, "_get", Nasdaq())
    refresh_ahead(conn, now=THURSDAY)
    emptied = {day: rows for day, rows in TRUTH.items() if day < date(2026, 10, 18)}
    monkeypatch.setattr(ahead, "_get", Nasdaq(truth=emptied, fail_on=date(2026, 10, 24)))

    with pytest.raises(EventFetchError):
        refresh_ahead(conn, now=THURSDAY)
    assert _listed(conn) == NEXT_WEEK


def _outage():
    raise EventFetchError("ff_calendar_thisweek.json: HTTP 500")


def _throttled():
    raise EventRateLimited(92)


@pytest.mark.parametrize(("feed", "nasdaq", "status", "says"), [
    (lambda: parse_events(FF_WEEK), Nasdaq(down=True), "ok", "ahead failed"),
    (_outage, Nasdaq(), "failed", "HTTP 500; 4 ahead from nasdaq over 8 days"),
    (_throttled, Nasdaq(), "nothing", "rate limited"),
], ids=["nasdaq-down", "forexfactory-down", "forexfactory-throttled"])
def test_the_market_job_is_judged_on_forexfactory_and_lists_ahead_anyway(
        conn, tmp_path, monkeypatch, feed, nasdaq, status, says):
    """ForexFactory's week is the history, so it alone decides the status: a
    back-off stays `nothing` though the days ahead were rewritten. Nasdaq down
    is named in the detail, and an outage or a back-off at ForexFactory still
    lists the days ahead, which is when they are most worth having."""
    ctx = jobs.Context(archive_dir=tmp_path, db_path=tmp_path / "ahead.db")
    monkeypatch.setattr(ahead, "fetch_events", feed)
    monkeypatch.setattr(jobs, "datetime", _Frozen)
    monkeypatch.setattr(ahead, "_get", nasdaq)

    outcome = jobs._market(conn, ctx)

    assert outcome.status == status
    assert says in outcome.detail
    assert _listed(conn) == (set() if nasdaq.down else NEXT_WEEK)


def test_the_cli_fetch_lists_ahead_and_keeps_its_exit_code(conn, tmp_path, monkeypatch):
    """`optjournal market --fetch` is the cron's command, so ForexFactory's
    back-off is still exit 4, and the days ahead are listed all the same."""
    from optjournal import cli

    db = tmp_path / "ahead.db"
    monkeypatch.setattr(ahead, "fetch_events", _throttled)
    monkeypatch.setattr(cli, "datetime", _Frozen)
    monkeypatch.setattr(ahead, "_get", Nasdaq())

    assert cli.main(["market", "--fetch", "--db", str(db)]) == cli.EXIT_THROTTLED
    assert _listed(conn) == NEXT_WEEK
    run = conn.execute("SELECT status, detail FROM job_runs WHERE job = 'market'"
                       " ORDER BY id DESC").fetchone()
    assert run["status"] == "nothing" and "4 ahead" in run["detail"]


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return THURSDAY if tz is None else THURSDAY.astimezone(tz)


# -------------------------------------------------------- reading Nasdaq's rows


def test_the_default_filter_keeps_the_releases_that_move_a_us_book():
    """Nasdaq grades nothing, and the Market tab opens on High and Medium. The
    titles are Nasdaq's own, captured; the variants it lists beside each
    headline stay Low, so the default view is the prints and not their parts."""
    shown = ["CPI", "Core CPI", "PPI", "Core PPI", "Nonfarm Payrolls",
             "Unemployment Rate", "Average Hourly Earnings", "Core PCE Price Index",
             "PCE price index", "GDP", "Retail Sales", "Core Retail Sales",
             "ISM Manufacturing PMI", "ISM Non-Manufacturing PMI",
             "Fed Interest Rate Decision", "FOMC Statement", "FOMC Press Conference",
             "Fed Chair Warsh Speaks", "Initial Jobless Claims", "JOLTS Job Openings",
             "Michigan Consumer Sentiment", "Philadelphia Fed Manufacturing Index"]
    hidden = ["CPI Index, n.s.a.", "Core CPI Index", "Cleveland CPI",
              "Philly Fed CAPEX Index", "Private Nonfarm Payrolls",
              "U6 Unemployment Rate", "Atlanta Fed GDPNow", "GDP Sales",
              "Retail Sales Ex Gas/Autos", "ISM Manufacturing Employment",
              "Interest Rate Projection - Current", "3-Month Bill Auction",
              "Fed Waller Speaks", "Natural Gas Storage"]
    reply = _reply([_row("08:30", name) for name in shown + hidden])

    events = parse_day(reply, day=date(2026, 10, 14), requested=date(2026, 10, 15))

    kept = [e.title for e in events
            if e.country in DEFAULT_COUNTRIES and e.impact in DEFAULT_IMPACTS]
    assert sorted(kept) == sorted(shown)


def test_a_reply_is_filed_the_way_forexfactory_files_its_own():
    """Countries become ForexFactory's currencies, and one it does not cover is
    dropped. A blank is no figure, a holiday is a Holiday, a euro member's
    release is named for its country, and Nasdaq's month-on-month and
    year-on-year prints under one title are two rows rather than one."""
    reply = _reply([
        _row("08:30", "Core CPI", consensus="0.2%"),
        _row("08:30", "Core CPI", consensus="2.5%"),
        _row("05:00", "ZEW Economic Sentiment", country="Germany"),
        _row("02:00", "German CPI", country="Germany"),
        _row("All Day", "China - National Day", country="China"),
        _row("All Day", "Hong Kong - National Day", country="Hong Kong"),
    ])

    events = parse_day(reply, day=date(2026, 10, 14), requested=date(2026, 10, 15))

    assert [(e.country, e.title, e.impact) for e in events] == [
        ("CNY", "China - National Day", "Holiday"),
        ("EUR", "German CPI", "Low"),
        ("EUR", "German ZEW Economic Sentiment", "Low"),
        ("USD", "Core CPI", "High"),
        ("USD", "Core CPI", "High"),
    ]
    assert len({e.event_id for e in events}) == 5
    core = [e for e in events if e.title == "Core CPI"]
    assert [(e.forecast, e.previous) for e in core] == [("0.2%", None), ("2.5%", None)]
    assert datetime.fromtimestamp(events[0].starts_at, MARKET_TZ).strftime("%H:%M") == "00:00"
    assert events[0].raw["requested"] == "2026-10-15"


def test_a_time_it_cannot_read_raises_rather_than_dropping_the_row():
    with pytest.raises(EventFetchError, match="unreadable time"):
        parse_day(_reply([_row("Tentative", "CPI")]), day=date(2026, 10, 14),
                  requested=date(2026, 10, 15))


# -------------------------------------------------------- what the page reads


def test_next_week_is_its_own_page_of_the_calendar(conn, monkeypatch):
    """`week=1` is Mon 19 to Sun 25 with its own vocabulary and counts, and every
    day of it is marked as listed ahead, the quiet ones too: ForexFactory has
    served none of it. The 18th starts ForexFactory's next week, so it is the one
    day marked on this week's page."""
    monkeypatch.setattr(ahead, "_get", Nasdaq())
    refresh_ahead(conn, now=THURSDAY)

    nxt = market_data(conn, now=THURSDAY, week=1)
    assert (nxt["from_day"], nxt["to_day"]) == ("2026-10-19", "2026-10-25")
    assert all(d["ahead"] for d in nxt["week"])
    assert not any(d["today"] for d in nxt["week"])
    assert [(e["day"], e["title"], e["source"]) for e in nxt["events"]] == [
        ("2026-10-20", "NAHB Housing Market Index", SOURCE),
        ("2026-10-22", "Initial Jobless Claims", SOURCE),
        ("2026-10-23", "Michigan Consumer Sentiment", SOURCE),
    ]
    assert nxt["default_events"] == 2, "claims and Michigan, graded Medium"
    assert [f["value"] for f in nxt["countries"]] == ["USD"]
    assert nxt["ahead_source"] == SOURCE

    this = market_data(conn, now=THURSDAY)
    assert [d["day"] for d in this["week"] if d["ahead"]] == ["2026-10-18"]
    assert {e["source"] for e in this["events"]} == {"forexfactory", SOURCE}


def test_an_untimed_monday_release_is_counted_on_its_own_week_only(conn, monkeypatch):
    """Nasdaq stamps a holiday 00:00, which is the instant one Market week ends
    and the next begins. New Zealand's Labour Day on Mon 26 is next week's row,
    and this week's chips, totals and hidden count must not include it."""
    store_events(conn, parse_events(FF_NEXT))
    truth = dict(TRUTH)
    truth[date(2026, 10, 26)] = [
        _row("All Day", "New Zealand - Labour Day", country="New Zealand")]
    monkeypatch.setattr(ahead, "_get", Nasdaq(truth=truth))
    refresh_ahead(conn, now=MONDAY)

    this, nxt = (market_data(conn, now=MONDAY, week=w) for w in (0, 1))

    assert "NZD" not in {f["value"] for f in this["countries"]}
    assert all(e["day"] <= this["to_day"] for e in this["events"])
    assert [(e["day"], e["impact"]) for e in nxt["events"] if e["country"] == "NZD"] == [
        ("2026-10-26", "Holiday")]


def test_the_0dte_chips_read_mondays_listing_on_a_friday_evening(conn, monkeypatch):
    """After Friday's settle the calculator is drawn for Monday, a day
    ForexFactory has not served. Its chips are Nasdaq's rows, and the payload
    says so, because their grade is then this journal's."""
    truth = dict(TRUTH)
    truth[date(2026, 10, 19)] = [_row("10:00", "ISM Manufacturing PMI")]
    monkeypatch.setattr(ahead, "_get", Nasdaq(truth=truth))
    refresh_ahead(conn, now=THURSDAY)
    for symbol, close in (("^GSPC", 6600.0), ("^VIX", 17.0)):
        upsert_bars(conn, conid=symbol, symbol=symbol, bar_size="1d", source="yahoo",
                    bars=[Bar(ts=int(datetime(2026, 10, 16, 16, tzinfo=MARKET_TZ)
                                     .timestamp()), open=close, high=close, low=close,
                              close=close, volume=0)])

    ctx = odte_context_data(conn, now=datetime(2026, 10, 16, 21, 30, tzinfo=UTC))

    assert ctx is not None and ctx["today"] == "2026-10-19"
    assert [e["title"] for e in ctx["events_today"]] == ["ISM Manufacturing PMI"]
    assert ctx["events_ahead"] is True


@pytest.mark.parametrize(("feed", "nasdaq", "code", "listed", "error"), [
    (lambda: parse_events(FF_WEEK), Nasdaq(), 200, 4, None),
    (lambda: parse_events(FF_WEEK), Nasdaq(down=True), 200, 0, "down"),
    (_throttled, Nasdaq(), 429, 4, None),
], ids=["both", "nasdaq-down", "forexfactory-throttled"])
def test_refresh_on_the_page_lists_the_days_ahead_too(
        conn, tmp_path, monkeypatch, feed, nasdaq, code, listed, error):
    """The Market tab's Refresh is the job's refresh, so pressing it on next week
    refreshes next week even while ForexFactory is backing off, and every reply
    says what happened to the days ahead."""
    from optjournal import web

    monkeypatch.setattr(ahead, "fetch_events", feed)
    monkeypatch.setattr(web, "datetime", _Frozen)
    monkeypatch.setattr(ahead, "_get", nasdaq)
    with web.serve_ephemeral(db_path=tmp_path / "ahead.db", archive_dir=tmp_path) as base:
        request = urllib.request.Request(f"{base}/api/market/fetch", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                status, reply = response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            status, reply = exc.code, json.loads(exc.read())

    assert (status, reply["ahead"]) == (code, listed)
    assert (reply["ahead_error"] is None) == (error is None)
    if error:
        assert error in reply["ahead_error"]
    assert _listed(conn) == (set() if nasdaq.down else NEXT_WEEK)


def _render(db, tmp_path, monkeypatch, fragment: str) -> tuple[str, str]:
    """The page at `fragment`, executed in a browser, served on Thursday 15 Oct."""
    from optjournal import browser, web

    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    monkeypatch.setattr(web, "datetime", _Frozen)
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path) as base:
        dom = browser.dump_dom(f"{base}/{fragment}", tmp_path / "chrome")
    if dom is None:
        pytest.skip("no browser produced a DOM (environment, not the page)")
    return " ".join(browser.rendered_text(dom).split()), browser.markup(dom)


def test_the_market_tab_opens_on_next_week_from_its_link(conn, tmp_path, monkeypatch):
    """`#tab=market&week=next` over ForexFactory's week and Nasdaq's listing of the
    next, executed in a browser: the stepper names the week in the page's date
    style, its seven days are dashed, the open day is a listed one and says whose
    listing and whose grades these are, and its rows are on screen."""
    monkeypatch.setattr(ahead, "_get", Nasdaq())
    refresh_ahead(conn, now=THURSDAY)

    text, markup = _render(tmp_path / "ahead.db", tmp_path, monkeypatch,
                           "#tab=market&week=next")

    assert "Next week · 19 Oct to 25 Oct" in text
    assert markup.count("mkahead") == 7, "every day of next week is listed ahead"
    assert "22 Oct" in text and "Initial Jobless Claims" in text
    assert ("Listed ahead by nasdaq, since forexfactory has not published this day "
            "yet. Nothing on it is released yet.") in text
    assert "forexfactory has not published the dashed days yet" in text


def test_a_listed_day_that_is_today_does_not_claim_nothing_is_out(
        tmp_path, monkeypatch):
    """With no ForexFactory week stored, today is a listed day too, and by the
    afternoon some of its rows are out: the note keeps its provenance and drops
    "nothing is released yet", which only a day still to come can say."""
    db = tmp_path / "bare.db"
    monkeypatch.setattr(ahead, "_get", Nasdaq())
    refresh_ahead(connect_migrated(db), now=THURSDAY)

    text, _markup = _render(db, tmp_path, monkeypatch, "#tab=market")

    assert "This week · 12 Oct to 18 Oct" in text
    assert "15 Oct" in text and "PPI" in text
    assert "Listed ahead by nasdaq, since forexfactory has not published this day yet" in text
    assert "Nothing on it is released yet" not in text
