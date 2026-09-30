"""What the broker cost, over the journal's whole life, scoped by the reader.

The sibling of `analysis.py`, and the difference is the source. `analysis` reads
one raw statement, which is how the CLI reports a statement's own costs and the
only way to see a section a database column does not carry. This module reads
SQLite, and that buys the three things a statement cannot give:

* **The account's whole history.** The newest archived statement covers the last
  30 calendar days, so a cost tab backed by it silently narrows to a month --
  agreeing with the rest of the page only for as long as no trade is older than
  that. The database holds every ingested fill.
* **A scope the reader chooses.** Costs can be narrowed to any set of asset
  categories, so "options only" and "options plus the stock I hedge with" are
  both answerable. A statement is a fixed slice of one account.

  0DTE is offered in the same selector but is not a category -- it is a subset of
  options, classified per round trip -- so it arrives as an explicit set of
  fills. This module takes that set and does not compute it: deciding what counts
  as 0DTE needs episodes, `stats.odte_scope` already answers it, and duplicating
  the rule here is how two surfaces come to disagree about which trades are which.
  See `CostScope`.
* **A cost that stays as-charged when the scope widens.** Figures are `Charge`,
  not `Money`: the per-currency ledger survives a mixed scope instead of
  collapsing to a restatement. See `money.Charge`.

**Attribution is the whole design.** Three kinds of cost, and they differ in what
they can honestly be pinned to:

1. *Execution commission and taxes* carry an asset category, so they narrow
   exactly with the scope.
2. *Fees and withholding* carry none. Market-data subscriptions and custody
   charges are levied on the account, and no fee row in this archive carries a
   contract id or trade id -- so they are reported whole, always, and labelled
   unattributable. Pro-rating them into a fill subset would be inventing a
   split, and hiding them under a narrow scope would make the tab claim a total
   it is not measuring.
3. *The AutoFX rate markup* is estimated, never billed. It is the only figure
   here with no billing currency at all, and it is kept structurally apart from
   everything stated -- see `Friction`.

Costs are presented POSITIVE. IBKR stores charges negative, and a report whose
breakdown disagreed in sign with the total it decomposes would be worse than no
breakdown; the flip happens once, at the query.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

#: `categorise_fee` is imported, not reimplemented: IBKR fee descriptions are free
#: text and the patterns that bucket them are a judgement, so one description
#: landing in two different categories depending on which reader saw it would be a
#: defect the two surfaces could not agree about. `analysis` owns the rule because
#: it had it first; this module reads the same fees out of SQLite.
from optjournal.analysis import categorise_fee
from optjournal.money import Charge
from optjournal.notes import AUTOFX, has_code

__all__ = [
    "AUTOFX_MARKUP_BPS",
    "AUTOFX_MARKUP_MEASURED_BPS",
    "CategoryCost",
    "CostReport",
    "CostScope",
    "FeeGroup",
    "Friction",
    "FxLeg",
    "FxPair",
    "WithholdingLine",
    "build_costs",
]

#: Markup IBKR embeds in the exchange rate on auto-conversions, in basis points,
#: as published for Interactive Brokers Ireland. The reasoning, the verification
#: against a year of conversions and the reason it is a constant rather than
#: something derived from reference rates all live in `analysis.AUTOFX_MARKUP_BPS`
#: -- the statement path measures the same cost and there must be one number.
AUTOFX_MARKUP_BPS = 3.0

#: What the same year of conversions actually implied, volume-weighted against
#: ECB daily mid. IBKR publishes 3.0 and hedges it with "typically" and "at its
#: discretion", so the realised markup is a RANGE, and this is its far end.
#:
#: Carried as data rather than left in prose because it is the largest single
#: uncertainty in the report -- around a quarter of account friction is this
#: estimate -- and a reader deserves to see the band rather than a point estimate
#: dressed up as a measurement.
AUTOFX_MARKUP_MEASURED_BPS = 3.2

#: Asset category holding currency conversions. Not a traded instrument: its
#: "quantity" is an amount of money, so a per-unit cost would be commission per
#: euro, which is not a rate anyone charges or reads.
CASH_CATEGORY = "CASH"


# --- scope -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostScope:
    """What the reader selected, as a predicate over `trades`.

    Two independent narrowings, because the selector offers two different kinds
    of thing and conflating them would misreport one of them:

    * `categories` is an asset category set -- OPT, STK, CASH. These are disjoint
      (a fill is in exactly one) so they simply add, which is what makes them
      safe to multi-select.
    * `fill_ids` is an explicit set of fills, which is how 0DTE arrives. 0DTE is
      NOT a category: it is a subset of options, defined per round trip by
      comparing entry date to expiry, so it cannot be a `WHERE` on a column.
      `stats.odte_scope` already resolves it to fills; this holds the answer.

    Selecting 0DTE alongside Options is therefore a *narrowing*, not a union --
    both conditions apply, so the result is the 0DTE options. That is the honest
    reading of two filters both being on, and the surface says which figures a
    subset selection moved rather than letting the total look category-wide.

    Frozen, so a scope can be passed around, compared and reported. Empty means
    no narrowing asked for, which is what an untouched filter means -- not "show
    nothing", which would make the tab open blank and look broken.
    """

    categories: frozenset[str] = frozenset()
    #: Fills of the round trips a subset selection resolved to, or None for no
    #: fill-level narrowing. An EMPTY frozenset is meaningful and distinct from
    #: None: it is a subset that matched nothing, and must report zeros rather
    #: than silently widening back to everything.
    fill_ids: frozenset[str] | None = None
    #: What the fill-level narrowing was, for a surface that has to say so.
    subset: str = ""

    @classmethod
    def of(cls, categories: Any = None, *, fill_ids: Any = None,
           subset: str = "") -> CostScope:
        """Normalise whatever a caller has -- None, a string, any iterable."""
        if isinstance(categories, str):
            categories = [categories]
        return cls(
            categories=frozenset(
                str(c).upper() for c in (categories or ()) if c
            ),
            fill_ids=None if fill_ids is None else frozenset(
                str(f) for f in fill_ids
            ),
            subset=subset,
        )

    @property
    def is_everything(self) -> bool:
        return not self.categories and self.fill_ids is None

    @property
    def is_subset(self) -> bool:
        """Whether a fill-level narrowing is in force, so a caller can label it."""
        return self.fill_ids is not None

    def condition(self) -> tuple[str, tuple[Any, ...]]:
        """A bare condition for the whole selection, or nothing at all.

        A CONDITION, not a clause: no `WHERE`, so `_where` can AND it with the
        period without either fragment knowing which comes first. Empty for the
        unnarrowed case rather than a tautology -- `1=1` in a query that reads
        fine without it is noise a reader has to decode.
        """
        parts: list[str] = []
        params: list[Any] = []
        if self.categories:
            names = sorted(self.categories)
            parts.append(f"asset_category IN ({','.join('?' for _ in names)})")
            params += names
        if self.fill_ids is not None:
            # An empty subset must match no row. `IN ()` is a syntax error in
            # SQLite, so the impossible condition is spelled out -- silently
            # dropping the clause would widen a zero-match subset to everything.
            if not self.fill_ids:
                parts.append("0")
            else:
                ids = sorted(self.fill_ids)
                parts.append(f"trade_id IN ({','.join('?' for _ in ids)})")
                params += ids
        if not parts:
            return "", ()
        return " AND ".join(parts), tuple(params)

    def covers(self, category: str | None) -> bool:
        return not self.categories or str(category or "").upper() in self.categories


# --- stated costs ------------------------------------------------------------


@dataclass(slots=True)
class CategoryCost:
    """Execution cost for one asset category: what it was billed, and per unit.

    `quantity` is contracts or shares, and is deliberately absent for `CASH`,
    where the traded amount is money.
    """

    category: str
    fills: int = 0
    orders: int = 0
    quantity: float = 0.0
    commission: Charge = field(default_factory=Charge)
    taxes: Charge = field(default_factory=Charge)
    #: Fills whose commission was a CREDIT. IBKR issues these when it adjusts a
    #: per-order minimum across a split order, and they are netted off rather
    #: than added -- surfaced because a negative cost is worth explaining.
    credit_fills: int = 0

    @property
    def total(self) -> Charge:
        return self.commission + self.taxes

    @property
    def per_unit(self) -> Charge | None:
        """Cost per contract or share, or None where that is not a rate.

        Divides the ledger and the base by the SAME quantity, so a per-unit
        figure can never be an as-charged numerator over a restated divisor.
        """
        if not self.quantity or self.category == CASH_CATEGORY:
            return None
        total = self.total
        return Charge(
            base=total.base / self.quantity,
            by_ccy={c: a / self.quantity for c, a in total.by_ccy.items()},
        )


# --- fx ----------------------------------------------------------------------


@dataclass(slots=True)
class FxLeg:
    """One side of the AutoFX split for a pair: how much moved, and what it cost.

    Two of these per pair. The split is the point: a manual conversion pays a
    stated commission and an auto-conversion pays a markup embedded in the rate,
    so charging a markup on the manual leg would double-count a cost that is
    already in `commission`.
    """

    conversions: int = 0
    notional: float = 0.0
    commission: Charge = field(default_factory=Charge)

    def markup_at(self, bps: float) -> float:
        """The rate markup on this leg's notional, at `bps`.

        A float and not a `Charge`, because no currency was ever billed: it is
        an estimate of value lost in a rate. `Friction` is where it is kept
        apart from everything stated.
        """
        return self.notional * bps / 10_000.0


@dataclass(slots=True)
class FxPair:
    """One currency pair's conversion activity, split manual against automatic."""

    symbol: str
    auto: FxLeg = field(default_factory=FxLeg)
    manual: FxLeg = field(default_factory=FxLeg)

    @property
    def conversions(self) -> int:
        return self.auto.conversions + self.manual.conversions

    @property
    def notional(self) -> float:
        return self.auto.notional + self.manual.notional

    @property
    def commission(self) -> Charge:
        """Stated commission across both legs. On auto-conversions this is zero
        by construction -- IBKR says so, and every AFx row in this archive
        confirms it."""
        return self.auto.commission + self.manual.commission

    @property
    def commission_bps(self) -> float | None:
        """Stated commission as basis points of notional.

        Comparable with `AUTOFX_MARKUP_BPS` on purpose: it is the only way to
        see that a manual conversion's dollar minimum (8.9 bps on a €1,944
        trade in this archive) costs multiples of the 3 bps an auto-conversion
        embeds in the rate.
        """
        if not self.notional:
            return None
        return self.commission.base / self.notional * 10_000.0

    def markup_at(self, bps: float) -> float:
        return self.auto.markup_at(bps)


# --- unattributable ----------------------------------------------------------

@dataclass(slots=True)
class FeeGroup:
    """One category of account-level fee. Carries no asset attribution, ever."""

    name: str
    count: int = 0
    total: Charge = field(default_factory=Charge)
    examples: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WithholdingLine:
    """Tax withheld at source on one symbol's dividends.

    Not a broker charge -- the broker forwards it -- but a real recurring loss,
    and the effective rate is the only way to see that a non-treaty rate is
    being applied when a treaty rate was available.
    """

    symbol: str
    currency: str
    gross: Charge = field(default_factory=Charge)
    withheld: Charge = field(default_factory=Charge)

    @property
    def effective_rate(self) -> float | None:
        """Withheld over gross, or None when there is no matching dividend.

        IBKR reports dividends net, so gross is net plus withheld. Withholding
        on credit interest arrives with no DIVIDEND counterpart, and dividing by
        the withholding alone would report a meaningless 100%.
        """
        total = self.gross.base + self.withheld.base
        if not self.gross.base or not total:
            return None
        return self.withheld.base / total * 100.0


# --- the report --------------------------------------------------------------


@dataclass(slots=True)
class Friction:
    """Every cost in scope, with measured and estimated kept structurally apart.

    The split is a type, not a caption. Around a quarter of this account's
    friction is the AutoFX estimate, and a total that silently blends it with
    figures IBKR actually billed would present one number where the reader needs
    two -- so `stated` is a `Charge` (billed, per currency) and `estimated` is a
    bare range in base currency (no currency was ever charged).
    """

    stated: Charge = field(default_factory=Charge)
    #: Low and high ends of the rate-markup estimate, from the published and the
    #: measured bps. Equal only if the two constants ever converge.
    estimated_low: float = 0.0
    estimated_high: float = 0.0

    @property
    def estimated_mid(self) -> float:
        """The midpoint of the estimate, for a surface that shows one figure.

        Offered because a headline has to print something, and the midpoint is
        the honest single value when the two ends are equally credible: IBKR
        publishes 3.0 bps and a year of real conversions implied 3.2, with no
        basis for preferring either. It is NOT a measurement, and every surface
        that prints it is expected to carry the range alongside -- `total_low`
        and `total_high` exist so that costs nothing.
        """
        return (self.estimated_low + self.estimated_high) / 2.0

    @property
    def total_low(self) -> float:
        return self.stated.base + self.estimated_low

    @property
    def total_mid(self) -> float:
        return self.stated.base + self.estimated_mid

    @property
    def total_high(self) -> float:
        return self.stated.base + self.estimated_high

    @property
    def is_estimated(self) -> bool:
        """Whether any part of this total was never billed."""
        return bool(self.estimated_low or self.estimated_high)


@dataclass(slots=True)
class CostReport:
    """What the broker cost, in one scope, over one period.

    `by_category`, `fx`, `fees` and `withholding` are the raw material; the
    `*_friction` properties compose it. Nothing here decides presentation: the
    scope narrows what is attributable and the unattributable is always present,
    so a caller can render either without re-querying.
    """

    base_currency: str
    scope: CostScope
    from_date: str | None
    to_date: str | None
    by_category: list[CategoryCost] = field(default_factory=list)
    fx: list[FxPair] = field(default_factory=list)
    fees: list[FeeGroup] = field(default_factory=list)
    withholding: list[WithholdingLine] = field(default_factory=list)

    # -- attributable

    @property
    def attributable(self) -> Charge:
        """Commission and taxes on the categories in scope."""
        return sum((c.total for c in self.by_category), Charge())

    @property
    def fills(self) -> int:
        return sum(c.fills for c in self.by_category)

    @property
    def credit_fills(self) -> int:
        return sum(c.credit_fills for c in self.by_category)

    # -- unattributable

    @property
    def unattributable(self) -> Charge:
        """Fees, whole. Never narrowed by the scope, because nothing in the data
        ties a market-data subscription or a custody charge to an instrument."""
        return sum((f.total for f in self.fees), Charge())

    # -- estimated

    @property
    def autofx_notional(self) -> float:
        return sum(p.auto.notional for p in self.fx)

    @property
    def autofx_conversions(self) -> int:
        return sum(p.auto.conversions for p in self.fx)

    def autofx_markup(self, bps: float = AUTOFX_MARKUP_BPS) -> float:
        return self.autofx_notional * bps / 10_000.0

    # -- composed

    @property
    def friction(self) -> Friction:
        """The headline: everything in scope, measured apart from estimated.

        The AutoFX markup enters only when conversions are in scope, which needs
        no test here because it is already true of `self.fx`: the scope narrows
        the QUERY, so a report built without CASH holds no pairs and its markup
        is zero by construction. A `scope.covers(CASH)` guard stood here and was
        dead -- mutating it to `True` changed no figure, which is how it was
        found. Enforcement belongs where the rows are chosen, not restated at the
        one place that would then look like the rule's home.
        """
        return Friction(
            stated=self.attributable + self.unattributable,
            estimated_low=self.autofx_markup(AUTOFX_MARKUP_BPS),
            estimated_high=self.autofx_markup(AUTOFX_MARKUP_MEASURED_BPS),
        )


# --- building ----------------------------------------------------------------


def _within(period: str | None, column: str) -> tuple[str, tuple[Any, ...]]:
    """A prefix match on a stored ISO date, so a month or a year both work.

    `LIKE 'YYYY-MM%'` rather than a date range because the column is a plain ISO
    string and that is what `stats` already matches on.
    """
    if not period:
        return "", ()
    return f"{column} LIKE ?", (f"{period}%",)


def _where(*conditions: tuple[str, tuple[Any, ...]]) -> tuple[str, tuple[Any, ...]]:
    """AND the non-empty conditions into one clause, with their params in order.

    Takes conditions rather than clauses so no caller has to know whether it is
    writing the first fragment -- the reason the scope exposes `condition()` and
    not a ready-made `WHERE`.
    """
    parts = [text for text, _ in conditions if text]
    params = tuple(value for _, values in conditions for value in values)
    return ("WHERE " + " AND ".join(parts) if parts else "", params)


def build_costs(
    conn: sqlite3.Connection,
    *,
    scope: CostScope | None = None,
    period: str | None = None,
    base_currency: str = "EUR",
) -> CostReport:
    """Read every cost in the database, narrowed to `scope` and `period`.

    One pass over `trades` and one over `cash_transactions`. The scope narrows
    what is attributable; fees and withholding are read whole regardless, because
    nothing in the data attributes them and a total that hid them under a narrow
    scope would be measuring less than it claims.
    """
    scope = scope or CostScope()
    report = CostReport(
        base_currency=base_currency,
        scope=scope,
        from_date=None,
        to_date=None,
    )

    categories: dict[str, CategoryCost] = {}
    pairs: dict[str, FxPair] = {}
    orders_by_category: dict[str, set[str]] = {}

    trade_where, trade_params = _where(
        scope.condition(), _within(period, "trade_date")
    )
    rows = conn.execute(
        "SELECT trade_id, trade_date, asset_category, symbol, currency, quantity,"
        " ib_order_id, proceeds_base, ib_commission, ib_commission_base,"
        " ib_commission_currency, taxes, fx_rate_to_base, notes"
        f" FROM trades {trade_where} ORDER BY trade_date",
        trade_params,
    ).fetchall()

    for row in rows:
        category = str(row["asset_category"] or "?").upper()
        cost = categories.setdefault(category, CategoryCost(category=category))
        cost.fills += 1
        if row["ib_order_id"]:
            orders_by_category.setdefault(category, set()).add(str(row["ib_order_id"]))

        # Sign flip once, here: IBKR stores a charge negative and this report
        # presents cost positive.
        commission_base = -(row["ib_commission_base"] or 0.0)
        commission_native = -(row["ib_commission"] or 0.0)
        # The currency the COMMISSION was billed in, which is not the
        # instrument's: IBKR bills conversion commission in the base currency
        # while the row's currency is the pair's quote.
        commission_ccy = (
            str(row["ib_commission_currency"] or "").upper()
            or str(row["currency"] or "").upper()
        )
        cost.commission += Charge.of(
            [(commission_base, commission_native, commission_ccy)]
        )
        if commission_base < 0:
            cost.credit_fills += 1

        if row["taxes"]:
            # Taxes carry no currency of their own, so the instrument's is the
            # only reading the data supports.
            tax_native = -(row["taxes"] or 0.0)
            rate = row["fx_rate_to_base"] or 1.0
            cost.taxes += Charge.of(
                [(tax_native * rate, tax_native, str(row["currency"] or "").upper())]
            )

        if category != CASH_CATEGORY:
            if row["quantity"] is not None:
                cost.quantity += abs(float(row["quantity"]))
            continue

        symbol = str(row["symbol"] or "?")
        pair = pairs.setdefault(symbol, FxPair(symbol=symbol))
        leg = pair.auto if has_code(row["notes"], AUTOFX) else pair.manual
        leg.conversions += 1
        # Turnover accumulates regardless of direction, so magnitude.
        leg.notional += abs(row["proceeds_base"] or 0.0)
        leg.commission += Charge.of(
            [(commission_base, commission_native, commission_ccy)]
        )

    for category, cost in categories.items():
        cost.orders = len(orders_by_category.get(category, ()))

    report.by_category = sorted(
        categories.values(), key=lambda c: -c.total.base
    )
    report.fx = sorted(pairs.values(), key=lambda p: -p.notional)
    if rows:
        report.from_date = str(rows[0]["trade_date"] or "") or None
        report.to_date = str(rows[-1]["trade_date"] or "") or None

    _read_cash(conn, report, period=period)
    return report


def _read_cash(
    conn: sqlite3.Connection, report: CostReport, *, period: str | None
) -> None:
    """Fees and withholding, read whole. Deliberately ignores the scope.

    These carry no asset category -- no fee row in this archive carries a
    contract id or a trade id -- so there is nothing to narrow them by. Reported
    always, and labelled unattributable by the surface that shows them.
    """
    where, params = _where(_within(period, "date_time"))
    fees: dict[str, FeeGroup] = {}
    dividends: dict[str, Charge] = {}
    withheld: dict[str, Charge] = {}
    ccy_of: dict[str, str] = {}

    for row in conn.execute(
        "SELECT date_time, type, description, symbol, amount, amount_base, currency"
        f" FROM cash_transactions {where}",
        params,
    ):
        kind = str(row["type"] or "").upper()
        currency = str(row["currency"] or "").upper()
        # SIGNED, and summed with the sign. A refund is a real row: IBKR cancels
        # a market-data charge with a positive `CANCEL[...]` row of the same
        # size, reclaims withholding the same way, and reverses a dividend with a
        # negative one. Taking each row's magnitude booked every reversal as a
        # further charge (3.89 of EUR market data where the account paid 1.29).
        # Cost positive, like every other figure here: the sign flips once, on
        # the charge, and a period refunded more than it paid is a net credit.
        stated = Charge.of([(row["amount_base"] or 0.0, row["amount"] or 0.0,
                             currency)])
        charge = Charge.of([(-(row["amount_base"] or 0.0), -(row["amount"] or 0.0),
                             currency)])

        if "FEES" in kind:
            name = categorise_fee(row["description"])
            group = fees.setdefault(name, FeeGroup(name=name))
            group.count += 1
            group.total += charge
            if len(group.examples) < 3 and row["description"]:
                group.examples.append(str(row["description"]))
        elif "WITHHOLDING" in kind or "WHTAX" in kind:
            key = str(row["symbol"] or "") or "(non-dividend)"
            withheld[key] = withheld.get(key, Charge()) + charge
            ccy_of.setdefault(key, currency)
        elif "DIVIDEND" in kind:
            key = str(row["symbol"] or "") or "(unknown)"
            # Income, so read as stated rather than flipped: the gross figure the
            # withholding is a fraction of.
            dividends[key] = dividends.get(key, Charge()) + stated
            ccy_of.setdefault(key, currency)

    report.fees = sorted(fees.values(), key=lambda f: -f.total.base)
    report.withholding = [
        WithholdingLine(
            symbol=key,
            currency=ccy_of.get(key, ""),
            gross=dividends.get(key, Charge()),
            withheld=withheld.get(key, Charge()),
        )
        for key in sorted(set(dividends) | set(withheld))
    ]
