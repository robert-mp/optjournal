"""The market clock: the zone, and the four conversions stated against it.

Beside `clock.py` rather than inside `test_bars.py`, where these lived while the
clock did. Nothing here opens a database or reads a bar -- the whole module is
pure functions of a string or an int -- so a test that needed the `conn` fixture
would be testing something else.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from optjournal.clock import MARKET_TZ, epoch_et, et_day, expiry_epoch


def _utc_day(day: str) -> int:
    """Epoch seconds for a YYYY-MM-DD day read as UTC.

    Deliberately NOT `epoch_et`: the join test below needs a stamp built by
    something other than the function under test, or it would agree with itself.
    """
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def test_journal_stamps_are_read_as_us_eastern():
    """Settled from the data, not assumed: Stockholm fills land 03:19-10:57 and a
    Korean fill lands 20:03, both of which are inside those exchanges' sessions
    in ET and outside them in UTC. A wrong zone would put every entry marker four
    hours off the line it marks.
    """
    # 10:35 EDT is 14:35Z in July (UTC-4) and 15:35Z in January (UTC-5).
    assert epoch_et("2026-07-24 10:35:01") == 1784903701
    summer = datetime.fromtimestamp(epoch_et("2026-07-24 10:35:00"), UTC)
    winter = datetime.fromtimestamp(epoch_et("2026-01-15 10:35:00"), UTC)
    assert (summer.hour, winter.hour) == (14, 15), "DST is not being applied"


@pytest.mark.parametrize("stamp", [None, "", "nonsense", "24/07/2026"])
def test_an_unparseable_stamp_is_none_not_zero(stamp):
    """Epoch zero is 1970, which would place a marker at the far left of every
    chart rather than nowhere.
    """
    assert epoch_et(stamp) is None


def test_two_daily_series_join_on_the_trading_day_not_the_timestamp():
    """The source does not stamp them alike: an option's daily bar arrives at
    04:00Z (midnight ET) and its underlying's at 13:30Z (the session open). Same
    provider, same interval, two conventions -- so an exact-timestamp join finds
    nothing, silently, and the band just fails to appear.
    """
    day = _utc_day("2026-07-27")
    assert et_day(day + 4 * 3600) == et_day(day + 13 * 3600 + 1800) == "2026-07-27"


def test_the_cached_helpers_stay_pure_across_a_dst_boundary():
    """`et_day` and `expiry_epoch` are `functools.cache`d, so purity is load-bearing.

    Both are keyed on one scalar and read no clock, no database and no global --
    which is what makes caching them safe, and what this pins. The DST pair is the
    case worth naming: the ET offset changes across it (EDT is UTC-4, EST UTC-5),
    so a cache keyed on anything coarser than the exact timestamp, or a helper
    that consulted "now", would answer one of these two wrongly. The account has
    held positions across two such boundaries.
    """
    edt = int(datetime(2026, 7, 1, 12, tzinfo=MARKET_TZ).timestamp())
    est = int(datetime(2026, 1, 15, 12, tzinfo=MARKET_TZ).timestamp())
    assert et_day(edt) == "2026-07-01"
    assert et_day(est) == "2026-01-15"
    # Same answer on a second call, which is the cache's only observable effect.
    assert (et_day(edt), et_day(est)) == ("2026-07-01", "2026-01-15")
    # A cached function must still accept the None the payload really carries.
    assert expiry_epoch(None) is None and expiry_epoch("") is None
    assert expiry_epoch("20260904") == expiry_epoch("2026-09-04")


def test_an_expiry_lands_on_the_sixteen_hundred_close_in_either_format():
    """A leg states `2026-09-04`; a position snapshot keeps IBKR's `20260918`.

    Handling one and rejecting the other silently produced a band for the
    snapshot-only LEAP and none for any traded lifecycle, so both formats and the
    16:00 ET instant are pinned together.
    """
    assert expiry_epoch("20260904") == expiry_epoch("2026-09-04")
    assert expiry_epoch("nonsense") is None
    close = datetime.fromtimestamp(expiry_epoch("2026-09-04"), MARKET_TZ)
    assert (close.hour, close.minute) == (16, 0), "an expiry is the 16:00 ET close"
