"""Next week's economic calendar, from Nasdaq, until ForexFactory serves that week.

ForexFactory is the calendar (see `events`) and it serves the current week only:
`ff_calendar_nextweek.json` answers 404. Nasdaq's keyless
`api.nasdaq.com/api/calendar/economicevents?date=YYYY-MM-DD`, the provider
`earnings` already reads, lists weeks ahead. Its rows are a STAND-IN: they fill
the days ForexFactory has not served and give way when it does, so ForexFactory's
rows stay the history.

ONE RULE FOR OVERLAP. A week that holds any ForexFactory row shows ForexFactory's
rows only. ForexFactory's week runs Sunday to Saturday in New York, measured on
every week stored since August. So `refresh_ahead` first deletes the Nasdaq rows
in every such week, and then fetches only the days outside them. Nothing is shown
twice, and no read has to choose between two sources.

THE DATES ARE OFF BY ONE DAY, measured on 2026-10-15 against the week ForexFactory
had served: `date=D` answers with the events of D-1, and the field Nasdaq calls
`gmt` holds New York time. CPI (14 Oct 08:30 ET in ForexFactory) came back under
`date=2026-10-15` at "08:30", and the 28 Oct FOMC decision under `date=2026-10-29`.
That is a bug Nasdaq could fix, so every refresh CALIBRATES rather than trusting
the shift: it asks for the Wednesday of the newest week ForexFactory served and
counts, for each shift of -1, 0 and +1 days, how many rows land on the same New
York minute as a ForexFactory row with a matching title. Over twelve measured
days the true shift matched 7 to 46 rows and the others 0 or 1. When no shift
matches anything, the dates or the clock changed in a way this cannot place, and
it raises rather than filing events on the wrong day or at the wrong hour.

NASDAQ GRADES NOTHING, so `grade` does, and the page says the grade is this
journal's. The patterns name Nasdaq's own titles and give each release the grade
ForexFactory gave the same release in the weeks stored since August, so the
default filter (`events.DEFAULT_IMPACTS`) keeps CPI, PPI, the jobs report, the
FOMC, PCE, GDP, retail sales and ISM. The 0DTE tab's tiers were the first
candidate and do not fit: they are written against ForexFactory's titles, and on
Nasdaq's they rank five "Philly Fed" sub-indexes above the headline
"Philadelphia Fed Manufacturing Index" and every "CPI Index, n.s.a." variant level
with CPI itself.

COUNTRIES ARE ForexFactory's. Nasdaq lists twenty countries by name; the ones
ForexFactory covers map to its currency codes, so the Market tab's country filter
and the 0DTE tab's USD rule read both sources alike. The rest are dropped: a
country that vanishes the day ForexFactory takes the week over would make the
same week read two ways.
"""

from __future__ import annotations

import hashlib
import html
import http.client
import json
import re
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from optjournal.clock import MARKET_TZ
from optjournal.events import SOURCE as FOREXFACTORY
from optjournal.events import (
    EventFetchError,
    EventRateLimited,
    MarketEvent,
    fetch_events,
    store_events,
)

__all__ = [
    "MEASURED_SHIFT",
    "SOURCE",
    "Ahead",
    "Refreshed",
    "calibrate",
    "covered_weeks",
    "grade",
    "parse_day",
    "refresh_ahead",
    "refresh_calendar",
    "week_of",
]

#: The value stored in `market_events.source` for these rows.
SOURCE = "nasdaq"

_URL = "https://api.nasdaq.com/api/calendar/economicevents?date={day}"
_TIMEOUT_S = 15
#: What `earnings` sends: Nasdaq refuses a bare client.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json",
}

#: The day a reply's rows belong to, relative to the `date` asked for. Measured,
#: and used only when there is no ForexFactory week to calibrate against.
MEASURED_SHIFT = -1
_SHIFTS = (-1, 0, 1)

#: The pages the Market tab can show: this week and next, Monday-anchored.
_WEEKS_SHOWN = 2
#: Requests in flight at once. See `refresh_ahead`.
_PARALLEL = 4

#: Nasdaq's country -> ForexFactory's currency, and the adjective ForexFactory
#: puts before a euro member's release ("German Prelim CPI m/m").
_COUNTRIES = {
    "United States": ("USD", ""),
    "Euro Zone": ("EUR", ""),
    "Germany": ("EUR", "German"),
    "France": ("EUR", "French"),
    "Italy": ("EUR", "Italian"),
    "Spain": ("EUR", "Spanish"),
    "United Kingdom": ("GBP", ""),
    "Japan": ("JPY", ""),
    "Australia": ("AUD", ""),
    "New Zealand": ("NZD", ""),
    "Canada": ("CAD", ""),
    "Switzerland": ("CHF", ""),
    "China": ("CNY", ""),
}

#: Matched on Nasdaq's titles as sent, anchored so the variants it lists beside a
#: headline ("Core CPI Index", "Cleveland CPI", "Philly Fed CAPEX Index") stay Low.
_HIGH = re.compile("|".join([
    r"^(core )?(cpi|ppi|pce price index|retail sales)$",
    r"^nonfarm payrolls$", r"^unemployment rate$", r"^average hourly earnings$",
    r"^gdp$", r"^ism manufacturing pmi$", r"interest rate decision$",
    r"^fomc (statement|press conference|economic projections|meeting minutes)$",
    r"^fed chair \w+ speaks$",
]), re.I)
_MEDIUM = re.compile("|".join([
    r"^initial jobless claims$", r"^adp nonfarm employment change$",
    r"^ism (non-manufacturing|services) pmi$", r"^ism manufacturing prices$",
    r"^jolts job openings$", r"^cb consumer confidence$",
    r"^michigan (consumer sentiment|\d-year inflation expectations)$",
    r"^philadelphia fed manufacturing index$", r"^gdp price index$",
    r"president trump speaks$",
]), re.I)

_CLOCK = re.compile(r"(\d{2}):(\d{2})")
#: Nasdaq's two untimed spellings: a market holiday, and a release with no hour.
_UNTIMED = ("All Day", "24H")


def grade(title: str) -> str:
    """This journal's grade for one of Nasdaq's titles: High, Medium or Low."""
    if _HIGH.search(title):
        return "High"
    return "Medium" if _MEDIUM.search(title) else "Low"


def week_of(day: date) -> date:
    """The Sunday that starts ForexFactory's week holding `day`."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=MARKET_TZ).timestamp())


def _day(stamp: int) -> date:
    return datetime.fromtimestamp(stamp, MARKET_TZ).date()


def covered_weeks(conn: sqlite3.Connection) -> set[date]:
    """The Sundays of every week ForexFactory has served at least one row in."""
    rows = conn.execute(
        "SELECT starts_at FROM market_events WHERE source = ?", (FOREXFACTORY,))
    return {week_of(_day(row[0])) for row in rows}


def _rows(payload: object) -> list[dict]:
    """A reply's rows, or a refusal. Nasdaq answers HTTP 200 either way, so its
    own `status.rCode` decides, the rule `earnings.parse_earnings` follows."""
    if not isinstance(payload, dict):
        raise EventFetchError("Nasdaq's calendar reply was not an object")
    status = payload.get("status")
    code = str(status.get("rCode")) if isinstance(status, dict) else None
    if code != "200":
        raise EventFetchError(f"Nasdaq's calendar answered rCode {code}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise EventFetchError("Nasdaq's calendar reply carried no data")
    rows = data.get("rows")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise EventFetchError("Nasdaq's calendar rows were not a list")
    for row in rows:
        if not isinstance(row, dict) or {"gmt", "country", "eventName"} - set(row):
            raise EventFetchError(f"Nasdaq calendar row not understood: {row!r}")
    return rows


def _text(value: object) -> str | None:
    """Nasdaq's figure, or None for the `&nbsp;` and blank it sends for "none"."""
    text = html.unescape(str(value or "")).strip()
    return text or None


def parse_day(payload: object, *, day: date, requested: date) -> list[MarketEvent]:
    """One reply's rows as events on `day`, at the New York times Nasdaq sends.

    `requested` is the `date` asked for, kept in `raw` so a stored row says which
    reply it came from. Raises on a shape or a time it does not recognise, like
    `events.parse_events`: a day missing half its rows looks like a quiet day.
    """
    events: list[MarketEvent] = []
    seen: dict[tuple[int, str, str], int] = {}
    for row in _rows(payload):
        country = str(row["country"])
        if country not in _COUNTRIES:
            continue
        currency, adjective = _COUNTRIES[country]
        name = _text(row["eventName"]) or ""
        clock = str(row["gmt"]).strip()
        hour = _CLOCK.fullmatch(clock)
        if hour is None and clock not in _UNTIMED:
            raise EventFetchError(f"unreadable time {clock!r} for {name!r}")
        hh, mm = (int(hour[1]), int(hour[2])) if hour else (0, 0)
        starts_at = int(datetime(day.year, day.month, day.day, hh, mm,
                                 tzinfo=MARKET_TZ).timestamp())
        # Nasdaq lists a month-on-month and a year-on-year print under one title
        # ("Core CPI" twice), so the occurrence is part of the identity.
        key = (starts_at, country, name)
        seen[key] = seen.get(key, 0) + 1
        ident = f"{starts_at}|{country}|{name}|{seen[key]}"
        holiday = clock == "All Day" and name.startswith(f"{country} - ")
        events.append(MarketEvent(
            event_id=hashlib.sha256(ident.encode()).hexdigest()[:16],
            starts_at=starts_at,
            country=currency,
            title=(f"{adjective} {name}" if adjective and adjective not in name
                   and country not in name else name),
            impact="Holiday" if holiday else grade(name),
            forecast=_text(row.get("consensus")),
            previous=_text(row.get("previous")),
            raw={**row, "requested": requested.isoformat()},
        ))
    events.sort(key=lambda e: (e.starts_at, e.country, e.title))
    return events


def _words(title: str) -> frozenset[str]:
    # One-letter words go: they are the "m/m" and "y/y" ForexFactory appends.
    return frozenset(w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 1)


def _same(ours: str, theirs: str) -> bool:
    a, b = _words(ours), _words(theirs)
    return bool(a and b) and (a <= b or b <= a or 2 * len(a & b) >= len(a | b))


def calibrate(
    payload: object, *, requested: date, held: Iterable[tuple[int, str, str]],
) -> int:
    """Which day a reply for `requested` describes, as a shift in days.

    `held` is ForexFactory's `(starts_at, country, title)` rows over the days
    either side of `requested`. A shift scores one for each row it places on the
    minute of a ForexFactory row with the same currency and a matching title.
    Ties go to `MEASURED_SHIFT`. Raises when no shift places a single row.
    """
    at: dict[tuple[int, str], list[str]] = {}
    for starts_at, country, title in held:
        at.setdefault((starts_at, country), []).append(title)
    counts = {
        shift: sum(
            1 for event in parse_day(payload, day=requested + timedelta(days=shift),
                                     requested=requested)
            if any(_same(event.title, title)
                   for title in at.get((event.starts_at, event.country), ())))
        for shift in _SHIFTS
    }
    best = max(_SHIFTS, key=lambda shift: (counts[shift], shift == MEASURED_SHIFT))
    if not counts[best]:
        raise EventFetchError(
            f"Nasdaq's calendar for {requested} lines up with none of "
            f"ForexFactory's events either side of it, so its dates cannot be placed")
    return best


def _get(day: date, *, timeout: int = _TIMEOUT_S) -> object:
    """One day's reply. Every way the request can fail is an `EventFetchError`."""
    url = _URL.format(day=day.isoformat())
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise EventFetchError(f"Nasdaq's calendar for {day}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError,
            http.client.HTTPException) as exc:
        # `HTTPException` for a body cut short (IncompleteRead), as in `events`.
        raise EventFetchError(
            f"Nasdaq's calendar for {day}: {type(exc).__name__}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Ahead:
    """What one refresh wrote, for the job's detail line."""

    stored: int
    days: int
    #: The calibrated shift, so a fix on Nasdaq's side shows up as a 0 here.
    shift: int


def refresh_ahead(conn: sqlite3.Connection, *, now: datetime) -> Ahead:
    """Replace the Nasdaq rows on every day the Market tab shows and ForexFactory
    does not cover, from today on.

    The rows ForexFactory now covers are deleted FIRST and committed, so they go
    even when Nasdaq cannot be reached. Then one request to calibrate and one per
    day, and each fetched day's rows are replaced whole: a listing that moves an
    event must not leave the old row behind. Nothing is written until every day
    has answered, so a failure leaves the previous listing standing.

    The days are asked for `_PARALLEL` at a time because the page's Refresh waits
    on this: one at a time took 18.6s for eight days, measured.
    """
    covered = covered_weeks(conn)
    stale = [row[0] for row in conn.execute(
        "SELECT event_id, starts_at FROM market_events WHERE source = ?", (SOURCE,))
        if week_of(_day(row[1])) in covered]
    conn.executemany("DELETE FROM market_events WHERE source = ? AND event_id = ?",
                     [(SOURCE, event_id) for event_id in stale])
    conn.commit()

    # Never empty: the last day shown is a Sunday that starts a week
    # ForexFactory cannot have served yet.
    today = now.astimezone(MARKET_TZ).date()
    monday = today - timedelta(days=today.weekday())
    days = [day for day in (monday + timedelta(days=n) for n in range(7 * _WEEKS_SHOWN))
            if day >= today and week_of(day) not in covered]

    shift = MEASURED_SHIFT
    if covered:
        probe = max(covered) + timedelta(days=3)          # that week's Wednesday
        held = conn.execute(
            "SELECT starts_at, country, title FROM market_events WHERE source = ?"
            " AND starts_at >= ? AND starts_at < ?",
            (FOREXFACTORY, _midnight(probe - timedelta(days=1)),
             _midnight(probe + timedelta(days=2))),
        ).fetchall()
        shift = calibrate(_get(probe), requested=probe,
                          held=[(r[0], r[1], r[2]) for r in held])

    asked = [day - timedelta(days=shift) for day in days]
    with ThreadPoolExecutor(max_workers=_PARALLEL) as pool:
        replies = list(pool.map(_get, asked))
    fetched = {day: parse_day(reply, day=day, requested=on)
               for day, on, reply in zip(days, asked, replies, strict=True)}

    # Coverage again, after the network: a refresh running beside this one (the
    # job and Refresh) may have stored ForexFactory's new week meanwhile.
    covered = covered_weeks(conn)
    fetched = {day: events for day, events in fetched.items()
               if week_of(day) not in covered}
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    for day, events in fetched.items():
        conn.execute(
            "DELETE FROM market_events WHERE source = ? AND starts_at >= ?"
            " AND starts_at < ?",
            (SOURCE, _midnight(day), _midnight(day + timedelta(days=1))))
        conn.executemany(
            "INSERT OR REPLACE INTO market_events (source, event_id, starts_at,"
            " country, title, impact, forecast, previous, raw, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(SOURCE, e.event_id, e.starts_at, e.country, e.title, e.impact,
              e.forecast, e.previous, json.dumps(e.raw, sort_keys=True), stamp)
             for e in events])
    conn.commit()
    return Ahead(sum(len(events) for events in fetched.values()), len(days), shift)


@dataclass(frozen=True, slots=True)
class Refreshed:
    """One refresh of both feeds, for the three callers that run one: the `market`
    job, the page's Refresh and `optjournal market --fetch`. Each turns it into
    its own shape (a ledger row, an HTTP reply, an exit code), so the order of the
    two feeds and what each failure means are decided here once.
    """

    fetched: int
    stored: int
    #: ForexFactory's failure, None when its week was stored. `EventRateLimited`
    #: is a back-off rather than a fault.
    error: EventFetchError | None
    #: The days ahead, or why they were not refreshed.
    ahead: Ahead | EventFetchError

    @property
    def status(self) -> str:
        """The ledger's word, and ForexFactory's alone. Its week is the history,
        and the days ahead are rewritten on every run, so counting them would
        call almost every run `ok`, a rate-limited one included."""
        if self.error is None:
            return "ok" if self.stored else "nothing"
        return "nothing" if isinstance(self.error, EventRateLimited) else "failed"

    @property
    def detail(self) -> str:
        if isinstance(self.error, EventRateLimited):
            week = f"rate limited: {self.error}"
        elif self.error is not None:
            week = str(self.error)
        else:
            week = f"{self.fetched} fetched, {self.stored} stored"
        if isinstance(self.ahead, EventFetchError):
            return f"{week}; ahead failed: {self.ahead}"[:400]
        days = "day" if self.ahead.days == 1 else "days"
        return (f"{week}; {self.ahead.stored} ahead from {SOURCE} over "
                f"{self.ahead.days} {days}, date shift {self.ahead.shift:+d}")[:400]


def refresh_calendar(conn: sqlite3.Connection, *, now: datetime) -> Refreshed:
    """ForexFactory's week, then the days ahead of it whatever ForexFactory
    answered: an outage or a back-off there is when the days ahead are most worth
    having. A feed's failure is in the result rather than raised."""
    fetched = stored = 0
    error: EventFetchError | None = None
    try:
        events = fetch_events()
    except EventFetchError as exc:
        error = exc
    else:
        fetched, stored = len(events), store_events(conn, events)
    ahead: Ahead | EventFetchError
    try:
        ahead = refresh_ahead(conn, now=now)
    except EventFetchError as exc:
        ahead = exc
    return Refreshed(fetched, stored, error, ahead)
