"""The market clock: one zone, and the four conversions stated against it.

Every timestamp this journal stores is a local time in ONE zone, and which zone
is a property of the market rather than of any module -- `epoch_et` records the
evidence that settled it from real fills. Four modules need that rule, and they
sit at different depths: `web.py` stamps replay markers, `serialize.py` and
`cli.py` render an event calendar, `demo.py` prices synthetic contracts against
an expiry instant.

A leaf, for the reason `notes.py` and `money.py` are: the rule is shared across
layers that must not hold each other, so it lives where every layer can hold it
and depends on nothing itself. It previously lived in `bars.py`, which had
discovered it -- correct at the time and no longer, once four modules were
reaching a 1294-line storage-and-modelling module for pure functions of a
string. `bars.py` is still the heaviest CONSUMER of these; it is no longer their
address.

This changes no dependency count on its own: every module that imports the clock
also needs something else from `bars.py`. What it buys is one address for the ET
rule, and clock tests that live beside the clock.
"""

from __future__ import annotations

from datetime import datetime
from functools import cache
from zoneinfo import ZoneInfo

__all__ = ["MARKET_TZ", "epoch_et", "et_day", "expiry_epoch"]

#: The clock every journal timestamp is stated in. See epoch_et for the
#: evidence; it is also the zone the chart labels its x axis in, so fills and
#: bars land on one timeline without conversion.
MARKET_TZ = ZoneInfo("America/New_York")


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


#: Cached: a pure function of one short string, called from inside per-bar loops.
#: Measured over the demo journal, one `build_state` made 1,490 calls against 11
#: distinct expiries. The key space is the number of contracts the journal has
#: ever held, so it cannot grow with traffic.
@cache
def expiry_epoch(expiry: str | None) -> int | None:
    """Epoch of an option's expiry, at the 16:00 ET close of its expiry date.

    Both formats the payload actually carries are accepted: a leg states an
    expiry as ``2026-09-04`` while a position snapshot row keeps IBKR's raw
    ``20260918``. Handling one and rejecting the other silently produced a band
    for the snapshot-only LEAP and none for any traded lifecycle.

    Public because `demo.py` prices its synthetic contracts against the same
    expiry instant `bars.py` solves vol at. It had its own copy taking the zone
    as a parameter and was called with the market zone -- so the two agreed only
    by the caller remembering to pass the right clock, for a value that is a
    property of the market rather than of the caller.
    """
    text = str(expiry or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            day = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(day.replace(hour=16, tzinfo=MARKET_TZ).timestamp())
    return None


#: Cached, and this is the one that pays: `zoneinfo` conversion plus `strftime`
#: per call, from inside every bar loop. One `build_state` made 11,446 calls
#: against 1,030 distinct timestamps -- an 11x repeat, and caching both this and
#: `expiry_epoch` took the demo payload from 61ms to 40ms.
#:
#: Unbounded is correct here rather than lazy: the key space is exactly the
#: distinct bar timestamps in the database (1,291 in the real journal, 1,554 in
#: the demo, growing by roughly fifteen a trading day), and an entry is an int
#: plus a ten-character string. A `maxsize` would add eviction bookkeeping to
#: protect against a few hundred kilobytes.
@cache
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
