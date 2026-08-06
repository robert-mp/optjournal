"""The broker-neutral shape of one executed fill.

This is the seam between a broker's statement and the journal's database. A
`StatementSource` (see `sources.py`) turns whatever a broker sends -- IBKR's Flex
XML with its camelCase attributes today, another broker's JSON tomorrow -- into a
stream of these, and `ingest.py` writes them without knowing which broker they
came from.

Named in trading terms rather than IBKR's: `exec_id` not `ibExecID`,
`realized_pnl` not `fifoPnlRealized`. The point of the type is that a second
broker fills these fields from its own vocabulary, so the fields cannot carry the
first broker's.

A leaf, like `money.py`: it imports nothing internal, so both the sources that
produce it and the ingest that consumes it can hold one without a dependency
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
    conid: str | None
    underlying_symbol: str | None
    underlying_conid: str | None

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
