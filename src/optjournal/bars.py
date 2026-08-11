"""Which bars this journal needs, and where they are kept.

``marketdata`` knows how to ask a source for OHLCV and nothing else. This
module owns the journal-shaped half: which contract over which window, the
idempotent write, and the timestamped series the replay chart draws.

**The fetch set is derived, not scheduled.** A position's bar window is a
finite, immutable interval -- once a position closes, the bars covering it
never change again. So there is nothing to poll: the manifest falls out of the
episodes the accounting layer already computed (``history.build_history``),
each of which carries its own ``conid``, ``opened_at`` and ``closed_at``. No
new bookkeeping, and no cadence to tune. While the book is flat the manifest is
empty and a run fetches nothing.

Three consequences worth stating because they are easy to "fix" wrongly:

* **Episodes, not lifecycles, are the source.** An episode exists per contract
  round trip, including for a contract the archive holds no fills for at all
  (``Episode.snapshot_only``) -- which is how the LEAP, opened before the
  archive begins, gets a window at all. Lifecycles group episodes for display;
  they would lose exactly the case with no fills to group.

* **Merging is per ``(conid, bar_size)``, never per conid.** One underlying can
  legitimately want two windows at two granularities -- TSLA hourly across a
  five-day short put, and TSLA daily across a LEAP's whole life. Those are
  different keys and both are kept; collapsing them by conid would silently
  pick one and lose the other.

* **Option legs are daily whatever the window.** Not a policy choice: the
  public source serves option contracts at daily granularity only (an intraday
  request returns an empty series). Underlyings get hourly for a short window
  and daily for a long one, so a five-day trade shows an hourly underlying
  against daily option marks. That asymmetry is real and the chart must say so
  rather than imply the option line has intraday resolution.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from optjournal.blackscholes import (
    bs_delta,
    bs_price,
    expected_move,
    implied_vol,
)
from optjournal.clock import MARKET_TZ, epoch_et, et_day, expiry_epoch
from optjournal.history import build_history
from optjournal.marketdata import BAR_SIZES, SOURCE_RANK, Bar, BarFetchError, fetch_bars

__all__ = [
    "BackfillOutcome",
    "BarRequest",
    "SessionAudit",
    "audit_perishable",
    "backfill_bars",
    "bars_manifest",
    "band_contracts",
    "last_traded_day",
    "market_traded_on",
    "BandContract",
    "close_series",
    "ReplayLeg",
    "ReplaySeries",
    "delta_around",
    "expected_move_band",
    "modelled_marks",
    "replay_model",
    "replay_bars",
    "upsert_bars",
]

#: Calendar days of context either side of a holding window. Four rather than
#: two because a session pad has to survive a weekend: two trading days before
#: a Monday open is the previous Thursday.
PAD_DAYS = 4

#: Above this many calendar days a window is charted daily rather than hourly.
#: Roughly four trading weeks: a 5-8 day hold wants hourly (21-42 points),
#: while a months-long position wants daily or the series becomes thousands of
#: points for no added insight.
HOURLY_LIMIT_DAYS = 40

#: Context either side of the trade window when CHARTING, as a FRACTION of the
#: window itself. PAD_DAYS is what gets FETCHED (wide is free and already
#: stored); this is what gets drawn.
#:
#: Proportional rather than fixed, because a fixed count cannot serve both ends
#: of the range. The previous rule kept 7 hourly bars -- exactly one session, and
#: a reasonable answer for a multi-day hold -- but on a one-day-old position that
#: is 8 pad bars around 6 real ones, 57% of the chart, and on a 0DTE held two
#: hours it would be 14 around 3. Measured on the real journal, not estimated.
#:
#: 0.15 rather than a larger share because the preference is explicitly for LESS
#: padding: it holds the pad at or under ~25% of the chart for every window
#: length the journal actually produces, where 0.25 peaked at 40%. Tabulated
#: across 1..757 bars before choosing, not guessed.
CONTEXT_FRACTION = 0.15

#: Floor and ceiling on that fraction, in bars.
#:
#: The floor is ONE bar. It exists so the shortest trades keep an answer to "what
#: was it doing just before I entered" -- pure proportionality gives a one-bar
#: trade no lead-in at all, which loses the only thing the padding is for. One
#: rather than two because two makes a 1-bar trade 80% padding, which is worse
#: than the fixed rule this replaces; at a floor of one it is 67%, and a chart
#: with a single bar inside has no good answer anyway.
#:
#: The ceiling exists because context stops paying at some width: three years of
#: daily bars around a LEAP is not context, it is a different chart. Per bar size
#: because a session is 7 hourly bars but 1 daily one, so the same number means
#: different things -- 7 hourly bars is one session of lead-in, 5 daily bars is a
#: trading week.
CONTEXT_MIN_BARS = 1
CONTEXT_MAX_BARS = {"1h": 7, "1d": 5}

#: Calendar days of bars READ either side of a window, before trimming. Wider
#: than any ceiling above can keep, so the trim decides the chart rather than the
#: query silently capping it. Free: these rows are already stored locally.
_READ_PAD_DAYS = 14

#: How far back to look for a contract whose opening fill predates the archive.
#: The source truncates to whatever it actually holds -- a 2025-01-01 request
#: for the LEAP returned bars from 2025-02-03, the contract's listing date --
#: so asking wide costs nothing and needs no guess about an unknown open date.
SNAPSHOT_FLOOR_DAYS = 1100

#: Bars DRAWN for a contract whose open date is unknown. Fetching 1100 days is
#: right (see above); charting all of them is not -- the real LEAP produced 757
#: daily points spanning three years for a position held about one, and a chart
#: that wide answers a different question than "how has this position behaved".
#:
#: A count rather than a date, because the honest statement is "we do not know
#: when this was opened" and any date would be a guess. Anchored on the RIGHT
#: edge: the most recent bars are the ones a holder is actually looking at, and
#: truncating the left says nothing false -- the series simply starts where the
#: chart starts, as it already does for a contract whose history begins at its
#: listing date.
#:
#: Two years of sessions, so a LEAP still shows its whole life when its life is
#: shorter than that, and the longest-dated one shows the part that matters.
SNAPSHOT_DRAW_BARS = 504

#: Calendar days of daily history to keep for a WATCHED symbol.
#:
#: Sized from what the watchlist reports: a 20-session realised vol needs 21
#: closes, and 60 calendar days is ~41 sessions -- enough for the vol plus a
#: week's change, with room for holidays. Comfortably past HOURLY_LIMIT_DAYS, so
#: `_bar_size_for` resolves a watch window to daily without a special case.
WATCH_LOOKBACK_DAYS = 60

_COLUMNS = (
    "conid", "symbol", "bar_size", "ts",
    "open", "high", "low", "close", "volume",
    "source", "fetched_at",
)

#: The trust order from marketdata, expressed in SQL so the upsert's guard and
#: the Python constant cannot drift apart.
_RANK_CASE = "CASE price_bars.source " + " ".join(
    f"WHEN '{name}' THEN {rank}" for name, rank in sorted(SOURCE_RANK.items())
) + " ELSE 0 END"

_UPSERT = f"""
INSERT INTO price_bars ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" for _ in _COLUMNS)})
ON CONFLICT(conid, bar_size, ts) DO UPDATE SET
  symbol = excluded.symbol,
  open = excluded.open, high = excluded.high,
  low = excluded.low, close = excluded.close,
  volume = excluded.volume,
  source = excluded.source, fetched_at = excluded.fetched_at
WHERE ? >= ({_RANK_CASE})
"""


@dataclass(frozen=True)
class BarRequest:
    """One contract, one granularity, one window. ``start``/``end`` are epoch
    seconds; ``kind`` is ``option`` or ``underlying`` for reporting only.
    """

    conid: str
    symbol: str
    bar_size: str
    start: int
    end: int
    kind: str
    #: True when this data exists only while the session is running and can
    #: never be backfilled -- an option's intraday bars. Everything else can be
    #: re-fetched at leisure, so only these justify polling during market hours.
    perishable: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.conid, self.bar_size)


@dataclass(frozen=True)
class BackfillOutcome:
    """What a backfill run did. ``skipped`` counts requests the source knew but
    held no bars for -- the option-intraday case -- which is not a failure.
    """

    requested: int = 0
    written: int = 0
    skipped: int = 0
    failures: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


def _epoch(stamp: str | None) -> int | None:
    """Epoch seconds from a journal timestamp, or None when unparseable.

    Journal stamps are ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM:SS``; only the date
    matters for a bar window, so the time is tolerated and ignored.
    """
    if not stamp:
        return None
    text = str(stamp).strip()[:10]
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(day.timestamp())


def _bar_size_for(start: int, end: int, *, kind: str) -> str:
    """Hourly for a short underlying window, daily otherwise.

    An option leg's HISTORY is daily-only: the source keeps no intraday option
    bars for a past session, so asking hourly over a historical window spends a
    request to receive an empty series. Measured pre-market, every contract in
    this book returned zero hourly bars while the underlying still returned five
    days of them -- the retention is asymmetric, not merely short.

    The live session is the exception, and `bars_manifest` handles it separately:
    an option DOES serve hourly bars while its session is in progress. Those can
    only be collected as they happen, never backfilled.
    """
    if kind == "option":
        return "1d"
    span_days = (end - start) / 86400
    return "1h" if span_days <= HOURLY_LIMIT_DAYS else "1d"


def _underlying_conids(conn: sqlite3.Connection) -> dict[str, str]:
    """Underlying symbol -> IBKR conid, from anywhere the journal states it.

    The LEAP has no trade row, so its underlying id has to come from another
    contract on the same name or from the securities table. A symbol that
    resolves nowhere is skipped by the caller rather than given a made-up key.
    """
    found: dict[str, str] = {}
    for sql in (
        "SELECT DISTINCT underlying_symbol AS sym, underlying_conid AS cid "
        "FROM trades WHERE underlying_symbol IS NOT NULL AND underlying_conid IS NOT NULL",
        "SELECT DISTINCT underlying_symbol AS sym, underlying_conid AS cid "
        "FROM securities WHERE underlying_symbol IS NOT NULL AND underlying_conid IS NOT NULL",
    ):
        for row in conn.execute(sql):
            symbol = str(row["sym"]).strip()
            conid = str(row["cid"]).strip()
            if symbol and conid:
                found.setdefault(symbol, conid)
    return found


def bars_manifest(
    conn: sqlite3.Connection, *, now: datetime | None = None,
    perishable_only: bool = False,
) -> list[BarRequest]:
    """Every (contract, granularity, window) the journal's own positions imply.

    Windows for the same ``(conid, bar_size)`` are merged to their union, so an
    overlapping pair becomes one request rather than two.

    ``perishable_only`` narrows the result to data that cannot be collected
    later: the intraday bars of an option that is still open. That is what a
    market-hours poll should ask for, and asking for the whole manifest seven
    times a session would re-fetch three years of settled daily history to
    collect a handful of new hourly rows.
    """
    moment = now or datetime.now(UTC)
    ceiling = int((moment + timedelta(days=1)).timestamp())
    pad = PAD_DAYS * 86400
    floor_start = int((moment - timedelta(days=SNAPSHOT_FLOOR_DAYS)).timestamp())

    underlyings = _underlying_conids(conn)
    merged: dict[tuple[str, str], BarRequest] = {}

    def add(
        conid: str, symbol: str, start: int, end: int, kind: str, *, open_: bool
    ) -> None:
        size = _bar_size_for(start, end, kind=kind)
        _emit(conid, symbol, start, end, kind, size)
        # An hourly underlying window ALSO needs its daily series, because the
        # expected-move band solves implied vol by pairing an option's daily
        # close with the underlying's daily close for the same session. Without
        # this, a short trade got hourly underlying bars and no daily ones, so
        # there was nothing to pair and the band silently never appeared -- it
        # worked only for TSLA, whose LEAP happened to force a daily request on
        # the same conid. One extra request per underlying per window.
        if kind == "underlying" and size == "1h":
            _emit(conid, symbol, start, end, kind, "1d")
        # A STILL-OPEN option whose replay is drawn hourly also gets an hourly
        # request, and it is the only perishable one in the manifest. Gated on
        # the underlying's own granularity rule rather than a second threshold,
        # so the option is collected hourly exactly when the chart renders it
        # hourly: the strangles qualify, the LEAP does not -- months of hourly
        # option bars would be thousands of rows the chart never draws.
        if kind == "option" and open_ and _bar_size_for(
            start, end, kind="underlying"
        ) == "1h":
            _emit(conid, symbol, start, end, kind, "1h", perishable=True)

    def _emit(
        conid: str, symbol: str, start: int, end: int, kind: str, size: str,
        *, perishable: bool = False,
    ) -> None:
        request = BarRequest(
            conid=str(conid), symbol=str(symbol), bar_size=size,
            start=start, end=end, kind=kind, perishable=perishable,
        )
        existing = merged.get(request.key)
        if existing is None:
            merged[request.key] = request
            return
        merged[request.key] = BarRequest(
            conid=existing.conid, symbol=existing.symbol, bar_size=size,
            start=min(existing.start, request.start),
            end=max(existing.end, request.end),
            kind=existing.kind,
            # Sticky through a merge: two windows for one contract are the same
            # rows, and if either was only collectable live then so is the union.
            perishable=existing.perishable or request.perishable,
        )

    report = build_history(conn, asset_category="OPT")
    for episode in report.episodes:
        opened = _epoch(episode.opened_at)
        closed = _epoch(episode.closed_at)
        # No opening fill in the archive: ask wide and let the source truncate
        # to the contract's real listed life. Guessing an open date from the
        # cost basis would match several sessions and state a fact we lack.
        start = (opened - pad) if opened is not None else floor_start
        end = min((closed + pad) if closed is not None else ceiling, ceiling)
        if end <= start:
            continue
        open_ = not episode.is_closed
        add(episode.conid, episode.symbol, start, end, "option", open_=open_)
        name = (episode.underlying_symbol or "").strip()
        conid = underlyings.get(name)
        if name and conid:
            add(conid, name, start, end, "underlying", open_=open_)

    # Watched symbols. Without this a watchlist row is permanently blank: the
    # manifest derives windows from POSITIONS, so a symbol you merely watch has
    # no bars at all -- measured, GOOG had 5 daily closes and PLTR 4, which is
    # not enough for a 20-day realised vol.
    #
    # Daily only, and never perishable. A watchlist wants a trend and a
    # volatility, both of which daily closes answer; hourly bars for a symbol
    # holding no position would multiply requests for a column nobody reads at
    # that resolution.
    for row in conn.execute("SELECT symbol FROM watchlist"):
        name = str(row["symbol"] or "").strip().upper()
        if not name:
            continue
        # A watched symbol has no conid -- it is not a contract this account has
        # traded, so IBKR has never named it here. `price_bars` is keyed on conid,
        # so it needs a stable synthetic one, and `watch:SYMBOL` is both stable
        # and impossible to collide with an IBKR integer id. The watchlist reads
        # bars BY SYMBOL, so nothing downstream depends on the shape of this key;
        # it exists only to keep the primary key honest. If the same name is later
        # traded, the real conid's rows arrive alongside and the symbol lookup
        # finds both, which is why this is `setdefault`-like rather than a
        # rewrite: the real id wins nothing and loses nothing.
        add(underlyings.get(name) or f"watch:{name}", name,
            int((moment - timedelta(days=WATCH_LOOKBACK_DAYS)).timestamp()),
            ceiling, "watchlist", open_=False)

    requests = [r for r in merged.values() if r.perishable or not perishable_only]
    return sorted(requests, key=lambda r: (r.kind, r.symbol, r.bar_size))


def upsert_bars(
    conn: sqlite3.Connection,
    *,
    conid: str,
    symbol: str,
    bar_size: str,
    source: str,
    bars: list[Bar],
) -> int:
    """Write bars idempotently. Returns the number of rows offered.

    Re-running a backfill is free: the primary key makes a repeat a no-op
    update, and the rank guard means a lower-trust source cannot overwrite a
    higher-trust row it happens to also cover.
    """
    if bar_size not in BAR_SIZES:
        raise ValueError(f"unsupported bar size {bar_size!r}")
    if not bars:
        return 0
    rank = SOURCE_RANK.get(source, 0)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    conn.executemany(
        _UPSERT,
        [
            (
                str(conid), str(symbol), bar_size, bar.ts,
                bar.open, bar.high, bar.low, bar.close, bar.volume,
                source, stamp, rank,
            )
            for bar in bars
        ],
    )
    conn.commit()
    return len(bars)


def backfill_bars(
    conn: sqlite3.Connection,
    *,
    source: str = "yahoo",
    fetch: Callable[..., list[Bar]] = fetch_bars,
    now: datetime | None = None,
    perishable_only: bool = False,
) -> BackfillOutcome:
    """Fetch and store every window the manifest names.

    ``fetch`` is injected so the suite can exercise the whole path without a
    network call. A per-request failure is collected rather than raised: one
    unreachable contract should not abandon the rest of the book.

    ``perishable_only`` restricts the run to the intraday bars of open options --
    the market-hours poll. See :func:`bars_manifest`.
    """
    requests = bars_manifest(conn, now=now, perishable_only=perishable_only)
    written = skipped = 0
    failures: list[str] = []
    for request in requests:
        try:
            bars = fetch(
                request.symbol,
                bar_size=request.bar_size,
                start=request.start,
                end=request.end,
                source=source,
            )
        except BarFetchError as exc:
            failures.append(str(exc))
            continue
        if not bars:
            skipped += 1
            continue
        written += upsert_bars(
            conn, conid=request.conid, symbol=request.symbol,
            bar_size=request.bar_size, source=source, bars=bars,
        )
    return BackfillOutcome(
        requested=len(requests), written=written,
        skipped=skipped, failures=tuple(failures),
    )


@dataclass(frozen=True, slots=True)
class SessionAudit:
    """Whether one session's perishable bars actually landed.

    Exists because the live poll treats a fetch failure as a quiet retry, which
    is right seven times a session and wrong once a day: the intraday series is
    cumulative within a session, so a single lost poll costs nothing, but a
    session where EVERY poll failed is gone for good and says nothing about it.
    One question, asked once, about the only thing that cannot be recovered.
    """

    #: The ET trading date examined, YYYY-MM-DD.
    day: str
    #: Whether the market traded that day at all -- see market_traded_on.
    market_traded: bool
    #: Contracts that were asked for AND have hourly bars for the day.
    covered: tuple[str, ...]
    #: Contracts that were asked for and have none. The reportable set.
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when there is nothing for a human to do.

        A day the market did not trade is not a fault, and neither is a book
        holding nothing eligible -- both produce no bars for entirely correct
        reasons, and alarming on either would train the reader to ignore this.
        """
        return not self.market_traded or not self.missing


def market_traded_on(conn: sqlite3.Connection, day: str) -> bool:
    """Whether any UNDERLYING has hourly bars for the given ET date.

    The holiday oracle, and deliberately not a hardcoded calendar. US markets
    shut around nine days a year plus the odd half-session; a date list would
    need maintaining forever and would be wrong the first year it was not. The
    underlying's own hourly series answers the question directly: unlike an
    option's, it is retained for days and is re-fetched by the daily backfill,
    so by the time this runs it is an independent witness to whether the session
    happened. No bars for anyone means the market was shut; bars for the
    underlying but none for an option means collection failed.
    """
    conids = set(_underlying_conids(conn).values())
    if not conids:
        return False
    placeholders = ", ".join("?" for _ in conids)
    rows = conn.execute(
        f"SELECT ts FROM price_bars WHERE bar_size = '1h' AND conid IN ({placeholders})",
        sorted(conids),
    ).fetchall()
    return any(et_day(int(row["ts"])) == day for row in rows)


def last_traded_day(
    conn: sqlite3.Connection, *, now: datetime | None = None, lookback: int = 10
) -> str | None:
    """The most recent ET date strictly before today that an underlying traded.

    Walking backwards from yesterday rather than subtracting one day skips
    weekends and holidays with the same mechanism and no calendar: the first day
    that has underlying bars is the last session. Bounded by ``lookback``
    because the source only retains a few days of hourly, so a longer walk would
    silently pick a date whose option bars were never collectable anyway.
    """
    moment = (now or datetime.now(UTC)).astimezone(MARKET_TZ)
    for back in range(1, lookback + 1):
        day = (moment - timedelta(days=back)).strftime("%Y-%m-%d")
        if market_traded_on(conn, day):
            return day
    return None


def _first_fill_epochs(conn: sqlite3.Connection) -> dict[str, int]:
    """Earliest fill epoch per option conid, for excluding same-day openings."""
    rows = conn.execute(
        "SELECT conid, MIN(first_fill_at) AS opened FROM trade_legs"
        " WHERE asset_category = 'OPT' GROUP BY conid"
    ).fetchall()
    out: dict[str, int] = {}
    for row in rows:
        stamp = epoch_et(row["opened"])
        if stamp is not None:
            out[str(row["conid"])] = stamp
    return out


def audit_perishable(
    conn: sqlite3.Connection,
    *,
    day: str | None = None,
    now: datetime | None = None,
) -> SessionAudit:
    """Did the given session actually produce the option bars it should have?

    Eligibility comes from ``bars_manifest(perishable_only=True)`` -- the exact
    set the live poll asks for -- rather than from a second rule of its own. An
    audit with its own idea of what should have been collected drifts from the
    collector and then reports on a book neither of them has.

    One correctness cost of reusing the manifest, stated because it bounds what
    this can catch: the manifest is derived from what is open NOW, so a contract
    that CLOSED during the audited session is no longer eligible and goes
    unexamined. The reverse case is handled -- a contract opened after the
    session cannot have bars for it, and counting that as missing would alarm
    every time a position was opened.
    """
    audited = day or last_traded_day(conn, now=now)
    if audited is None:
        # No underlying bars for any recent day. Either the journal is empty or
        # the daily backfill has not run; both are the daily job's business, not
        # this one's, and reporting them here would double up on its failure.
        moment = (now or datetime.now(UTC)).astimezone(MARKET_TZ)
        return SessionAudit(
            day=(moment - timedelta(days=1)).strftime("%Y-%m-%d"),
            market_traded=False, covered=(), missing=(),
        )
    traded = market_traded_on(conn, audited)
    if not traded:
        # Nothing was collectable, so nothing is missing. Walking the manifest
        # anyway would fill `missing` on a day the market was shut -- true of the
        # bars and false about the world, and a payload whose `missing` list
        # contradicts its own `ok` is worse than one that says nothing.
        return SessionAudit(day=audited, market_traded=False, covered=(), missing=())
    end_of_day = int(
        datetime.strptime(audited, "%Y-%m-%d")
        .replace(hour=23, minute=59, tzinfo=MARKET_TZ).timestamp()
    )
    opened = _first_fill_epochs(conn)
    covered: list[str] = []
    missing: list[str] = []
    for request in bars_manifest(conn, now=now, perishable_only=True):
        entry = opened.get(request.conid)
        if entry is not None and entry > end_of_day:
            continue  # opened after the session; it could not have bars for it
        stamps = conn.execute(
            "SELECT ts FROM price_bars WHERE conid = ? AND bar_size = '1h'",
            (request.conid,),
        ).fetchall()
        found = any(et_day(int(row["ts"])) == audited for row in stamps)
        (covered if found else missing).append(request.symbol)
    return SessionAudit(
        day=audited, market_traded=traded,
        covered=tuple(sorted(covered)), missing=tuple(sorted(missing)),
    )


def close_series(
    conn: sqlite3.Connection,
    conid: str,
    *,
    bar_size: str,
    start: int | None = None,
    end: int | None = None,
) -> list[tuple[int, float]]:
    """Timestamped closes for one conid at one granularity, oldest first.

    ``bar_size`` is required and single, never "whatever this conid has". An
    underlying legitimately holds BOTH hourly across a five-day trade and daily
    across a LEAP; selecting both and ordering by ts would splice three years of
    daily onto two weeks of hourly and draw the join as a price move.

    Timestamps are carried, unlike the row miniature this replaces, but for the
    LABELS rather than for x: the chart plots bar position, because sessions are
    not evenly spaced and a linear time axis spends most of its width on hours
    the market was shut. Which bar a tick names still has to be true, and that
    needs the stamp.

    Null closes are dropped. A quiet strike genuinely has no print, and the
    window is clipped inclusively so a caller asking for a trade's span gets
    exactly that span.
    """
    clauses = ["bar_size = ?", "conid = ?", "close IS NOT NULL"]
    args: list[Any] = [bar_size, str(conid)]
    if start is not None:
        clauses.append("ts >= ?")
        args.append(int(start))
    if end is not None:
        clauses.append("ts <= ?")
        args.append(int(end))
    rows = conn.execute(
        f"SELECT ts, close FROM price_bars WHERE {' AND '.join(clauses)} ORDER BY ts",
        args,
    ).fetchall()
    return [(int(r["ts"]), float(r["close"])) for r in rows]


@dataclass(frozen=True, slots=True)
class ReplaySeries:
    """The underlying series one replay chart draws, and which grid it is on.

    A dataclass rather than the three-key dict this returned, built at three
    separate exits and read by string key at six call sites. `bars["conid"]` is a
    `KeyError` at render time if it is ever mistyped, and the empty case had to
    restate all three keys to stay the same shape as the populated one.

    `bar_size` is carried even when `points` is empty, because the panel says
    which grid it drew: a reader comparing two charts must not have to guess
    whether a flat stretch is a quiet week or a coarser grid.
    """

    #: None when the symbol resolves to no underlying the journal knows.
    conid: str | None
    #: The grid actually drawn, or the preferred one when nothing was stored.
    bar_size: str | None
    points: list[tuple[int, float]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReplayLeg:
    """One leg, with everything the modelled P&L needs to follow it over time."""

    conid: str
    strike: float
    right: str
    expiry: str
    multiplier: float = 100.0
    #: (epoch, signed quantity delta, price per unit) per fill, oldest first.
    fills: tuple[tuple[int, float, float], ...] = ()
    #: For a contract with no fills anywhere: the position held throughout the
    #: window and the price it was acquired at, from the snapshot's cost basis.
    seed_quantity: float = 0.0
    seed_price: float = 0.0


@dataclass(frozen=True, slots=True)
class BandContract:
    """One leg, reduced to what a vol solve needs and nothing else."""

    conid: str
    strike: float
    right: str
    expiry: str
    #: (epoch, price per unit) from the trade's OWN fills. A fill is an option
    #: price the market actually charged, so it is a vol observation in exactly
    #: the way a daily close is -- and the only one available for a session the
    #: price source has no history for. See _vol_series.
    anchors: tuple[tuple[int, float], ...] = ()


def _spot_at(points: list[tuple[int, float]], ts: int) -> float | None:
    """Underlying price at ``ts``, interpolated across the bar it falls in.

    Used to place a value on the chart's own grid, NOT to pair with an option
    price -- see expected_move_band for why that pairing has to be daily-to-daily.
    """
    if not points or ts < points[0][0] or ts > points[-1][0]:
        return None
    for index in range(1, len(points)):
        if points[index][0] >= ts:
            prev_ts, prev_px = points[index - 1]
            next_ts, next_px = points[index]
            width = (next_ts - prev_ts) or 1
            return prev_px + ((ts - prev_ts) / width) * (next_px - prev_px)
    return points[-1][1]


def _vol_series(
    conn: sqlite3.Connection,
    contracts: list[BandContract],
    points: list[tuple[int, float]],
    underlying_conid: str | None,
) -> dict[str, list[tuple[int, float]]]:
    """Implied vol per leg per session, from each option's OWN daily closes.

    The reference implementation this feature imitates labels its band
    "IV ref: VIX" -- S&P 500 implied vol applied to a gold ETF -- because the
    underlying is all it has. We hold the contract's prices, so we can ask what
    the market charged for THIS contract.

    An option's daily close is paired with the underlying's daily close for the
    same session, matched on the ET trading DAY (see et_day for why the raw
    timestamps cannot be compared). Pairing against the CHART series instead
    looks equivalent and is not: a chart may be hourly, and an option's daily
    stamp then falls in the overnight gap between two hourly bars, so
    interpolating there prices the option against a spot the market never showed
    and books the discrepancy as volatility.

    Per LEG rather than averaged, because pricing a leg wants its own vol; the
    band averages afterwards, since expected move is a property of the underlying
    and a strangle's two legs are two observations of it.
    """
    if not points or not contracts or not underlying_conid:
        return {}
    low, high = points[0][0], points[-1][0]
    daily = {
        et_day(stamp): close
        for stamp, close in close_series(conn, str(underlying_conid), bar_size="1d")
    }
    out: dict[str, list[tuple[int, float]]] = {}
    for contract in contracts:
        expiry = expiry_epoch(contract.expiry)
        if expiry is None:
            continue
        # A day of slack either side because the join is BY DAY and the source
        # stamps the two series differently: an option's bar for the same session
        # lands at 04:00Z, hours BEFORE a chart window that starts at the 13:30Z
        # open, so clipping to the exact window drops the first day's vol.
        closes = close_series(
            conn, contract.conid, bar_size="1d",
            start=low - 86400, end=high + 86400,
        )
        found: list[tuple[int, float]] = []
        for stamp, close in closes:
            spot = daily.get(et_day(stamp))
            if spot is None:
                continue
            years = (expiry - stamp) / (365.0 * 86400)
            vol = implied_vol(close, spot, contract.strike, years, contract.right)
            if vol is not None:
                found.append((stamp, vol))
        found.extend(_anchor_vols(contract, points, expiry))
        if found:
            out[contract.conid] = sorted(found)
    return out


def _anchor_vols(
    contract: BandContract, points: list[tuple[int, float]], expiry: int
) -> list[tuple[int, float]]:
    """Implied vol from the trade's own fills.

    This exists because a price source's history can start AFTER a trade did.
    Measured on this journal: the TSLA 270P was sold on 2026-07-24, and the
    source's first bar for that contract is 2026-07-27 -- so the band, the
    effective delta and the modelled P&L were all absent across the entry
    session, which is the one part of a replay a reader most wants. Asking the
    source for an earlier window returns nothing; the data does not exist. The
    fill does: the market charged 5.24 for that contract at 10:35, which is an
    option price as real as any close.

    Spot comes from _spot_at -- interpolated WITHIN the bar the fill falls in --
    and this is the one place that pairing is correct, where pairing a daily
    close that way is not. A daily option bar is stamped 04:00Z, which on an
    hourly chart lands in the overnight gap, so interpolating there prices the
    option against a spot the market never showed. A fill carries a genuine
    intraday instant that the hourly series brackets, so the interpolation is
    between two prices that really did surround it.

    Only fills inside the chart window are used, since a spot outside it cannot
    be read; anything the solve rejects (a price below intrinsic, a fill after
    expiry) is dropped rather than defaulted, so a gap stays a gap.
    """
    if not points:
        return []
    low, high = points[0][0], points[-1][0]
    found: list[tuple[int, float]] = []
    for stamp, price in contract.anchors:
        if stamp < low or stamp > high:
            continue
        spot = _spot_at(points, stamp)
        if spot is None:
            continue
        years = (expiry - stamp) / (365.0 * 86400)
        vol = implied_vol(price, spot, contract.strike, years, contract.right)
        if vol is not None:
            found.append((stamp, vol))
    return found


def _held_forward(series: list[tuple[int, float]], stamp: int) -> float | None:
    """The last observed value at or before ``stamp``, or None before the first.

    Held forward rather than interpolated toward the next observation: an
    interpolated vol asserts a value between two closes that nothing was priced
    at, while holding forward says only "this is the last thing the market told
    us". None before the first observation, so a gap reads as absent information
    rather than as a narrow range or a zero P&L.
    """
    found: float | None = None
    for observed_at, value in series:
        if observed_at > stamp:
            break
        found = value
    return found


def expected_move_band(
    conn: sqlite3.Connection,
    contracts: list[BandContract],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
    vols: dict[str, list[tuple[int, float]]] | None = None,
) -> list[list[float]]:
    """A one-standard-deviation envelope, per underlying bar.

    Spot and time-to-expiry are per BAR, so the envelope moves and tapers hourly;
    the vol input steps daily, because the source serves no intraday option
    history and there is nothing finer to solve against.

    The horizon is the NEAREST expiry among the legs, which is the one that
    dominates the risk. That is also what makes the envelope narrow as a trade
    ages and step outward when a roll pushes expiry further out.
    """
    # `is None`, not `vols or ...`: an empty dict is the legitimate answer when
    # nothing solved, and the truthiness spelling would re-run the whole solve
    # for exactly that case.
    if vols is None:
        vols = _vol_series(conn, contracts, points, underlying_conid)
    if not vols:
        return []
    expiries = [
        expiry
        for expiry in (expiry_epoch(c.expiry) for c in contracts if c.conid in vols)
        if expiry is not None
    ]
    if not expiries:
        return []
    horizon = min(expiries)
    band: list[list[float]] = []
    for stamp, spot in points:
        observed = [
            vol
            for vol in (_held_forward(series, stamp) for series in vols.values())
            if vol is not None
        ]
        if not observed:
            continue
        average = sum(observed) / len(observed)
        move = expected_move(spot, average, (horizon - stamp) / (365.0 * 86400))
        if move is None:
            continue
        band.append([stamp, round(spot - move, 4), round(spot + move, 4)])
    return band


def band_contracts(legs: list[ReplayLeg]) -> list[BandContract]:
    """The vol-solve view of a replay's legs: one `BandContract` each.

    The single projection, used by BOTH consumers. The band and the modelled
    marks are drawn on one chart and must agree about what the market charged for
    each contract, so deriving them from separately-built contract lists made
    "one vol series per leg" a property of two functions happening to match --
    they did match, verified across both journals, but nothing held them there.

    A leg's own fills become its anchors, so a session the price source has no
    history for still prices from what the market really charged. A snapshot-only
    leg has no fills to anchor with -- that is what makes it snapshot-only -- and
    is seeded from its cost basis instead, which carries no timestamp.

    Deduplication is the caller's: `_replay_legs` already groups by conid, which
    is what makes a contract sold to open and bought to close ONE leg with two
    anchors rather than two legs weighting that strike double in the average.
    """
    return [
        BandContract(
            conid=leg.conid,
            strike=leg.strike,
            right=leg.right,
            expiry=leg.expiry,
            anchors=tuple((stamp, price) for stamp, _qty, price in leg.fills),
        )
        for leg in legs
    ]


def modelled_marks(
    conn: sqlite3.Connection,
    legs: list[ReplayLeg],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
    vols: dict[str, list[tuple[int, float]]] | None = None,
) -> list[list[float | None]]:
    """Modelled P&L and effective delta per bar: ``[ts, pnl, delta]``.

    P&L is cash flow to date plus the mark-to-market of whatever is still open --
    the standard formulation, and the reason it behaves correctly through a
    partial close or a roll rather than only for a clean open-then-close. A
    closing fill moves value out of the open leg and into realised cash, so once
    a position is flat the figure FREEZES at what the trade made instead of
    continuing to mark a contract nobody holds.

    The PRICING is exact: repricing an option's own close at the vol solved from
    it returns that close to 1e-14, so nothing is approximated in the model
    itself. The chart's mark is not a restatement of a broker figure though --
    it applies that vol to the BAR's spot and the BAR's time to expiry, and the
    source stamps an option close at midnight ET while the underlying bar for the
    same session carries the session's own time. So the series TRACKS the broker's
    mark rather than matching it: the LEAP lands $75 from the snapshot's
    unrealised on a $3,000 position, which is that drift plus the snapshot being a
    day older than the latest bar. Close enough to trust the shape, not close
    enough to quote as the position's value -- which is why the card beside the
    chart still states the broker's figure.

    Gross of commission, unlike every accounting figure in this journal. A
    per-bar commission would have to invent when the cost was incurred, and the
    card beside the chart already states the net figure the broker billed.

    Effective delta is ``sum(signed quantity * delta)``, without the multiplier,
    so a delta-neutral strangle reads 0.0 and a short put reads a positive
    fraction -- the scale the reference chart uses.

    It is ``None`` on any bar where nothing is HELD: before the opening fill, and
    after a close takes the position flat. 0.0 cannot serve there, because on a
    symmetric axis 0.0 means delta-neutral -- a real state, and the one a strangle
    is opened in -- so reporting it for an empty position states a position that
    was never held. P&L keeps reporting through the same bars; see the note at
    the append.
    """
    if vols is None:
        vols = _vol_series(conn, band_contracts(legs), points, underlying_conid)
    if not vols:
        return []
    marks: list[list[float | None]] = []
    for stamp, spot in points:
        cash = 0.0
        value = 0.0
        delta = 0.0
        priced = False
        # Separate from `priced`, and that distinction is the whole point. A bar
        # can be PRICEABLE (its contract has a solvable vol) while nothing is
        # HELD -- before the opening fill, and after a close takes the position
        # flat. Sharing one flag made those bars report `delta` as the sum of
        # `0 * bs_delta(...)`, an exact 0.0, and on a SYMMETRIC axis 0.0 is not
        # an absence: it is the centre line, the state a strangle is opened in.
        # So a closed trade drew twelve bars of "we were delta-neutral" when the
        # truth was "we were not in the trade" -- the same fabrication
        # `delta_around` already refuses to make for an opening event, and the
        # same rule `markAt` follows in returning null rather than a neighbour's
        # figure. Measured on this journal: the TSLA short put reported +0.3171
        # then 0.0000 for twelve bars after its buyback, and every replay with
        # context before entry did the mirror image.
        held = False
        for leg in legs:
            expiry = expiry_epoch(leg.expiry)
            series = vols.get(leg.conid)
            if expiry is None or not series:
                continue
            # Position and cash as of this bar. A snapshot-only contract has no
            # fills anywhere -- that is what makes it snapshot-only -- so it is
            # seeded from its cost basis and held flat across the window.
            quantity = leg.seed_quantity
            cash -= leg.seed_quantity * leg.seed_price * leg.multiplier
            for fill_at, delta_qty, price in leg.fills:
                if fill_at > stamp:
                    break
                quantity += delta_qty
                cash -= delta_qty * price * leg.multiplier
            vol = _held_forward(series, stamp)
            if vol is None:
                continue
            years = (expiry - stamp) / (365.0 * 86400)
            if years < 0:
                # The contract is gone. Pricing past expiry is not merely
                # imprecise, it is confidently wrong: bs_price clamps to
                # intrinsic and bs_delta to 1.0, so a held position draws a P&L
                # that keeps swinging with spot and a delta pinned at the top of
                # its axis for as long as the window runs. Measured on the demo's
                # snapshot-only call, that was two months of tail on a contract
                # that had settled. Skipping means the series ENDS at expiry,
                # which is where the band already ends.
                continue
            unit = bs_price(spot, leg.strike, years, vol, leg.right)
            value += quantity * unit * leg.multiplier
            delta += quantity * bs_delta(spot, leg.strike, years, vol, leg.right)
            priced = True
            if quantity:
                held = True
        if not priced:
            continue
        # P&L is still reported when nothing is held, and that asymmetry is
        # deliberate rather than an oversight. Cash flow to date with no open leg
        # left to mark IS the trade's result: it is frozen because the trade
        # finished, so the figure is true. Exposure has no such post-close value
        # -- there is nothing to be exposed by -- so it is absent instead.
        marks.append([
            stamp, round(cash + value, 2), round(delta, 4) if held else None,
        ])
    return marks


def replay_model(
    conn: sqlite3.Connection,
    legs: list[ReplayLeg],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
) -> tuple[list[list[float]], list[list[float]]]:
    """The band and the modelled marks for one replay, as ``(band, marks)``.

    Solves the vol series ONCE and hands it to both. They are the two halves of
    one picture -- an envelope and the P&L series drawn inside it -- and they were
    each solving it independently: measured over the demo journal, that was 20
    solves for 10 replays, with `_vol_series` accounting for 60% of the whole
    `build_state` and half of that being exact recomputation.

    Sharing the solve is also the stronger correctness statement, not just the
    faster one. The band and the marks now cannot disagree about what the market
    charged for a contract, because there is one answer rather than two that
    happen to match -- the same reason `band_contracts` is a single projection.

    Composed here rather than in `web.py` so the caller cannot get the sharing
    half-right: passing the solve to one function and not the other would look
    correct and silently keep the cost.
    """
    contracts = band_contracts(legs)
    vols = _vol_series(conn, contracts, points, underlying_conid)
    band = expected_move_band(
        conn, contracts, points, underlying_conid=underlying_conid, vols=vols
    )
    marks = modelled_marks(
        conn, legs, points, underlying_conid=underlying_conid, vols=vols
    )
    return band, marks


def delta_around(
    marks: list[list[float | None]], stamp: int
) -> tuple[float | None, float | None]:
    """Effective delta immediately before and after an event, as ``(before, after)``.

    The pair a roll is judged by: the reference implementation this feature
    imitates puts "Eff Delta 17 -> 10" on its roll card, because the number that
    matters about a roll is how much exposure it removed.

    ``before`` is the last mark STRICTLY before the event and ``after`` the first
    at or after it, so an opening fill correctly reports ``None -> 0.56``: there
    was no position to have a delta, and inventing 0.0 there would read as
    "we were delta-neutral" rather than "we were not in the trade".

    Either side may be None at a window edge, when no bar in the window had a
    solvable vol, or because the neighbouring bar HELD nothing -- `modelled_marks`
    reports delta as None off-position, and this carries that through rather than
    flattening it to 0.0. A closing event therefore reads ``0.32 -> None``, which
    is the honest shape: it had exposure, and now there is none to have.
    """
    before: float | None = None
    after: float | None = None
    for row in marks:
        if row[0] < stamp:
            before = row[2]
        else:
            after = row[2]
            break
    return before, after


def _trim_to_context(
    points: list[tuple[int, float]], start: int, end: int, bar_size: str
) -> list[tuple[int, float]]:
    """The trade window plus context proportional to it, floored and capped.

    Trimming by BAR COUNT rather than by clock is what keeps two charts
    comparable: a window measured in calendar days lands on a different number
    of sessions depending on which weekday the trade opened, and once the x axis
    is ordinal a calendar-day pad has no consistent width at all.

    The COUNT is proportional to the window because one number cannot serve a
    two-hour 0DTE and a ten-day hold. See CONTEXT_FRACTION for the measurements;
    the short version is that a fixed one-session pad made a day-old position 57%
    padding, and the floor and ceiling keep the proportional rule honest at both
    extremes.

    Bars are counted, not timestamps -- so a weekend or a holiday inside the
    window costs nothing, and the padding is the same shape on a Monday trade as
    on a Thursday one.
    """
    inside = [p for p in points if start <= p[0] <= end]
    ceiling = CONTEXT_MAX_BARS.get(bar_size, CONTEXT_MIN_BARS)
    keep = max(CONTEXT_MIN_BARS,
               min(ceiling, round(len(inside) * CONTEXT_FRACTION)))
    before = [p for p in points if p[0] < start]
    after = [p for p in points if p[0] > end]
    return before[-keep:] + inside + after[:keep]


def replay_bars(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    opened_at: str | None,
    closed_at: str | None,
    now: datetime | None = None,
) -> ReplaySeries:
    """The underlying series a replay chart should draw for one trade window.

    Granularity comes from the same ``_bar_size_for`` the manifest used, so what
    the chart asks for is what the backfill stored -- a short trade gets its
    hourly series and a LEAP its daily one, by construction rather than by
    coincidence.

    A preferred size that yields nothing falls back to the other, because a
    partial backfill should degrade to a coarser chart rather than a blank one.
    The size actually drawn is returned so the panel can say which it is: a
    reader comparing two charts must not have to guess whether a flat stretch is
    a quiet week or a coarser grid.
    """
    conids = _underlying_conids(conn)
    conid = conids.get(str(symbol or "").strip())
    if not conid:
        return ReplaySeries(conid=None, bar_size=None)
    moment = now or datetime.now(UTC)
    start = _epoch(opened_at)
    # A journal stamp truncates to its date, so a closing day's epoch is that
    # day's MIDNIGHT -- every bar of the session the trade closed in sorts after
    # it. Harmless while the window was padded by whole days; it would have cut
    # the closing session out of the trade the moment the trim got precise.
    closed = _epoch(closed_at)
    end = (closed + 86400 - 1) if closed is not None else int(moment.timestamp())
    if start is None:
        # No opening fill anywhere (a snapshot-only contract such as the LEAP).
        # Its window is unknown, so draw every bar held rather than inventing an
        # entry date by matching cost basis against the series.
        start = 0
    # Read WIDER than any trim could keep, so `_trim_to_context` is the only
    # thing deciding how much context is drawn. PAD_DAYS (4) was used here, and
    # it silently capped the daily ceiling at ~4 bars: a request for 5 could not
    # be honoured because the read window did not contain a fifth. Two weeks
    # covers the widest ceiling at both granularities (7 hourly bars is one
    # session, 5 daily bars is a trading week) with room for weekends and
    # holidays, and reading more rows from a local SQLite table is free.
    pad = _READ_PAD_DAYS * 86400
    lo, hi = max(0, start - pad), end + pad
    preferred = _bar_size_for(start or lo, end, kind="underlying")
    for size in (preferred, "1d" if preferred == "1h" else "1h"):
        points = close_series(conn, conid, bar_size=size, start=lo, end=hi)
        if points:
            if start:
                points = _trim_to_context(points, start, end, size)
            else:
                # An unknown window (snapshot-only) has nothing to be context
                # FOR, so there is no window to pad -- but "everything held" was
                # 757 daily bars over three years for the LEAP. Keep the most
                # recent stretch instead: no date is invented, and the bars kept
                # are the ones a holder is looking at.
                points = points[-SNAPSHOT_DRAW_BARS:]
            return ReplaySeries(conid=conid, bar_size=size, points=points)
    return ReplaySeries(conid=conid, bar_size=preferred)
