"""The broker-neutral shapes a statement reduces to.

This is the seam between a broker's statement and the journal's database. A
`StatementSource` (see `sources.py`) turns whatever a broker sends -- IBKR's Flex
XML with its camelCase attributes today, another broker's JSON tomorrow -- into
these, and `ingest.py` writes them without knowing which broker they came from.

Five shapes, one per statement section the journal stores: a fill, a cash
transaction, a position snapshot, a contract definition and a daily NAV row.
They are separate types rather than one union because they answer different
questions and have different identities -- a fill is an event, a snapshot is a
point-in-time observation, a contract is a definition that outlives both.

Named in trading terms rather than IBKR's: `exec_id` not `ibExecID`,
`realized_pnl` not `fifoPnlRealized`, `as_of` not `reportDate`, `contract_id` not
`conid`. The point of the types is that a second broker fills these fields from
its own vocabulary, so the fields cannot carry the first broker's.

`contract_id` was `conid` until PLAN.md task 8 step 2, and the delay was the
point: renaming it here alone would have left the seam speaking two dialects
(`NormalisedFill.conid` beside `NormalisedPosition.contract_id`), which is worse
than one consistent wrong name. It moved when every shape here could move
together, in a commit that touches no schema and no payload.

**The DATABASE columns are still `conid`, and that is deliberate, not a leftover.**
`ingest.py` is the one place the two vocabularies meet: its SQL names the column,
its values read the attribute, so `fill.contract_id` is written into `conid` on one
line. That asymmetry is visible in exactly one file rather than spread across
thirteen, and it is what makes the schema rename (task 8 steps 3-5) a change to the
schema alone. See PLAN.md for why those steps wait for a second broker.

A leaf, like `money.py`: it imports nothing internal, so both the sources that
produce these and the ingest that consumes them can hold one without a dependency
direction between them.

Deliberately NOT normalised: the amounts are as the broker stated them. Base
conversions (`proceeds * rate`, the commission-currency rule) are the journal's
arithmetic, so `ingest` computes them -- a broker reports what it charged, not how
this journal chooses to translate it. `raw` carries the source's own dict for the
`raw` column, so a field can be promoted to a real column later without a refetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalisedFill:
    """One executed fill, in broker-neutral terms.

    Every field a `trades` row needs except the three the journal adds itself
    (`broker`, `source_file`, `first_seen_at`) and the `_base` translations it
    derives. Frozen, so a source cannot hand back a half-built fill that a later
    line mutates -- the same guarantee `Money` gives.
    """

    #: The broker's own identifier for this execution and the order it belonged
    #: to. Unique only WITHIN the broker -- identity in the database is
    #: `(broker, trade_id)`, see db._rekey_trades_by_broker.
    trade_id: str
    exec_id: str
    transaction_id: str
    order_id: str | None

    account_id: str
    trade_date: str | None
    date_time: str | None

    asset_category: str | None
    symbol: str | None
    contract_id: str | None
    underlying_symbol: str | None
    underlying_contract_id: str | None

    put_call: str | None
    strike: float | None
    expiry: str | None
    multiplier: float | None

    buy_sell: str | None
    open_close: str | None
    notes: str | None
    level_of_detail: str | None

    quantity: int | float
    trade_price: float | None
    currency: str | None
    fx_rate_to_base: float

    proceeds: float | None
    commission: float | None
    #: The currency the commission was billed in, which need not be the
    #: instrument's. The journal checks the two agree before converting; see
    #: ingest._commission_base.
    commission_currency: str | None
    taxes: float | None
    realized_pnl: float | None
    mtm_pnl: float | None

    #: The source's own representation, stored verbatim in the `raw` column.
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalisedCash:
    """One cash transaction: a fee, a dividend, a withholding, an interest line.

    Identity is `(broker, transaction_id)`. The broker's own id, unique only
    within it -- see db._rekey_by_broker for why that took a migration.

    `kind` is the transaction type, and it is the field a second broker is most
    likely to spell differently. `analysis.py` branches on it by substring
    (`"FEES" in kind`), which is why the source is responsible for handing over a
    stable string rather than an enum member whose repr leaks a class name.
    """

    transaction_id: str
    account_id: str
    kind: str | None
    date_time: str | None
    settle_date: str | None
    description: str | None
    symbol: str | None
    contract_id: str | None

    amount: float
    currency: str | None
    fx_rate_to_base: float

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalisedPosition:
    """One contract held, as of one statement date.

    Identity is `(broker, as_of, contract_id)` -- an observation, not an event, so a
    re-fetch of the same day CORRECTS the row rather than adding one.

    Load-bearing beyond the position list: `cost_basis` is the only trace of a
    position opened before the earliest archived statement, and `history` resolves
    open-versus-closed against the newest snapshot per broker. A source that
    returns nothing here does not merely leave a table empty; it makes every one
    of that broker's episodes look closed.
    """

    contract_id: str
    account_id: str
    #: The statement date this observation belongs to (IBKR's `reportDate`).
    as_of: str | None
    symbol: str | None
    asset_category: str | None

    underlying_symbol: str | None
    put_call: str | None
    strike: float | None
    expiry: str | None
    multiplier: float | None

    quantity: int | float | None
    mark_price: float | None
    position_value: float | None
    cost_basis: float | None
    cost_basis_price: float | None
    unrealized_pnl: float | None
    side: str | None
    opened_at: str | None

    currency: str | None
    fx_rate_to_base: float

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalisedSecurity:
    """One contract DEFINITION: what an id means, independent of any holding.

    Identity is `(broker, contract_id)`. A definition rather than an observation,
    so it upserts: the same contract restated by a later statement refreshes the
    row.

    Why it needs the broker in its key even though a contract is a contract: the
    id is the BROKER's numbering, not the exchange's. Two brokers can both call
    something 12345, and keyed on the id alone the second one's row silently
    overwrites the first's -- turning one broker's TSLA option into another's
    entirely different contract, with the journal reporting no error at all.
    """

    contract_id: str
    symbol: str | None
    description: str | None
    asset_category: str | None
    sub_category: str | None
    currency: str | None
    multiplier: float | None
    strike: float | None
    expiry: str | None
    put_call: str | None
    underlying_contract_id: str | None
    underlying_symbol: str | None
    isin: str | None
    listing_exchange: str | None

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalisedNav:
    """One day's account value: the figure a trade ledger cannot reconstruct.

    Identity is `(broker, as_of)`. Deriving cash from fills needs a starting
    balance no Activity statement carries, so this is reported data, not derived --
    and it is the denominator behind "gain as % of net liquidation".

    Amounts are already in the account's base currency (IBKR calls the section
    EquitySummaryInBase), so unlike every other shape here there is no rate: a
    source that reports NAV per currency must convert before handing it over,
    because the journal has no rate of its own to apply.

    `cash`, `stock` and `options` are optional because IBKR splits them into
    long/short pairs in some deployments and reports a single figure in others.
    `total` is not optional: a NAV row without a NAV is noise.
    """

    as_of: str
    account_id: str
    currency: str
    total: float
    cash: float | None = None
    stock: float | None = None
    options: float | None = None

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StatementMeta:
    """What one statement file says about itself: the provenance row.

    Not a section, and not data the journal reports on -- it is the audit trail
    that answers "where did this row come from". `statements.source_file` is a
    foreign key from every other table, so this must be written before them.

    The digest and the asset filter are the JOURNAL's facts, not the broker's, so
    they are not here: `ingest` hashes the bytes it read and knows which filter it
    was invoked with. A source reports only what the statement states about itself.
    """

    account_id: str | None
    from_date: str | None
    to_date: str | None
    #: When the broker generated the file, if it says. IBKR does; it is what makes
    #: "this statement is a day stale" answerable without refetching.
    generated_at: str | None
    base_currency: str
