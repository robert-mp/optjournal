# Trade Confirmations: what is known, and what is not

Same-day fills. IBKR's Activity Statement is T+1, so a trade made this morning
reaches this journal tomorrow; a Trade Confirmation Flex Query returns it within
5 to 10 minutes of the fill, all day. That is the whole point of the feature.

**Status.** The storage layer is done and tested: `trades.source_kind` records
which query delivered a fill and `ingest.SOURCE_RANK` orders them, so a
same-session confirm may be superseded by the next day's settled statement and
never the reverse. Nothing fetches or parses a confirm yet.

**What it is waiting on.** A Trade Confirmation Flex Query has to be created in
IBKR Client Portal, and its query id supplied. Everything below the fetch depends
on reading a real payload: IBKR publishes the field LIST but not the XML
attribute names, so the adapter cannot be written faithfully from documentation
alone. Creating it (documented, July 2026): **Performance & Reports → Flex
Queries**, then the `+` in **Trade Confirmation Flex Query Templates**. In each
section's pop-up, choose the level of detail FIRST, then tick fields. The query
id is behind the Info icon beside the saved query.

The same token and the same two endpoints serve it — `/SendRequest` then
`/GetStatement`, `v=3` — so `flex.py` needs a second query id and nothing else at
the transport layer. Pacing is 1 request/second and 10/minute per token, and the
per-query cooldown in `flex._check_cooldown` is already keyed by query id, so the
two queries get independent budgets for free.

## Two schema blockers

Both are `NOT NULL` columns on `trades` that a confirm appears not to carry.
Neither can be resolved without a real payload, and neither should be guessed:

| column | why it blocks |
| --- | --- |
| `fx_rate_to_base` | Documented for the Activity `Trade` element, absent from the confirm's documented field list and from the sample payload found. A confirm therefore carries no base-currency conversion. Storing `1.0` would silently corrupt every non-base figure; the honest options are to relax the column or to refuse confirms for instruments not quoted in the base currency. |
| `transaction_id` | Not in IBKR's documented confirm field list and absent from the sample. Present in a third-party parser's model, which may be a permissive superset. |

## The element names

Inferred, not documented — IBKR publishes no XSD and no sample payload:

    FlexQueryResponse type="TCF"
      FlexStatements
        FlexStatement
          TradeConfirms
            TradeConfirm    levelOfDetail="EXECUTION"
            Order           levelOfDetail="ORDER"
            SymbolSummary   levelOfDetail="SYMBOL_SUMMARY"
            AssetSummary

`<TradeConfirms>` holds FOUR row element types, selected by the level of detail
chosen per section, and in the sample the `<Order>` rows carry the *same 78
attributes* as `<TradeConfirm>` and differ only in `levelOfDetail`. So the
adapter must filter on `levelOfDetail` rather than on the element name, or it
will double-count.

## Attribute names diverge from the Activity Statement

This is the trap, and IBKR documents both label sets, which is how it was found:

| concept | Activity `Trade` | Trade Confirm |
| --- | --- | --- |
| price | `tradePrice` | `price` |
| gross | `tradeMoney` | `amount` |
| commission | `ibCommission` | `commission` |
| commission currency | `ibCommissionCurrency` | `commissionCurrency` |
| tax | `taxes` | `tax` |
| codes | `notes` | `code` |
| settle date | `settleDateTarget` | `settleDate` |
| execution id | `ibExecID` | `execID` |
| order id | `ibOrderID` | `orderID` |
| account | `accountId` | label `ClientAccountID` |
| currency | `currency` | label `CurrencyPrimary` |
| asset class | `assetCategory` | label `AssetClass` |

The labels are documented; the camelCase attribute names are inferred from a
third-party parser and one sample payload. `tradeID` is the same in both, which
is what makes the existing primary key work unchanged.

Open/close comes through `code` (`O`, `C`, `P`, semicolon-delimited), not through
`openCloseIndicator` — that is documented for the Activity `Trade` and absent
from the confirm's field list. `orderTime` carries a date AND a time despite the
name. `dateTime` is the execution stamp; there is no separate `tradeTime`.

## No realised P&L, which is the design already assumed

IBKR's Trade Confirmation configuration page lists every selectable field and
contains no P&L field at all — no realised P&L, no MTM, no cost basis, no close
price — where the Activity Flex Trades page documents all four explicitly. The
sample payload has none of them either.

That is documented negative evidence for the ranking already built: a confirm
knows the fill happened and little about what it netted, so the Activity
Statement outranks it. One caveat recorded honestly: a third-party parser's
confirm model *does* declare `fifoPnlRealized` and `mtmPnl`, which is either a
permissive superset or something that surfaces at the closed-lot level of detail
that IBKR documents and no sample shows. Unresolved, and it does not change the
ranking either way.

## Retention

Four previous calendar years plus the current one, the same as any saved Flex
query. A single request covers at most ~365 days.
