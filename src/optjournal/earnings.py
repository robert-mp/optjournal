"""The next earnings date for a symbol, from Nasdaq's public analyst endpoint.

A leaf like ``iv`` and ``marketdata``: it knows how to ask and how to read the
answer. No database, no journal shapes, no page.

WHERE THE DATE COMES FROM. ``api.nasdaq.com/api/analyst/{symbol}/earnings-date``
answers with Zacks Investment Research's next date, and it says which KIND of date
it is, in words. Two shapes, both measured against the live endpoint:

* CONFIRMED -- "is expected* to report earnings on 10/01/2026 after market
  close". The company has announced it, and the sentence carries the timing.
* ESTIMATED -- "is estimated to report earnings on 10/28/2026. The upcoming
  earnings date is derived from an algorithm based on a company's historical
  reporting dates." A guess from the cadence, which can move when the company
  announces. The page marks it as an estimate rather than printing it as a date.

A fund has no earnings: SPY answers ``rCode`` 400 with no data, which is an answer
(``None``) rather than a failure.

Chosen after the Yahoo endpoints this journal already reaches were probed and found
to carry no earnings date at all (see ``db.py``'s comment on ``earnings_on``). The
date is attributed on screen to Nasdaq and Zacks, because this journal did not
measure it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = ["Earnings", "EarningsFetchError", "fetch_earnings", "parse_earnings"]

_URL = "https://api.nasdaq.com/api/analyst/{symbol}/earnings-date"
_TIMEOUT_S = 15
#: Nasdaq refuses a bare client; a browser user agent and a JSON accept header are
#: what it answers, measured.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "application/json",
}
_ON = re.compile(r"report earnings on\s+(\d{2})/(\d{2})/(\d{4})")
_TIMING = {"before market open": "before open", "after market close": "after close"}


class EarningsFetchError(RuntimeError):
    """Nasdaq could not be asked, or answered with something unreadable."""


@dataclass(frozen=True)
class Earnings:
    """One symbol's next earnings date and what kind of date it is."""

    #: ISO day, YYYY-MM-DD.
    day: str
    #: True when the company has announced it; False for Zacks' estimate.
    confirmed: bool
    #: "before open", "after close", or None when the source does not say.
    timing: str | None


def parse_earnings(payload: Any) -> Earnings | None:
    """Read one reply. None for a symbol with no earnings (a fund), or no date."""
    if not isinstance(payload, dict):
        raise EarningsFetchError("response was not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    text = str(data.get("reportText") or "")
    found = _ON.search(text)
    if not found:
        return None
    month, day, year = (int(part) for part in found.groups())
    try:
        iso = date(year, month, day).isoformat()
    except ValueError:
        return None
    lowered = text.lower()
    timing = next((word for phrase, word in _TIMING.items() if phrase in lowered), None)
    # "estimated" is the source's own word for a cadence guess; a confirmed date
    # reads "expected*" and carries its timing.
    return Earnings(day=iso, confirmed="estimated to report" not in lowered,
                    timing=timing)


def fetch_earnings(symbol: str, *, timeout: int = _TIMEOUT_S) -> Earnings | None:
    """One symbol's next earnings date, or None when it has none."""
    url = _URL.format(symbol=symbol.strip().upper())
    request = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise EarningsFetchError(f"{symbol} earnings: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EarningsFetchError(
            f"{symbol} earnings: {type(exc).__name__}: {exc}") from exc
    return parse_earnings(payload)
