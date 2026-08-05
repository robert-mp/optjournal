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
from zoneinfo import ZoneInfo

from optjournal.blackscholes import (
    bs_delta,
    bs_price,
    expected_move,
    implied_vol,
)
from optjournal.history import build_history
from optjournal.marketdata import BAR_SIZES, SOURCE_RANK, Bar, BarFetchError, fetch_bars

__all__ = [
    "BackfillOutcome",
    "BarRequest",
    "backfill_bars",
    "bars_manifest",
    "MARKET_TZ",
    "BandContract",
    "close_series",
    "ReplayLeg",
    "delta_around",
    "expected_move_band",
    "modelled_marks",
    "epoch_et",
    "et_day",
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

#: The clock every journal timestamp is stated in. See epoch_et for the
#: evidence; it is also the zone the chart labels its x axis in, so fills and
#: bars land on one timeline without conversion.
MARKET_TZ = ZoneInfo("America/New_York")

#: Bars of context kept either side of the trade window when CHARTING. Counted
#: in bars, not calendar days: PAD_DAYS is what gets FETCHED (wide is free and
#: already stored), but four calendar days rendered 45-58% of every chart as
#: padding -- on a ten-bar GOOG trade the lead-in was larger than the trade. It
#: also varied with the weekday, since Thursday plus four days is two sessions
#: while Monday plus four is four. One session of hourly context answers "what
#: was it doing just before I entered"; a daily chart gets a few sessions,
#: because at that grid one bar either side is invisible.
CONTEXT_BARS = {"1h": 7, "1d": 3}

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


def epoch_et(stamp: str | None) -> int | None:
    """Epoch seconds from a journal timestamp, read as US EASTERN time.

    Settled from the data rather than assumed, because a wrong zone would put
    every entry marker four hours off the line it marks. Fills carry the local
    time of ONE clock, and three markets agree on which: Nasdaq Stockholm fills
    land 03:19-10:57 (its 09:00-17:30 CET session is 03:00-11:30 ET), a Korean
    fill lands 20:03 (KRX opens 20:00 ET), and every US option fill lands
    09:55-11:24 inside the 09:30-16:00 session. Under UTC, Stockholm's 03:19 and
    Korea's 20:03 are both outside any session those exchanges run.

    ZoneInfo rather than a fixed offset because the account has held positions
    across a DST boundary -- the LEAP spans two -- and EDT is UTC-4 while EST is
    UTC-5.
    """
    if not stamp:
        return None
    text = str(stamp).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(naive.replace(tzinfo=MARKET_TZ).timestamp())
    return None


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

    def _emit(
        conid: str, symbol: str, start: int, end: int, kind: str, size: str
    ) -> None:
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


def _expiry_epoch(expiry: str | None) -> int | None:
    """Epoch of an option's expiry, at the 16:00 ET close of its expiry date.

    Both formats the payload actually carries are accepted: a leg states an
    expiry as ``2026-09-04`` while a position snapshot row keeps IBKR's raw
    ``20260918``. Handling one and rejecting the other silently produced a band
    for the snapshot-only LEAP and none for any traded lifecycle.
    """
    text = str(expiry or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            day = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(day.replace(hour=16, tzinfo=MARKET_TZ).timestamp())
    return None


def et_day(stamp: int) -> str:
    """The ET calendar date a bar belongs to, as YYYY-MM-DD.

    The join key between two daily series, because the SOURCE does not stamp them
    alike: an option's daily bar arrives at 04:00Z (midnight ET) while its
    underlying's arrives at 13:30Z (the session open). Same provider, same
    interval, two conventions -- so matching on the raw timestamp finds nothing,
    silently, and the band simply fails to appear. The trading day is what both
    actually mean.
    """
    return datetime.fromtimestamp(stamp, MARKET_TZ).strftime("%Y-%m-%d")


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
        expiry = _expiry_epoch(contract.expiry)
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
) -> list[list[float]]:
    """A one-standard-deviation envelope, per underlying bar.

    Spot and time-to-expiry are per BAR, so the envelope moves and tapers hourly;
    the vol input steps daily, because the source serves no intraday option
    history and there is nothing finer to solve against.

    The horizon is the NEAREST expiry among the legs, which is the one that
    dominates the risk. That is also what makes the envelope narrow as a trade
    ages and step outward when a roll pushes expiry further out.
    """
    vols = _vol_series(conn, contracts, points, underlying_conid)
    if not vols:
        return []
    expiries = [
        expiry
        for expiry in (_expiry_epoch(c.expiry) for c in contracts if c.conid in vols)
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


def modelled_marks(
    conn: sqlite3.Connection,
    legs: list[ReplayLeg],
    points: list[tuple[int, float]],
    *,
    underlying_conid: str | None,
) -> list[list[float]]:
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
    """
    contracts = [
        BandContract(
            conid=leg.conid, strike=leg.strike, right=leg.right, expiry=leg.expiry,
            # The fills, so a session the price source has no history for still
            # prices from what the market charged us. A snapshot-only leg is
            # seeded from a cost basis with no timestamp, so it cannot anchor.
            anchors=tuple((stamp, price) for stamp, _qty, price in leg.fills),
        )
        for leg in legs
    ]
    vols = _vol_series(conn, contracts, points, underlying_conid)
    if not vols:
        return []
    marks: list[list[float]] = []
    for stamp, spot in points:
        cash = 0.0
        value = 0.0
        delta = 0.0
        priced = False
        for leg in legs:
            expiry = _expiry_epoch(leg.expiry)
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
            unit = bs_price(spot, leg.strike, years, vol, leg.right)
            value += quantity * unit * leg.multiplier
            delta += quantity * bs_delta(spot, leg.strike, years, vol, leg.right)
            priced = True
        if not priced:
            continue
        marks.append([stamp, round(cash + value, 2), round(delta, 4)])
    return marks


def delta_around(marks: list[list[float]], stamp: int) -> tuple[float | None, float | None]:
    """Effective delta immediately before and after an event, as ``(before, after)``.

    The pair a roll is judged by: the reference implementation this feature
    imitates puts "Eff Delta 17 -> 10" on its roll card, because the number that
    matters about a roll is how much exposure it removed.

    ``before`` is the last mark STRICTLY before the event and ``after`` the first
    at or after it, so an opening fill correctly reports ``None -> 0.56``: there
    was no position to have a delta, and inventing 0.0 there would read as
    "we were delta-neutral" rather than "we were not in the trade".

    Either side may be None at a window edge, or when no bar in the window had a
    solvable vol.
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
    """The trade window plus a bounded number of bars either side.

    Trimming by BAR COUNT rather than by clock is what keeps two charts
    comparable: a window measured in calendar days lands on a different number
    of sessions depending on which weekday the trade opened, and once the x axis
    is ordinal a calendar-day pad has no consistent width at all.
    """
    keep = CONTEXT_BARS.get(bar_size, 3)
    before = [p for p in points if p[0] < start]
    inside = [p for p in points if start <= p[0] <= end]
    after = [p for p in points if p[0] > end]
    return before[-keep:] + inside + after[:keep]


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
    pad = PAD_DAYS * 86400
    lo, hi = max(0, start - pad), end + pad
    preferred = _bar_size_for(start or lo, end, kind="underlying")
    for size in (preferred, "1d" if preferred == "1h" else "1h"):
        points = close_series(conn, conid, bar_size=size, start=lo, end=hi)
        if points:
            # An unknown window (snapshot-only) has nothing to be context FOR,
            # so everything held is the answer rather than a trimmed slice.
            if start:
                points = _trim_to_context(points, start, end, size)
            return {"conid": conid, "bar_size": size, "points": points}
    return {"conid": conid, "bar_size": preferred, "points": []}
