"""Economic and geopolitical events, for the Market tab.

One feed, one module: fetch, parse, normalise, write. Deliberately NOT a Protocol
with a registry like `sources.py` -- that seam earned its abstraction by having a
second implementation in prospect and a schema whose identity depended on it. A
calendar has one feed today, and an abstraction with one implementation is
untested by construction. The `(source, event_id)` primary key is what keeps a
second feed possible; the Protocol can arrive with the second feed, which is when
it can first be verified.

THE SOURCE. ForexFactory publishes a keyless weekly JSON at
`nfs.faireconomy.media/ff_calendar_thisweek.json`: a flat list of
`{title, country, date, impact, forecast, previous}`, ISO dates carrying an
offset. Reached with `urllib` like `marketdata.fetch_bars`, so this adds no
dependency.

ONE WEEK ONLY, verified rather than assumed: `ff_calendar_nextweek`,
`_thismonth` and `_lastweek` all 404. Two consequences.

* No forward view beyond the current week, so a "next FOMC in 12 days" card
  cannot be built from this feed.
* The TABLE is the history. Rows persist rather than being replaced per fetch, so
  weekly fetches accumulate a past calendar the feed itself will not serve. That
  is why `store_events` corrects in place instead of deleting the week first.

WHAT IS NOT OURS TO JUDGE. `impact` is the feed's assessment of importance. It is
stored verbatim and attributed to the feed when displayed, the same way the AutoFX
markup is presented as IBKR's published rate rather than as a measured cost. An
unrecognised value RAISES rather than being bucketed as Low: a silently
downgraded event is exactly the kind of well-formed-but-wrong output this
project keeps finding.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = [
    "DEFAULT_COUNTRIES",
    "DEFAULT_IMPACTS",
    "IMPACTS",
    "IMPACT_ORDER",
    "SOURCE",
    "EventFetchError",
    "EventRateLimited",
    "MarketEvent",
    "default_scope",
    "fetch_events",
    "parse_events",
    "store_events",
    "upcoming",
]

#: The feed this module reads, and the value stored in `market_events.source`.
SOURCE = "forexfactory"

_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

#: Every `impact` the feed emits, observed across a full week (99 events).
#: Checked rather than mapped: see the module docstring on why an unknown value
#: raises instead of defaulting.
IMPACTS = frozenset({"High", "Medium", "Low", "Holiday"})

#: The same values, ordered by how much they move a book. A `frozenset` cannot
#: state that, and severity is the order a reader wants the choices offered in --
#: sorting by count instead would reshuffle the buttons as the week filled up.
#: `Holiday` sits last because it is a category rather than a grade: a closed
#: session is worth knowing about, but it is not "more than Low".
IMPACT_ORDER = ("High", "Medium", "Low", "Holiday")

#: The default VIEW: what an options book actually reacts to. Measured over a
#: real stored week -- 99 events, of which 82 are Low impact and 25 are EUR rows
#: that are all Low.
#:
#: `Medium` is included and `High` alone is not enough: High-only was 4 of that
#: 99, which is too quiet to consult -- the whole table holds 10 USD High rows
#: against 11 USD Medium, so excluding Medium discarded half the releases that
#: move a US book. `All` is a COUNTRY the feed really sends (its global rows --
#: OPEC meetings and the like), so a USD-only default that omitted it would
#: silently drop oil from an oil-sensitive book.
#:
#: Here rather than in `cli.py` (where it was first written) because the page and
#: the CLI both need the same default, and two copies would drift the moment one
#: of them gained a country. Narrowing the VIEW only -- `store_events` keeps
#: every country and impact the feed sends, which is the rule `ingest` learned
#: the hard way: a filter applied on the way IN cannot be undone without a
#: refetch, and the feed will not serve a past week.
DEFAULT_COUNTRIES = ("USD", "All")
DEFAULT_IMPACTS = ("High", "Medium")


def default_scope() -> str:
    """The default filter in words, for whoever is about to label it.

    Here rather than in each caller because the CLI and the web view both name
    this slice, and the CLI's copy had already drifted -- it printed "USD
    high-impact" while `DEFAULT_IMPACTS` held two grades. A label that narrates a
    filter it does not apply is worse than no label, so there is one of these.

    Countries are comma-separated: space-separated, "USD All high-impact" reads as
    a currency called "USD All" rather than as two choices, and `All` really is a
    country value here (the feed's global rows) rather than a wildcard.
    """
    return (", ".join(DEFAULT_COUNTRIES) + " "
            + "/".join(impact.lower() for impact in DEFAULT_IMPACTS) + "-impact")

#: Matching `marketdata`, whose Yahoo calls have the same shape and constraints.
_TIMEOUT_S = 20
_USER_AGENT = "Mozilla/5.0 (optjournal)"


class EventFetchError(RuntimeError):
    """The request failed, or the response was not readable as the feed."""


class EventRateLimited(EventFetchError):
    """The feed asked us to back off, and said for how long.

    Its own class because the two failures deserve different responses: a parse
    failure or a 404 means something changed and a human should look, while a 429
    means try later and nothing is wrong. A nightly cron should stay silent on
    this and shout about the other -- the same distinction `flex.FetchCooldown`
    draws against a real fetch error.

    Observed in practice, not anticipated: the feed is behind Cloudflare and
    returned 429 with `retry-after: 92` after a handful of requests while this
    module was being written. It was still limiting three minutes later, so the
    window is real and not a formality.
    """

    def __init__(self, retry_after_s: int) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(
            f"the calendar feed is rate limiting; retry in {retry_after_s}s"
        )


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One event, in the shape the journal stores.

    Frozen for the reason `NormalisedFill` is: a parser cannot hand back a
    half-built row that a later line mutates.

    `event_id` is OURS. The feed supplies no id, so it is a short hash of
    `(starts_at, country, title)` -- verified distinct across all 99 rows of a
    real week. That makes a re-fetch correct a revised forecast in place rather
    than adding a second copy of the same event.
    """

    event_id: str
    #: Epoch seconds UTC. The feed sends an offset; the instant is what is stored,
    #: and the page renders it in `bars.MARKET_TZ` like every other stamp here.
    starts_at: int
    country: str
    title: str
    impact: str
    forecast: str | None
    previous: str | None
    raw: dict


def _event_id(starts_at: int, country: str, title: str) -> str:
    key = f"{starts_at}|{country}|{title}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def parse_events(payload: object) -> list[MarketEvent]:
    """The feed's JSON as `MarketEvent`s, oldest first.

    Separate from `fetch_events` so the parser is testable against a captured
    response with no network -- the same split `marketdata.parse_chart` uses, and
    for the same reason: the parse is where drift shows up.

    Raises on a shape it does not recognise rather than skipping rows. A calendar
    missing half its events looks like a quiet week.
    """
    if not isinstance(payload, list):
        raise EventFetchError(
            f"expected a list of events, got {type(payload).__name__}"
        )

    events: list[MarketEvent] = []
    for row in payload:
        if not isinstance(row, dict):
            raise EventFetchError(f"event is not an object: {row!r}")
        missing = {"title", "country", "date", "impact"} - set(row)
        if missing:
            raise EventFetchError(f"event missing {sorted(missing)}: {row!r}")

        impact = str(row["impact"])
        if impact not in IMPACTS:
            raise EventFetchError(
                f"unknown impact {impact!r} for {row['title']!r}. The feed's "
                f"vocabulary changed; add it to IMPACTS deliberately rather than "
                f"letting an event be filed under the wrong importance."
            )

        stamp = str(row["date"])
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise EventFetchError(f"unreadable date {stamp!r}") from exc
        # An offset-naive stamp would be an assumption about the feed's zone, and
        # the feed has always sent one. Treat its absence as drift.
        if when.tzinfo is None:
            raise EventFetchError(f"date {stamp!r} carries no timezone offset")

        starts_at = int(when.timestamp())
        title = str(row["title"])
        country = str(row["country"])
        events.append(MarketEvent(
            event_id=_event_id(starts_at, country, title),
            starts_at=starts_at,
            country=country,
            title=title,
            impact=impact,
            # Empty strings mean "not published", which is not the same as zero.
            forecast=str(row.get("forecast") or "") or None,
            previous=str(row.get("previous") or "") or None,
            raw=dict(row),
        ))
    events.sort(key=lambda e: (e.starts_at, e.country, e.title))
    return events


def fetch_events(*, url: str = _URL, timeout: int = _TIMEOUT_S) -> list[MarketEvent]:
    """This week's events from the feed.

    No local cooldown, unlike `flex.fetch`: this spends nothing against a lockout
    budget and returns the same bytes all week, so a repeat is wasteful rather
    than damaging. But the SERVER rate limits -- see `EventRateLimited` -- so a
    caller that retries in a loop will be refused, and the nightly cron is the
    intended caller for that reason.

    Raises `EventRateLimited` on 429, carrying the server's own `retry-after`, so
    a cron can distinguish "try later" from "something changed".
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # Default to a minute when the header is absent or unparseable: the
            # observed value was 92s, and guessing short would just be refused
            # again rather than causing harm.
            raw = exc.headers.get("retry-after") if exc.headers else None
            try:
                retry_after = int(str(raw).strip())
            except (TypeError, ValueError):
                retry_after = 60
            raise EventRateLimited(retry_after) from exc
        raise EventFetchError(f"fetching {url} failed: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EventFetchError(f"fetching {url} failed: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EventFetchError(f"{url} did not return JSON: {exc}") from exc
    return parse_events(payload)


def store_events(
    conn: sqlite3.Connection, events: list[MarketEvent], *, source: str = SOURCE
) -> int:
    """Write events, correcting rows already held. Returns the number written.

    An upsert rather than a delete-then-insert of the week: the feed serves one
    week, so deleting first would throw away every past event the journal has
    accumulated -- which is the only place that history exists.

    `forecast` and `previous` are updated because they are revised between the
    announcement and the release, and a stale forecast beside a released number
    reads as a miss that never happened.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    written = 0
    for event in events:
        conn.execute(
            "INSERT INTO market_events (source, event_id, starts_at, country,"
            " title, impact, forecast, previous, raw, fetched_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source, event_id) DO UPDATE SET"
            " impact=excluded.impact, forecast=excluded.forecast,"
            " previous=excluded.previous, raw=excluded.raw,"
            " fetched_at=excluded.fetched_at",
            (source, event.event_id, event.starts_at, event.country, event.title,
             event.impact, event.forecast, event.previous,
             json.dumps(event.raw, sort_keys=True), now),
        )
        written += 1
    conn.commit()
    return written


def upcoming(
    conn: sqlite3.Connection,
    *,
    start: int,
    end: int,
    countries: tuple[str, ...] = (),
    impacts: tuple[str, ...] = (),
) -> list[dict]:
    """Stored events in `[start, end]`, oldest first.

    Filtering is the caller's to state rather than this function's to assume: the
    Market tab defaults to `DEFAULT_COUNTRIES` / `DEFAULT_IMPACTS`, but the table
    holds ten countries and a reader may want any slice of them. An empty filter
    means no filter, the same convention `ingest.ASSET_FILTER_ALL` uses -- and the
    same one the web view's two filter axes use for an empty selection.
    """
    where = ["starts_at BETWEEN ? AND ?"]
    params: list[object] = [start, end]
    if countries:
        where.append(f"country IN ({','.join('?' * len(countries))})")
        params.extend(countries)
    if impacts:
        where.append(f"impact IN ({','.join('?' * len(impacts))})")
        params.extend(impacts)
    rows = conn.execute(
        f"SELECT * FROM market_events WHERE {' AND '.join(where)}"
        " ORDER BY starts_at, country, title",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
