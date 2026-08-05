"""Cost analysis for an EUR-base IBKR account.

The account this was built for makes almost no option trades but converts
currency constantly and pays a long tail of small recurring fees. Those are
the costs worth surfacing, so this module measures three things:

* FX conversion volume by pair, and commission paid on it.
* Recurring fees, categorised, because a €1/month subscription is invisible
  per-occurrence and material per-year.
* Dividend withholding, expressed as an effective rate, because paying the
  non-treaty rate instead of the treaty rate is a silent recurring loss.

All amounts are converted to the account's base currency using the
`fxRateToBase` IBKR supplies per record, so figures are comparable.

What this module does with FX *spread*: IBKR reports zero commission on
auto-conversions, because the cost is a markup embedded in the exchange rate
instead. That markup is published, so it is applied as a known constant --
see `AUTOFX_MARKUP_BPS`. It is applied only to conversions IBKR flagged
`AFx`, and it is reported as an estimate, separately from stated costs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

__all__ = [
    "AUTOFX_MARKUP_BPS",
    "CommissionGroup",
    "CostReport",
    "FeeCategory",
    "FxPair",
    "WithholdingLine",
    "analyse",
    "categorise_fee",
]

ZERO = Decimal("0")

#: Markup IBKR embeds in the exchange rate on auto-conversions, in basis
#: points. Interactive Brokers Ireland Limited (the entity for EUR-base
#: accounts, CBI ref C423427) publishes this under Commissions -> Spot
#: Currencies: "For currency trades executed under the auto currency
#: conversion service, IB will typically add or subtract (at its discretion)
#: 0.03% to the exchange rate that would otherwise apply. Please note that IB
#: does not separately charge a commission on these auto-conversion trades."
#:
#: 0.03% == 3 bps. This is why `ibCommission` is zero on every AFx row: the
#: cost was never meant to appear there.
#:
#: Treat the resulting figure as an estimate, not a measurement. IBKR says
#: "typically" and "at its discretion", so the realised markup on any single
#: conversion may differ. Verified against 12 months of real conversions:
#: volume-weighted deviation from ECB daily mid came to 3.2 bps against the
#: published 3.0, consistent to within noise. Per conversion the deviation is
#: dominated by intraday drift (sd ~37 bps on EUR.USD) and is not measurable;
#: only the aggregate converges. That is why this is a published constant
#: rather than something computed from reference rates.
AUTOFX_MARKUP_BPS = Decimal("3")

#: Asset category this journal is scoped to. Ingest filters to it, so the
#: database holds only these trades -- but `analyse` reads the raw statement,
#: which holds the whole account. Without an explicit scope the cost report
#: silently blended stock commission into a figure labelled as this journal's.
DEFAULT_JOURNAL_ASSET = "OPT"

#: IBKR trade-note code marking a conversion as executed by the auto currency
#: conversion service. py_ibkr models this as `Code.AUTOFX`; the wire value is
#: compared directly so an ad-hoc member minted by `compat` still matches.
AUTOFX_CODE = "AFx"


def _is_autofx(trade) -> bool:
    """True when IBKR flagged this conversion as an auto-conversion.

    Conversions without the flag are not priced at `AUTOFX_MARKUP_BPS`. In
    real data these split cleanly: flagged rows are genuine conversions,
    unflagged ones are sub-cent residual sweeps with a `CUSTHSFX` execution
    ID and no order type. A manual IDEALPRO conversion would also be
    unflagged, and would carry a real `ibCommission` instead -- so applying
    the markup to everything would double-count it.
    """
    return any(
        str(getattr(note, "value", note)) == AUTOFX_CODE
        for note in trade.notes or ()
    )

#: Fee description patterns, most specific first. IBKR fee descriptions are
#: free text, so this is heuristic by necessity; `OTHER` is the honest
#: bucket for anything unmatched rather than a forced guess.
FEE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Market data", re.compile(r"\b(OPRA|NP L1|L1|L2|SNAPSHOT|MARKET DATA)\b", re.I)),
    ("Custody", re.compile(r"CUSTODY", re.I)),
    ("Commission adj", re.compile(r"COMMISSION|EXECUTION", re.I)),
    ("Interest", re.compile(r"INTEREST|CREDIT INT", re.I)),
    ("Transaction tax", re.compile(r"STAMP|FTT|TRANSACTION TAX", re.I)),
)


def categorise_fee(description: str | None) -> str:
    """Map a free-text IBKR fee description to a coarse category."""
    text = description or ""
    for name, pattern in FEE_PATTERNS:
        if pattern.search(text):
            return name
    return "Other"


def _to_base(amount: Decimal | None, rate: Decimal | None) -> Decimal:
    """Convert a record amount into the account's base currency."""
    if amount is None:
        return ZERO
    return amount * (rate if rate is not None else Decimal("1"))


def _commission_to_base(
    amount: Decimal | None, rate: Decimal | None,
    commission_ccy: str | None, instrument_ccy: str | None, base_ccy: str,
) -> Decimal:
    """Commission into base, at a rate that actually applies to IT.

    `fxRateToBase` is the INSTRUMENT's rate, and `_to_base` is right for every
    amount denominated in the instrument's currency -- proceeds, taxes, notional.
    Commission is the exception: IBKR bills the commission on an FX conversion in
    the BASE currency while the row's currency is the pair's quote, so converting
    it here multiplied a EUR amount by a SEK->EUR rate and reported a cost 11x
    too small. Mirrors ingest._commission_base, which fixes the stored column;
    this path recomputes from the statement and so needs the same rule.

    A commission in some third currency has no rate in the statement. It is left
    unconverted rather than dropped: a cost report that silently omits a charge
    is worse than one that states it at an unconverted magnitude, and the ingest
    warning names the row either way.
    """
    if amount is None:
        return ZERO
    if commission_ccy and commission_ccy == base_ccy:
        return amount
    if not commission_ccy or commission_ccy == instrument_ccy:
        return _to_base(amount, rate)
    return amount


@dataclass(slots=True)
class FxPair:
    symbol: str
    conversions: int = 0
    notional_base: Decimal = ZERO
    #: Signed, like CommissionGroup.commission_signed: negative is a charge.
    commission_signed: Decimal = ZERO
    autofx_conversions: int = 0
    autofx_notional_base: Decimal = ZERO

    @property
    def commission_base(self) -> Decimal:
        """Cost as a positive number. See `CommissionGroup.commission_base`."""
        return -self.commission_signed

    @property
    def commission_bps(self) -> Decimal | None:
        """Commission as basis points of notional, or None if no notional."""
        if not self.notional_base:
            return None
        return (self.commission_base / self.notional_base) * Decimal("10000")

    @property
    def autofx_spread_base(self) -> Decimal:
        """Estimated cost of IBKR's rate markup on auto-conversions.

        Applied to `autofx_notional_base` only, never to the whole pair, so
        manual conversions and residual sweeps are not charged a markup they
        did not incur.
        """
        return self.autofx_notional_base * AUTOFX_MARKUP_BPS / Decimal("10000")


@dataclass(slots=True)
class CommissionGroup:
    """Execution commission for one asset category.

    Distinct from fees: commission is `Trade.ibCommission`, charged per fill
    and attached to a trade. Fees are `CashTransaction` rows of type FEES and
    are account-level. Verified non-overlapping against real data -- none of
    the fee rows carry a conid or a tradeID, so a fee can never also be a
    trade commission.
    """

    asset_category: str
    fills: int = 0
    quantity: int = 0
    #: Commission AS CHARGED, keyed by the currency IBKR billed it in. A group
    #: can span currencies (this account's stock trades span four), and those
    #: amounts cannot be added -- so the breakdown is carried and the decision
    #: about whether one currency can speak for the whole figure is left to the
    #: layer that composes the payload. This module stays a leaf: it produces
    #: the raw material and imports nothing to interpret it.
    native_by_ccy: dict[str, Decimal] = field(default_factory=dict)
    #: Taxes as charged, keyed the same way. Friction is commission + taxes, so
    #: a friction figure can only be exact when BOTH are, in one currency.
    taxes_native_by_ccy: dict[str, Decimal] = field(default_factory=dict)
    #: Accumulated in IBKR's own sign convention: negative is a charge,
    #: positive a credit. Kept signed on purpose -- see `commission_base`.
    commission_signed: Decimal = ZERO
    taxes_signed: Decimal = ZERO
    #: Fills carrying a positive (credit) commission. Surfaced because a
    #: nonzero count is the fingerprint of a per-order minimum adjustment,
    #: and it is the case the old per-fill abs() silently inverted.
    credit_fills: int = 0

    @property
    def commission_base(self) -> Decimal:
        """Cost as a positive number, with credits netted off.

        The sign is flipped once, here, rather than per fill. Applying abs()
        to each fill turned IBKR's commission *credits* into extra charges:
        when an order splits into several fills, IBKR charges the per-order
        minimum against one fill and credits part of it back on another, so a
        credit of c was counted as +c instead of -c and the group came out 2c
        too high. Verified against order 1096738670 (TSLA, 2026-03-09): fills
        of -0.3481 and +0.0088 USD net to the -0.3393 the order actually paid.

        Negative output is meaningful, not a bug: it means the category was a
        net credit over the period.
        """
        return -self.commission_signed

    @property
    def taxes_base(self) -> Decimal:
        """Taxes as a positive cost. Same sign handling as `commission_base`."""
        return -self.taxes_signed

    @property
    def per_unit_base(self) -> Decimal | None:
        """Commission per contract or share. The figure that scales with volume."""
        if not self.quantity:
            return None
        return self.commission_base / Decimal(self.quantity)


@dataclass(slots=True)
class FeeCategory:
    name: str
    count: int = 0
    total_base: Decimal = ZERO
    examples: list[str] = field(default_factory=list)

    @property
    def per_year_base(self) -> Decimal:
        return self.total_base


@dataclass(slots=True)
class WithholdingLine:
    symbol: str
    currency: str
    gross_base: Decimal
    withheld_base: Decimal

    @property
    def effective_rate(self) -> Decimal | None:
        """Withheld / (net + withheld), since IBKR reports dividends net.

        Returns None when there is no matching dividend. Withholding on
        credit interest, for instance, arrives as a WHTAX row with no
        DIVIDEND counterpart, and dividing by the withholding alone would
        report a meaningless 100%.
        """
        if not self.gross_base:
            return None
        total = self.gross_base + self.withheld_base
        if not total:
            return None
        return (self.withheld_base / total) * Decimal("100")


@dataclass(slots=True)
class CostReport:
    base_currency: str
    from_date: object
    to_date: object
    fx: list[FxPair]
    commissions: list[CommissionGroup]
    fees: list[FeeCategory]
    withholding: list[WithholdingLine]
    fx_caveat: str
    #: Asset category this journal covers. Costs on it are attributable to the
    #: journal; everything else is account-level context.
    journal_asset: str = DEFAULT_JOURNAL_ASSET

    @property
    def journal_commissions(self) -> list[CommissionGroup]:
        return [g for g in self.commissions if g.asset_category == self.journal_asset]

    @property
    def other_commissions(self) -> list[CommissionGroup]:
        return [g for g in self.commissions if g.asset_category != self.journal_asset]

    @property
    def journal_commission_base(self) -> Decimal:
        return sum((g.commission_base for g in self.journal_commissions), ZERO)

    @property
    def journal_native_by_ccy(self) -> dict[str, Decimal]:
        """Journal commission as charged, per billing currency.

        Magnitudes, matching `commission_base`'s sign convention: the whole
        report presents cost as positive, and a breakdown that disagreed with
        the total it decomposes would be worse than no breakdown.
        """
        out: dict[str, Decimal] = {}
        for g in self.journal_commissions:
            for ccy, amount in g.native_by_ccy.items():
                out[ccy] = out.get(ccy, ZERO) - amount
        return out

    @property
    def journal_friction_native_by_ccy(self) -> dict[str, Decimal]:
        """Friction as charged, per currency: commission plus taxes.

        Merged rather than gated separately, so a scope whose commission is USD
        and whose taxes are SEK produces two entries and is correctly refused a
        single exact figure.
        """
        out = dict(self.journal_native_by_ccy)
        for g in self.journal_commissions:
            for ccy, amount in g.taxes_native_by_ccy.items():
                out[ccy] = out.get(ccy, ZERO) - amount
        return out

    @property
    def journal_taxes_base(self) -> Decimal:
        return sum((g.taxes_base for g in self.journal_commissions), ZERO)

    @property
    def journal_friction_base(self) -> Decimal:
        """Cost attributable to the instruments this journal actually covers.

        Only per-trade charges qualify, because only they carry an
        assetCategory. This is the number to read when asking what the
        journalled book costs to run.
        """
        return self.journal_commission_base + self.journal_taxes_base

    @property
    def journal_per_unit_base(self) -> Decimal | None:
        """Commission per contract across the journal's scope.

        The forward-looking figure: options commission is charged per contract
        with no reference to money at risk, so this is the only number in the
        report that predicts cost at higher volume.
        """
        qty = sum(g.quantity for g in self.journal_commissions)
        if not qty:
            return None
        return self.journal_commission_base / Decimal(qty)

    @property
    def other_commission_base(self) -> Decimal:
        return sum((g.commission_base for g in self.other_commissions), ZERO)

    @property
    def other_taxes_base(self) -> Decimal:
        return sum((g.taxes_base for g in self.other_commissions), ZERO)

    @property
    def account_friction_base(self) -> Decimal:
        """Cost the journal's scope cannot claim, and why it cannot.

        Three components, none of them attributable to `journal_asset`:
        commission on other instruments belongs to those instruments; fees
        carry no assetCategory at all (market-data subscriptions and custody
        charges are levied on the account, and in this data none of the fee
        rows carries a conid or tradeID); and the AutoFX markup arises from
        currency conversion, which IBKR never ties back to the trade that
        caused it. Splitting any of the three into an options share would mean
        inventing the split, so they are reported whole and kept separate.
        """
        return (
            self.other_commission_base
            + self.other_taxes_base
            + self.total_fees_base
            + self.total_autofx_spread_base
        )
    @property
    def total_fees_base(self) -> Decimal:
        return sum((c.total_base for c in self.fees), ZERO)

    @property
    def total_commission_base(self) -> Decimal:
        """Execution commission across every asset category.

        This is the single authoritative commission total. The per-pair
        commission in the FX section is a breakout of the CASH group here, not
        an additional cost.
        """
        return sum((g.commission_base for g in self.commissions), ZERO)

    @property
    def total_taxes_base(self) -> Decimal:
        return sum((g.taxes_base for g in self.commissions), ZERO)

    @property
    def total_friction_base(self) -> Decimal:
        """Every cost incurred, in base currency: stated plus estimated.

        Stated costs are commission, fees and taxes -- figures IBKR reports
        explicitly. To those is added the estimated AutoFX rate markup, which
        IBKR charges but never itemises. Excluding it understated friction and
        made auto-conversion look free; including it is the whole reason
        `AUTOFX_MARKUP_BPS` exists.

        Read alongside `total_stated_friction_base` when the distinction
        between measured and estimated matters.
        """
        return self.total_stated_friction_base + self.total_autofx_spread_base

    @property
    def total_stated_friction_base(self) -> Decimal:
        """Costs IBKR states explicitly. Contains no estimate."""
        return self.total_commission_base + self.total_fees_base + self.total_taxes_base

    @property
    def total_autofx_notional_base(self) -> Decimal:
        return sum((p.autofx_notional_base for p in self.fx), ZERO)

    @property
    def total_autofx_spread_base(self) -> Decimal:
        """Estimated AutoFX markup across every pair. See `AUTOFX_MARKUP_BPS`."""
        return sum((p.autofx_spread_base for p in self.fx), ZERO)

    @property
    def total_fx_notional_base(self) -> Decimal:
        return sum((p.notional_base for p in self.fx), ZERO)

    @property
    def total_fx_commission_base(self) -> Decimal:
        return sum((p.commission_base for p in self.fx), ZERO)


def analyse(
    statement,
    base_currency: str = "EUR",
    journal_asset: str = DEFAULT_JOURNAL_ASSET,
) -> CostReport:
    """Build a CostReport from one py_ibkr FlexStatement.

    `journal_asset` is the asset category this journal is scoped to. It does
    not filter anything -- every category is still measured -- but it decides
    which costs the report presents as attributable to the journal and which
    as account-level context.
    """
    pairs: dict[str, FxPair] = {}
    groups: dict[str, CommissionGroup] = {}

    for t in statement.Trades or ():
        cat = str(getattr(t.assetCategory, "value", t.assetCategory) or "?").upper()
        rate = t.fxRateToBase
        g = groups.setdefault(cat, CommissionGroup(asset_category=cat))
        g.fills += 1
        # Signed, not abs(): a fill can carry a commission *credit* when IBKR
        # adjusts a per-order minimum across a split order, and abs() would
        # book that credit as a further charge.
        commission = _commission_to_base(
            t.ibCommission, rate,
            # getattr, like assetCategory above: a statement object need not
            # carry every field, and a fake trade in a test that has nothing to
            # say about commission currency should fall through to the
            # instrument's rate rather than raise.
            str(getattr(t, "ibCommissionCurrency", None) or "") or None,
            str(getattr(t, "currency", None) or "") or None,
            base_currency,
        )
        g.commission_signed += commission
        g.taxes_signed += _to_base(t.taxes, rate)
        if t.ibCommission:
            billed = (str(getattr(t, "ibCommissionCurrency", None) or "")
                      or str(getattr(t, "currency", None) or "") or base_currency)
            g.native_by_ccy[billed] = g.native_by_ccy.get(billed, ZERO) + t.ibCommission
        if t.taxes:
            # Taxes carry no currency field of their own, so the instrument's
            # is the only interpretation the statement supports.
            tc = str(getattr(t, "currency", None) or "") or base_currency
            g.taxes_native_by_ccy[tc] = g.taxes_native_by_ccy.get(tc, ZERO) + t.taxes
        if commission > ZERO:
            g.credit_fills += 1
        # Quantity is only a meaningful denominator for contracts and shares.
        # A per-unit figure on a currency conversion would be commission per
        # euro, which is not a rate anyone charges or reads.
        if cat != "CASH" and t.quantity is not None:
            g.quantity += int(abs(t.quantity))

        if cat != "CASH":
            continue
        sym = str(t.symbol or "?")
        p = pairs.setdefault(sym, FxPair(symbol=sym))
        p.conversions += 1
        # proceeds is signed by direction; magnitude is the converted value.
        # abs() is correct here -- turnover accumulates regardless of side.
        notional = abs(_to_base(t.proceeds, rate))
        p.notional_base += notional
        p.commission_signed += commission
        if _is_autofx(t):
            p.autofx_conversions += 1
            p.autofx_notional_base += notional

    fees: dict[str, FeeCategory] = {}
    dividends: dict[str, Decimal] = defaultdict(lambda: ZERO)
    withheld: dict[str, Decimal] = defaultdict(lambda: ZERO)
    ccy_of: dict[str, str] = {}

    for c in statement.CashTransactions or ():
        kind = str(c.type).upper()
        amount_base = _to_base(c.amount, c.fxRateToBase)

        if "FEES" in kind:
            name = categorise_fee(c.description)
            cat = fees.setdefault(name, FeeCategory(name=name))
            cat.count += 1
            cat.total_base += abs(amount_base)
            if len(cat.examples) < 3 and c.description:
                cat.examples.append(c.description)
        elif "WHTAX" in kind:
            key = str(c.symbol or "(non-dividend)")
            withheld[key] += abs(amount_base)
            ccy_of.setdefault(key, str(c.currency or ""))
        elif "DIVIDEND" in kind:
            key = str(c.symbol or "(unknown)")
            dividends[key] += amount_base
            ccy_of.setdefault(key, str(c.currency or ""))

    lines = [
        WithholdingLine(
            symbol=key,
            currency=ccy_of.get(key, ""),
            gross_base=dividends.get(key, ZERO),
            withheld_base=withheld.get(key, ZERO),
        )
        for key in sorted(set(dividends) | set(withheld))
    ]

    autofx_pairs = [p for p in pairs.values() if p.autofx_conversions]
    if autofx_pairs:
        n = sum(p.autofx_conversions for p in autofx_pairs)
        total = sum(p.conversions for p in pairs.values())
        caveat = (
            f"{n} of {total} conversions are AutoFX, priced by IBKR as a "
            f"{AUTOFX_MARKUP_BPS} bps markup on the rate rather than a "
            f"commission. That markup is estimated here from IBKR's published "
            f"schedule, so it is not a measured figure."
        )
    elif pairs:
        caveat = (
            "No conversion carries the AutoFX flag, so no rate markup is "
            "estimated; commission shown is what IBKR reported."
        )
    else:
        caveat = "No currency conversions in this statement."

    return CostReport(
        base_currency=base_currency,
        from_date=statement.fromDate,
        to_date=statement.toDate,
        fx=sorted(pairs.values(), key=lambda p: -p.notional_base),
        commissions=sorted(groups.values(), key=lambda g: -g.commission_base),
        fees=sorted(fees.values(), key=lambda c: -c.total_base),
        withholding=lines,
        fx_caveat=caveat,
        journal_asset=journal_asset,
    )


def format_report(report: CostReport) -> str:
    """Render a CostReport as plain text."""
    out: list[str] = []
    cur = report.base_currency
    out.append(f"Cost report  {report.from_date} .. {report.to_date}  (base {cur})")

    out.append("\nExecution commission")
    out.append(f"  {'asset':<10}{'fills':>7}{'qty':>9}{'commission':>13}{'per unit':>11}")
    for g in report.commissions:
        per = f"{g.per_unit_base:,.4f}" if g.per_unit_base is not None else "-"
        out.append(
            f"  {g.asset_category:<10}{g.fills:>7}{g.quantity or '-':>9}"
            f"{g.commission_base:>13,.4f}{per:>11}"
        )
    out.append(
        f"  {'TOTAL':<10}{sum(g.fills for g in report.commissions):>7}{'':>9}"
        f"{report.total_commission_base:>13,.4f}"
    )
    if report.total_taxes_base:
        out.append(f"  {'taxes':<10}{'':>16}{report.total_taxes_base:>13,.4f}")

    out.append("\nFX conversions")
    out.append(
        f"  {'pair':<10}{'count':>7}{'notional':>14}{'commission':>12}"
        f"{'AFx':>6}{'AFx notional':>14}{f'@{AUTOFX_MARKUP_BPS}bps':>10}"
    )
    for p in report.fx:
        out.append(
            f"  {p.symbol:<10}{p.conversions:>7}{p.notional_base:>14,.2f}"
            f"{p.commission_base:>12,.2f}{p.autofx_conversions:>6}"
            f"{p.autofx_notional_base:>14,.2f}{p.autofx_spread_base:>10,.2f}"
        )
    out.append(
        f"  {'TOTAL':<10}{sum(p.conversions for p in report.fx):>7}"
        f"{report.total_fx_notional_base:>14,.2f}"
        f"{report.total_fx_commission_base:>12,.2f}"
        f"{sum(p.autofx_conversions for p in report.fx):>6}"
        f"{report.total_autofx_notional_base:>14,.2f}"
        f"{report.total_autofx_spread_base:>10,.2f}"
    )
    out.append("  note: commission above is the CASH row of the commission")
    out.append("        table, shown per pair -- not an additional cost.")
    out.append(f"  note: {report.fx_caveat}")

    out.append("\nFees")
    out.append(f"  {'category':<18}{'count':>7}{'total':>12}")
    for c in report.fees:
        out.append(f"  {c.name:<18}{c.count:>7}{c.total_base:>12,.2f}")
    out.append(f"  {'TOTAL':<18}{sum(c.count for c in report.fees):>7}"
               f"{report.total_fees_base:>12,.2f}")
    for c in report.fees:
        if c.examples:
            out.append(f"    {c.name}: {c.examples[0]}")

    out.append("\nDividend withholding")
    out.append(f"  {'symbol':<16}{'ccy':>5}{'net':>10}{'withheld':>11}{'eff rate':>10}")
    for w in report.withholding:
        rate = f"{w.effective_rate:.1f}%" if w.effective_rate is not None else "-"
        out.append(
            f"  {w.symbol:<16}{w.currency:>5}{w.gross_base:>10,.2f}"
            f"{w.withheld_base:>11,.2f}{rate:>10}"
        )

    scope = report.journal_asset
    out.append(f"\n{scope} -- attributable to this journal ({cur})")
    if report.journal_commissions:
        for g in report.journal_commissions:
            per = (
                f"  {g.per_unit_base:,.4f} per unit"
                if g.per_unit_base is not None
                else ""
            )
            out.append(
                f"  {'commission':<24}{g.commission_base:>12,.4f}  stated"
                f"   {g.fills} fill(s){per}"
            )
        if report.journal_taxes_base:
            out.append(f"  {'taxes':<24}{report.journal_taxes_base:>12,.4f}  stated")
        out.append(f"  {'journal friction':<24}{report.journal_friction_base:>12,.4f}")
    else:
        out.append(f"  no {scope} trades in this statement")

    out.append(f"\nAccount-level -- all instruments, context only ({cur})")
    others = ", ".join(g.asset_category for g in report.other_commissions)
    if report.other_commissions:
        out.append(
            f"  {'commission, other':<24}{report.other_commission_base:>12,.4f}"
            f"  stated   {others}"
        )
    if report.other_taxes_base:
        out.append(f"  {'taxes, other':<24}{report.other_taxes_base:>12,.4f}  stated")
    out.append(
        f"  {'fees':<24}{report.total_fees_base:>12,.4f}"
        f"  stated   no asset attribution"
    )
    out.append(
        f"  {'AutoFX markup':<24}{report.total_autofx_spread_base:>12,.4f}"
        f"  estimated @ {AUTOFX_MARKUP_BPS} bps"
    )
    out.append(f"  {'account friction':<24}{report.account_friction_base:>12,.4f}")

    out.append(f"\n  {'account total':<24}{report.total_friction_base:>12,.4f}")
    out.append(
        f"  of which stated {report.total_stated_friction_base:,.4f}, "
        f"estimated {report.total_autofx_spread_base:,.4f}."
    )
    credits = sum(g.credit_fills for g in report.commissions)
    if credits:
        out.append(
            f"  note: {credits} fill(s) carried a commission credit, netted off "
            f"rather than added."
        )
    return "\n".join(out)
