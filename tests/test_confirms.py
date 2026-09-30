"""Trade Confirmations: the parse, the estimate, and the supersede.

The payload these are written against is real. IBKR publishes no XSD and no
sample for a `TCF` query, and `docs/trade-confirmations.md` had inferred the
attribute names from a third-party parser -- three of which were WRONG. So the
fixture here is a redacted copy of a payload this journal actually fetched, and
every name asserted below was read off it rather than reasoned about.

The most important test in the file is the supersede: the same fill arrives twice,
once same-session and once settled, and the settled one has to win without being
announced as new. Getting that backwards would mean a journal whose realised P&L
silently walks back to what the fill looked like mid-session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import connect_migrated

from optjournal.confirms import (
    CONFIRM_QUERY_TYPE,
    ConfirmParseError,
    base_rate,
    parse_confirms,
    split_codes,
    statement_meta,
)
from optjournal.ingest import ingest_confirms, ingest_file

#: One execution, with the account number and name redacted. Byte-shaped like the
#: real thing: 82 attributes, `levelOfDetail="EXECUTION"`, and no `fxRateToBase`,
#: `transactionID` or P&L field anywhere -- the three absences the design turns on.
CONFIRM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Confirms" type="TCF">
<FlexStatements count="1">
<FlexStatement accountId="U0000000" fromDate="20260924" toDate="20260924"
 period="Today" whenGenerated="20260924;111200">
<TradeConfirms>
<TradeConfirm accountId="U0000000" acctAlias="" currency="USD" assetCategory="OPT"
 symbol="GOOG  261030P00310000" description="GOOG 30OCT26 310 P" conid="922397461"
 underlyingConid="208813720" underlyingSymbol="GOOG" putCall="P" strike="310"
 expiry="20261030" multiplier="100" tradeID="1592002840"
 execID="0002920a.6ab5328c.01.01" orderID="1290844394" reportDate="20260924"
 tradeDate="20260924" dateTime="20260924;101659" orderTime="20260924;100458"
 settleDate="20260925" exchange="PHLX" buySell="SELL" quantity="-2" price="4.9"
 amount="-980" proceeds="980" netCash="978.606632" commission="-1.393368"
 commissionCurrency="USD" tax="0" code="O" levelOfDetail="EXECUTION"
 orderType="LMT" isAPIOrder="N" traderID="C1" transactionType="ExchTrade"/>
</TradeConfirms>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>
"""

#: The same execution as the Activity Statement sends it. The attribute names are
#: the STATEMENT's, which is the whole point: `tradePrice` not `price`,
#: `ibCommission` not `commission`, and it carries the three things a confirm
#: cannot -- an FX rate, a transaction id and a realised P&L.
ACTIVITY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Activity" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="U0000000" fromDate="20260924" toDate="20260925"
 period="LastMonth" whenGenerated="20260925;050000">
<AccountInformation accountId="U0000000" currency="EUR"/>
<Trades>
<Trade accountId="U0000000" currency="USD" fxRateToBase="0.87" assetCategory="OPT"
 symbol="GOOG  261030P00310000" conid="922397461" underlyingConid="208813720"
 underlyingSymbol="GOOG" putCall="P" strike="310" expiry="20261030"
 multiplier="100" tradeID="1592002840" ibExecID="0002920a.6ab5328c.01.01"
 transactionID="6711999999" ibOrderID="1290844394" reportDate="20260925"
 tradeDate="20260924" dateTime="20260924;101659" settleDateTarget="20260925"
 exchange="PHLX" buySell="SELL" quantity="-2" tradePrice="4.9" tradeMoney="-980"
 proceeds="980" ibCommission="-1.5" ibCommissionCurrency="USD" taxes="0"
 openCloseIndicator="O" notes="" levelOfDetail="EXECUTION" fifoPnlRealized="0"
 mtmPnl="0"/>
</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>
"""


@pytest.fixture()
def confirm_file(tmp_path) -> Path:
    path = tmp_path / "confirm-20260924.xml"
    path.write_text(CONFIRM_XML, encoding="utf-8")
    return path


def _fixed_rate(rate: float = 0.8795):
    """A rate resolver that makes no network call. (rate, estimated)."""
    return lambda ccy: (1.0, False) if ccy == "EUR" else (rate, True)


def test_the_parse_reads_the_confirms_own_attribute_names(confirm_file):
    """Eight names diverge from the Activity Statement, and every one is asserted.

    This is the test the docs could not be written from: `price`, `commission`,
    `tax`, `code`, `execID` and `orderID` all differ from the statement's spelling,
    and a parser built on the statement's names would produce a row of nulls that
    ingests cleanly and reports a fill worth nothing.
    """
    pairs = parse_confirms(confirm_file, rate_for=lambda c: _fixed_rate()(c)[0])
    assert len(pairs) == 1
    _account, fill = pairs[0]
    assert fill.trade_id == "1592002840"
    assert fill.exec_id == "0002920a.6ab5328c.01.01"      # execID, not ibExecID
    assert fill.trade_price == 4.9                        # price, not tradePrice
    assert fill.commission == -1.393368                   # commission
    assert fill.taxes == 0.0                              # tax, not taxes
    assert fill.order_id == "1290844394"                  # orderID
    assert fill.proceeds == 980.0
    assert fill.quantity == -2
    assert fill.buy_sell == "SELL"
    assert fill.currency == "USD"
    assert fill.level_of_detail == "EXECUTION"


def test_the_three_absences_the_design_is_built_on(confirm_file):
    """A confirm carries no transaction id, no FX rate and no P&L.

    Asserted as ABSENCES because each one drove a decision: the nullable column,
    the estimated rate, and the ranking that lets the statement win. If IBKR ever
    starts sending them this fails, which is the right time to revisit all three.
    """
    _account, fill = parse_confirms(
        confirm_file, rate_for=lambda c: _fixed_rate()(c)[0])[0]
    assert fill.transaction_id is None
    assert fill.realized_pnl is None
    assert fill.mtm_pnl is None
    assert "fxRateToBase" not in fill.raw, (
        "the payload now carries an FX rate, so the live-rate estimate is no longer "
        "necessary -- see confirms.base_rate"
    )


def test_open_close_comes_out_of_the_code_field(confirm_file):
    """A confirm has no `openCloseIndicator`; the letter is inside `code`.

    Split into the same two columns the Activity Statement fills, because that is
    what the rest of the journal reads: `history.py` tests `open_close`, and a row
    with 'O' sitting in `notes` instead would read as neither open nor close.
    """
    _account, fill = parse_confirms(
        confirm_file, rate_for=lambda c: _fixed_rate()(c)[0])[0]
    assert fill.open_close == "O"
    assert fill.notes is None, "the open marker was left in the notes as well"

    # And the mixed case, which is what a real close with a code looks like.
    assert split_codes("C;P") == ("C", "P")
    # A fill through zero carries both markers, which the Activity Statement
    # stores as "C;O"; keeping only the first made it read as a plain close.
    assert split_codes("C;O;P") == ("C;O", "P")
    assert split_codes("IPO;M") == (None, "IPO;M")
    assert split_codes(None) == (None, None)


def test_only_execution_rows_are_read(tmp_path):
    """`<Order>` rows carry the SAME attributes and differ only in the level.

    So filtering on the element name would double-count every fill on a query with
    order-level detail enabled -- one fill reported as two, at twice the proceeds.
    """
    doubled = CONFIRM_XML.replace(
        "</TradeConfirms>",
        '<Order accountId="U0000000" tradeID="1592002840" quantity="-2"'
        ' price="4.9" proceeds="980" levelOfDetail="ORDER" currency="USD"/>'
        "</TradeConfirms>",
    )
    path = tmp_path / "confirm-doubled.xml"
    path.write_text(doubled, encoding="utf-8")
    pairs = parse_confirms(path, rate_for=lambda c: _fixed_rate()(c)[0])
    assert len(pairs) == 1, "an ORDER-level row was read as a second execution"
    assert pairs[0][1].level_of_detail == "EXECUTION"


def test_an_activity_statement_pointed_at_this_reader_is_refused(tmp_path):
    """Pointing the confirm query id at the wrong saved query is an easy mistake.

    Refused on the root's `type`, because the alternative is silent: an Activity
    payload has no `<TradeConfirm>` rows, so it would parse to zero fills and report
    a healthy, empty run forever.
    """
    path = tmp_path / "confirm-wrong.xml"
    path.write_text(ACTIVITY_XML, encoding="utf-8")
    with pytest.raises(ConfirmParseError, match="not 'TCF'"):
        parse_confirms(path, rate_for=lambda c: (1.0))
    assert CONFIRM_QUERY_TYPE == "TCF"


def test_the_statement_metadata_takes_the_base_currency_from_the_caller(confirm_file):
    """A confirm has no AccountInformation section, so it cannot state the base.

    Passed in rather than defaulted, because defaulting to EUR on an account that
    reports in USD would convert every figure by a rate it does not need.
    """
    meta = statement_meta(confirm_file, base_currency="EUR")
    assert len(meta) == 1
    assert meta[0].from_date == "2026-09-24"
    assert meta[0].base_currency == "EUR"
    assert meta[0].generated_at == "2026-09-24 11:12:00"


def test_a_base_currency_fill_is_not_an_estimate(monkeypatch):
    """No conversion applies, so the rate is exactly 1.0 and nothing is modelled.

    This is why `fx_rate_estimated` is its own column rather than read off
    `source_kind`: a same-session EUR fill on a EUR account is provisional in its
    P&L and exact in its money, and one flag cannot say both.
    """
    from optjournal import marketdata

    def forbidden(*args, **kwargs):
        raise AssertionError("fetched a rate for the base currency")

    monkeypatch.setattr(marketdata, "fetch_quote", forbidden)
    assert base_rate("EUR", "EUR") == (1.0, False)
    assert base_rate(None, "EUR") == (1.0, False)


def test_a_rate_that_cannot_be_fetched_fails_open_and_says_so(monkeypatch):
    """The fill matters more than the conversion.

    Every native figure on the row is broker-stated and correct; only the base
    conversion is unavailable. Refusing the fill would lose real data over a
    derived number, so this stores 1.0 and marks the row estimated -- which the
    page renders as a warning and tomorrow's statement corrects.
    """
    from optjournal import marketdata

    def down(*args, **kwargs):
        raise marketdata.BarFetchError("EURUSD=X quote: HTTP Error 502")

    monkeypatch.setattr(marketdata, "fetch_quote", down)
    rate, estimated = base_rate("USD", "EUR")
    assert (rate, estimated) == (1.0, True), (
        "a failed rate fetch must still mark the row, or the page would present an "
        "unconverted figure as a converted one"
    )


def test_ingesting_a_confirm_stores_it_as_provisional(tmp_path, confirm_file):
    """End to end into a real journal: native figures exact, base figures marked."""
    conn = connect_migrated(tmp_path / "j.db")
    result = ingest_confirms(conn, confirm_file, base_currency="EUR",
                             rate_for=_fixed_rate())
    assert result.trades_inserted == 1

    row = conn.execute("SELECT * FROM trades").fetchone()
    assert row["source_kind"] == "confirm"
    assert row["fx_rate_estimated"] == 1
    assert row["transaction_id"] is None
    # Broker-stated, and therefore exact.
    assert row["trade_price"] == 4.9
    assert row["proceeds"] == 980.0
    # Ours, and therefore approximate -- but arithmetically consistent with the
    # rate stored beside it, which is what makes the estimate auditable.
    assert row["proceeds_base"] == pytest.approx(980.0 * 0.8795)
    assert row["fifo_pnl_realized"] is None, (
        "a confirm reported a realised P&L, which IBKR does not send"
    )


def test_re_polling_the_same_session_is_free(tmp_path, confirm_file):
    """The poll runs every half hour, so re-reading this morning must cost nothing.

    Not deduplicated by DIGEST, deliberately: `whenGenerated` changes on every
    request, so the bytes are never identical and a digest check would re-ingest
    every fill every poll. The rank check is what makes it idempotent.
    """
    conn = connect_migrated(tmp_path / "j.db")
    first = ingest_confirms(conn, confirm_file, base_currency="EUR",
                            rate_for=_fixed_rate())
    second = ingest_confirms(conn, confirm_file, base_currency="EUR",
                             rate_for=_fixed_rate(0.90))
    assert (first.trades_inserted, second.trades_inserted) == (1, 0)
    assert second.trades_skipped_existing == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 1
    # And the second poll's different rate did NOT churn the stored row: a fill has
    # one conversion, and re-rating it every half hour would make the day's P&L
    # drift with the FX market rather than with the trading.
    assert conn.execute(
        "SELECT fx_rate_to_base AS r FROM trades").fetchone()["r"] == 0.8795


def test_the_statement_supersedes_the_confirm_without_announcing_it(
    tmp_path, confirm_file,
):
    """THE TEST THE WHOLE RANKING EXISTS FOR.

    The same execution, same `tradeID`, arriving twice: same-session first, settled
    the next morning. The statement has to win -- it carries the realised P&L, the
    settled commission and IBKR's own FX rate -- and it must NOT re-announce the
    fill as new, because the morning a reader stops needing to be told about a
    trade is the morning it settles.
    """
    conn = connect_migrated(tmp_path / "j.db")
    ingest_confirms(conn, confirm_file, base_currency="EUR", rate_for=_fixed_rate())
    seen_first = conn.execute("SELECT first_seen_at AS f FROM trades").fetchone()["f"]

    activity = tmp_path / "activity-20260925T050000Z.xml"
    activity.write_text(ACTIVITY_XML, encoding="utf-8")
    result = ingest_file(conn, activity)

    assert result.trades_superseded == 1, "the statement did not supersede"
    assert result.trades_inserted == 0, (
        "a settled fill was announced as NEW, so the reader is told twice about one "
        "trade -- once when it filled and once when it settled"
    )
    row = conn.execute("SELECT * FROM trades").fetchone()
    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 1
    assert row["source_kind"] == "activity"
    # The broker's rate replaces ours, and the row stops claiming an estimate.
    assert (row["fx_rate_to_base"], row["fx_rate_estimated"]) == (0.87, 0)
    assert row["transaction_id"] == "6711999999", "the settled id did not land"
    assert row["ib_commission"] == -1.5, "the settled commission did not land"
    assert row["fifo_pnl_realized"] == 0.0, "realised P&L is still unknown"
    assert row["first_seen_at"] == seen_first, (
        "a supersede re-dated first_seen_at, so every trade confirmed yesterday "
        "reads as new the morning its statement lands"
    )


def test_a_confirm_cannot_walk_a_settled_row_back(tmp_path, confirm_file):
    """The other direction, which is the one that would corrupt the journal.

    A confirm query re-run after the statement has landed -- an ordinary thing, the
    poll does not know what the sync did -- must not replace settled figures with
    what the fill looked like mid-session.
    """
    conn = connect_migrated(tmp_path / "j.db")
    activity = tmp_path / "activity-20260925T050000Z.xml"
    activity.write_text(ACTIVITY_XML, encoding="utf-8")
    ingest_file(conn, activity)

    result = ingest_confirms(conn, confirm_file, base_currency="EUR",
                             rate_for=_fixed_rate())
    assert result.trades_skipped_existing == 1
    assert result.trades_superseded == 0
    row = conn.execute("SELECT * FROM trades").fetchone()
    assert (row["source_kind"], row["fx_rate_estimated"]) == ("activity", 0)
    assert row["ib_commission"] == -1.5, "a confirm overwrote the settled commission"


def test_a_confirm_that_fails_part_way_leaves_nothing_behind(tmp_path):
    """H1, for the confirm writer: the job commits on the same connection.

    A fill with no symbol violates `trades.symbol NOT NULL` after the provenance
    row is written, so without the savepoint that row survived the caller's
    commit and claimed a file whose fills never landed.
    """
    conn = connect_migrated(tmp_path / "j.db")
    path = tmp_path / "confirm-20260924.xml"
    path.write_text(CONFIRM_XML.replace('symbol="GOOG  261030P00310000"',
                                        'symbol=""'), encoding="utf-8")
    with pytest.raises(Exception, match="NOT NULL"):
        ingest_confirms(conn, path, base_currency="EUR", rate_for=_fixed_rate())
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0

    path.write_text(CONFIRM_XML, encoding="utf-8")
    result = ingest_confirms(conn, path, base_currency="EUR", rate_for=_fixed_rate())
    assert result.trades_inserted == 1


def test_a_confirm_stores_its_dates_in_the_forms_the_statement_does(
    tmp_path, confirm_file,
):
    """M3: every reader of these columns was written against the statement's forms.

    py_ibkr turns the statement's `20260924;101659` into `2026-09-24 10:16:59`, and
    a confirm stored IBKR's compact text as it came. So replay's `epoch_et` read a
    same-session fill as no time at all, a month filter's `LIKE '2026-09%'` missed
    it, and the page showed the raw stamp. Compared against the activity row for
    the SAME execution, so the two forms cannot drift apart again.
    """
    from optjournal.clock import epoch_et

    columns = "trade_date, date_time, expiry"
    confirm_db = connect_migrated(tmp_path / "c.db")
    ingest_confirms(confirm_db, confirm_file, base_currency="EUR",
                    rate_for=_fixed_rate())
    from_confirm = tuple(confirm_db.execute(f"SELECT {columns} FROM trades").fetchone())

    activity = tmp_path / "activity-20260925T050000Z.xml"
    activity.write_text(ACTIVITY_XML, encoding="utf-8")
    activity_db = connect_migrated(tmp_path / "a.db")
    ingest_file(activity_db, activity)
    from_activity = tuple(
        activity_db.execute(f"SELECT {columns} FROM trades").fetchone())

    assert from_confirm == from_activity == (
        "2026-09-24", "2026-09-24 10:16:59", "2026-10-30")
    assert epoch_et(from_confirm[1]) is not None

    stored = confirm_db.execute(
        "SELECT from_date, to_date, when_generated FROM statements").fetchone()
    assert tuple(stored) == ("2026-09-24", "2026-09-24", "2026-09-24 11:12:00")
    statement = activity_db.execute(
        "SELECT from_date, when_generated FROM statements").fetchone()
    assert tuple(statement) == ("2026-09-24", "2026-09-25 05:00:00"), (
        "the statement's own forms moved; the confirm must follow them"
    )


def test_a_date_the_parser_cannot_read_is_kept_as_sent(tmp_path):
    """Normalising must not lose a value: an unreadable one stays as IBKR wrote it."""
    path = tmp_path / "confirm-odd.xml"
    path.write_text(CONFIRM_XML.replace('expiry="20261030"', 'expiry="2026-10"'),
                    encoding="utf-8")
    _account, fill = parse_confirms(path, rate_for=lambda c: 1.0)[0]
    assert fill.expiry == "2026-10"
    assert fill.trade_date == "2026-09-24"


def test_a_confirm_archive_is_named_for_the_session_it_holds(tmp_path, monkeypatch):
    """L5: named by the poll's UTC date, an evening poll filed Monday under Tuesday.

    The live archive held `confirm-20260929.xml` whose payload covered 2026-09-28.
    The name now comes from the payload's own `toDate`, so a file holds what its
    name says and the next day's first poll cannot overwrite it.
    """
    from optjournal import flex

    payload = CONFIRM_XML.replace('fromDate="20260924" toDate="20260924"',
                                  'fromDate="20260928" toDate="20260928"').encode()
    monkeypatch.setattr(flex, "read_token", lambda account=None: "tok")
    monkeypatch.setattr(flex, "_client_factory", lambda **kw: type(
        "C", (), {"download": lambda self, *a, **k: payload})())
    result = flex.fetch_confirms("1621016", archive_dir=tmp_path, force=True)
    assert result.raw_path.name == "confirm-20260928.xml"
    assert result.raw_path.read_bytes() == payload


def test_re_ingesting_a_grown_confirm_refreshes_its_statement_row(tmp_path):
    """L5: the upsert kept the FIRST poll's period and stamp forever.

    Each poll overwrites the day's file with a payload that is later and may cover
    more, so the provenance row has to follow the file it describes.
    """
    conn = connect_migrated(tmp_path / "j.db")
    path = tmp_path / "confirm-20260924.xml"
    path.write_text(CONFIRM_XML, encoding="utf-8")
    ingest_confirms(conn, path, base_currency="EUR", rate_for=_fixed_rate())

    path.write_text(CONFIRM_XML.replace(
        'toDate="20260924"', 'toDate="20260925"').replace(
        'whenGenerated="20260924;111200"', 'whenGenerated="20260925;153000"'),
        encoding="utf-8")
    ingest_confirms(conn, path, base_currency="EUR", rate_for=_fixed_rate())
    row = conn.execute(
        "SELECT from_date, to_date, when_generated FROM statements").fetchone()
    assert tuple(row) == ("2026-09-24", "2026-09-25", "2026-09-25 15:30:00")
