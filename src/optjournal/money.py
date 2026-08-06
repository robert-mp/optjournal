"""A monetary figure together with the currency it was actually charged in.

Every cash figure in this journal has two readings, and conflating them is the
defect this type exists to prevent:

* **base** -- the accounting translation. Each contributing row converted at
  IBKR's own rate for *its own date*, then summed. Always available, always
  addable across currencies, and never exactly what left the account.
* **native** -- the amount as charged, in the currency it was charged in.
  Exact, but unaddable: USD, SEK and KRW commission cannot share a number.
  Offered only when one currency accounts for the whole figure.

Before this type the pair was spelled as three parallel fields per figure
(`x_base`, `x_native`, `x_native_ccy`) plus a five-line ledger accumulation at
each producer and a two-call unpack at each consumer. Nine fields carried three
figures, `one_currency` was invoked twice per payload key to reach each half of
its tuple, and `options_friction_native_ccy` read *another figure's* currency
field because it had no way to carry its own. All of those were symptoms of the
amount and its currency being separable in the first place.

This module imports nothing internal on purpose: it is a value type, so every
layer may hold one without acquiring a dependency direction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: The money figures every fill row in this journal carries -- each with a
#: `_base` twin and a shared `currency`. Named here, beside the type that reads
#: them, so a leg, an order, a strategy group and a lifecycle cannot disagree
#: about which figures exist.
FILL_MONEY_FIELDS = ("proceeds", "commission", "realized_pnl")


def win_rate(wins: int, losses: int) -> float | None:
    """Percentage of decided outcomes that won, or None when none were decided.

    ``None`` rather than ``0.0``, which is the whole reason this is a named
    function: a win rate of zero means "everything lost", and a scope with no
    closed round trips has not lost anything. The display shows an em dash for
    the first and "0.0%" for the second, so collapsing them would report a
    flawless month as a total failure.

    Lives here, beside `Money`, because this module is the leaf every layer may
    import and the rule was written out three times character for character --
    `HistoryReport`, `MonthStats` and `Cohort` -- none of them carrying the
    reasoning above. Three copies of an undocumented convention is how one of
    them later "fixes" the None into a zero.
    """
    decided = wins + losses
    return None if not decided else wins / decided * 100.0


def one_currency(by_ccy: dict[str, float]) -> tuple[float | None, str | None]:
    """The total and its currency, when exactly one currency accounts for it.

    The primitive behind `Money.gated`. Kept separate and public because it is
    the whole judgement in one place, testable without constructing a figure.

    A native figure is exact but unaddable, so it is offered only when the
    scope is single-currency, and withheld -- `(None, None)` -- the moment a
    second currency appears. That is the display's signal to fall back to the
    base restatement rather than show an exact-looking figure that silently
    dropped part of the total.

    Currencies with no amount are ignored rather than counted: a scope of USD
    option trades plus a zero-commission EUR conversion row is still honestly
    a USD figure.
    """
    live = {ccy: amount for ccy, amount in by_ccy.items() if amount}
    if len(live) != 1:
        return None, None
    ccy, amount = next(iter(live.items()))
    return amount, ccy


@dataclass(frozen=True, slots=True)
class Money:
    """A figure in base currency, plus the as-charged amount where one exists.

    Frozen so a figure cannot be half-updated: changing the amount without the
    currency, or the reverse, is not expressible.
    """

    base: float
    #: The as-charged amount, or None when no single currency accounts for it.
    native: float | None = None
    #: The currency `native` is in. None exactly when `native` is None.
    currency: str | None = None

    def __post_init__(self) -> None:
        # The two halves are meaningless apart: an amount with no currency
        # cannot be labelled, and a currency with no amount says nothing.
        if (self.native is None) != (self.currency is None):
            raise ValueError(
                f"native and currency must both be set or both absent, got "
                f"native={self.native!r} currency={self.currency!r}"
            )

    @classmethod
    def restated(cls, base: float) -> Money:
        """Base only -- no single currency accounts for this figure.

        Use where a native figure cannot exist even in principle, as distinct
        from one withheld because the scope happens to be mixed. `friction`
        includes the estimated AutoFX markup, which IBKR never billed as a
        line item in any currency, so it is `restated` by nature.
        """
        return cls(base=base)

    @classmethod
    def gated(cls, base: float, by_ccy: dict[str, float]) -> Money:
        """Base, plus the native figure when one currency accounts for all of it."""
        native, ccy = one_currency(by_ccy)
        return cls(base=base, native=native, currency=ccy)

    @classmethod
    def charged(
        cls, rows: Iterable[tuple[float | None, float | None, str | None]]
    ) -> Money:
        """Accumulate base and the per-currency ledger in one pass, then gate.

        Each row is `(base, native, currency)` -- the shape an episode or a
        fill already has. This replaces the accumulate-then-gate block that was
        written out four times in `stats.py`, identically each time.
        """
        base = 0.0
        ledger: dict[str, float] = {}
        for row_base, row_native, row_ccy in rows:
            base += row_base or 0.0
            if row_native and row_ccy:
                ledger[row_ccy] = ledger.get(row_ccy, 0.0) + row_native
        return cls.gated(base, ledger)

    @classmethod
    def at_rate(
        cls, native: float | None, rate: float | None, currency: str | None
    ) -> Money:
        """One row's amount, with the base derived from that row's own rate.

        For figures IBKR reports natively and never converts -- a position's
        market value, cost basis and unrealised P&L all arrive with the row's
        `fxRateToBase` and no base column at all. A single row is
        single-currency by construction, so the gate has nothing to decide and
        the native is always offered.

        This replaces `natCash(v, rate)` in the page, which multiplied to base
        and then applied the display rate -- two hops, so a USD value shown in
        USD had round-tripped through EUR at two different rates. Carrying the
        native means the display can use it verbatim.
        """
        if native is None or currency is None:
            return cls(base=0.0) if native is None else cls(base=float(native))
        amount = float(native)
        base = amount if rate is None else amount * float(rate)
        return cls(base=base, native=amount, currency=currency)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], field: str) -> Money:
        """One figure aggregated from fill rows following this project's shape.

        A fill row -- a `trade_legs` view row, an order, an episode-backed
        event -- carries every money figure three ways: `field` is the native
        amount, `field_base` the translation, and `currency` the one it was
        billed in. This walks that convention so the extraction is written
        once instead of at each aggregation site.

        Every level above a leg (order, strategy group, position lifecycle)
        derives its figures from the SAME leaf rows rather than summing the
        level below. The base is identical either way because sums are
        associative -- but the gate is not: asked at each level against the
        union of those legs' currencies it answers correctly everywhere, where
        re-gating an already-gated total cannot tell a native withheld for
        being mixed from one that was never there.

        `trade_legs` carries a currency; `trade_orders` deliberately carries
        none, because an order can span them. That asymmetry is why the leaf is
        the only honest source.
        """
        return cls.charged(
            (row.get(f"{field}_base"), row.get(field), row.get("currency"))
            for row in rows
        )

    @property
    def is_exact(self) -> bool:
        """Whether an as-charged figure is available."""
        return self.native is not None

    def __abs__(self) -> Money:
        """Magnitude, carrying the currency with it.

        The reason this is one operation: `options_friction` used to be
        `abs(commissions_native)` with its currency read from
        `commissions_native_ccy` -- a separate field on a different figure.
        Taking the magnitude of an amount while leaving its currency behind is
        now unrepresentable.
        """
        return Money(
            base=abs(self.base),
            native=None if self.native is None else abs(self.native),
            currency=self.currency,
        )

    def per(self, quantity: float) -> Money | None:
        """This figure divided by a quantity, or None when the quantity is zero.

        Both halves divide by the *same* quantity, so a per-unit figure can
        never be an as-charged amount over a restated denominator.
        """
        if not quantity:
            return None
        return Money(
            base=self.base / quantity,
            native=None if self.native is None else self.native / quantity,
            currency=self.currency,
        )

    def payload(self) -> dict[str, float | str | None]:
        """The JSON shape. Keys are always present, `null` when withheld.

        Stable shape rather than omitted keys, so the page tests for a null
        value and never for a missing property.
        """
        return {"base": self.base, "native": self.native, "ccy": self.currency}
