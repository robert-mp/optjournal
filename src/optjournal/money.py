"""A monetary figure together with the currency it was actually charged in.

Every cash figure in this journal has two readings, and conflating them is the
defect this type exists to prevent:

* **base** -- the accounting translation. Each contributing row converted at
  IBKR's own rate for *its own date*, then summed. Always available, always
  addable across currencies, and never exactly what left the account.
* **native** -- the amount as charged, in the currency it was charged in.
  Exact, but unaddable: USD, SEK and KRW commission cannot share a number.
  Offered only when one currency accounts for the whole figure.

Two types, one distinction. `Money` is any figure: it offers the as-charged
amount when one currency accounts for the whole thing and withholds it when the
scope is mixed, because USD, SEK and KRW cannot share a number. `Charge` is a
*cost*, and keeps the whole per-currency ledger instead of collapsing it -- the
cost report is scoped by the reader, and widening from options to the account
should not turn every exact figure into a restatement when each charge is still
known. A `Charge` yields its `Money` on request, so the two agree where they
overlap.

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
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class Charge:
    """A cost, kept as the set of amounts actually billed, plus the translation.

    `Money` answers "what is this figure, and can one currency speak for it?".
    Under a scope spanning asset categories the answer to the second half is no,
    and `Money.gated` then withholds the native entirely -- correctly, because
    USD, SEK and KRW cannot share a number. But a *cost* report is the one place
    that withholding loses the thing the reader came for: widening the scope from
    options to the whole account should not turn every as-charged figure into a
    restatement, because the charges are all still known individually.

    So this keeps the ledger. `base` is the addable translation, `by_ccy` is what
    IBKR actually billed, per currency, and no information is discarded at any
    scope. A single-currency `Charge` still answers `money` for the surfaces that
    want one figure, so the two types agree where they overlap and this one is
    strictly more informative where they do not.

    Frozen and normalised on construction: zero-amount currencies are dropped, so
    a USD option scope plus a zero-commission EUR conversion row is a USD charge
    rather than one claiming two currencies. Build with `of`, which does the
    normalising -- the constructor is for callers that already hold a clean
    ledger.
    """

    #: Defaults to zero so `Charge()` is the empty cost -- the identity for `+`,
    #: and what a category with no charges yet honestly holds. `Money` takes no
    #: such default on purpose: a figure with no amount says nothing, where a
    #: cost of nothing is a fact.
    base: float = 0.0
    #: Amount billed per currency, non-zero entries only. Empty when nothing was
    #: charged, which is different from an unknown charge -- see `is_free`.
    by_ccy: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def of(cls, rows: Iterable[tuple[float | None, float | None, str | None]]) -> Charge:
        """Accumulate `(base, native, currency)` rows into one charge.

        The same row shape `Money.charged` takes, deliberately: a caller that
        has rows for one can hand them to the other without reshaping, and the
        two stay comparable when a surface shows both.
        """
        base = 0.0
        ledger: dict[str, float] = {}
        for row_base, row_native, row_ccy in rows:
            base += row_base or 0.0
            if row_native and row_ccy:
                ledger[row_ccy] = ledger.get(row_ccy, 0.0) + row_native
        return cls(base=base, by_ccy={c: a for c, a in ledger.items() if a})

    @property
    def money(self) -> Money:
        """This charge as a `Money`, gating the native the way every other figure does.

        The bridge to surfaces that show one figure. A single-currency charge
        keeps its as-charged amount; a mixed one falls back to the base, which is
        exactly `Money.gated`'s rule -- stated here by delegation rather than
        reimplemented, so the two can never disagree.
        """
        return Money.gated(self.base, dict(self.by_ccy))

    @property
    def is_free(self) -> bool:
        """Whether nothing was charged at all.

        Distinct from an empty ledger with a non-zero base, which is how an
        *estimated* cost arrives: the AutoFX markup has a base and no billing
        currency, because IBKR never itemised it in one. See `Money.restated`.
        """
        return not self.base and not self.by_ccy

    def __add__(self, other: Charge) -> Charge:
        """Sum two charges, merging their ledgers.

        Addition is the whole reason a cost report can be scoped: the page adds
        the categories the reader selected, and each currency stays its own
        column through the sum.
        """
        merged = dict(self.by_ccy)
        for ccy, amount in other.by_ccy.items():
            merged[ccy] = merged.get(ccy, 0.0) + amount
        return Charge(base=self.base + other.base,
                      by_ccy={c: a for c, a in merged.items() if a})

    def __abs__(self) -> Charge:
        """Magnitude of every component. Cost is presented positive."""
        return Charge(base=abs(self.base),
                      by_ccy={c: abs(a) for c, a in self.by_ccy.items()})

    def payload(self) -> dict[str, Any]:
        """The JSON shape: the gated figure, plus the ledger that produced it.

        Carries `Money`'s three keys verbatim so a consumer already reading a
        money-shaped payload needs no new branch, and adds `charged` as the
        per-currency detail. A page can lead with the single number and dissect
        it without a second request.
        """
        return {**self.money.payload(), "charged": dict(self.by_ccy)}
