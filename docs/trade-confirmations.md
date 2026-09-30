# Trade Confirmations: what is known, and what is not

Same-day fills. IBKR's Activity Statement is T+1, so a trade made this morning
reaches this journal tomorrow; a Trade Confirmation Flex Query returns it within
5 to 10 minutes of the fill, all day. That is the whole point of the feature.

**Status. Built and running**, against a real payload fetched 2026-09-24.
`confirms.py` parses it, `flex.fetch_confirms` downloads it, `ingest.ingest_confirms`
writes it, and the `confirm` job polls it every 25 minutes through market hours.
`trades.source_kind` plus `ingest.SOURCE_RANK` rank the two queries, so a
same-session confirm is superseded by the next day's settled statement and never
the reverse -- tested in both directions.

**How to configure one.** **Performance & Reports → Flex Queries**, then the `+`
in **Trade Confirmation Flex Query Templates**. In each section's pop-up, choose the
level of detail FIRST, then tick fields. The query id is behind the Info icon beside
the saved query. Set it in the page under Settings → Confirms Query ID, or export
`$OPTJOURNAL_CONFIRM_QUERY_ID`. Absent is a supported state: the poll stays idle and
only the Activity Statement is required for this journal to work.

**What reading a real payload changed.** Three of the inferences below were wrong,
and they are corrected in place: `proceeds` exists under that name (the third-party
parser suggested gross arrived only as `amount`, which is also present and signed
the other way), and `accountId`, `currency` and `assetCategory` use the same
camelCase attributes as the Activity statement rather than diverging from their
documented labels. Everything else inferred held.

The same token and the same two endpoints serve it — `/SendRequest` then
`/GetStatement`, `v=3` — so `flex.py` needs a second query id and nothing else at
the transport layer. Pacing is 1 request/second and 10/minute per token, and the
per-query cooldown in `flex._check_cooldown` is already keyed by query id, so the
two queries get independent budgets for free.

## Two schema blockers, both now resolved

Both were `NOT NULL` columns on `trades`, and the payload confirmed a confirm
carries neither. Both are now nullable (`db._RELAXED_TRADE_COLUMNS`, which triggers
the table rebuild, since SQLite cannot relax a constraint in place):

| column | why it blocks |
| --- | --- |
| `fx_rate_to_base` | Confirmed absent from a real payload: 82 attributes, no `fxRateToBase`. **Resolved by estimating, which is a departure this project otherwise forbids.** `confirms.base_rate` fetches a live rate (`USDEUR=X`, the same source the price bars use) and `trades.fx_rate_estimated` marks the row, so the page can say so and tomorrow's statement overwrites it with IBKR's own. `docs/design-notes.md` requires every reported figure to be broker-stated; the owner chose same-session P&L over that, and the condition of the exception is that it is visible rather than silent. Storing `1.0` unmarked was never an option: it would corrupt every non-base figure invisibly. |
| `transaction_id` | Confirmed absent. Nullable now, and left absent rather than manufactured -- an invented id is worse than none, because it looks like the broker's. Not an identity either way: that is `(broker, trade_id)`, and `cash_transactions` keys on its own id in its own table. |

## The element names

Confirmed against a real payload. IBKR still publishes no XSD:

    FlexQueryResponse type="TCF"
      FlexStatements
        FlexStatement
          TradeConfirms
            TradeConfirm    levelOfDetail="EXECUTION"
            Order           levelOfDetail="ORDER"
            SymbolSummary   levelOfDetail="SYMBOL_SUMMARY"
            AssetSummary

`<TradeConfirms>` holds FOUR row element types, selected by the level of detail
chosen per section, and the `<Order>` rows carry the *same attributes* as
`<TradeConfirm>` and differ only in `levelOfDetail` -- a real execution row has
**82** of them. So the adapter filters on `levelOfDetail` rather than on the
element name, or it would double-count every fill on a query with order-level
detail enabled. Pinned by a test.

## Attribute names diverge from the Activity Statement

This is the trap, and every row below is now READ OFF A PAYLOAD rather than
inferred from the documented labels:

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
| gross, also present | `proceeds` | `proceeds` (same name; `amount` is the same figure signed the other way) |
| account | `accountId` | `accountId` (the LABEL is `ClientAccountID`, the attribute is not) |
| currency | `currency` | `currency` (label `CurrencyPrimary`) |
| asset class | `assetCategory` | `assetCategory` (label `AssetClass`) |

`tradeID` is the same in both -- **verified**, not assumed: the confirm's
`1592002840` sits in the same monotonic space as the stored `1587365449` three
sessions earlier, and `execID` is byte-format-identical to the stored `ib_exec_id`
(`0002920a.6ab5328c.01.01` against `0002be84.6ab14e37.03.01`). So the primary key
catches every duplicate and the `trades_exec` unique index agrees with it rather
than fighting it.

Open/close comes through `code` (`O`, `C`, `P`, semicolon-delimited), not through
`openCloseIndicator` — that is documented for the Activity `Trade` and absent
from the confirm's field list. `orderTime` carries a date AND a time despite the
name. `dateTime` is the execution stamp; there is no separate `tradeTime`.

Dates arrive in IBKR's compact form (`20260924`, `20260924;101659`). They are
stored in the forms py_ibkr gives Activity rows (`2026-09-24`,
`2026-09-24 10:16:59`), parsed by the same py_ibkr functions, because every
reader (replay, bars, the month filters, the page) was written against those.
Rows written in the compact form before that was fixed are rewritten when the
journal is opened (`db._normalise_confirm_dates`).

## No realised P&L, which is the design already assumed

IBKR's Trade Confirmation configuration page lists every selectable field and
contains no P&L field at all — no realised P&L, no MTM, no cost basis, no close
price — where the Activity Flex Trades page documents all four explicitly. The
real payload has none of them either, which a test now asserts as an ABSENCE: if
IBKR ever starts sending them, it fails, and that is the right moment to revisit
the ranking.

That is documented negative evidence for the ranking already built: a confirm
knows the fill happened and little about what it netted, so the Activity
Statement outranks it. One caveat recorded honestly: a third-party parser's
confirm model *does* declare `fifoPnlRealized` and `mtmPnl`, which is either a
permissive superset or something that surfaces at the closed-lot level of detail
that IBKR documents and no sample shows. Unresolved, and it does not change the
ranking either way.

## Retention, and one file per day

Four previous calendar years plus the current one, the same as any saved Flex
query. A single request covers at most ~365 days.

The ARCHIVE keeps one file per day, `confirm-YYYYMMDD.xml`, overwritten by each
poll -- where a statement gets one file per fetch. The day is the payload's own
`toDate` rather than the poll's UTC date, so an evening poll in Europe files the
US session under that session's name, and each re-ingest refreshes the file's
`statements` row (period and `whenGenerated`) to match what the file now holds. The reason is in the payload:
`whenGenerated` changes on every request, so the bytes are never identical and the
content dedupe cannot collapse them. Polling every 25 minutes would otherwise
archive fifteen files and open fifteen `statements` rows for one day of fills.
Nothing is lost by overwriting: a confirm payload is cumulative for its period, so
the last poll of the day is a superset of every earlier one.

Every archive walker globs `activity-*.xml`, so confirms are invisible to the
statements inventory, `archive.newest_statement` and the ingest -- which is why the
prefix differs rather than the suffix.
