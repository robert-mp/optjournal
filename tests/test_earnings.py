"""The earnings date parser, against replies captured from Nasdaq itself.

Fixtures rather than a network call, the arrangement `test_marketdata` uses for
`parse_chart`: the three shapes that matter were saved from the live endpoint on
2026-09-29 and each is one branch of `parse_earnings`.

WHY THE THREE. A CONFIRMED date reads "is expected* to report earnings on
10/01/2026 after market close" -- the company has announced it and the sentence
carries the timing. An ESTIMATED one reads "is estimated to report earnings on
10/28/2026" and is Zacks' guess from past reporting dates, which moves when the
company announces; printing it as a date would be a guess wearing a date's
clothes. A FUND has no earnings at all, and the endpoint says so with a 400 and no
data block, which is an ANSWER rather than a failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from optjournal.earnings import EarningsFetchError, parse_earnings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "earnings"


def _reply(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_an_announced_date_is_confirmed_and_carries_its_timing():
    found = parse_earnings(_reply("NKE"))
    assert found is not None
    assert (found.day, found.confirmed, found.timing) == (
        "2026-10-01", True, "after close")


def test_zacks_estimate_is_marked_as_an_estimate():
    """The distinction the column depends on: this date can move."""
    found = parse_earnings(_reply("TSLA"))
    assert found is not None
    assert (found.day, found.confirmed) == ("2026-10-28", False)


def test_a_fund_has_no_earnings_and_that_is_an_answer():
    """SPY: rCode 400 and no data block. None, not a raise -- a fund reporting
    earnings would be the surprise."""
    assert parse_earnings(_reply("SPY")) is None


@pytest.mark.parametrize("payload", [
    {"data": {"reportText": "no date in this sentence"}},
    {"data": {"reportText": "report earnings on 13/45/2026"}},
    {"data": None},
])
def test_an_unreadable_reply_is_an_absence(payload):
    """An impossible date is dropped rather than stored: a countdown to 13/45
    would be arithmetic over a day that does not exist."""
    assert parse_earnings(payload) is None


def test_a_reply_that_is_not_an_object_is_loud():
    """A malformed reply is a broken source, not an absent date, and the two have
    different remedies."""
    with pytest.raises(EarningsFetchError):
        parse_earnings("<html>403</html>")


@pytest.mark.parametrize("code", [429, 500, 503])
def test_an_error_reply_is_a_failure_not_an_absence(code):
    """L8: Nasdaq answers a throttled or failed request with HTTP 200, a non-200
    `rCode` and no data. Read as "no earnings", that wiped a stored date, so only
    the documented no-earnings reply (400, a fund) is an absence."""
    payload = {"data": None, "message": None,
               "status": {"rCode": code, "bCodeMessage": None}}
    with pytest.raises(EarningsFetchError, match=str(code)):
        parse_earnings(payload)
