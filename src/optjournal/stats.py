"""Monthly statistics and daily P&L series for the journal dashboard.

Kept separate from `analysis` (which measures account *costs* from a single
statement) and from `history` (which reconstructs position episodes). This
module answers the calendar-shaped questions a journal dashboard asks: what
happened in March, and what happened on the 14th.

Two deliberate choices about what gets counted, because the obvious approach
is wrong in both cases:

* **Options P&L counts only fully closed round trips, attributed to the day
  the position closed.** Summing IBKR's per-fill `fifo_pnl_realized_base` --
  the previous rule, still used for other asset categories -- has two leaks
  for options: a *partial* close books realised P&L while the position is
  still open (sell 2, buy back 1: IBKR realises the 1-lot immediately), and
  a close spanning two days scatters one outcome across both. Episode-based
  P&L makes the money follow the same rule as the win/loss counts: nothing
  counts until the position is flat, and the whole outcome lands on the
  close date. Premium collected on an open short is therefore never P&L --
  it is a liability until the position closes.

* **Win/loss counts come from episodes, not fills.** A round trip closed by
  two partial fills is one outcome, not two, so counting fills would inflate
  both the trade count and the win rate. `total_trades` counts fills because
  that is what "how many executions" means; the win/loss block counts
  episodes because that is what "did it work" means. The dashboard labels
  which is which rather than blurring them.

Other asset categories keep the per-fill sum: IBKR's per-fill realised P&L
is the correct realisation rule for share lots (each lot sold is realised,
full stop), and "closed" for an open-ended stock holding is not the crisp
event it is for an options round trip.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from optjournal.history import build_history
from optjournal.money import Money, win_rate

__all__ = [
    "ALL_TRADES",
    "Cohort",
    "DayPnl",
    "EQUITY_CATEGORY",
    "EQUITY_TRADES",
    "MonthStats",
    "TradeScope",
    "annual_stats",
    "available_months",
    "available_years",
    "cohort_data",
    "daily_series",
    "fx_quotes",
    "month_range",
    "month_stats",
    "monthly_stats",
    "odte_cohorts",
    "odte_scope",
    "scope_for",
]


def _in_period(value: str | None, period: str | None) -> bool:
    """Whether a stored date falls inside `period`, which may be a year.

    `period` is matched as a prefix of the normalised ISO day, so "2025-03"
    selects a month and "2025" a year with one predicate. That is what lets
    `month_stats` serve the Annual tab unchanged: the year figures come from
    the same summations, over the same columns, with the same episode-versus-
    fill distinctions as the monthly ones, so the two reconcile by
    construction rather than by two implementations agreeing.

    Prefix-matching the *normalised* day matters -- the raw column mixes ISO
    `2025-01-14 14:30:05` with IBKR's compact `20250114`, and the compact form
    would not match an ISO prefix.

    An empty period means "everything", so this returns True.
    """
    if not period:
        return True
    day = _day_of(value)
    return day is not None and day.startswith(period)


def _month_of(value: str | None) -> str | None:
    """YYYY-MM from a stored date, tolerating both ISO and IBKR compact forms."""
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    if len(text) >= 7 and text[4] == "-":
        return text[:7]
    if len(text) >= 6 and text[:6].isdigit():
        return f"{text[:4]}-{text[4:6]}"
    return None


def _day_of(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().replace("/", "-").split(" ")[0].split("T")[0]
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return None


def _category_where(asset_category: str | None) -> tuple[str, tuple[Any, ...]]:
    """A trades WHERE clause narrowing to one asset category, or nothing.

    `None` means every category, and returns an empty clause rather than a
    tautology -- the callers interpolate this straight into their SQL.

    One helper because the same predicate was spelled three ways across four
    queries here: a ternary tuple twice, and twice as a `clauses` list built up
    for a single condition that never gained a second one. Three spellings of one
    rule is how a fix reaches some of the queries and not the rest.
    """
    if not asset_category:
        return "", ()
    return "WHERE asset_category = ?", (asset_category,)


def fx_quotes(conn: sqlite3.Connection, base: str) -> list[dict[str, Any]]:
    """Alternative display currencies, with the rate converting base into each.

    A quote here is a *presentation* rate, not a reconciliation. Every `*_base`
    figure in this payload was converted by IBKR at its own trade or snapshot
    date, so no single rate reproduces them all -- on this account the
    order-implied USD rate (0.87952, trade date) and the snapshot rate (0.86732)
    differ by 1.4%. Displaying totals in a non-base currency therefore restates
    them at one stated rate, and the page labels it that way rather than letting
    the numbers look like IBKR's own.

    The newest position snapshot is the only dated FX rate the statement gives
    us. With no snapshot there are no quotes, and the page hides the toggle
    rather than inventing a rate.

    Offered codes are restricted to currencies that appear on *option* trades.
    The snapshot table carries every currency the account holds anything in --
    after the equities re-ingest that meant SEK and KRW from stock positions --
    but this is an options journal, and restating its figures into a currency
    no option ever traded in is noise, not information. The snapshot remains
    the *rate* source; option trades define the *set*.
    """
    option_codes = {
        str(r["currency"] or "").upper()
        for r in conn.execute(
            "SELECT DISTINCT currency FROM trades WHERE asset_category = 'OPT'"
        )
    }
    rows = conn.execute(
        "SELECT currency, fx_rate_to_base, report_date FROM position_snapshots"
        " WHERE fx_rate_to_base IS NOT NULL AND fx_rate_to_base > 0"
        " ORDER BY report_date DESC"
    ).fetchall()
    quotes: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row["currency"] or "").upper()
        if not code or code == base.upper() or code in quotes:
            continue
        if code not in option_codes:
            continue
        quotes[code] = {
            "code": code,
            # Stored rate is native -> base, so invert for base -> native.
            "per_base": 1.0 / float(row["fx_rate_to_base"]),
            "as_of": str(row["report_date"] or ""),
            "source": "position snapshot",
        }
    return list(quotes.values())


def first_activity(conn: sqlite3.Connection) -> str | None:
    """The day this account first did anything, or None for an empty journal.

    Any activity at all -- a fill or a cash row -- so an account that funded in
    one month and traded in the next dates from the funding. Both callers need
    the same instant and would drift if each asked separately: `month_range`
    walks forward from it, and `logbook_data` counts days since it.

    Normalised through `_day_of`, because the raw columns mix ISO
    `2025-01-14 14:30:05` with IBKR's compact `20250114`.
    """
    row = conn.execute(
        "SELECT MIN(d) FROM (SELECT MIN(trade_date) AS d FROM trades"
        " UNION ALL SELECT MIN(date_time) FROM cash_transactions)"
    ).fetchone()
    return _day_of(str(row[0])) if row and row[0] else None


def month_range(conn: sqlite3.Connection) -> list[str]:
    """Every calendar month from the account's first activity to today, newest first.

    This is the *browsable* range, deliberately wider than `available_months`
    (months with fills in the current scope). The calendar walks it month by
    month, and the dropdown offers all of it: a month you held positions but
    did not trade is a real month of the account's life, and rendering it as
    an honest zero beats pretending it does not exist. Derived from any
    activity at all -- trades or cash rows -- so a fills-free account start
    still counts.
    """
    first = (first_activity(conn) or "")[:7]
    if len(first) != 7:
        return []
    y, m = int(first[:4]), int(first[5:7])
    today = date.today()
    out: list[str] = []
    while (y, m) <= (today.year, today.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    out.reverse()
    return out


@dataclass(slots=True)
class DayPnl:
    day: str
    trades: int = 0
    #: Realised P&L for the day. IBKR reports it natively per fill and per
    #: episode with the currency it settled in, so a day whose trades all
    #: settled in one currency has an exact figure, not only a translation.
    realized: Money = Money.restated(0.0)

    @property
    def is_green(self) -> bool:
        return self.realized.base > 0

    @property
    def is_red(self) -> bool:
        return self.realized.base < 0


@dataclass(slots=True)
class MonthStats:
    month: str            #: "YYYY-MM", "YYYY" for an annual row, or "ALL"
    base_currency: str = "EUR"
    asset_category: str = "OPT"

    total_trades: int = 0          #: fills
    orders: int = 0
    #: Realised, already net of commission. IBKR reports each episode's P&L in
    #: the currency it settled in, so the dollars that actually moved are known
    #: -- `.base` is the translation, `.native` the exact figure where one
    #: currency accounts for the whole period.
    net_pnl: Money = Money.restated(0.0)
    #: For options, the commission of round trips *closed in the period* --
    #: the same attribution as the P&L, wins and trade count, because IBKR's
    #: episode P&L is already net of every leg's commission. Summing by fill
    #: date (the old rule, still used for other categories) showed the same
    #: euros twice across months: July displayed the opening legs' commission,
    #: and August's net P&L contained it again. Signed, like the fill sum was.
    #:
    #: `.base` is an accounting translation: each row converted at IBKR's own
    #: rate for ITS OWN date. Displaying that sum in a non-base currency
    #: multiplies it by a single later snapshot rate, so a USD charge makes a
    #: round trip -- USD to EUR at trade date, EUR to USD at snapshot --
    #: through two different rates, and does not come back. On this account
    #: that read $7.0021 for commission IBKR actually billed as $6.9652.
    #: `.native` is that exact figure, present only where one currency
    #: accounts for all of it; see `Money`.
    commissions: Money = Money.restated(0.0)
    #: Account-level fee rows. IBKR bills each in a currency and stores it
    #: alongside the base translation, so this takes the same treatment as
    #: commission -- and had to, because the cost report already presented
    #: `fees` as a Money while this panel showed a base-only float for the
    #: same charges.
    fees: Money = Money.restated(0.0)

    #: Episode-derived, so a two-fill close counts once.
    closed_episodes: int = 0
    open_episodes: int = 0
    wins: int = 0
    losses: int = 0
    #: None -- not zero -- when nothing won or lost: an average of no outcomes
    #: is undefined, and zero would read as a break-even trade.
    avg_win: Money | None = None
    avg_loss: Money | None = None

    #: Net premium sitting in *currently open* episodes: positive when short
    #: premium was collected, negative for long debits. Point-in-time like
    #: `open_episodes`, not a period figure. Reported so the money excluded
    #: from Net P&L is visible somewhere honest -- collected premium is a
    #: liability until the position closes, not profit. Premium is cash in the
    #: contract's own currency, so it takes the same native treatment as
    #: commission.
    open_premium: Money = Money.restated(0.0)

    #: Commission already paid on *currently open* episodes. Point-in-time,
    #: like `open_premium`, and excluded from `commissions` for the same reason
    #: the premium is excluded from P&L: it belongs to an outcome that has not
    #: landed yet. Surfaced so the cash is visible somewhere honest rather than
    #: vanishing until the close month.
    #:
    #: A separate figure from `commissions` because the populations differ:
    #: one is closed round trips, the other still-open ones, and a scope can
    #: easily be single-currency in one and mixed in the other.
    open_commission: Money = Money.restated(0.0)

    #: Net Asset Value at the period's end, from the newest equity summary on
    #: or before it. None when the Flex query template does not have the
    #: "Equity Summary in Base" section enabled -- unavailable, not zero.
    net_liq_base: float | None = None
    #: The summary date the figure came from, so a stale NAV is labelled.
    net_liq_date: str | None = None

    days: list[DayPnl] = field(default_factory=list)

    @property
    def win_rate(self) -> float | None:
        """See `money.win_rate`: None when nothing was decided, not zero."""
        return win_rate(self.wins, self.losses)

    @property
    def green_days(self) -> int:
        return sum(1 for d in self.days if d.is_green)

    @property
    def red_days(self) -> int:
        return sum(1 for d in self.days if d.is_red)

    @property
    def gain_pct_of_net_liq(self) -> float | None:
        if not self.net_liq_base:
            return None
        # Base over base: NAV arrives from IBKR in base only (the section is
        # EquitySummaryByReportDateInBase), so the ratio has no native form.
        return self.net_pnl.base / self.net_liq_base * 100.0

    @property
    def options_friction(self) -> Money:
        """Friction attributable to `asset_category`, base and as-charged.

        Commission is charged per trade, so it carries an assetCategory and
        the ingest filter genuinely applies to it. This is the only friction
        figure on this panel that is scoped to the journalled instruments.

        Derived from `commissions` rather than tracked separately: they are the
        same money, and computing the magnitude twice is how the two drift
        apart. Taking `abs` of a `Money` carries the currency along, so the
        as-charged figure cannot end up labelled with another figure's
        currency -- which is what the two separate properties this replaces
        had to do, reading `commissions_native_ccy` for their own label.
        """
        return abs(self.commissions)

    @property
    def account_friction_base(self) -> float:
        """Friction that cannot be attributed to `asset_category`.

        Fee rows carry no assetCategory at all -- market-data subscriptions
        and custody charges are levied on the account, not on a trade -- so
        ingest deliberately does not filter them and they cannot be
        apportioned to options without inventing the split. The AutoFX markup
        has the same problem: IBKR never ties a conversion back to the trade
        that caused it.

        Narrower than `CostReport.account_friction_base` despite the matching
        name: this panel reads the `asset_category`-filtered database, so it
        cannot see commission on other instruments at all, while the cost
        report reads the raw statement and includes it. Do not present the two
        under the same label -- they differ by the whole of stock commission.
        """
        return abs(self.fees.base)

    @property
    def total_friction_base(self) -> float:
        """Both scopes together.

        Presenting this single figure under a panel headed "options" was
        wrong: it labelled account-level fees as this journal's cost, which
        is the same defect the cost report carried. Read
        `options_friction` and `account_friction_base` instead wherever
        the scope is being claimed.

        Base only, and not a `Money`: it merges a per-trade charge with
        account-level fees, so no single currency can speak for the sum even
        when the commission half is uniform.
        """
        return self.options_friction.base + self.account_friction_base


@dataclass(frozen=True)
class TradeScope:
    """A subset of the journal, identified by the fills that belong to it.

    The Trade Types control is a scope selector, and a scope has to be defined
    at fill level because that is what every summation here iterates over. Both
    id sets `None` means "everything", which is the default and costs nothing.

    Membership is by fill, not by predicate re-evaluation, so the scope agrees
    with the cohort it came from by construction. In particular the 0DTE scope
    is *not* "every fill whose trade date equals its expiry": that would also
    catch the expiry-day close of a position held for a month, which is not
    0DTE trading. It is the fills of the round trips classified as 0DTE.
    """

    key: str
    label: str
    trade_ids: frozenset[str] | None = None
    order_ids: frozenset[str] | None = None

    def has_trade(self, trade_id: Any) -> bool:
        return self.trade_ids is None or str(trade_id or "") in self.trade_ids

    def has_episode(self, episode: Any) -> bool:
        """In scope when any of the episode's own fills is.

        Any rather than all: a scope built from whole episodes contains all of
        their fills, so the two agree -- and a partial overlap should surface
        the episode rather than silently drop it.
        """
        if self.trade_ids is None:
            return True
        return any(str(t) in self.trade_ids for t in episode.trade_ids)


#: The default: no filtering at all, and no id sets to build or carry.
ALL_TRADES = TradeScope(key="all", label="Options")

#: The Equities selection. Not a fill subset like the 0DTE scope but a switch
#: to a different asset category -- stocks are not a kind of options trade --
#: so its id sets stay None ("everything") and `build_state` swaps the
#: category the summations run over instead. It lives here so the UI, the
#: `?type=` parameter and the payload share one key/label vocabulary.
EQUITY_TRADES = TradeScope(key="equities", label="Equities")
EQUITY_CATEGORY = "STK"


def odte_scope(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
    report: Any = None,
) -> TradeScope:
    """The 0DTE round trips, as a fill-level scope.

    Includes open episodes as well as closed ones. An open 0DTE position is odd
    but reachable -- a statement cut on expiry day, before the contract died --
    and a scope that filters the journal should not hide a position the
    Positions tab would still show. `odte_cohorts` stays closed-only because
    comparing outcomes needs outcomes.
    """
    if report is None:
        report = build_history(
            conn, asset_category=asset_category, base_currency=base_currency
        )
    trade_ids = {str(t) for e in report.episodes if e.is_odte is True for t in e.trade_ids}
    # Orders are aggregated in a view keyed by ib_order_id, so filtering them
    # needs the order ids those fills belong to rather than the fills.
    order_ids = {
        str(row["ib_order_id"])
        for row in conn.execute(
            "SELECT trade_id, ib_order_id FROM trades WHERE ib_order_id IS NOT NULL"
        )
        if str(row["trade_id"]) in trade_ids
    }
    return TradeScope(
        key="odte",
        label="0DTE",
        trade_ids=frozenset(trade_ids),
        order_ids=frozenset(order_ids),
    )


#: Selectable scopes, by the key the UI and the `?type=` parameter use.
SCOPE_BUILDERS = {"all": None, "odte": odte_scope}


def scope_for(
    conn: sqlite3.Connection,
    key: str | None,
    *,
    asset_category: str | None = "OPT",
    report: Any = None,
) -> TradeScope:
    """Resolve a scope key, falling back to everything for anything unknown.

    Tolerant on purpose: the key arrives from a query parameter, and an
    unrecognised one should show the whole journal rather than fail.
    """
    builder = SCOPE_BUILDERS.get((key or "all").lower())
    if builder is None:
        return ALL_TRADES
    return builder(conn, asset_category=asset_category, report=report)


def available_months(
    conn: sqlite3.Connection,
    asset_category: str | None = "OPT",
    scope: TradeScope = ALL_TRADES,
) -> list[str]:
    """Months with at least one trade in scope, newest first.

    Scoped, so the month dropdown cannot offer a month that the active filter
    has emptied -- picking one would show a blank dashboard and look broken.
    """
    where, params = _category_where(asset_category)
    rows = conn.execute(
        f"SELECT DISTINCT trade_date, trade_id FROM trades {where}", params
    ).fetchall()
    months = {
        m
        for m in (
            _month_of(r["trade_date"]) for r in rows if scope.has_trade(r["trade_id"])
        )
        if m
    }
    return sorted(months, reverse=True)


def available_years(
    conn: sqlite3.Connection, asset_category: str | None = "OPT"
) -> list[str]:
    """Years with at least one trade, newest first.

    Unscoped, unlike `available_months`: the only caller is the Annual tab,
    which shows no filter bar and so must not narrow. See `build_state`.
    """
    where, params = _category_where(asset_category)
    rows = conn.execute(f"SELECT DISTINCT trade_date FROM trades {where}", params).fetchall()
    years = {d[:4] for d in (_day_of(r["trade_date"]) for r in rows) if d}
    return sorted(years, reverse=True)


def _period_stats(
    conn: sqlite3.Connection,
    periods: list[str],
    *,
    asset_category: str | None,
    base_currency: str,
) -> list[MonthStats]:
    """`month_stats` over several periods, sharing one episode history pass.

    The Annual tab asks for every month, every year and an all-time row at
    once. Each `month_stats` call otherwise rebuilds the whole episode history,
    so a thirteen-month archive did that fifteen times per page load for
    identical results.
    """
    report = build_history(
        conn, asset_category=asset_category, base_currency=base_currency
    )
    return [
        month_stats(
            conn, period, asset_category=asset_category,
            base_currency=base_currency, report=report,
        )
        for period in periods
    ]


def annual_stats(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
) -> list[MonthStats]:
    """One `MonthStats` per calendar year, newest first.

    Each year is `month_stats` over a year-wide period rather than a separate
    aggregation, so a change to how P&L or win rate is counted lands on the
    monthly and annual views together. A year with fills but nothing closed
    still gets a row: "traded, decided nothing" is a real outcome and hiding
    it would make the years stop accounting for all the activity.

    Takes no `TradeScope`, unlike `month_stats`. The Annual tab renders no
    filter bar, and a tab whose numbers move with a control it does not display
    leaves the reader nothing to explain the change with. A scope parameter here
    would be an unused hook inviting exactly that.
    """
    return _period_stats(
        conn, available_years(conn, asset_category),
        asset_category=asset_category, base_currency=base_currency,
    )


def monthly_stats(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
) -> list[MonthStats]:
    """One `MonthStats` per calendar month, newest first.

    The same rows the month selector produces one at a time, so the Annual
    tab's breakdown and the Dashboard agree for any month the reader checks --
    they are the same call with the same period string.

    Unscoped for the same reason as `annual_stats`: it feeds the Annual tab,
    which carries no filter.
    """
    return _period_stats(
        conn, available_months(conn, asset_category),
        asset_category=asset_category, base_currency=base_currency,
    )


@dataclass(slots=True)
class Cohort:
    """Outcome summary for a subset of closed episodes."""

    label: str
    episodes: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: Money = Money.restated(0.0)
    commission_base: float = 0.0
    contracts: int = 0

    @property
    def win_rate(self) -> float | None:
        """See `money.win_rate`: None when nothing was decided, not zero."""
        return win_rate(self.wins, self.losses)

    @property
    def avg_pnl(self) -> Money | None:
        """Mean outcome per round trip, wins and losses together.

        The figure that answers "is this cohort worth trading": a high win
        rate with a worse average is a losing strategy, and the two numbers
        only mean something side by side.
        """
        return self.net_pnl.per(self.episodes)


def _cohort(label: str, episodes: list[Any]) -> Cohort:
    c = Cohort(label=label)
    for e in episodes:
        c.episodes += 1
        c.commission_base += abs(e.commission_base)
        c.contracts += e.contracts
        if e.realized_pnl_base > 0:
            c.wins += 1
        elif e.realized_pnl_base < 0:
            c.losses += 1
    # Built once rather than advanced per episode: a frozen figure cannot have
    # its amount moved without its currency coming along.
    c.net_pnl = Money.charged(
        (e.realized_pnl_base, e.realized_pnl, e.currency) for e in episodes
    )
    return c


def odte_cohorts(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
    report: Any = None,
) -> tuple[Cohort, Cohort, int]:
    """0DTE round trips against everything else, plus the unclassifiable count.

    Returned as a pair because a 0DTE win rate in isolation says nothing --
    the question is always whether same-day expiries did better or worse than
    the rest of the book, and that needs both sides on screen.

    The third value counts closed episodes whose DTE cannot be determined,
    which is anything without an expiry. Reported rather than silently folded
    into "not 0DTE": a stock has no DTE, and claiming it was not a 0DTE trade
    is a different statement from admitting the question does not apply.
    """
    if report is None:
        report = build_history(
            conn, asset_category=asset_category, base_currency=base_currency
        )
    odte = [e for e in report.closed if e.is_odte is True]
    rest = [e for e in report.closed if e.is_odte is False]
    unknown = sum(1 for e in report.closed if e.is_odte is None)
    return _cohort("0DTE", odte), _cohort("Everything else", rest), unknown


def cohort_data(c: Cohort) -> dict[str, Any]:
    """JSON-safe view, including the computed properties `asdict` would drop."""
    return {
        "label": c.label,
        "episodes": c.episodes,
        "wins": c.wins,
        "losses": c.losses,
        "win_rate": c.win_rate,
        "net_pnl": c.net_pnl.payload(),
        "avg_pnl": None if c.avg_pnl is None else c.avg_pnl.payload(),
        "commission_base": c.commission_base,
        "contracts": c.contracts,
    }


#: The category whose P&L is episode-based. Exactly "OPT": for anything else
#: -- including the mixed `asset_category=None` -- the per-fill rule stands,
#: which is what "preserve existing behaviour for other asset types" means.
_EPISODE_PNL_CATEGORY = "OPT"


def daily_series(
    conn: sqlite3.Connection,
    period: str | None = None,
    asset_category: str | None = "OPT",
    scope: TradeScope = ALL_TRADES,
    report: Any = None,
) -> list[DayPnl]:
    """Realised P&L and fill count per calendar day, ascending.

    `period` is a year or a month; see `_in_period`. Fill counts always land
    on the fill's own day -- they measure activity. Where the *money* lands
    depends on the category: options P&L is attributed to the day the round
    trip closed (see the module docstring), so a day with only opening or
    partial-close fills shows activity and no P&L. Other categories keep
    IBKR's per-fill realisation on the fill's day.
    """
    episode_pnl = asset_category == _EPISODE_PNL_CATEGORY
    where, params = _category_where(asset_category)

    # Counts and P&L rows accumulate separately because a `Money` is frozen:
    # the figure is built once per day, from every row that contributed, rather
    # than advanced in place with its currency tracked somewhere else.
    counts: dict[str, int] = {}
    ledger: dict[str, list[tuple[float | None, float | None, str | None]]] = {}
    for row in conn.execute(
        f"SELECT trade_date, trade_id, fifo_pnl_realized_base, fifo_pnl_realized,"
        f" currency FROM trades {where}", params
    ):
        day = _day_of(row["trade_date"])
        if day is None or not _in_period(row["trade_date"], period):
            continue
        if not scope.has_trade(row["trade_id"]):
            continue
        counts[day] = counts.get(day, 0) + 1
        if not episode_pnl:
            ledger.setdefault(day, []).append(
                (row["fifo_pnl_realized_base"], row["fifo_pnl_realized"],
                 row["currency"])
            )

    if episode_pnl:
        if report is None:
            report = build_history(conn, asset_category=asset_category)
        for ep in report.closed:
            day = _day_of(ep.closed_at)
            if day is None or not _in_period(ep.closed_at, period):
                continue
            if not scope.has_episode(ep):
                continue
            ledger.setdefault(day, []).append(
                (ep.realized_pnl_base, ep.realized_pnl, ep.currency)
            )
    return [
        DayPnl(day=day, trades=counts.get(day, 0),
               realized=Money.charged(ledger.get(day, ())))
        for day in sorted(set(counts) | set(ledger))
    ]


def _net_liq_for(
    conn: sqlite3.Connection, period: str | None
) -> tuple[float | None, str | None]:
    """NAV at a period's end: the newest equity summary on or before it.

    "On or before" rather than "inside": a month with trades but no summary
    row (the section was enabled later, or the archive starts mid-history)
    still gets the latest known NAV rather than pretending none exists. The
    date rides along so the display can say how stale the figure is.

    Comparing normalised day strings works because both sides are ISO-ordered;
    the period end key is the period prefix plus '\uffff', which sorts after
    every day inside it and before the next period.
    """
    rows = conn.execute(
        "SELECT report_date, total_base FROM equity_summaries"
        " ORDER BY report_date"
    ).fetchall()
    end_key = (period + "\uffff") if period else "\uffff"
    best: tuple[float | None, str | None] = (None, None)
    for row in rows:
        day = _day_of(row["report_date"])
        if day is None or day > end_key:
            continue
        best = (row["total_base"], day)
    return best


def month_stats(
    conn: sqlite3.Connection,
    period: str | None = None,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
    scope: TradeScope = ALL_TRADES,
    report: Any = None,
) -> MonthStats:
    """Statistics for one period, or for everything when `period` is None.

    `period` is a month ("2025-03") or a whole year ("2025"). Both widths run
    through the identical summations, which is what makes the Annual tab's
    figures reconcile with the monthly ones instead of being a second opinion.

    `scope` restricts every trade-derived figure to a subset of fills. `report`
    lets a caller building many periods reuse one `build_history` pass -- the
    Annual tab asks for a dozen months, two years and an all-time row on every
    page load, and rebuilding the episode history for each was the whole cost.
    """
    stats = MonthStats(
        month=period or "ALL",
        base_currency=base_currency,
        asset_category=asset_category or "ALL",
    )

    where, params = _category_where(asset_category)

    episode_pnl = asset_category == _EPISODE_PNL_CATEGORY
    orders: set[str] = set()
    #: Base total and the per-currency native ledger, accumulated together so
    #: the figure can be gated once at the end. Local rather than accumulated
    #: onto `stats` because a `Money` is frozen: an amount cannot be advanced
    #: without its currency, which is the property that keeps the two in step.
    fill_commission_base = 0.0
    native: dict[str, float] = {}
    fill_pnl: list[tuple[float | None, float | None, str | None]] = []
    for row in conn.execute(
        f"SELECT trade_date, trade_id, ib_order_id, fifo_pnl_realized_base,"
        f" fifo_pnl_realized, ib_commission_base, ib_commission, currency"
        f" FROM trades {where}", params
    ):
        if not _in_period(row["trade_date"], period):
            continue
        if not scope.has_trade(row["trade_id"]):
            continue
        stats.total_trades += 1
        if row["ib_order_id"]:
            orders.add(str(row["ib_order_id"]))
        if not episode_pnl:
            # Per-fill realisation: the rule for share lots, where each lot
            # sold is realised and "fully closed" is not a crisp event.
            # Commission rides the same basis: on the fill's day, because
            # that is also where the P&L it nets against is attributed.
            fill_pnl.append((row["fifo_pnl_realized_base"],
                             row["fifo_pnl_realized"], row["currency"]))
            fill_commission_base += row["ib_commission_base"] or 0.0
            if row["ib_commission"]:
                native[row["currency"]] = (
                    native.get(row["currency"], 0.0) + row["ib_commission"]
                )
    stats.orders = len(orders)
    if not episode_pnl:
        stats.commissions = Money.gated(fill_commission_base, native)
        stats.net_pnl = Money.charged(fill_pnl)

    # Fees are account-level CashTransaction rows, never trade-linked -- verified
    # against real data, where none of the 65 fee rows carries a conid or tradeID.
    # Deliberately NOT scoped: there is nothing to filter them on, and pro-rating
    # them into a fill subset would be inventing an attribution. Under an active
    # scope they stay the account's figure, which is what the pill already says.
    fee_rows: list[tuple[float | None, float | None, str | None]] = []
    for row in conn.execute(
        "SELECT date_time, amount_base, amount, currency, type FROM cash_transactions"
        " WHERE UPPER(type) LIKE '%FEES%'"
    ):
        if not _in_period(row["date_time"], period):
            continue
        fee_rows.append((row["amount_base"], row["amount"], row["currency"]))
    stats.fees = Money.charged(fee_rows)

    if report is None:
        report = build_history(
            conn, asset_category=asset_category, base_currency=base_currency
        )
    # Attributed by close date, matching the monthly convention: an episode
    # opened in December and closed in January is a January outcome, and so a
    # 2026 one. Attributing by entry instead would make the annual rows stop
    # summing to the monthly ones.
    closed = [
        e for e in report.closed
        if _in_period(e.closed_at, period) and scope.has_episode(e)
    ]
    stats.closed_episodes = len(closed)
    stats.open_episodes = sum(1 for e in report.open if scope.has_episode(e))
    if episode_pnl:
        # The whole outcome of a fully closed round trip, landing on its close
        # date. An open episode contributes nothing -- including any realised
        # P&L IBKR booked on a *partial* close, and any premium collected on
        # the opening sale. Those count on the day the position goes flat.
        stats.net_pnl = Money.charged(
            (e.realized_pnl_base, e.realized_pnl, e.currency) for e in closed
        )
        # Commission follows the trade, not the fill: the round trip's whole
        # commission -- opening legs included -- lands in the close period,
        # because the net P&L above already contains it. A month that merely
        # opened a position shows no commission, exactly as it shows no trade.
        #
        # Episodes carry the base amount, the native amount AND the currency it
        # was charged in, so the exact figure needs no extra query -- only the
        # check that one currency speaks for the whole round-trip set, which is
        # what `Money.charged` does in the same pass as the sum.
        stats.commissions = Money.charged(
            (e.commission_base, e.commission, e.currency) for e in closed
        )
        stats.open_commission = Money.charged(
            (e.commission_base, e.commission, e.currency)
            for e in report.open if scope.has_episode(e)
        )
    # Premium is cash received or paid in the contract's own currency, so it
    # takes the same treatment as commission: exact when one currency accounts
    # for the whole figure, withheld when they are mixed.
    stats.open_premium = Money.charged(
        (e.proceeds_base, e.proceeds, e.currency)
        for e in report.open if scope.has_episode(e)
    )
    wins = [e for e in closed if e.realized_pnl_base > 0]
    losses = [e for e in closed if e.realized_pnl_base < 0]
    stats.wins, stats.losses = len(wins), len(losses)
    # `Money.per` divides base and native by the same count, so an average can
    # never be an exact numerator over a restated one.
    stats.avg_win = Money.charged(
        (e.realized_pnl_base, e.realized_pnl, e.currency) for e in wins
    ).per(len(wins))
    stats.avg_loss = Money.charged(
        (e.realized_pnl_base, e.realized_pnl, e.currency) for e in losses
    ).per(len(losses))
    stats.net_liq_base, stats.net_liq_date = _net_liq_for(conn, period)

    stats.days = daily_series(conn, period, asset_category, scope, report=report)
    return stats


def stats_data(stats: MonthStats) -> dict[str, Any]:
    """JSON-safe view, including the computed properties `asdict` would drop."""
    return {
        "month": stats.month,
        "base_currency": stats.base_currency,
        "asset_category": stats.asset_category,
        "total_trades": stats.total_trades,
        "orders": stats.orders,
        "net_pnl": stats.net_pnl.payload(),
        # A `Money` figure serialises as one nested object rather than three
        # parallel keys. The page reads `.base`, `.native` and `.ccy` off it
        # through a single helper, so a new gated figure costs no new display
        # branch. Figures that stay flat floats -- net_pnl, avg_win, net_liq,
        # the friction rollups -- are the ones for which no as-charged amount
        # can exist, and the shape says so.
        "commissions": stats.commissions.payload(),
        "fees": stats.fees.payload(),
        "closed_episodes": stats.closed_episodes,
        "open_episodes": stats.open_episodes,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": stats.win_rate,
        "avg_win": None if stats.avg_win is None else stats.avg_win.payload(),
        "avg_loss": None if stats.avg_loss is None else stats.avg_loss.payload(),
        "open_premium": stats.open_premium.payload(),
        "open_commission": stats.open_commission.payload(),
        "net_liq_base": stats.net_liq_base,
        "net_liq_date": stats.net_liq_date,
        "gain_pct_of_net_liq": stats.gain_pct_of_net_liq,
        "options_friction": stats.options_friction.payload(),
        "account_friction_base": stats.account_friction_base,
        "total_friction_base": stats.total_friction_base,
        "green_days": stats.green_days,
        "red_days": stats.red_days,
        "days": [
            {"day": d.day, "trades": d.trades, "realized": d.realized.payload()}
            for d in stats.days
        ],
    }
