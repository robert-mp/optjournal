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

from optjournal.fills import NormalisedFill


class StatementSource(Protocol):
    """One broker's statements, read into broker-neutral fills.

    A statement can hold more than one account, so the unit is
    `(account_id, fills)` per statement block. The base currency travels
    separately because it is a property of the account, not of any one fill.
    """

    #: The `broker` value stamped on every row this source produces, and the key
    #: it registers under. `db.DEFAULT_BROKER` for IBKR.
    broker: str

    def base_currency(self, path: Path) -> str:
        """The account's base currency, for converting native amounts."""
        ...

    def statements(self, path: Path) -> Iterator[tuple[str, list[NormalisedFill]]]:
        """Yield `(account_id, fills)` for each statement block in the file."""
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
