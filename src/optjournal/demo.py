"""A synthetic Flex statement, for exercising paths the real account cannot.

The real account holds two option fills in twelve months, both the same order,
with nothing closed. That leaves most of this project unexercised by real data:
no round trip, so Net P&L, Win Rate, Avg Win and Avg Loss are permanently
empty; no `leg_count > 1`, so the strategy grouping the `option_orders` view
exists for has never had a spread to group; no expiry, assignment or roll, so
`disposition_of` is only tested against fixtures; one month of activity, so the
Calendar shows a single day and the Annual tab stays disabled. Closed-position
P&L had to be validated against a *stock* round trip for want of an options one.

So this module emits a statement with all of that in it. It is generated rather
than checked in as a file so the arithmetic is derived, not transcribed: cost,
netCash, tradeMoney and the base-currency conversions are computed from
quantity, price and rate exactly as IBKR computes them, which is what makes it
usable as an oracle instead of merely as something that parses.

Deliberately obvious as a fake: account `U0000000`, query name
`optjournal-demo`, and underlyings the real account has never traded. Nothing
here may be written into `raw/` -- see `assert_not_real` -- because that archive
is the provenance root for every report and costs IBKR requests to rebuild.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

#: Marks the output unmistakably. `statements` and the UI show the query name.
QUERY_NAME = "optjournal-demo"
DEMO_ACCOUNT = "U0000000"
BASE_CURRENCY = "EUR"

#: Fourteen months, spanning two calendar years so the Annual view has
#: something to compare. Ends mid-period so positions are still open.
FROM_DATE = date(2025, 1, 2)
TO_DATE = date(2026, 2, 27)

#: One rate per calendar month, drifting. A constant rate would hide exactly
#: the class of bug that shipped twice this project: reading a native amount as
#: though it were already base currency.
_FX = {
    (2025, 1): "0.9682", (2025, 2): "0.9601", (2025, 3): "0.9245",
    (2025, 4): "0.8811", (2025, 5): "0.8842", (2025, 6): "0.8776",
    (2025, 7): "0.8534", (2025, 8): "0.8607", (2025, 9): "0.8519",
    (2025, 10): "0.8622", (2025, 11): "0.8648", (2025, 12): "0.8503",
    (2026, 1): "0.8721", (2026, 2): "0.8790",
}

MULTIPLIER = Decimal("100")
#: IBKR US options, tiered: USD 0.65 per contract, USD 1.00 minimum per order.
PER_CONTRACT = Decimal("0.65")
ORDER_MINIMUM = Decimal("1.00")


def fx_for(day: date) -> Decimal:
    return Decimal(_FX[(day.year, day.month)])


def _occ(underlying: str, expiry: date, put_call: str, strike: Decimal) -> str:
    """An OCC-21 symbol, the format IBKR reports for options."""
    root = f"{underlying:<6}"
    cents = int(strike * 1000)
    return f"{root}{expiry:%y%m%d}{put_call}{cents:08d}"


@dataclass(slots=True)
class Leg:
    """One contract's worth of a single order."""

    underlying: str
    expiry: date
    put_call: str
    strike: Decimal
    quantity: int          #: signed; negative is short
    price: Decimal
    open_close: str        #: "O" or "C"
    realized: Decimal = Decimal("0")
    notes: str = ""
    #: Split this leg across several fills. Exercises the per-order commission
    #: minimum being charged once and adjusted across fills, which is the case
    #: that produced a commission *credit* in the real data.
    fills: tuple[int, ...] = ()


@dataclass(slots=True)
class Order:
    """One `ibOrderID`. More than one leg is what makes a spread."""

    day: date
    legs: list[Leg]
    label: str = ""
    time: str = "143005"


@dataclass(slots=True)
class Position:
    """A row of the closing OpenPositions snapshot."""

    underlying: str
    expiry: date
    put_call: str
    strike: Decimal
    quantity: int
    mark: Decimal
    cost_basis: Decimal
    #: True for a contract with no opening fill anywhere in the period, the
    #: case that forced `position_snapshots` to be an independent source.
    snapshot_only: bool = False


@dataclass(slots=True)
class Cash:
    day: date
    type: str
    description: str
    amount: Decimal
    currency: str = "USD"
    symbol: str = ""
    notes: str = ""


def commission_for(quantity: int, fills: tuple[int, ...] = ()) -> list[Decimal]:
    """Per-fill commission for one leg, honouring the per-order minimum.

    Modelled on what the real statement does: each fill is charged
    provisionally as though it were its own order -- so the minimum binds on
    each -- and the final fill is trued up to the real order total. When the
    provisional charges over-collect, that true-up is a *credit*: a positive
    `ibCommission`. Verified against order 1096738670 in the real archive,
    whose fills were -0.3481 and +0.0088 USD for a -0.3393 order.

    That credit is the whole reason commission must be summed signed rather
    than per-fill absolute, so the demo data has to contain one.
    """
    total = max(abs(Decimal(quantity)) * PER_CONTRACT, ORDER_MINIMUM)
    if not fills:
        return [-total]
    provisional = [
        max(abs(Decimal(q)) * PER_CONTRACT, ORDER_MINIMUM) for q in fills[:-1]
    ]
    return [-p for p in provisional] + [-(total - sum(provisional))]


def _script() -> tuple[list[Order], list[Position], list[Cash]]:
    """The scripted year. Each entry exists to reach a specific code path."""
    orders: list[Order] = []

    def d(y: int, m: int, day: int) -> date:
        return date(y, m, day)

    # 1. A winning round trip: short put sold for 6.40, bought back at 1.85.
    exp = d(2025, 2, 21)
    orders.append(Order(d(2025, 1, 14), label="win: short put", legs=[
        Leg("NVDA", exp, "P", Decimal("120"), -2, Decimal("6.40"), "O"),
    ]))
    orders.append(Order(d(2025, 2, 10), label="win: closed", legs=[
        Leg("NVDA", exp, "P", Decimal("120"), 2, Decimal("1.85"), "C",
            realized=Decimal("907.40")),
    ]))

    # 2. A losing round trip, so Avg Loss and Win Rate have both sides.
    exp = d(2025, 4, 17)
    orders.append(Order(d(2025, 3, 5), label="loss: long call", legs=[
        Leg("SPY", exp, "C", Decimal("580"), 1, Decimal("9.10"), "O"),
    ]))
    orders.append(Order(d(2025, 4, 14), label="loss: closed", legs=[
        Leg("SPY", exp, "C", Decimal("580"), -1, Decimal("2.05"), "C",
            realized=Decimal("-706.30")),
    ]))

    # 3. Expiry. Closing quantity at zero price, note Ep -> disposition EXPIRED.
    exp = d(2025, 6, 20)
    orders.append(Order(d(2025, 5, 12), label="expiry: opened", legs=[
        Leg("NVDA", exp, "P", Decimal("105"), -3, Decimal("2.10"), "O"),
    ]))
    orders.append(Order(d(2025, 6, 20), label="expiry: worthless", time="160000",
                        legs=[
        Leg("NVDA", exp, "P", Decimal("105"), 3, Decimal("0"), "C",
            realized=Decimal("628.05"), notes="Ep"),
    ]))

    # 4. Assignment, note A -> disposition ASSIGNED.
    exp = d(2025, 8, 15)
    orders.append(Order(d(2025, 7, 9), label="assignment: opened", legs=[
        Leg("NVDA", exp, "P", Decimal("112"), -1, Decimal("3.35"), "O"),
    ]))
    orders.append(Order(d(2025, 8, 15), label="assignment", time="160000", legs=[
        Leg("NVDA", exp, "P", Decimal("112"), 1, Decimal("0"), "C",
            realized=Decimal("333.35"), notes="A"),
    ]))

    # 5. A vertical spread: two legs, one order. leg_count > 1 is the whole
    #    reason the option_orders view is separate from option_legs.
    exp = d(2025, 10, 17)
    orders.append(Order(d(2025, 9, 8), label="spread: put credit vertical", legs=[
        Leg("NVDA", exp, "P", Decimal("170"), -4, Decimal("4.55"), "O"),
        Leg("NVDA", exp, "P", Decimal("160"), 4, Decimal("2.10"), "O"),
    ]))
    orders.append(Order(d(2025, 10, 13), label="spread: closed", legs=[
        Leg("NVDA", exp, "P", Decimal("170"), 4, Decimal("1.20"), "C",
            realized=Decimal("1334.80")),
        Leg("NVDA", exp, "P", Decimal("160"), -4, Decimal("0.40"), "C",
            realized=Decimal("-682.60")),
    ]))

    # 6. A roll: close the near expiry and open the next in one order.
    near, far = d(2025, 11, 21), d(2025, 12, 19)
    orders.append(Order(d(2025, 10, 20), label="roll: original", legs=[
        Leg("SPY", near, "P", Decimal("560"), -2, Decimal("5.80"), "O"),
    ]))
    orders.append(Order(d(2025, 11, 17), label="roll: out to December", legs=[
        Leg("SPY", near, "P", Decimal("560"), 2, Decimal("2.40"), "C",
            realized=Decimal("677.40")),
        Leg("SPY", far, "P", Decimal("555"), -2, Decimal("6.15"), "O"),
    ]))
    orders.append(Order(d(2025, 12, 19), label="roll: expired worthless",
                        time="160000", legs=[
        Leg("SPY", far, "P", Decimal("555"), 2, Decimal("0"), "C",
            realized=Decimal("1228.70"), notes="Ep"),
    ]))

    # 7. A 0DTE round trip: opened and closed on expiry day.
    same = d(2026, 1, 16)
    orders.append(Order(same, label="0DTE: opened", time="100200", legs=[
        Leg("SPY", same, "C", Decimal("600"), -3, Decimal("1.95"), "O"),
    ]))
    orders.append(Order(same, label="0DTE: closed", time="153000", legs=[
        Leg("SPY", same, "C", Decimal("600"), 3, Decimal("0.35"), "C",
            realized=Decimal("476.10")),
    ]))

    # 8. A multi-fill order, which is what produces a commission credit.
    exp = d(2026, 3, 20)
    orders.append(Order(d(2026, 1, 27), label="multi-fill, credited commission",
                        legs=[
        Leg("NVDA", exp, "P", Decimal("140"), -5, Decimal("5.05"), "O",
            fills=(-1, -1, -1, -1, -1)),
    ]))

    # 9. Still open at period end. The second has no opening fill in the
    #    period at all, so its basis exists only in the snapshot.
    positions = [
        Position("NVDA", exp, "P", Decimal("140"), -5, Decimal("3.80"),
                 Decimal("-2521.75")),
        Position("SPY", d(2026, 6, 18), "C", Decimal("640"), 2, Decimal("11.40"),
                 Decimal("3980.00"), snapshot_only=True),
    ]

    cash: list[Cash] = []
    day = FROM_DATE.replace(day=5)
    while day <= TO_DATE:
        cash.append(Cash(day, "Other Fees",
                         f"OPRA NP L1 FOR {day:%b %Y}".upper(), Decimal("-1.50")))
        day = (day.replace(day=28) + timedelta(days=8)).replace(day=5)
    cash += [
        Cash(d(2025, 6, 12), "Dividends", "NVDA CASH DIVIDEND USD 0.01 PER SHARE",
             Decimal("4.00"), symbol="NVDA"),
        Cash(d(2025, 6, 12), "Withholding Tax", "NVDA CASH DIVIDEND - US TAX",
             Decimal("-0.60"), symbol="NVDA"),
        Cash(d(2025, 3, 6), "Other Fees", "EUR CUSTODY FEE FOR FEB 2026",
             Decimal("-0.02"), currency="EUR"),
    ]
    return orders, positions, cash


# --------------------------------------------------------------------- rendering

#: Every attribute IBKR emits on a Trade that anything downstream reads. Copied
#: from a real element so the synthetic one parses through the same models
#: rather than through a lenient subset.
_TRADE_TEMPLATE = {
    "accountId": DEMO_ACCOUNT, "acctAlias": "", "currency": "USD",
    "assetCategory": "OPT", "subCategory": "", "symbol": "", "description": "",
    "conid": "", "securityID": "", "securityIDType": "", "cusip": "", "isin": "",
    "figi": "", "listingExchange": "CBOE", "underlyingConid": "",
    "underlyingSymbol": "", "underlyingSecurityID": "",
    "underlyingListingExchange": "NASDAQ", "issuer": "", "issuerCountryCode": "",
    "multiplier": "100", "strike": "", "expiry": "", "putCall": "",
    "principalAdjustFactor": "", "reportDate": "", "tradeDate": "",
    "dateTime": "", "settleDateTarget": "",
    "transactionType": "ExchTrade", "exchange": "CBOE", "quantity": "",
    "tradePrice": "", "tradeMoney": "", "proceeds": "", "taxes": "0",
    "ibCommission": "", "ibCommissionCurrency": "USD", "netCash": "",
    "closePrice": "", "openCloseIndicator": "", "notes": "", "cost": "",
    "fifoPnlRealized": "0", "mtmPnl": "0", "origTradePrice": "0",
    "origTradeDate": "", "origTradeID": "", "origOrderID": "0",
    "clearingFirmID": "", "transactionID": "", "buySell": "",
    "ibOrderID": "", "ibExecID": "", "brokerageOrderID": "", "orderReference": "",
    "volatilityOrderLink": "", "exchOrderId": "N/A", "extExecID": "",
    "orderTime": "", "openDateTime": "", "holdingPeriodDateTime": "",
    "whenRealized": "", "whenReopened": "", "levelOfDetail": "EXECUTION",
    "changeInPrice": "0", "changeInQuantity": "0", "orderType": "LMT",
    "traderID": "", "isAPIOrder": "N", "accruedInt": "0", "serialNumber": "",
    "deliveryType": "", "commodityType": "", "fineness": "0.0", "weight": "0.0",
    "tradeID": "", "fxRateToBase": "", "relatedTradeID": "",
    "relatedTransactionID": "", "origTransactionID": "0", "positionActionID": "",
    "initialInvestment": "", "model": "", "requestID": "", "rtn": "",
}


def _conid(symbol: str) -> str:
    """A stable synthetic conid.

    Uses a content hash rather than `hash()`, which Python randomises per
    process: conids would then differ between runs, so re-ingesting the demo
    would create a second set of contracts instead of being idempotent.
    """
    digest = hashlib.sha1(symbol.encode()).hexdigest()[:8]
    return str(900_000_000 + int(digest, 16) % 90_000_000)


_UNDERLYING_CONID = {"NVDA": "4815747", "SPY": "756733"}


def _q(v: Decimal | int | str) -> str:
    """Render a number the way IBKR does: plain, no exponent, no padding."""
    if isinstance(v, Decimal):
        v = v.normalize()
        return format(v, "f")
    return str(v)


def _trade_elements(orders: list[Order]) -> list[dict[str, str]]:
    """Expand the script into one attribute dict per fill."""
    out: list[dict[str, str]] = []
    seq = 0
    for order_no, order in enumerate(orders, start=1):
        order_id = str(1_100_000_000 + order_no * 137)
        rate = fx_for(order.day)
        for leg in order.legs:
            sym = _occ(leg.underlying, leg.expiry, leg.put_call, leg.strike)
            splits = leg.fills or (leg.quantity,)
            comms = commission_for(leg.quantity, leg.fills)
            for i, (qty, comm) in enumerate(zip(splits, comms)):
                seq += 1
                proceeds = -Decimal(qty) * leg.price * MULTIPLIER
                money = Decimal(qty) * leg.price * MULTIPLIER
                # Realized is reported once per leg, on the last fill, and is
                # already net of commission -- asserted by the history tests.
                realized = leg.realized if i == len(splits) - 1 else Decimal("0")
                a = dict(_TRADE_TEMPLATE)
                a.update(
                    symbol=sym,
                    description=(f"{leg.underlying} {leg.expiry:%d%b%y} "
                                 f"{_q(leg.strike)} {leg.put_call}").upper(),
                    conid=_conid(sym), subCategory=leg.put_call,
                    underlyingSymbol=leg.underlying,
                    underlyingConid=_UNDERLYING_CONID[leg.underlying],
                    strike=_q(leg.strike), expiry=f"{leg.expiry:%Y%m%d}",
                    putCall=leg.put_call,
                    reportDate=f"{order.day:%Y%m%d}", tradeDate=f"{order.day:%Y%m%d}",
                    dateTime=f"{order.day:%Y%m%d};{order.time}",
                    orderTime=f"{order.day:%Y%m%d};{order.time}",
                    settleDateTarget=f"{order.day + timedelta(days=1):%Y%m%d}",
                    quantity=_q(qty), tradePrice=_q(leg.price),
                    tradeMoney=_q(money), proceeds=_q(proceeds),
                    ibCommission=_q(comm), netCash=_q(proceeds + comm),
                    cost=_q(-(proceeds + comm)),
                    closePrice=_q(leg.price), openCloseIndicator=leg.open_close,
                    notes=leg.notes, fifoPnlRealized=_q(realized),
                    buySell="BUY" if qty > 0 else "SELL",
                    ibOrderID=order_id,
                    tradeID=str(1_500_000_000 + seq),
                    transactionID=str(6_300_000_000 + seq),
                    ibExecID=f"0000{seq:04d}.demo.01.01",
                    brokerageOrderID=f"demo.{order_id}.{i}",
                    extExecID=str(2_000_000 + seq),
                    fxRateToBase=_q(rate),
                )
                out.append(a)
    return out


def _position_elements(positions: list[Position]) -> list[dict[str, str]]:
    rate = fx_for(TO_DATE)
    out = []
    for p in positions:
        sym = _occ(p.underlying, p.expiry, p.put_call, p.strike)
        value = Decimal(p.quantity) * p.mark * MULTIPLIER
        out.append({
            "accountId": DEMO_ACCOUNT, "acctAlias": "", "currency": "USD",
            "fxRateToBase": _q(rate), "assetCategory": "OPT",
            "subCategory": p.put_call, "symbol": sym,
            "description": f"{p.underlying} {p.expiry:%d%b%y} {_q(p.strike)} "
                           f"{p.put_call}".upper(),
            "conid": _conid(sym), "securityID": "", "securityIDType": "",
            "cusip": "", "isin": "", "figi": "", "listingExchange": "CBOE",
            "underlyingConid": _UNDERLYING_CONID[p.underlying],
            "underlyingSymbol": p.underlying, "underlyingSecurityID": "",
            "underlyingListingExchange": "NASDAQ", "issuer": "",
            "multiplier": "100", "strike": _q(p.strike),
            "expiry": f"{p.expiry:%Y%m%d}", "putCall": p.put_call,
            "reportDate": f"{TO_DATE:%Y%m%d}", "position": _q(p.quantity),
            "markPrice": _q(p.mark), "positionValue": _q(value),
            "openPrice": _q((p.cost_basis / Decimal(p.quantity) / MULTIPLIER)),
            "costBasisPrice": _q(abs(p.cost_basis / Decimal(p.quantity)
                                     / MULTIPLIER)),
            "costBasisMoney": _q(p.cost_basis),
            "percentOfNAV": "", "fifoPnlUnrealized": _q(value - p.cost_basis),
            "side": "Short" if p.quantity < 0 else "Long",
            "levelOfDetail": "SUMMARY",
            "openDateTime": "" if p.snapshot_only else f"{FROM_DATE:%Y%m%d};143005",
            "holdingPeriodDateTime": "", "code": "", "originatingOrderID": "",
            "originatingTransactionID": "", "accruedInt": "",
        })
    return out


def _cash_elements(cash: list[Cash]) -> list[dict[str, str]]:
    out = []
    for i, c in enumerate(cash, start=1):
        rate = Decimal("1") if c.currency == BASE_CURRENCY else fx_for(c.day)
        out.append({
            "accountId": DEMO_ACCOUNT, "acctAlias": "", "currency": c.currency,
            "fxRateToBase": _q(rate),
            "assetCategory": "OPT" if c.symbol else "",
            "symbol": c.symbol, "description": c.description,
            "conid": "", "securityID": "", "securityIDType": "", "cusip": "",
            "isin": "", "figi": "", "listingExchange": "",
            "underlyingConid": "", "underlyingSymbol": "",
            "issuer": "", "multiplier": "0", "strike": "", "expiry": "",
            "putCall": "", "principalAdjustFactor": "",
            "dateTime": f"{c.day:%Y%m%d};000000",
            "settleDate": f"{c.day:%Y%m%d}",
            "amount": _q(c.amount), "type": c.type,
            "tradeID": "", "code": c.notes, "transactionID": str(7_100_000 + i),
            "reportDate": f"{c.day:%Y%m%d}", "clientReference": "",
            "actionID": "", "levelOfDetail": "DETAIL",
        })
    return out


def _security_elements(trades, positions) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for a in trades:
        seen.setdefault(a["conid"], {
            "assetCategory": "OPT", "subCategory": a["subCategory"],
            "symbol": a["symbol"], "description": a["description"],
            "conid": a["conid"], "securityID": "", "securityIDType": "",
            "cusip": "", "isin": "", "figi": "", "underlyingConid":
            a["underlyingConid"], "underlyingSymbol": a["underlyingSymbol"],
            "underlyingSecurityID": "", "underlyingListingExchange": "NASDAQ",
            "listingExchange": "CBOE", "maturity": "", "issueDate": "",
            "issuer": "", "multiplier": "100", "strike": a["strike"],
            "expiry": a["expiry"], "putCall": a["putCall"],
            "currency": "USD", "settlementPolicyMethod": "",
        })
    for a in positions:
        seen.setdefault(a["conid"], dict(seen.get(a["conid"], {}), **{
            "assetCategory": "OPT", "subCategory": a["subCategory"],
            "symbol": a["symbol"], "description": a["description"],
            "conid": a["conid"], "underlyingConid": a["underlyingConid"],
            "underlyingSymbol": a["underlyingSymbol"], "listingExchange": "CBOE",
            "multiplier": "100", "strike": a["strike"], "expiry": a["expiry"],
            "putCall": a["putCall"], "currency": "USD",
        }))
    return list(seen.values())


def build_demo_statement() -> str:
    """The synthetic statement, as XML text."""
    orders, positions, cash = _script()
    trades = _trade_elements(orders)
    pos = _position_elements(positions)

    root = ET.Element("FlexQueryResponse", queryName=QUERY_NAME, type="AF")
    stmts = ET.SubElement(root, "FlexStatements", count="1")
    st = ET.SubElement(stmts, "FlexStatement",
                       accountId=DEMO_ACCOUNT,
                       fromDate=f"{FROM_DATE:%Y%m%d}", toDate=f"{TO_DATE:%Y%m%d}",
                       period="", whenGenerated=f"{TO_DATE:%Y%m%d};060000")
    ET.SubElement(st, "AccountInformation", accountId=DEMO_ACCOUNT,
                  acctAlias="", currency=BASE_CURRENCY, name="DEMO ACCOUNT",
                  accountType="Individual", ibEntity="IBIE",
                  dateOpened=f"{FROM_DATE:%Y%m%d}")

    for tag, container, rows in (
        ("OpenPosition", "OpenPositions", pos),
        ("Trade", "Trades", trades),
        ("CashTransaction", "CashTransactions", _cash_elements(cash)),
        ("SecurityInfo", "SecuritiesInfo", _security_elements(trades, pos)),
    ):
        parent = ET.SubElement(st, container)
        for row in rows:
            ET.SubElement(parent, tag, **row)
    for empty in ("CorporateActions", "Transfers"):
        ET.SubElement(st, empty)

    ET.indent(root, space=" ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def assert_not_real(archive_dir: Path, db_path: Path | None = None) -> None:
    """Refuse to write synthetic data anywhere the real archive lives.

    The archive is the provenance root for every report, statements are
    deduplicated by content hash, and re-fetching one costs an IBKR request
    against a lockout budget. A fake statement landing in `raw/` would be
    indistinguishable from a real one after the fact.
    """
    real_archive = Path(__file__).resolve().parent.parent.parent / "raw"
    if archive_dir.resolve() == real_archive.resolve():
        raise ValueError(
            f"refusing to write demo data into the real archive {real_archive}. "
            f"Pass --out with a separate directory."
        )
    if db_path is not None:
        real_db = Path(__file__).resolve().parent.parent.parent / "journal.db"
        if db_path.resolve() == real_db.resolve():
            raise ValueError(
                f"refusing to ingest demo data into the real database {real_db}. "
                f"Pass --db with a separate path."
            )


def write_demo_statement(archive_dir: Path, db_path: Path | None = None) -> Path:
    """Write the synthetic statement into `archive_dir` and return its path."""
    assert_not_real(archive_dir, db_path)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"activity-demo-{TO_DATE:%Y%m%dT000000Z}.xml"
    dest.write_text(build_demo_statement(), encoding="utf-8")
    return dest
