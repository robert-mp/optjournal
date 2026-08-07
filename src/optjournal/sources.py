"""Where fills come from: one broker per implementation, one seam for all.

`ingest.py` used to read py_ibkr's models directly -- `t.assetCategory`,
`t.fifoPnlRealized`, `t.ibExecID` -- so every IBKR-shaped decision reached into
the ingest, and a second broker would have meant editing the writer rather than
adding a reader. The README described a parse boundary that did not exist in code;
this module is that boundary.

A `StatementSource` reads a statement and yields its fills as `NormalisedFill`s
(see `fills.py`), which carry no broker's vocabulary. Adding a broker is adding
one implementation and one registry entry -- `ingest.py` does not change and does
not learn the broker's name.

A `typing.Protocol`, not an ABC: a source is anything with the right shape, so
`ingest` need not import any concrete source and a source need not inherit
anything. The registry (`SOURCES`) mirrors `stats.SCOPE_BUILDERS` -- a dict keyed
by the name the `--broker` flag uses -- with one deliberate difference: an unknown
key raises rather than falling open. An unrecognised trade-type scope should show
the whole journal, but an unrecognised broker means the caller asked for a reader
this build does not have, and journalling a statement under the wrong parser is
worse than stopping.

Only the IBKR source exists today, and it is a thin adaptor over the existing
`flex.load` and `sections.raw_sections` -- the parsing did not move, only the
attribute reads that were scattered through the ingest.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from optjournal.fills import (
    NormalisedCash,
    NormalisedFill,
    NormalisedNav,
    NormalisedPosition,
    NormalisedSecurity,
    StatementMeta,
)


class StatementSource(Protocol):
    """One broker's statement file, read into broker-neutral shapes.

    One method per section the journal stores. `statements()` is the odd one out
    and stays that way: a statement file can hold several account blocks, and a
    fill has to be attributed to the block it came from, so it yields
    `(account_id, fills)` pairs. The other four carry their own `account_id` per
    row, so they are flat iterators.

    Every method may yield nothing. A section is absent when the broker's report
    template omits it, and that is a normal state rather than an error -- IBKR's
    EquitySummaryInBase is only present when the Flex query enables it. What is
    NOT harmless is `positions()` returning nothing when positions are held: see
    `NormalisedPosition`, whose absence makes every episode look closed.

    Returning iterators rather than lists so a large statement need not be held
    in memory twice, and generators keep each reader a single pass over its
    section.
    """

    #: The `broker` value stamped on every row this source produces, and the key
    #: it registers under. `db.DEFAULT_BROKER` for IBKR.
    broker: str

    def base_currency(self, path: Path) -> str:
        """The account's base currency, for converting native amounts."""
        ...

    def metadata(self, path: Path) -> Iterator[StatementMeta]:
        """What each statement block in the file says about itself."""
        ...

    def statements(self, path: Path) -> Iterator[tuple[str, list[NormalisedFill]]]:
        """Yield `(account_id, fills)` for each statement block in the file."""
        ...

    def cash_transactions(self, path: Path) -> Iterator[NormalisedCash]:
        """Fees, dividends, withholding and interest lines."""
        ...

    def positions(self, path: Path) -> Iterator[NormalisedPosition]:
        """Contracts held as of the statement date."""
        ...

    def securities(self, path: Path) -> Iterator[NormalisedSecurity]:
        """Contract definitions: what each id means."""
        ...

    def equity_summaries(self, path: Path) -> Iterator[NormalisedNav]:
        """Daily account value, in the account's base currency."""
        ...


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    inner = getattr(value, "value", value)
    text = str(inner)
    return text or None


def _notes(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return ";".join(str(getattr(c, "value", c)) for c in value)
    return _enum_value(value)


def _s(value: Any) -> str | None:
    if value is None:
        return None
    return str(value) or None


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _qty(value: Any) -> int | float | None:
    """Integers exact, fractions lossless -- see db.py's quantity note."""
    f = _f(value)
    if f is None:
        return None
    i = int(round(f))
    return i if abs(f - i) < 1e-9 else f


def _model_dump(model: Any) -> dict[str, Any]:
    """Best-effort dict of a pydantic model, for the `raw` column."""
    for attr in ("model_dump", "dict"):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - raw column is best-effort
                break
    return {}


class IbkrSource:
    """Reads IBKR Flex statements via py_ibkr.

    The one place in the codebase that knows py_ibkr's attribute names. Every
    `t.somethingCamelCase` that used to live in `ingest._ingest_trades` is here,
    turning into a `NormalisedFill` field named in trading terms.
    """

    broker = "ibkr"

    def base_currency(self, path: Path) -> str:
        from optjournal.sections import raw_sections

        rows = raw_sections(path).get("AccountInformation") or []
        for row in rows:
            code = (row.get("currency") or "").strip()
            if code:
                return code
        return "EUR"

    def metadata(self, path: Path) -> Iterator[StatementMeta]:
        from optjournal.flex import load

        base = self.base_currency(path)
        for stmt in load(path).FlexStatements:
            yield StatementMeta(
                account_id=_s(stmt.accountId),
                from_date=_s(stmt.fromDate),
                to_date=_s(stmt.toDate),
                generated_at=_s(stmt.whenGenerated),
                base_currency=base,
            )

    def statements(self, path: Path) -> Iterator[tuple[str, list[NormalisedFill]]]:
        from optjournal.flex import load

        for stmt in load(path).FlexStatements:
            account_id = _s(stmt.accountId) or ""
            fills = [self._fill(t, account_id) for t in (stmt.Trades or ())]
            yield account_id, fills

    @staticmethod
    def _fill(t: Any, account_id: str) -> NormalisedFill:
        return NormalisedFill(
            trade_id=_s(t.tradeID) or "",
            exec_id=_s(t.ibExecID) or "",
            transaction_id=_s(t.transactionID) or "",
            order_id=_s(t.ibOrderID),
            account_id=account_id,
            trade_date=_s(t.tradeDate),
            date_time=_s(t.dateTime),
            asset_category=_enum_value(t.assetCategory),
            symbol=_s(t.symbol),
            conid=_s(t.conid),
            underlying_symbol=_s(t.underlyingSymbol),
            underlying_conid=_s(t.underlyingConid),
            put_call=_enum_value(t.putCall),
            strike=_f(t.strike),
            expiry=_s(t.expiry),
            multiplier=_f(t.multiplier),
            buy_sell=_enum_value(t.buySell),
            open_close=_enum_value(t.openCloseIndicator),
            notes=_notes(t.notes),
            level_of_detail=_enum_value(t.levelOfDetail),
            quantity=_qty(t.quantity),
            trade_price=_f(t.tradePrice),
            currency=_s(t.currency),
            fx_rate_to_base=_f(t.fxRateToBase) or 1.0,
            proceeds=_f(t.proceeds),
            commission=_f(t.ibCommission),
            commission_currency=_s(t.ibCommissionCurrency),
            taxes=_f(t.taxes),
            realized_pnl=_f(t.fifoPnlRealized),
            mtm_pnl=_f(t.mtmPnl),
            raw=_model_dump(t),
        )

    # --- the sections py_ibkr models, read through its objects ---------------

    def cash_transactions(self, path: Path) -> Iterator[NormalisedCash]:
        from optjournal.flex import load

        for stmt in load(path).FlexStatements:
            account_id = _s(stmt.accountId) or ""
            for c in stmt.CashTransactions or ():
                yield NormalisedCash(
                    transaction_id=_s(c.transactionID) or "",
                    account_id=account_id,
                    # `_enum_value`, not str(): py_ibkr yields CashAction members
                    # whose str() is 'CashAction.FEES'. analysis.py branches on
                    # this by substring, so the leak would change which bucket a
                    # row lands in rather than merely how it prints.
                    kind=_enum_value(c.type),
                    date_time=_s(c.dateTime),
                    settle_date=_s(getattr(c, "settleDate", None)),
                    description=_s(c.description),
                    symbol=_s(c.symbol),
                    conid=_s(c.conid),
                    amount=_f(c.amount) or 0.0,
                    currency=_s(c.currency),
                    fx_rate_to_base=_f(c.fxRateToBase) or 1.0,
                    raw=_model_dump(c),
                )

    # --- the sections py_ibkr does NOT model, read through the shim ----------
    #
    # `raw_sections` returns attribute dicts straight from the XML, so these read
    # `row.get("camelCase")` rather than an object attribute. That is still IBKR
    # vocabulary and still belongs here: the point of the seam is that the
    # vocabulary lives in ONE module, not that it arrives as objects.

    def positions(self, path: Path) -> Iterator[NormalisedPosition]:
        from optjournal.sections import raw_sections

        for row in raw_sections(path).get("OpenPositions") or ():
            yield NormalisedPosition(
                conid=_s(row.get("conid")) or "",
                account_id=_s(row.get("accountId")) or "",
                as_of=_s(row.get("reportDate")),
                symbol=_s(row.get("symbol")),
                asset_category=(row.get("assetCategory") or "").upper() or None,
                underlying_symbol=_s(row.get("underlyingSymbol")),
                put_call=_s(row.get("putCall")),
                strike=_f(row.get("strike")),
                expiry=_s(row.get("expiry")),
                multiplier=_f(row.get("multiplier")),
                quantity=_qty(row.get("position")),
                mark_price=_f(row.get("markPrice")),
                position_value=_f(row.get("positionValue")),
                cost_basis=_f(row.get("costBasisMoney")),
                cost_basis_price=_f(row.get("costBasisPrice")),
                unrealized_pnl=_f(row.get("fifoPnlUnrealized")),
                side=_s(row.get("side")),
                opened_at=_s(row.get("openDateTime")),
                currency=_s(row.get("currency")),
                fx_rate_to_base=_f(row.get("fxRateToBase")) or 1.0,
                raw=dict(row),
            )

    def securities(self, path: Path) -> Iterator[NormalisedSecurity]:
        from optjournal.sections import raw_sections

        for row in raw_sections(path).get("SecuritiesInfo") or ():
            yield NormalisedSecurity(
                conid=_s(row.get("conid")) or "",
                symbol=_s(row.get("symbol")),
                description=_s(row.get("description")),
                asset_category=(row.get("assetCategory") or "").upper() or None,
                sub_category=_s(row.get("subCategory")),
                currency=_s(row.get("currency")),
                multiplier=_f(row.get("multiplier")),
                strike=_f(row.get("strike")),
                expiry=_s(row.get("expiry")),
                put_call=_s(row.get("putCall")),
                underlying_conid=_s(row.get("underlyingConid")),
                underlying_symbol=_s(row.get("underlyingSymbol")),
                isin=_s(row.get("isin")),
                listing_exchange=_s(row.get("listingExchange")),
                raw=dict(row),
            )

    def equity_summaries(self, path: Path) -> Iterator[NormalisedNav]:
        from optjournal.sections import raw_sections

        sections = raw_sections(path)
        base = self.base_currency(path)
        for row in sections.get("EquitySummaryInBase") or ():
            day = _s(row.get("reportDate"))
            total = _combined(row, "total")
            # A NAV row without a NAV is noise. Skipped silently here rather than
            # warned: the source reports what the statement says, and whether an
            # omission is worth telling the user about is the ingest's call.
            if not day or total is None:
                continue
            yield NormalisedNav(
                as_of=day,
                account_id=_s(row.get("accountId")) or "",
                currency=base,
                total=total,
                cash=_combined(row, "cash"),
                stock=_combined(row, "stock"),
                options=_combined(row, "options"),
                raw=dict(row),
            )


def _combined(row: dict[str, str], name: str) -> float | None:
    """A figure IBKR reports either whole or split into long/short halves.

    Some Flex deployments emit `cash`, others `cashLong`/`cashShort`. Both are
    accepted, and a row carrying neither yields None rather than 0.0 -- absent and
    zero are different claims about an account.
    """
    whole = _f(row.get(name))
    if whole is not None:
        return whole
    long_, short = _f(row.get(f"{name}Long")), _f(row.get(f"{name}Short"))
    if long_ is None and short is None:
        return None
    return (long_ or 0.0) + (short or 0.0)


#: Registered sources, keyed by the name the `--broker` flag uses. Add a broker
#: by adding an implementation and a line here. Unknown keys raise -- see
#: `source_for` and the module docstring for why this registry, unlike
#: `stats.SCOPE_BUILDERS`, does not fall open.
SOURCES: dict[str, StatementSource] = {
    IbkrSource.broker: IbkrSource(),
}


def source_for(broker: str) -> StatementSource:
    """The source for `broker`, or a clear error naming what is available."""
    try:
        return SOURCES[broker]
    except KeyError:
        known = ", ".join(sorted(SOURCES)) or "(none)"
        raise ValueError(
            f"no statement source for broker {broker!r}; known brokers: {known}"
        ) from None
