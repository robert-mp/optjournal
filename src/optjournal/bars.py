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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from optjournal.history import build_history
from optjournal.marketdata import BAR_SIZES, SOURCE_RANK, Bar, BarFetchError, fetch_bars

__all__ = [
    "BackfillOutcome",
    "BarRequest",
    "backfill_bars",
    "bars_manifest",
    "close_series",
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

#: How far back to look for a contract whose opening fill predates the archive.
#: The source truncates to whatever it actually holds -- a 2025-01-01 request
#: for the LEAP returned bars from 2025-02-03, the contract's listing date --
#: so asking wide costs nothing and needs no guess about an unknown open date.
SNAPSHOT_FLOOR_DAYS = 1100

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

    An option leg is always daily: the source has no intraday option history,
    so asking hourly would spend a request to receive an empty series.
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
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[BarRequest]:
    """Every (contract, granularity, window) the journal's own positions imply.

    Windows for the same ``(conid, bar_size)`` are merged to their union, so an
    overlapping pair becomes one request rather than two.
    """
    moment = now or datetime.now(UTC)
    ceiling = int((moment + timedelta(days=1)).timestamp())
    pad = PAD_DAYS * 86400
    floor_start = int((moment - timedelta(days=SNAPSHOT_FLOOR_DAYS)).timestamp())

    underlyings = _underlying_conids(conn)
    merged: dict[tuple[str, str], BarRequest] = {}

    def add(conid: str, symbol: str, start: int, end: int, kind: str) -> None:
        size = _bar_size_for(start, end, kind=kind)
        request = BarRequest(
            conid=str(conid), symbol=str(symbol), bar_size=size,
            start=start, end=end, kind=kind,
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
        add(episode.conid, episode.symbol, start, end, "option")
        name = (episode.underlying_symbol or "").strip()
        conid = underlyings.get(name)
        if name and conid:
            add(conid, name, start, end, "underlying")

    return sorted(merged.values(), key=lambda r: (r.kind, r.symbol, r.bar_size))


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
) -> BackfillOutcome:
    """Fetch and store every window the manifest names.

    ``fetch`` is injected so the suite can exercise the whole path without a
    network call. A per-request failure is collected rather than raised: one
    unreachable contract should not abandon the rest of the book.
    """
    requests = bars_manifest(conn, now=now)
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

    Timestamps are carried, unlike the row miniature this replaces: a chart with
    a real time axis cannot infer x from position in the list, because sessions
    are not evenly spaced (weekends, holidays, and a half-length 15:30 bar).

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


def replay_bars(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    opened_at: str | None,
    closed_at: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
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
    empty: dict[str, Any] = {"conid": None, "bar_size": None, "points": []}
    if not conid:
        return empty
    moment = now or datetime.now(UTC)
    start = _epoch(opened_at)
    end = _epoch(closed_at) or int(moment.timestamp())
    if start is None:
        # No opening fill anywhere (a snapshot-only contract such as the LEAP).
        # Its window is unknown, so draw every bar held rather than inventing an
        # entry date by matching cost basis against the series.
        start = 0
    pad = PAD_DAYS * 86400
    lo, hi = max(0, start - pad), end + pad
    preferred = _bar_size_for(start or lo, end, kind="underlying")
    for size in (preferred, "1d" if preferred == "1h" else "1h"):
        points = close_series(conn, conid, bar_size=size, start=lo, end=hi)
        if points:
            return {"conid": conid, "bar_size": size, "points": points}
    return {"conid": conid, "bar_size": preferred, "points": []}
