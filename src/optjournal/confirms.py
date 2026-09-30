"""Trade Confirmations: same-session fills, and the one estimate they force.

IBKR serves the same fill through two Flex query types. The Activity Statement is
T+1 and settled; a Trade Confirmation is available within minutes of the fill and
is provisional. This module reads the second kind. `ingest.py` writes both, ranked
by `ingest.SOURCE_RANK`, so a confirm is superseded by the next day's statement
and never the reverse.

WHY THIS IS NOT IN `sources.py`. That module is the seam for statement FILES, and
every attribute read in it goes through py_ibkr's models -- which do not model the
`TCF` payload at all. This reader parses the XML directly, and it also has to
reach the network for an FX rate, which has no business inside a parse seam. So
confirms get their own module and `sources.py` stays a pure adaptor.

WHAT A CONFIRM DOES NOT CARRY, verified against a real payload (82 attributes,
2026-09-24) rather than inferred from documentation:

* No `fxRateToBase`. Every `*_base` figure in this journal is a native amount
  times that rate, so a non-base fill has no broker-stated conversion. See
  `base_rate`: this journal fetches a live one and marks the row.
* No `transactionID`. IBKR's settled-record id, and a confirm is not settled.
  The column is nullable for exactly this reason.
* No realised P&L, no MTM, no cost basis. Confirmed absent, which is the
  documented negative evidence behind the ranking: a confirm knows the fill
  happened and little about what it netted.

Three things the docs had inferred WRONGLY, corrected here from the payload:
`proceeds` exists under that name (the inferred table said gross arrives only as
`amount`, which is there too and signed the other way); and `accountId`,
`currency` and `assetCategory` use the same camelCase spellings as the Activity
statement rather than diverging from their documented labels.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from py_ibkr.flex.utils import parse_date, parse_datetime

from optjournal.fills import NormalisedFill, StatementMeta

__all__ = [
    "CONFIRM_QUERY_TYPE",
    "EXECUTION_DETAIL",
    "ConfirmParseError",
    "base_rate",
    "parse_confirms",
    "statement_meta",
]

log = logging.getLogger(__name__)

#: The `type` attribute on a Trade Confirmation payload's root element. Checked
#: rather than assumed: pointing the confirm query id at an Activity Statement is
#: an easy mistake to make in Client Portal, and the failure it would otherwise
#: produce is a silent zero-fill parse rather than a refusal.
CONFIRM_QUERY_TYPE = "TCF"

#: The only `levelOfDetail` this reader accepts.
#:
#: `<TradeConfirms>` can hold four row element types -- `TradeConfirm`, `Order`,
#: `SymbolSummary`, `AssetSummary` -- and in a real payload the `Order` rows carry
#: the SAME attributes as the executions and differ only in this field. Filtering
#: on the element name would therefore double-count every fill whose query has
#: order-level detail enabled. Filtering on the level cannot.
EXECUTION_DETAIL = "EXECUTION"

#: The open/close markers inside `code`.
#:
#: A confirm has no `openCloseIndicator`; the information is one letter inside the
#: semicolon-delimited `code` string, mixed in with every other trade code. The
#: Activity statement splits the two into `openCloseIndicator` and `notes`, and
#: this journal's columns follow that split -- measured against stored rows, where
#: `open_close` is 'O' or 'C' and `notes` holds the rest ('P', 'SL', 'IPO;M').
_OPEN_CLOSE_CODES = ("O", "C")


class ConfirmParseError(RuntimeError):
    """The payload is not a Trade Confirmation this reader can use."""


def _text(attrs: dict[str, str], name: str) -> str | None:
    value = (attrs.get(name) or "").strip()
    return value or None


def _date(attrs: dict[str, str], name: str) -> str | None:
    """A date in the form the Activity Statement's rows store: `2026-09-24`.

    The statement's dates reach the journal through py_ibkr, which parses IBKR's
    compact `20260924` into a `date`; a confirm is read here as text. Stored as
    sent, the two kinds of row disagreed on one column, and every reader had been
    written against the statement's form: replay dropped a same-session fill, a
    month filter missed it, and the page printed the raw stamp. Parsed by the SAME
    py_ibkr function, so the forms cannot drift apart.

    A value that function cannot read is kept as IBKR sent it rather than
    dropped: an odd date on the row is recoverable, a missing one is not.
    """
    raw = _text(attrs, name)
    try:
        parsed = parse_date(raw) if raw else None
    except ValueError:
        return raw
    return str(parsed) if parsed else raw


def _datetime(attrs: dict[str, str], name: str) -> str | None:
    """A stamp in the statement's form, `2026-09-24 10:16:59`. See `_date`."""
    raw = _text(attrs, name)
    try:
        parsed = parse_datetime(raw) if raw else None
    except ValueError:
        return raw
    return str(parsed) if parsed else raw


def _number(attrs: dict[str, str], name: str) -> float | None:
    raw = _text(attrs, name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _quantity(attrs: dict[str, str], name: str) -> int | float | None:
    """Integers exact, fractions lossless -- `db.py`'s quantity note applies."""
    value = _number(attrs, name)
    if value is None:
        return None
    nearest = int(round(value))
    return nearest if abs(value - nearest) < 1e-9 else value


def split_codes(code: str | None) -> tuple[str | None, str | None]:
    """`code` into (open_close, notes), the way the Activity statement splits it.

    Returns the open/close markers and every other code, each semicolon-joined in
    the order IBKR sent them. Two columns rather than one string because that is
    what the rest of this journal reads: `history.py` tests `open_close`, and a
    reader filtering on notes='O' would find nothing on an activity row. A fill
    through zero carries both markers, stored as "C;O" like the Activity
    Statement's `openCloseIndicator`.
    """
    if not code:
        return None, None
    parts = [p.strip() for p in code.split(";") if p.strip()]
    markers = [p for p in parts if p in _OPEN_CLOSE_CODES]
    rest = [p for p in parts if p not in _OPEN_CLOSE_CODES]
    return (";".join(markers) or None), (";".join(rest) or None)


def _root(path: Path) -> ET.Element:
    try:
        root = ET.fromstring(path.read_bytes())
    except ET.ParseError as exc:
        raise ConfirmParseError(f"{path.name} is not parseable XML: {exc}") from exc
    kind = (root.get("type") or "").strip()
    if kind != CONFIRM_QUERY_TYPE:
        raise ConfirmParseError(
            f"{path.name} is a Flex payload of type {kind or '(none)'!r}, not "
            f"{CONFIRM_QUERY_TYPE!r}. Check the confirm query id points at a "
            f"Trade Confirmation query rather than an Activity Statement."
        )
    return root


def statement_meta(path: Path, *, base_currency: str) -> list[StatementMeta]:
    """What each statement block in a confirm payload says about itself.

    The base currency is passed IN rather than read out: a confirm carries no
    AccountInformation section, so the only honest source is the journal's own
    activity statements. See `ingest`'s caller.
    """
    return [
        StatementMeta(
            account_id=_text(stmt.attrib, "accountId"),
            from_date=_date(stmt.attrib, "fromDate"),
            to_date=_date(stmt.attrib, "toDate"),
            generated_at=_datetime(stmt.attrib, "whenGenerated"),
            base_currency=base_currency,
        )
        for stmt in _root(path).iter("FlexStatement")
    ]


def parse_confirms(path: Path, *, rate_for: Any) -> list[tuple[str, NormalisedFill]]:
    """Every execution in the payload, as `(account_id, fill)` pairs.

    `rate_for` is called with a currency code and returns the rate converting it
    into the account's base. Injected rather than imported so the parse stays
    testable without a network call, and so one fetch per currency is the caller's
    decision rather than one per fill.

    Rows whose `levelOfDetail` is not `EXECUTION` are skipped -- see
    `EXECUTION_DETAIL` on why that is a filter on the attribute and not on the
    element name.
    """
    fills: list[tuple[str, NormalisedFill]] = []
    for stmt in _root(path).iter("FlexStatement"):
        account_id = _text(stmt.attrib, "accountId") or ""
        for row in stmt.iter():
            attrs = row.attrib
            if not attrs or _text(attrs, "levelOfDetail") != EXECUTION_DETAIL:
                continue
            if _text(attrs, "tradeID") is None:
                # Identity is `(broker, trade_id)`. A row without one cannot be
                # written, and inventing a key would make a duplicate on the next
                # poll rather than a supersede.
                log.warning("skipping a confirm row with no tradeID")
                continue
            fills.append((account_id, _fill(attrs, account_id, rate_for)))
    return fills


def _fill(attrs: dict[str, str], account_id: str, rate_for: Any) -> NormalisedFill:
    """One `<TradeConfirm>` as a `NormalisedFill`.

    The attribute names are the confirm's own, which diverge from the Activity
    statement's in eight places -- `price` not `tradePrice`, `commission` not
    `ibCommission`, `tax` not `taxes`, `code` not `notes`, `execID` not `ibExecID`,
    `orderID` not `ibOrderID`, `settleDate` not `settleDateTarget`, and no
    `openCloseIndicator` at all. Every one of those was confirmed against a real
    payload before this function existed.
    """
    open_close, notes = split_codes(_text(attrs, "code"))
    currency = _text(attrs, "currency")
    return NormalisedFill(
        trade_id=_text(attrs, "tradeID") or "",
        exec_id=_text(attrs, "execID"),
        # Absent from the payload, and left absent. The column is nullable so that
        # this can be the truth rather than a manufactured id.
        transaction_id=None,
        order_id=_text(attrs, "orderID"),
        account_id=account_id,
        trade_date=_date(attrs, "tradeDate"),
        date_time=_datetime(attrs, "dateTime"),
        asset_category=_text(attrs, "assetCategory"),
        symbol=_text(attrs, "symbol"),
        contract_id=_text(attrs, "conid"),
        underlying_symbol=_text(attrs, "underlyingSymbol"),
        underlying_contract_id=_text(attrs, "underlyingConid"),
        put_call=_text(attrs, "putCall"),
        strike=_number(attrs, "strike"),
        expiry=_date(attrs, "expiry"),
        multiplier=_number(attrs, "multiplier"),
        buy_sell=_text(attrs, "buySell"),
        open_close=open_close,
        notes=notes,
        level_of_detail=_text(attrs, "levelOfDetail"),
        quantity=_quantity(attrs, "quantity"),
        trade_price=_number(attrs, "price"),
        currency=currency,
        fx_rate_to_base=rate_for(currency),
        proceeds=_number(attrs, "proceeds"),
        commission=_number(attrs, "commission"),
        commission_currency=_text(attrs, "commissionCurrency"),
        taxes=_number(attrs, "tax"),
        # Absent by design, not by omission: IBKR's confirm configuration offers
        # no P&L field at all, which is why the Activity Statement outranks this.
        realized_pnl=None,
        mtm_pnl=None,
        raw=dict(attrs),
    )


def base_rate(currency: str | None, base: str) -> tuple[float, bool]:
    """(rate into `base`, whether it was estimated).

    A confirm carries no FX rate, so for anything not already quoted in the base
    currency this fetches a LIVE one -- `USDEUR=X` on the same source the price
    bars come from, which on this account reads 0.8795 against the 0.87228 IBKR
    stated for a fill three sessions earlier.

    THIS IS A DEPARTURE FROM A RULE THIS PROJECT OTHERWISE HOLDS.
    `docs/design-notes.md` says every figure the accounting layers report is
    broker-stated, with modelled numbers quarantined so none can reach a headline.
    An estimated rate does reach them, because the owner chose same-session P&L
    over a strictly broker-stated one. The estimate is therefore RECORDED rather
    than hidden: `trades.fx_rate_estimated` marks the row, the page labels it, and
    the next Activity Statement overwrites it with IBKR's own rate.

    Fails CLOSED at 1.0 with a warning rather than raising. A rate this journal
    could not fetch must not cost you the fill: the native figures are all
    broker-stated and correct, the base ones are wrong until tomorrow, and the row
    is marked estimated either way.
    """
    if not currency or currency == base:
        return 1.0, False
    from optjournal.marketdata import BarFetchError, fetch_quote

    symbol = f"{currency}{base}=X"
    try:
        quote = fetch_quote(symbol)
    except BarFetchError as exc:
        log.warning("no live %s rate (%s); storing 1.0, marked estimated", symbol, exc)
        return 1.0, True
    if quote.price is None or quote.price <= 0:
        log.warning("live %s rate came back empty; storing 1.0, marked estimated",
                    symbol)
        return 1.0, True
    return float(quote.price), True
