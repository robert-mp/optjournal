"""The economic calendar: the parser, the key, and what it refuses.

No test here touches the network. The parser is exercised against a slice of a
real response, captured from the live feed rather than invented, so the shape
under test is the shape the feed actually sends -- the same reason `test_bars.py`
uses a captured chart payload.

What is worth testing here is not "does it parse". It is the three ways a calendar
can be quietly wrong: an event filed under the wrong importance, an event stored
twice, and an event whose past disappears when the feed moves on.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import connect_migrated

from optjournal.events import (
    DEFAULT_COUNTRIES,
    DEFAULT_IMPACTS,
    IMPACT_ORDER,
    IMPACTS,
    SOURCE,
    EventFetchError,
    EventRateLimited,
    parse_events,
    store_events,
    upcoming,
)

#: A slice of the live response: one row per impact value the feed emits, plus a
#: USD high-impact release. Captured, not written -- an invented fixture would
#: test the shape I assumed rather than the shape that arrives.
FEED = [
    {"title": "OPEC-JMMC Meetings", "country": "All",
     "date": "2026-08-02T05:15:00-04:00", "impact": "Medium",
     "forecast": "", "previous": ""},
    {"title": "Bank Holiday", "country": "AUD",
     "date": "2026-08-02T17:00:00-04:00", "impact": "Holiday",
     "forecast": "", "previous": ""},
    {"title": "Building Consents m/m", "country": "NZD",
     "date": "2026-08-02T18:45:00-04:00", "impact": "Low",
     "forecast": "", "previous": "-4.0%"},
    {"title": "ISM Manufacturing PMI", "country": "USD",
     "date": "2026-08-03T10:00:00-04:00", "impact": "High",
     "forecast": "54.0", "previous": "53.3"},
    {"title": "Non-Farm Employment Change", "country": "USD",
     "date": "2026-08-07T08:30:00-04:00", "impact": "High",
     "forecast": "75K", "previous": "147K"},
]


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    return connect_migrated(tmp_path / "events.db")


def test_the_feeds_impact_vocabulary_is_declared_not_guessed():
    """Every value the feed sends must be in IMPACTS, and vice versa.

    Both directions on purpose. A value the feed sends that IMPACTS lacks raises
    at parse time (next test); a value in IMPACTS the feed never sends is dead
    vocabulary that suggests the set was guessed rather than observed.
    """
    assert {row["impact"] for row in FEED} == set(IMPACTS), (
        "the captured feed and IMPACTS disagree -- one of them is stale"
    )


def test_an_unknown_impact_raises_rather_than_being_filed_as_low():
    """The defect this refuses: a high-impact event quietly downgraded.

    A calendar exists to say which day matters. Bucketing an unrecognised
    importance as Low produces a well-formed calendar with the one row that
    mattered de-emphasised -- and nothing anywhere would report it. So the feed
    changing its vocabulary is a loud failure, and adding a value is a deliberate
    edit to IMPACTS.
    """
    row = dict(FEED[0], impact="Critical")
    with pytest.raises(EventFetchError, match="unknown impact 'Critical'"):
        parse_events([row])


def test_a_date_without_an_offset_is_drift_not_a_default():
    """The feed has always sent an offset. Assuming one would be inventing a zone.

    An event stored at the wrong instant lands on the wrong DAY in the week strip
    whenever it sits near midnight, which is the one error a calendar cannot
    absorb.
    """
    with pytest.raises(EventFetchError, match="no timezone offset"):
        parse_events([dict(FEED[0], date="2026-08-02T05:15:00")])


def test_a_missing_field_raises_rather_than_skipping_the_row():
    """Skipping malformed rows would make a broken feed look like a quiet week."""
    del_title = {k: v for k, v in FEED[0].items() if k != "title"}
    with pytest.raises(EventFetchError, match="missing"):
        parse_events([del_title])
    with pytest.raises(EventFetchError, match="expected a list"):
        parse_events({"events": FEED})


def test_the_instant_is_stored_not_the_local_wall_clock():
    """08:30 ET on release day is 12:30Z, and that is what gets stored.

    One timeline, like every other stamp in this journal -- the page renders in
    MARKET_TZ. Asserted against a hand-computed epoch so a timezone library
    change cannot quietly move it.
    """
    events = {e.title: e for e in parse_events(FEED)}
    nfp = events["Non-Farm Employment Change"]
    # 2026-08-07 08:30 EDT (UTC-4) == 12:30 UTC.
    from datetime import UTC, datetime
    assert datetime.fromtimestamp(nfp.starts_at, UTC) == datetime(
        2026, 8, 7, 12, 30, tzinfo=UTC
    )


def test_an_unpublished_forecast_is_none_not_an_empty_string():
    """The feed sends "" for a figure it has not published.

    None and 0 and "" are three different claims. A holiday has no forecast; a
    release forecast of zero would be a number. Kept distinct so the page can
    render a dash rather than a misleading value.
    """
    holiday = next(e for e in parse_events(FEED) if e.title == "Bank Holiday")
    assert holiday.forecast is None and holiday.previous is None
    ism = next(e for e in parse_events(FEED) if e.title == "ISM Manufacturing PMI")
    assert (ism.forecast, ism.previous) == ("54.0", "53.3")


def test_the_event_id_is_stable_and_distinct():
    """The feed supplies no id, so ours has to be both.

    Stable: parsing the same response twice must give the same ids, or every
    fetch would duplicate the week. Distinct: two events at the same instant in
    the same country (three USD releases share 08:30) must not collide.
    """
    first = {e.event_id for e in parse_events(FEED)}
    second = {e.event_id for e in parse_events(FEED)}
    assert first == second, "ids are not stable across parses"
    assert len(first) == len(FEED), "two events collided on one id"

    # Same instant and country, different title -- the real 08:30 case.
    same_time = [
        dict(FEED[4], title="Unemployment Rate"),
        dict(FEED[4], title="Average Hourly Earnings m/m"),
    ]
    assert len({e.event_id for e in parse_events(same_time)}) == 2


def test_a_refetch_corrects_a_revised_forecast_without_duplicating(conn):
    """Forecasts are revised between announcement and release.

    So the write is an upsert: the row is corrected in place. Storing a second
    copy would put two NFPs on the same day in the week strip, and a stale
    forecast beside a released number reads as a miss that never happened.
    """
    assert store_events(conn, parse_events(FEED)) == len(FEED)
    revised = [dict(row, forecast="80K") if "Non-Farm" in row["title"] else row
               for row in FEED]
    store_events(conn, parse_events(revised))

    rows = conn.execute("SELECT COUNT(*) AS n FROM market_events").fetchone()
    assert rows["n"] == len(FEED), "the re-fetch duplicated events"
    got = conn.execute(
        "SELECT forecast FROM market_events WHERE title = ?",
        ("Non-Farm Employment Change",),
    ).fetchone()
    assert got["forecast"] == "80K", "the revision was not applied"


def test_stored_events_outlive_the_week_the_feed_serves(conn):
    """The table is the history, because the feed will not serve one.

    Verified against the live source: `ff_calendar_nextweek`, `_thismonth` and
    `_lastweek` all 404, so only the current week is ever available. A store that
    cleared the table first would make the journal's past calendar exactly as
    short as the feed's -- which is the reason `store_events` upserts rather than
    replacing the week.
    """
    store_events(conn, parse_events(FEED))
    # A later week arrives, sharing no event with the first.
    later = [dict(row, date=row["date"].replace("2026-08-0", "2026-08-1"))
             for row in FEED]
    store_events(conn, parse_events(later))
    total = conn.execute("SELECT COUNT(*) AS n FROM market_events").fetchone()["n"]
    assert total == 2 * len(FEED), (
        f"{total} rows: storing a new week discarded the old one, so the journal "
        f"has no calendar history the feed cannot re-serve"
    )


def test_upcoming_filters_are_the_callers_to_state(conn):
    """An empty filter means everything, matching ASSET_FILTER_ALL's convention.

    The Market tab narrows to DEFAULT_COUNTRIES/DEFAULT_IMPACTS, but the table
    holds ten countries and a reader may want all of them -- so the default here
    is not to narrow.
    """
    store_events(conn, parse_events(FEED))
    lo, hi = 0, 2 ** 31
    assert len(upcoming(conn, start=lo, end=hi)) == len(FEED)

    usd_high = upcoming(conn, start=lo, end=hi, countries=("USD",),
                        impacts=("High",))
    assert {e["title"] for e in usd_high} == {
        "ISM Manufacturing PMI", "Non-Farm Employment Change",
    }
    # And the window really windows: nothing before the first event.
    assert upcoming(conn, start=lo, end=1) == []


def test_the_two_axes_are_independent_not_one_conjunction(conn):
    """Each axis narrows alone, which a single "key" flag could not express.

    The web view used to receive one server-side boolean meaning "USD AND High",
    so "USD, every impact" -- the question a US book actually asks -- was
    unaskable without a new field. Filtering on one axis at a time is the
    behaviour that made two axes worth having, so it is asserted rather than
    assumed from the fact that both parameters exist.
    """
    store_events(conn, parse_events(FEED))
    lo, hi = 0, 2 ** 31

    usd_any = upcoming(conn, start=lo, end=hi, countries=("USD",))
    assert {e["impact"] for e in usd_any} == {"High"}, "the fixture's USD rows"
    assert len(usd_any) == 2

    any_medium = upcoming(conn, start=lo, end=hi, impacts=("Medium",))
    assert [e["country"] for e in any_medium] == ["All"], (
        "impact alone must not imply a country -- the feed's global rows are a "
        "country value ('All'), and dropping them would lose OPEC from an "
        "oil-sensitive book"
    )


def test_the_default_slice_keeps_the_feeds_global_rows(conn):
    """DEFAULT_COUNTRIES includes 'All', which is a country the feed really sends.

    Verified against the live table rather than reasoned about: `All` carries rows
    like OPEC-JMMC Meetings. A default of ("USD",) alone reads as "US only" but
    silently discards them, which is exactly the well-formed-but-wrong output this
    module's docstring is about.
    """
    store_events(conn, parse_events(FEED))
    rows = upcoming(conn, start=0, end=2 ** 31,
                    countries=DEFAULT_COUNTRIES, impacts=DEFAULT_IMPACTS)
    assert "OPEC-JMMC Meetings" in {e["title"] for e in rows}
    # And Medium is in the default at all: High-only was 4 of a real week's 99,
    # against 10 USD High and 11 USD Medium in the whole table.
    assert "Medium" in DEFAULT_IMPACTS


def test_the_impact_order_is_severity_and_covers_the_vocabulary():
    """IMPACT_ORDER ranks IMPACTS, and must not drift from it.

    The page renders the impact axis in this order and holds no copy of what
    "more important" means. A value in IMPACTS but missing here would silently
    vanish from the filter row -- a chip that cannot be pressed for events that
    are stored.
    """
    assert set(IMPACT_ORDER) == set(IMPACTS), (
        "IMPACT_ORDER and IMPACTS disagree -- a stored impact with no place in "
        "the order would have no filter chip"
    )
    assert IMPACT_ORDER[0] == "High", "severity order, most important first"
    assert IMPACT_ORDER[-1] == "Holiday", (
        "Holiday is a category, not a grade above Low -- see IMPACT_ORDER"
    )


# ------------------------------------------------------- the view's filter axes
#
# `serialize.market_data` builds what the Market tab filters ON: two vocabularies
# with counts, plus the journal's defaults marked. Tested here rather than in a
# web test because it is calendar behaviour -- the same reason `upcoming` is here.
#
# The failure these guard is specific and has a history: the payload used to carry
# ONE precomputed boolean per event and one precomputed count per day, so a filter
# the page applied and a count the server derived could disagree, and a day showed
# three dots then opened empty.


def _market(conn):
    """`market_data` over a window that contains the whole fixture.

    Anchored on the fixture's own first event rather than on `now`, so the shape
    under test does not depend on the day the suite runs -- the trap a hard-coded
    date is, one layer up.
    """
    from datetime import UTC, datetime

    from optjournal.serialize import market_data

    store_events(conn, parse_events(FEED))
    first = min(parse_events(FEED), key=lambda e: e.starts_at)
    # A Wednesday inside the fixture's week, so the Monday-anchored strip covers it.
    return market_data(conn, now=datetime.fromtimestamp(first.starts_at, UTC),
                       days=14)


def test_the_filter_axes_carry_their_vocabulary_with_counts(conn):
    """Each axis lists what is STORED in the window, with a count and a default.

    Built from the stored rows rather than from the feed's full alphabet: a
    currency with no events this week would be a chip that does nothing. The count
    is what lets a chip state its cost before it is pressed.
    """
    payload = _market(conn)

    countries = {row["value"]: row["events"] for row in payload["countries"]}
    assert countries == {"All": 1, "AUD": 1, "NZD": 1, "USD": 2}
    # Case-insensitively alphabetical: a plain `sorted` ranks "AUD" before "All"
    # by codepoint, which dropped the feed's one non-currency chip into the middle
    # of the row. Asserted because it is a choice, not an accident of `sorted`.
    assert [row["value"] for row in payload["countries"]] == ["All", "AUD", "NZD",
                                                             "USD"]

    impacts = {row["value"]: row["events"] for row in payload["impacts"]}
    assert impacts == {"High": 2, "Medium": 1, "Low": 1, "Holiday": 1}
    assert [row["value"] for row in payload["impacts"]] == list(IMPACT_ORDER), (
        "the impact axis renders in severity order, from IMPACT_ORDER"
    )

    # The defaults are marked so the page need not know them.
    assert {row["value"] for row in payload["countries"] if row["default"]} == {
        "USD", "All"}
    assert {row["value"] for row in payload["impacts"] if row["default"]} == {
        "High", "Medium"}


def test_a_day_count_is_the_total_so_the_strip_cannot_lie(conn):
    """`MarketDay.events` is the day's TOTAL, and the events are all sent.

    The page counts its own filtered subset from `events`, which is the only way
    the strip and the rows it opens cannot disagree. A per-filter count computed
    here would need one field per filter combination -- 2^n of them -- and the old
    single `key_events` field was exactly the version of that which drifted.
    """
    payload = _market(conn)

    assert sum(day["events"] for day in payload["week"]) <= payload["total_events"]
    assert payload["total_events"] == len(FEED)

    # Every event is present with the two values the page filters on, so no
    # narrowing needs a new request or a new server-side flag.
    for event in payload["events"]:
        assert event["country"] and event["impact"]
    # And the day totals really are totals: the fixture's Sunday holds one event
    # (the OPEC row and the AUD holiday are both 2026-08-02 in MARKET_TZ terms),
    # so a day with events is never reported as empty.
    by_day: dict[str, int] = {}
    for event in payload["events"]:
        by_day[event["day"]] = by_day.get(event["day"], 0) + 1
    for day in payload["week"]:
        assert day["events"] == by_day.get(day["day"], 0), (
            f"{day['day']}: strip count disagrees with the events sent"
        )


def test_the_default_scope_is_named_by_the_server_that_applies_it(conn):
    """The label comes from the constants, so it cannot describe a stale filter.

    This is the bug the CLI had: a hard-coded "USD high-impact" beside defaults
    that had grown to two impacts. A label narrating a filter it does not apply is
    worse than no label.
    """
    payload = _market(conn)
    assert payload["default_scope"] == "USD, All high/medium-impact"
    assert payload["default_events"] == 3, (
        "USD High x2 plus the 'All' Medium row -- the slice the label describes"
    )


def test_events_are_stamped_with_the_source_that_issued_them(conn):
    """The identity is (source, event_id), so the source has to be on the row.

    Same reason `trades` carries `broker`: an id belongs to the feed that issued
    it. A second feed numbering an event the same way must not overwrite this
    one's row -- a lesson that cost two migrations on the broker tables.
    """
    store_events(conn, parse_events(FEED))
    sources = {r["source"] for r in conn.execute("SELECT source FROM market_events")}
    assert sources == {SOURCE}

    # The same events under another source coexist rather than colliding.
    store_events(conn, parse_events(FEED), source="othercal")
    total = conn.execute("SELECT COUNT(*) AS n FROM market_events").fetchone()["n"]
    assert total == 2 * len(FEED), "a second feed overwrote the first's rows"


# --------------------------------------------------------------- rate limiting
#
# Not anticipated -- hit while writing this module. The feed sits behind
# Cloudflare and started answering 429 with `retry-after: 92` after a handful of
# requests, and was still refusing three minutes later. So the back-off is real,
# and the interesting question is whether a caller can tell it apart from the
# feed having changed shape.


def test_a_429_is_its_own_failure_carrying_the_servers_own_delay(monkeypatch):
    """Rate limiting is "try later", not "something is wrong".

    A nightly cron should stay silent on this and alert on a parse failure or a
    404 -- the same split `flex.FetchCooldown` draws against a real fetch error,
    and the reason `cmd_market` returns EXIT_THROTTLED rather than EXIT_ERROR.
    """
    import urllib.error

    from optjournal import events as events_module

    def limited(*_a, **_k):
        raise urllib.error.HTTPError(
            "u", 429, "Too Many Requests", {"retry-after": "92"}, None
        )

    monkeypatch.setattr(events_module.urllib.request, "urlopen", limited)
    with pytest.raises(EventRateLimited) as caught:
        events_module.fetch_events()
    assert caught.value.retry_after_s == 92
    # And it IS an EventFetchError, so a caller that does not care about the
    # distinction still catches it.
    assert isinstance(caught.value, EventFetchError)


def test_a_missing_retry_after_falls_back_rather_than_crashing(monkeypatch):
    """The header is the server's courtesy, not a guarantee.

    Guessing short is harmless -- the request is simply refused again -- so a
    missing or unparseable value must not turn a back-off into a traceback.
    """
    import urllib.error

    from optjournal import events as events_module

    for headers in ({}, {"retry-after": "soon"}):
        def limited(*_a, _h=headers, **_k):
            raise urllib.error.HTTPError("u", 429, "Too Many", _h, None)

        monkeypatch.setattr(events_module.urllib.request, "urlopen", limited)
        with pytest.raises(EventRateLimited) as caught:
            events_module.fetch_events()
        assert caught.value.retry_after_s == 60


def test_a_non_429_http_error_stays_a_plain_fetch_error(monkeypatch):
    """A 404 means the endpoint moved, which a human should see.

    Bucketing it with rate limiting would make a dead feed look like a busy one,
    and a cron told to stay quiet about back-offs would never report it.
    """
    import urllib.error

    from optjournal import events as events_module

    def gone(*_a, **_k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(events_module.urllib.request, "urlopen", gone)
    with pytest.raises(EventFetchError, match="404") as caught:
        events_module.fetch_events()
    assert not isinstance(caught.value, EventRateLimited)
