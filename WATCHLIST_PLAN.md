# The Watchlist panel

## Summary

This is a build of the mockup's Watchlist panel in which every number on screen is
either measured or typed, and nothing is fabricated to fill a shape. Three of the
five columns ship as drawn: the close (already there), a B-Xtrender reading (new
leaf `trend.py`, EMAs and a Wilder RSI over stored closes, so outside the
modelled-number quarantine), and a company name that is already in the chart meta
block `parse_quote` reads and discards, at zero extra request cost. The EARNINGS
column ships as user input in a new nullable `watchlist.earnings_on` column,
because nothing this repo can reach has an earnings date (chart meta has no
earnings key, the endpoint's `events` parameter serves dividends and splits at
every window tested, `quoteSummary` is 401, Nasdaq is 404, and `market_events` is a
macro calendar whose country column holds currency codes). The IV RANK ring does
NOT ship under that name and does not ship as a percentile either: implied vol is
unreachable twice over (every keyed endpoint answers 401, and the only IV this
journal can compute is per-contract from an option's own closes, which drifts in
moneyness and exists only for contracts already traded), so the same ring carries a
**realised vol rank**, `rv_rank`, computed the way the word rank is defined, as a
min-max position inside the symbol's own trailing year, with the low and the high
printed beside it and `MAY_MODEL` still at `{"replay", "demo"}`. IV rank's
conventional 30 is deliberately not carried over onto it; the cut point is
`vol.RANK_MIDPOINT`, the middle of the symbol's own year, which the arithmetic
justifies without importing anything. The "IAG READ" panel becomes YOUR NOTE,
rendering a column that already exists and already round-trips. A provider seam is
specified below and deliberately declined, because it would ship with zero
implementations, which is unfalsifiable as well as untested by construction.

What this build is NOT: it is not a rebrand (branding, the nine tabs and the
version chip are untouched), it is not a new endpoint (derived figures ride
`/api/state`, the name rides the existing `/api/quotes`), and it is not primarily
frontend work. The first two slices fix a live defect where the watchlist counts
one ET session twice when two conids cover it (NVDA realised vol 30.15% against
40.71% deduplicated) and widen `WATCH_LOOKBACK_DAYS` from 60 to 1100 so the
indicator has the ~120 sessions it needs instead of the 45 that five of six real
symbols hold. Until that backfill lands, every new figure is an em dash with a
title naming the shortfall, which is the deliverable rather than a shortcoming. The
two pieces of new frontend arithmetic (the meter's knob and the ring's arc) live in
a new `static/watch.js` with node tests, not in the page, because both take data
and return data and this repo already owns that seam.

## Decisions

### Does the watchlist get an IV RANK column, and does that require widening `tests/test_layering.py`'s MAY_MODEL?

**No IV rank, and MAY_MODEL stays at `{"replay", "demo"}`. The ring survives filled
with a realised vol rank.**

`vol.py:22-27` says it directly: "stdev(log returns) * sqrt(252) is arithmetic over
broker-stated prices -- no option model, no assumption about a distribution's
tails, nothing solved. So the watchlist stays OUTSIDE the modelled-number
quarantine and `test_layering`'s `MAY_MODEL` allowlist stays at two modules. That
allowlist IS the quarantine; growing it for a convenience column would be the
expensive kind of small decision."

The counter-argument would be that IVR is what a premium seller wants, so the
convenience is not trivial. It is answered twice over. First, there is no source to
import: the option chain is HTTP 401 Invalid Crumb, `quoteSummary` is 401,
`v7/quote` is 401, and the chart meta block carries 25 keys of which none is an
implied vol. Second, even with a model the quantity would be wrong: IV rank is a
position of constant-maturity ATM implied vol inside a trailing year, while the only
IV this journal can compute is per-contract from an option's own closes (382 daily
bars for the held LEAP, 3 to 9 for traded legs, zero option contracts at all for
SPY) and drifts in moneyness and time to expiry as it ages. Widening the quarantine
would buy a wrong number, which is the worst possible trade.

### Which realised-vol statistic fills the ring, and what may it be called?

**A min-max RANK, not a percentile. `rv_rank` = `(rv_now - lo) / (hi - lo) * 100`
over the symbol's own trailing year of 20-session realised vol readings, with `lo`
and `hi` on the wire beside it. The word "rank" then describes the arithmetic. The
band is `vol.RANK_MIDPOINT = 50.0`, and IV rank's conventional 30 is explicitly not
carried over.**

The previous draft of this plan defined `rv_rank` as a percentile (the share of
windows below today's) and then kept the word rank, the header RVR, the mockup's
"> 30" chips and its "Elevated" verdict. That was the defect this project hunts,
one level up: a threshold calibrated on one statistic carried onto a different
statistic, under the first statistic's name. In options vocabulary, which is the
vocabulary the mockup borrows, IV *Rank* is `(current - 52w min) / (52w max - 52w
min)` and IV *Percentile* is the share of days below current. They are different
numbers, they are differently distributed (a value's percentile inside its own
trailing year is roughly uniform over time, so "> 30" would be true on about 70% of
sessions for every symbol by construction), and 30 belongs to neither of them
generically: it belongs to IVR practice on implied vol.

So one statistic is picked and made whole. Min-max, because the mockup's whole
picture is the rank convention (a 0 to 100 arc with a reference tick), because it is
the same arithmetic every reader of the word already knows applied to a stated
substitution of INPUT only (realised for implied), and because it states itself in
one sentence a caption can print: "20-session realised vol is 84% of the way from
this year's low, 28.1%, to its high, 61.4%, over 232 windows". A percentile cannot
print its own meaning that compactly, and its robustness advantage buys nothing here
because the low and the high are on screen where an outlier is visible rather than
hidden inside a rate.

The cut point is the honest half of the fix. `RANK_MIDPOINT = 50.0` is the middle of
the symbol's own observed range, so it needs no external calibration and no import:
above it means nearer this year's high than its low, which is a statement the
arithmetic supports. `RANK_BANDS_SOURCE` carries that sentence and says why 30 was
refused, exactly as `trend.BANDS_SOURCE` carries the RSI import. The measured share
of windows above the midpoint over a real symbol's fetched year goes into the
constant's comment when slice 3 lands, beside the number it justifies, which is the
treatment `trend.py`'s bands get. It cannot be measured before that slice: five of
six real symbols hold 45 closes today, which is the whole reason these cells are
dashes.

The verdict word goes with the threshold it came from. "Elevated" is a claim about a
level against some normal; nothing here establishes a normal. The slot instead
prints the measurement in words, and the filter chips read `RVR > 50` and `RVR <= 50`
generated from the same constant.

### Are "Oversold" and "Overbought" presentable as bucket labels?

**No. The chips read "below -20", "mid" and "above +20", the payload key is
`bx_bucket` in `{"low", "mid", "high"}`, and the RSI convention's own words appear
only in the attributed caption.**

The previous draft attributed the bucket NUMBERS and left the WORDS alone, which
does not hold: "oversold" and "overbought" are claims that a share is cheaper or
dearer than it should be, and an RSI of an EMA difference re-centred by 50 is a
statement about the shape of recent closes, not about worth against price. This plan
deletes "Premium selling favored" with exactly that argument ("a percentile of what
the stock DID cannot recommend selling what the market CHARGES"), so keeping two
valuation words from the same mockup would be inconsistent inside one panel.

The repo's own precedent is `market_events.impact`: `db.py:335-337` stores it as
"the FEED's judgement of importance, not the journal's" and `page.html:3141-3143`
prints "impact is <feed>'s assessment, not this journal's". So the convention's
vocabulary survives where it is attributed. `trend.BANDS_SOURCE` reads
"Wilder's RSI 70/30, re-centred by the indicator's own -50; 'oversold' and
'overbought' are that convention's words for below and above the band, not this
journal's view of what a share is worth", and the filter caption renders it.

### The mockup's BXTRENDER column has two shades of green. Which one ships?

**One hue per sign, plus a DIRECTION GLYPH driven by `bx_daily_delta`. Not a fourth
colour.**

The objection is right that the second shade is a second state rather than
magnitude (+38.6 is dim and +28.7 is bright in the mockup), and right that the
published indicator's states are sign crossed with rising-or-falling (the four
colours in the original Pine). Silence would have shipped one hue and dropped the
indicator's second dimension. So the state ships.

It ships as a glyph because of `app.css:282-293`, which is this page's own rule:
"Sign is carried by a GLYPH as well as a hue, because hue alone does not survive
red-green colour blindness: measured, `--ok` against `--bad` separates by dE 2.4
under deuteranopia". A fourth hue meaning "positive but weakening" is a colour-only
signal, and two greens that must be told apart is a harder discrimination than
green against rose. It would also cost two new palette names in `:root` and all
three theme blocks, each then policed by
`test_every_theme_declares_the_same_palette` and the contrast tests, for a
distinction a glyph makes at any acuity. The mockup itself pairs the detail tile's
delta with an arrow, so the glyph is in the drawing already: the column renders
`+38.6 v` / `+22.4 ^` with a `title` naming the delta, `cls()` keeps the sign hue,
and one test binds the glyph to the sign of `bx_daily_delta`.

### Should the unreachable figures (IV rank, earnings, an editorial read) get a configured-provider seam?

**No. The seam is declined, with its exact shape written down here so the reader can
see what was declined: a `watchmetrics.py` leaf with `class MetricSource(Protocol)`
carrying `source: str` and `metrics(symbols) -> Iterator[WatchMetric]`, a
`METRIC_SOURCES` dict with a `metric_source_for(name)` that raises naming what
exists (the `sources.py:351-368` template), a `watch_metrics(source, symbol, as_of,
fetched_at, raw)` cache table keyed `(source, symbol)`, a GET reading the cache and
a POST spending requests, every cell an em dash titled "no implied-vol provider
configured".**

The README's calendar-feed rule states that an abstraction with one implementation
is untested by construction; a registry that ships EMPTY has zero implementations
and is additionally unfalsifiable. Concretely it is a credential decision, a config
surface this journal does not have (no settings table among `db.py`'s 13, no env
read below an entry point), a fetch/parse/store trio and a test suite, all in
service of a column no reader can populate. The condition that would change the
answer is named: a second real source in prospect, which is the same bar
`sources.py` had to clear.

### Where does the earnings date come from?

**You type it. A nullable `watchlist.earnings_on` TEXT column (YYYY-MM-DD), written
through `POST /api/watchlist` and `optjournal watch --earnings`, with the countdown
derived at serialize time from `clock.et_day` and never stored.**

Nothing this repo reaches has an earnings date: the chart meta block has no earnings
key, the endpoint's own `events` parameter returns dividends and splits at 1mo, 1y,
2y and 5y and never earnings, `quoteSummary` `calendarEvents` is 401,
`api.nasdaq.com` is 404, the keyless search endpoint has no earnings field, and
`market_events` is a macro calendar whose country column holds currency codes with
zero rows mentioning a ticker. Deriving a next date from a quarterly cadence would
be a guess wearing a date's clothes. User input is honest because the provenance is
unambiguous, and `db.py:346` already says `watchlist` is "the first table here that
is USER INPUT rather than ingested fact". The countdown is derived rather than
stored so it cannot drift from the date beside it; a past date renders as the date
plus "recorded, now past" rather than a negative number.

### Where does the company name come from, given `serialize.watchlist_data` reads SQLite only?

**`marketdata.Quote` gains `name: str | None` from the chart meta block's `longName`
falling back to `shortName`, so it arrives through `/api/quotes` and the row falls
back to the bare symbol until Refresh has run. Not cached on the watchlist table,
not taken from `securities.description`, not written to `price_bars`.**

It costs no extra request: `parse_quote` (`marketdata.py:220-261`) already receives
`longName` in the meta block and drops it, present for all seven probed symbols
including the ETF, and DELL's value is byte for byte the mockup's string.
`securities.description` covers one of the six real watched symbols, is structurally
empty for a never-traded symbol (SPY has zero rows), and spells TSLA "TESLA INC"
against the live source's "Tesla, Inc.", so one column would show two data
qualities. `watchlist` is the user-input table (`db.py:346`) and `price_bars` is
explicitly never written by `_quotes` (`web.py:630`). The consequence is designed
for rather than hidden: the row renders symbol-only before a quote, exactly as the
stored close already carries its `*` marker, and the search placeholder says whether
company search is available yet.

### Where does the B-Xtrender arithmetic live?

**A new leaf, `src/optjournal/trend.py`, added to `tests/test_layering.py`'s LEAVES
and importing only `math`. Not a second function in `vol.py`, and not in `bars.py`.**

`vol.py` is titled "Realised volatility and the move it implies, from closes alone"
and its body is one argument about realised versus implied; an oscillator there
would make the module name and the whole docstring a lie, and `MIN_RETURNS = 5` is
not this indicator's floor (35 is the mathematical one, about 120 the honest one). A
leaf may import nothing from the package (`tests/test_layering.py:54`), which forces
the ISO-week bucketing out of it anyway since that needs `clock.et_day`. So the work
splits three ways, each piece where its rule already lives: the arithmetic in the
leaf, "which stored closes are this symbol's sessions and which sessions are a week"
in `bars.py` (which already imports `clock` at `bars.py:45`), composition and key
naming in `serialize.py`.

### Which B-Xtrender formula, and which seeding?

**Short arm `rsi(ema(close,5) - ema(close,20), 15) - 50`, long arm `rsi(ema(close,20),
15) - 50`, with EMA and Wilder RSI both seeded by an SMA of the first n values
(TradingView-compatible). Periods and the 50 are named constants with the
attribution in the docstring (Bharat Jhunjhunwala, IFTA Journal 2019; TradingView
implementation by QuantTherapy).**

The original Pine v4 source and an independent Python reimplementation agree
expression for expression and default for default. A popular third repo does not: it
computes `ema(rsi(close,5) - 50, 3)`, the RSI of price rather than of the EMA
difference, which produces a well-formed number in the right range and the wrong
indicator. So the docstring states that the RSI reads the EMA DIFFERENCE and a test
pins a hand-computed vector rather than a shape. Seeding is not taste: measured over
TSLA's 761 closes, a 44-close window reads -1.3487 with SMA seeding and +9.4472 with
pandas-style recursive seeding, a 10.80 point gap that flips the bucket, converging
to 0.0003 at 200 closes. Since the number's whole meaning is "what the published
indicator says", the published seeding is the correct one.

### When may a B-Xtrender value be shown at all?

**`MIN_CLOSES = 35` is the mathematical floor and `MIN_SETTLED = 120` is the gate the
function actually enforces, in the arm's own unit (sessions for daily, ISO weeks for
weekly). Below it the function returns None and the cell renders an em dash titled
with the count held.**

At 45 closes the newest short-arm value is wrong by 2.2 to 9.6 points against the
converged value (GOOG 5.28, PLTR 2.78, SPY 2.22), and on TSLA a 44-close window
reads -1.35 where the converged value is +8.24, so the sign flips, and the sign is
the indicator's primary published state. Worse, at 35 to 39 closes the long arm
reports exactly -50.0000, the pegged extreme, which renders as the strongest signal
on the page. This is `vol.py`'s `MIN_RETURNS` discipline applied per indicator, and
the payload's existing `closes` count already exists so the page can "say WHY a vol
is missing rather than showing a bare dash" (`page.html:698`).

### How do the indicator's parameters and bands reach the screen?

**Generated from `trend.py`'s constants, both of them: the tile's caption names the
periods and the author ("B-Xtrender, short arm 5/20/15, centred on 50; Jhunjhunwala
2019"), and the filter caption names the band and its provenance from
`BX_OVERSOLD`, `BX_OVERBOUGHT` and `BANDS_SOURCE`. One test per string binds the
page's numbers and sentence to the Python constants.**

B-Xtrender at 5/20/15 and at other settings are different numbers, so a column
headed BXTRENDER showing +22.4 with no periods stated is a figure its reader cannot
reproduce. That is the gap `impact_source` closes for the calendar, and the same
answer applies. The bands need the same treatment for a different reason: the
published indicator states no oversold or overbought level at all (no hline, no
level inputs, only zero crossed with rising-or-falling), so any numeric band is
imported and the import must be named. It also does not travel between arms: over
TSLA's 727 sessions +/-20 cuts the outer 9.5% and 11.1% of the daily short arm but
36.3% and 29.0% of the long arm, and a bucket holding a third of all sessions has
stopped meaning anything. Hence one named arm and timeframe, stated on screen, with
the measured share beside each constant in Python where the judgement can be
audited once rather than per caption.

### Is the incomplete current week shown in the weekly tile?

**Yes, labelled: the value plus "2026-W33, 3 of 5 sessions in", carried as
`bx_weekly_week` and `bx_weekly_sessions` in the payload. Never a bare figure.**

Including the partial week matches TradingView and this repo's own stated precedent
that "a daily bar for a session in progress is legitimately incomplete and the chart
draws it as where it is now". But it repaints measurably: on TSLA the weekly short
arm moved -19.02, -18.63, -19.71 across the three sessions of 2026-W33 against
-19.84 for completed weeks only. An unlabelled figure that silently changes every
day is the same defect as an undated price, which `marketdata.parse_quote` refuses
outright.

### How is a week defined, and does it need a holiday calendar?

**ISO week, Monday start, bucketed on `clock.et_day`, the week's close being its last
session's close. No gap filling and no holiday list.**

Monday start matches TradingView's weekly bars for US equities, which is what makes
the number comparable to the published indicator. A holiday-shortened week is still
one week, which is also what TradingView does, so the sessions present in the data
are the definition. That is the same principle the perishable audit already uses when
it treats the underlying's own series as the holiday oracle rather than maintaining
a date list forever. Measured: 755 daily closes aggregate to 158 ISO weeks, the
current bucket holding 3 sessions and every prior one 5.

### How wide does the watched-symbol bar window need to be, and what does widening it cost?

**`bars.WATCH_LOOKBACK_DAYS` goes from 60 to 1100 calendar days, with the docstring
rewritten to say what now sizes it, and `sync.py`'s snapshot justification
re-measured in the same diff.**

60 calendar days is about 41 sessions, which is inside the warm-up zone for the daily
arm and about a seventh of what the weekly arm needs; five of the six real watched
symbols hold 45 or 46 closes and 10 ISO weeks today. The new reason is the weekly
arm's ~120 ISO weeks (about 840 calendar days, plus room); 1100 is the value
`SNAPSHOT_FLOOR_DAYS` already uses for the same class of reason. The old docstring's
reason ("a 20-session realised vol needs 21 closes") stops being the reason and is
replaced rather than left standing.

The request cost is nil: the manifest already emits exactly one daily request per
watched symbol (`bars.py:373`), `_bar_size_for` resolves any span past
`HOURLY_LIMIT_DAYS=40` to `'1d'` with no special case, and a live probe through this
repo's own `fetch_bars` at 1100 days returned 755 daily closes with zero nulls.

The STORAGE cost lands somewhere with a quantified docstring, which the previous
draft left stale. `sync.py:85-92` justifies taking a full snapshot on every changed
sync by naming what cannot be refetched: "1,816 `price_bars` rows of which 329 are
hourly option bars the README says cannot be backfilled at any price, plus
`market_events` and `watchlist`". Adding ~755 daily rows per watched symbol multiplies
the re-fetchable half several times over, so the sentence would end up describing a
backup that is mostly re-fetchable daily closes while claiming to protect the
perishable ones. Slice 1 re-measures both halves after the backfill and restates that
docstring, so the asymmetric-retention argument still reads true; the perishable
count does not change, which is the point worth stating.

### How are stored closes read, given `price_bars` is keyed on conid and the watchlist reads by symbol?

**A new `bars.watch_closes(conn, symbol, *, sessions)` returning `(et_day, close)`
newest first, one row per ET trading day, collapsing duplicates to the
highest-ranked source (the `_RANK_CASE` expression at `bars.py:151`) and
tie-breaking on the later `ts`. `serialize.watchlist_data` stops issuing its own
SELECT. The comment at `bars.py:364-372` is rewritten in the same diff.**

This is a live defect, not a preparation. NVDA holds two conids for the same days, 41
rows under 4815747 and 43 under the synthetic `watch:NVDA` key, with 39 ET days
present under both and identical closes. The serializer's query
(`serialize.py:781-786`) therefore returns adjacent duplicate days, so its 21 closes
span 12 sessions and realised vol reads 30.15% where 21 real sessions say 40.71%.
Every inserted duplicate is a zero-change day, and EMAs plus a Wilder RSI are far
more sensitive to those than a standard deviation is. The rule is the one the README
already blesses ("Two daily series from the same source are joined on the ET trading
day, not the timestamp") and it belongs in the `bars.py` reader beside
`close_series`, because it is journal shape plus the clock.

The comment beside the synthetic key currently reads "the real conid's rows arrive
alongside and the symbol lookup finds both, which is why this is `setdefault`-like
rather than a rewrite: the real id wins nothing and loses nothing". That is the
measured defect written down as a reassurance, and this repo's line is that stale
prose is a trap ("Prose is not a guard, `conid` sat on the seam for four months with
the reason written beside it"). It is replaced by the invariant that now holds: the
symbol lookup finding both conids is exactly what made a session count twice, and
the reader collapses on the ET trading day and prefers the higher-ranked source, with
a pointer to `watch_closes`.

### Does the existing realised vol number change meaning when the read window widens?

**No. `serialize.watchlist_data`'s parameter is renamed `lookback` to `sessions=21`
for the vol window, the read widens separately (`history=520`), and `realised_vol`
keeps being computed over its own 21-session slice.**

Fetching more closes and handing all of them to `realised_vol` would silently move a
figure the README's whole watchlist story is built on, and no test in the suite would
call that out as a change. Its value does change for NVDA, but only because the
duplicate sessions are the defect being fixed, and that change has its own named
test.

### Which HTTP surface carries which figure?

**Split by cost, not by tab. Everything derived from stored closes rides in the
existing `/api/state` watchlist rows. The company name rides in the existing
`/api/quotes` reply. No new endpoint.**

`web.py:620-664` writes the rule down: `/api/quotes` is separate because "putting it
there would spend N requests on every page load, every tab switch and every month
filter, for a column only one tab shows". Derived figures spend nothing, and the CLI
needs the identical numbers from the same serializer, so they belong in the payload.
The name is already in the reply the page fetches on first view, so it costs no
second per-symbol call, which is a design the current architecture explicitly
rejected.

### Where does the selection live, what happens at each edge, and can it be driven from the keyboard?

**`S.wsym` in the URL hash, validated in `applyHash` and healed in `draw()` against
`State.watchlist` before `syncHash`. The symbol cell holds a real
`<button data-wsel="SYM">` whose accessible name is the symbol plus the company
name, and the whole row delegates click to the same handler, so Enter and Space come
free. Arrow-key movement between rows is deliberately not built. First render selects
the first row of the currently visible filtered and sorted list, symbol-ascending by
default. Re-clicking the selected row does not clear it. A filter or search change
re-derives the selection from the filtered set. A filter matching NOTHING leaves
`S.wsym` alone. A removal reloads the payload and the heal picks the first visible
row; removing the last symbol falls back to the empty state, which still renders the
add form. Tab switch and reload both survive. Filters and sort are NOT in the hash.**

Three precedents exist with three answers, so the hash question is chosen rather
than inherited. In the hash because which drill-down is open is navigational
(`page.html:940-947`, the calday/replay argument) and a link to "DELL on the
watchlist" is worth carrying; healed before `syncHash` so the address bar never
carries a symbol the page cannot open (the `S.replay` discipline at
`page.html:3471`). Not a toggle, following Market (`page.html:963-968`): clearing
would leave the detail pane empty with no way back. Re-derived on filter change
because keeping an off-screen selection is the defect `page.html:1820-1824` records
fixing for `#calday`. Symbol-ascending default because it matches `watchlist_data`'s
own `ORDER BY symbol` and the CLI, so page and terminal agree on "first", and because
defaulting to a derived column (as the mockup's IVR-descending does) would make which
row opens depend on a number that is absent for five of six real symbols.

Keyboard reach is not optional here, by this page's own standard: `statCard` carries
`tabindex` because "a tooltip reachable only by mouse is still hidden from anyone
driving the page from the keyboard", and this plan rejects the mockup's icon-only
refresh partly because a control needs an accessible name. A master-detail table
whose master is mouse-only fails the same test. A real `<button>` rather than
`tabindex` on the `<tr>` because it gets the name, the role, the Enter/Space handling
and the global `:focus-visible` ring (`test_web.py:3437`) without inventing any of
them.

The zero-match case is a state, not an edge to be discovered at runtime: a non-empty
watchlist with an empty filtered set has no first row, and the likely accidental
implementation is a blank `<tbody>` beside a detail pane reading from `undefined`,
which is precisely what `sweep.check_no_junk_bindings` fails on. So both halves are
specified. The table renders one `colspan` row of prose naming which control is
hiding everything, with a "clear filters" button beside it (`data-wclear`). The
detail pane renders a `.wnone` block ("nothing to show: 12 symbols are hidden by the
BXTRENDER filter") rather than a half-populated card. `S.wsym` is left standing so
clearing the filter restores the previous selection. A search string matching nothing
takes the same path, naming the search box.

### How does sorting behave?

**`S.wsort` is a single string, `"column:dir"`. All five columns are sortable. A click
on the already-sorted column reverses it. A click on a new column starts descending
for the four numeric ones and ascending for symbol. Nulls sort last in BOTH
directions. The header glyph is an inactive double arrow unless sorted, and
`aria-sort` is `ascending`, `descending` or `none` to match.**

The mockup shows four inactive double-arrow glyphs and one active single arrow on the
sorted column, so two states exist in the drawing and the previous draft specified
one. Descending-first for a derived number is what a reader wants (the interesting
end of B-Xtrender or RVR is the top), ascending-first for a symbol is alphabetical,
and stating it per column type means the rule is not rediscovered per header. Nulls
last in both directions because a symbol holding no B-Xtrender must never top an
ascending sort: an absent figure is not a small one. One test asserts the reverse
actually reverses, because a sort that only ever ascends passes any test that checks
order once.

### What does the panel do at narrow widths, and how does the table pane behave?

**`.wsplit` is `grid-template-columns: minmax(0,1fr) minmax(0,1fr)` with a 14px gap,
which is the mockup's own proportion at the width this page actually has. At or below
1180px (an existing breakpoint, `app.css:439`) it collapses to one column with the
detail card below the table, and the `.wide`/`.tall` tile spans reset to auto. At or
below 760px `.filters.wf` collapses to one column via its own entry inside that
media query. The table pane is a scroll region: `.wtpane{max-height:620px;overflow-y:
auto}` with the measured figure written into the CSS comment, a sticky `thead`, and
the platform scrollbar left alone.**

A card is 1436px at the widest whatever the window (`app.css:296` caps `.wrap` at
1480px with 22px padding, measured in a browser at a 1600px viewport), while the
mockup's panes are roughly 850 plus 860. That is 50/50, so the RATIO is kept and only
the absolute width differs; the previous draft's `minmax(0,460px)` master was too
narrow for five columns plus a company name and would have squeezed the numeric
columns the tabular-nums alignment depends on. Moving the `.wrap` cap would change
every other tab, which is page-wide and out of scope.

The symbol cell gets a truncation rule regardless, because "CrowdStrike Holdings,
Inc." beside a symbol will exceed any share of the pane once stacked: `min-width:0`,
`white-space:nowrap` and `text-overflow:ellipsis`, which is the idiom `.mkdot` already
uses at `app.css:632-633`, with `title` carrying the full name. A test asserts the
cell carries all three, since any one of them missing silently disables the other
two.

Stacking detail below master is not a fallback, it is the page's only master-detail
shape: Calendar, Market and Replay all do it. `.duo` is not a candidate: it is a
fixed 50/50 flex with `flex-wrap:nowrap` that never stacks. The `.filters.wf` media
entry is called out because at specificity (0,2,0) it beats the 760px query's
`.filters` rule at (0,1,0) regardless of order, so without its own entry it silently
never collapses.

The scroll region is genuinely new: `grep -c max-height` and `grep -c overflow-y` over
`static/app.css` both return 0 today, so nothing is being "kept". The cap exists so
the two panes end level, which is the mockup's shape, and the number comes from
measuring the detail card's rendered height in a browser at a 1600px viewport, the
same way the `.wrap` cap was measured; 620px is the working value and the measured one
goes in the comment. `thead` is sticky (`position:sticky;top:0;background:var(--panel)`)
because a body scrolling under a static header loses the column names, which is worse
than not scrolling. The scrollbar is the platform's, deliberately: `app.css:254-257`
already sets `color-scheme` so the UA thumb picks up the dark ground, while a themed
thin scrollbar needs two colour names the palette does not have and a `::-webkit-`
pseudo-element no existing test polices. The mockup's brass thumb is listed below as
not built.

### Where does the meter's and the ring's arithmetic live?

**In a new `src/optjournal/static/watch.js`, as pure functions with a
`tests/frontend/watch.test.mjs` node suite. This reverses the previous draft, which
kept them in the page beside the markup on the performance chart's precedent.**

The previous draft's guards were a source grep for the literals `-50` and `+50` plus
the sweep's junk-text check, and neither reaches the arithmetic: a grep passes if the
string sits in a comment, and the rendered text contains no `undefined` or `NaN`
whatever the geometry says. So an inverted knob, a clamp to 0..100 or a dasharray
computed against the wrong domain would all ship green. `README.md:104` already owns
the answer: a function belongs in a static module "if it takes data and returns
data", and `meterKnob(value)`, `ringArc(rank, radius)`, `ringPoint(pct, radius)`,
`shownPrice(w, quote)` and `bxDirection(delta)` are all data in, number out. The
performance chart's precedent does not apply to them; it applies to markup assembly,
which stays in the page where the DOM is.

The cost is honest and small: `tests/test_frontend.py` currently hardcodes one
module, so its existence check and its `_BROWSER_ONLY` parametrisation are widened to
a `MODULES` tuple in the same diff, and `tests/test_readme.py:38` requires a module
table row because it globs `static/*.js`. What that buys is the first executed test
of any watchlist arithmetic, including the one rule a rewrite could silently drop
(see the price derivation below).

### How are the meter and the ring drawn?

**Inline SVG positioned by geometry attributes and coloured only by class. The meter
is a three-stop `linearGradient` track (`--badfill1` to `--bg2` to `--okfill1`) with a
centre tick and a knob, its scale ends fixed at the literal -50 and +50. The ring is
three elements in this order: a full-circle track, the value arc, and a threshold
tick, drawn from 12 o'clock clockwise, with the tick's position derived from the same
`RVR_MID` constant the filter chips use.**

`style="..."` attributes are forbidden anywhere in the page (`test_web.py:1142`), so
a knob cannot be positioned by inline CSS; SVG geometry attributes are the page's
existing answer (the performance chart at `page.html:1684` and the replay chart both
do it). Colour literals in attributes are the specific defect `test_web.py:3409`
exists for: the performance chart once hardcoded `#0a0806` in a stroke attribute and
wore Leather's background on every theme. A presentation attribute cannot hold
`var()`, so the gradient's stops are coloured from CSS (`.wmeter stop.lo{stop-color:
var(--badfill1)}` and so on) and carry no `stop-color` attribute at all, which a test
asserts directly.

The gradient is in the drawing and it earns its place: it is what makes the knob's
position readable without reading the number. The fixed scale is a real statable
bound rather than a choice: the output is RSI minus 50, so it lives in [-50, +50] by
construction, and three years of TSLA used only [-35.6, +41.6] of it. Auto-scaling to
the visible rows would make the knob position mean something different per refresh.
The ring needs its track (an arc with nothing behind it cannot be read as a
proportion) and its tick (the picture and the filter must agree about where 50 is),
and the start angle and direction are written down because "clockwise from the top" is
not implied by a dasharray.

### Which palette names does the new chrome use?

**Existing ones, all of them, and no new name is needed. Meter track
`--badfill1`/`--bg2`/`--okfill1`, its centre tick `--dim2`, its knob `--ok` or
`--bad` by sign. Ring track `--line2`, arc `--chartline`, tick `--dim2`. Bucket dots
`.wdot.lo` `--imphi`, `.wdot.mid` `--dim2`, `.wdot.hi` `--ok`. Selected row
`--seg1`/`--seg2` with `--accentedge` for the left accent, row hover `--bg2`. If a
later change does need a new name, it goes into `:root` and Leather, Admiralty and
Ledger in the same diff, with the distinctness and contrast tests run in that diff.**

`README.md:816-820` states the failure mode: "a block missing a name inherits it from
`:root`, which renders one theme's chrome on another's ground, silently and only on
the panels that use it". Naming the variables in the plan is what makes that
checkable before the diff exists. The selected-row names are the ones
`.seg button.on`, `.chip.on` and the selected calendar day already share, so a
selected row reads as the same family of selection the rest of the page uses. The dots
get their own `.wdot` class rather than reusing `.mkimp`: `.mkimp` means the calendar
feed's impact grade, and a class whose name describes a different concept is the kind
of small lie that outlives the person who wrote it.

### What tells the reader which row is open?

**`tr.wsel` plus `aria-current="true"`, one rule for the tinted ground and an inset
left accent, and a hover rule so the row reads as clickable.**

The mockup's brass left bar plus lifted ground is the only thing on screen tying the
left table to the right pane, and there is nothing to inherit: a grep over
`static/app.css` and `page.html` for `tbody tr`, `tr:hover`, `tr.on`,
`aria-selected` or `aria-current` returns nothing, so a master-detail table row is a
shape this stylesheet has never had. The accent is `box-shadow: inset 3px 0 0
var(--accentedge)` on the first cell rather than a `border-left`, because a border
shifts the row's content by 3px when it is selected and the whole table then twitches
per click. Every rule is scoped to `.wtab` so it cannot repaint the eight other
tables on the page. `aria-current` because a tint is not available to a screen
reader, and one test asserts exactly one row carries both for a given `S.wsym`.

### How is the detail pane laid out?

**Its own grid, `.wdet`, not `.stats`: three columns (`1fr 1fr 1.1fr`), the
B-Xtrender daily tile spanning columns 1 to 3 (`.wide`), the ring tile spanning rows
1 to 2 in column 3 (`.tall`), and EARNINGS and BXTRENDER WEEKLY in row 2. Tiles come
from a new `wtile(label, value, opts)` helper emitting `.stat.wtile`, so the padding,
radius, ground and border names are `.stat`'s, with a label-left value-right header
row, an optional delta slot and an optional children slot for the meter.**

`.stats` cannot express this: it is `display:flex;flex-wrap:wrap;--sw:18%` with
`flex:1 1 var(--sw)` (`app.css:437-439`), which lays equal tiles across one line, and
`statCard` (`page.html:1366-1371`) emits label-above-value with `.stat .v` fixed at
25px mono left-aligned and has no slot for a delta beside the value, no slot for a
meter under it, and no way to make one tile wider than its neighbours. Reusing
`.stat`'s own names for the box means the class-contract tests (`test_web.py:1209`
every class has rules, `:1228` every rule is reachable) stay satisfiable without
duplicating a padding value, and `statCard` stays the dashboard's, unchanged.

### Where does the shown price come from, on BOTH surfaces?

**One helper. `shownPrice(w, quote)` in `static/watch.js` returns `{price, live,
chg1}`, and the table cell and the detail header both call it. 1d comes from
`quote.previous_close` whenever a live quote is shown, and from the bars otherwise.
The `*` stale marker and its title come from the same result.**

This is the one rule in `watchlist()` that a measured defect produced, and the
previous draft's slice 5 did not carry it forward. `page.html:3192-3200` records it:
"showing the fetched quote (773.26) next to a change computed from stored bars
(-0.16%) put two DIFFERENT sessions in one row, because the bars ended Thursday and
the quote was Friday's". Nothing in the suite would have noticed its loss, either:
`previous_close` appears in `tests/test_web.py` only at `:1036`, as a payload key
name, never as an assertion about how the page computes the change.

Putting the derivation in one pure function fixes two things at once. It makes the
rule executable (node tests: quote present so `chg1` comes from `previous_close`, no
quote so both fall back to the bars, quote with a null `previous_close` so it falls
back too), and it makes the detail header structurally incapable of contradicting the
row it was opened from. The mockup prints "DELL 484.60" in both places, and a panel
reading `w.last` beside a row reading `qs[sym].price` is the strongest rule in this
codebase broken, not a cosmetic slip. A source assertion additionally pins that both
call sites use the helper.

### What is the panel's outer composition, and where do today's header items go?

**One card, deliberately, with the filter row nested inside it. `.crow` is regrouped
into exactly TWO flex children: a title cluster (`<h2>Watchlist</h2>` plus a BETA
`.pill.gap`) and an actions cluster (the add form plus a labelled Refresh
`.btn.sm`). The "N symbol(s)" pill is retired and its information moves into the
filter row as "showing 7 of 12", which is also the zero-match message's home.**

The mockup floats the heading above the cards and makes the filter row its own
bordered surface. This page's convention is one card per panel, and the Calendar
already nests a filter block (`.mkfilter`) inside its card, so following the mockup
here would make the Watchlist the only tab whose heading sits outside a card: a
page-wide visual decision taken on one tab, which is how a UI stops looking like one
idea. The regroup is needed regardless of the mockup: `.crow` is
`justify-content:space-between` with a two-child assumption, and today's four
children are why the symbol-count pill floats to mid-header.

Retiring the count pill rather than hiding it is the honest option once a filter
exists: "12 symbol(s)" beside a table showing 7 rows contradicts itself, and the
Market tab already prints "N hidden by the filter" (`page.html:3140`) for the same
reason. The add form stays in the header because it is an action, and it stays
rendered in the empty state, which is what `page.html:3170` argues for.

### What does the EARNINGS filter do to a row with no date?

**Keeps it. The chip reads "exclude <= 28d" and its title says the filter only
excludes dates you recorded, because an unknown date is not a known-clear one. The
chip is disabled with a title when no row carries a date at all.**

Sparse nulls are the normal state here, not an edge: `earnings_on` is typed, so most
rows will have none for a long time. Both readings of the chip are defensible and
they differ by the whole table, so the rule is stated rather than discovered. Keeping
nulls is the one that cannot mislead: dropping them would present a filtered list as
"nothing here reports within 28 days" when the truth is "nothing here reports within
28 days that you have told me about". A test over a mixed set (one inside 28 days,
one outside, one null) pins it so it cannot drift.

### How is freshness stated, given the tab carries three different ages?

**`quoteNote()` stays the one freshness sentence for prices, one per table, keeping
its exact vocabulary. The derived figures carry `closes_through`, the ET day of the
newest close used, on their own tiles. There is no single "Updated" footer. The
existing realised-versus-implied attribution and the "run `optjournal bars`"
remedy are carried forward explicitly.**

One stamp over a live quote, a stored daily close and a vol computed over 21 of those
closes claims a freshness the columns do not share, which is precisely the failure
`quoteNote()` (`page.html:3237-3263`) and the `*` stale marker were both built to
prevent. `quoteNote` deliberately prints the DATE past six hours rather than "22h
ago", and `test_web.py:3929` pins its vocabulary against `ago()`'s by asserting both
say "just now", "m ago" and "h ago", so it must remain a top-level function with
those phrases. `closes_through` is one new key that serves the `*` marker's title, the
vol, both B-Xtrender arms and the rank.

The two sentences at `page.html:3230-3239` matter more after this change, not less,
and nothing in the suite pins either today. "realised vol is what the stock DID, not
what the market charges for what it might do, implied vol needs an option chain this
journal cannot reach" is what stops a reader importing the mockup's meaning onto a
0-to-100 ring sitting where IV RANK was drawn, so it stays as the panel footer beside
the ring's caption and gains one assertion. "no vol yet for X: run `optjournal
bars`" is, with the window widened to 1100 days, the only instruction that turns the
new dashes into numbers, so it stays as a footer line naming the thin symbols and its
sentence is repeated in the per-cell dash titles.

### Does the note become editable in the panel?

**Yes, and `web._watchlist_write` gains key-present semantics: a field absent from the
body is left alone, a field present and empty is written NULL. `earnings_on` gets the
same treatment plus a YYYY-MM-DD format check rejected with kind `"date"`. The editor
is a `<textarea id="wnote">`, and `preserveInputs`/`restoreInputs` widen from `#body
input` to `#body input,#body textarea`.**

Today `str(note) if note else None` maps an empty string to None, which the COALESCE
reads as "keep the old value", so a note can be set and never cleared. Key-present
semantics preserve the documented behaviour that a bare re-add does not blank a note
(the add form sends no `note` key, which is what `test_web.py:1517` asserts) while
making editing possible. The editor belongs in the panel by the page's own rule:
`page.html:3170` argues that an empty state which only tells you to run a CLI command
is a dead end when the control could be right there, so a panel reading "no note on
DELL yet" without a field to write one would repeat the defect it is replacing. The
date check is a format check only, in the spirit of `_SYMBOL_OK`, which deliberately
does not validate against a ticker universe.

The `preserveInputs` widening is not incidental: the helper reads `#body input` only
(`page.html:3617`), so a textarea's contents, focus and cursor are destroyed by any
redraw, and `loadQuotes()` calls `draw()` on its own. That is the exact defect
`test_web.py:1579` exists for, and its assertion is that the helpers stay generic
rather than naming one field, which one extra selector satisfies.

### Does `render_watchlist` and the CLI keep pace?

**Yes, and `render_watchlist` gets its first tests ever: header list, body rows and
the align string change together, plus a populated case built with the real
serializer and an entry in the empty-payload parametrisation at
`test_render.py:136`. `cmd_watch` gains `--earnings` and `--clear-note` and its help
text stops claiming only "prices, realised vol".**

A grep for `render_watchlist` across `tests/` returns nothing, and `render.py:489-493`
holds a literal header list beside an align string `"<>>>>><"`: a header list shorter
than the row prints a misaligned table and nothing fails. The README states the rule
and the cost of breaking it: every payload consumer is bound to its producer by a
test, because the one that was not shipped broken (`orders` and `history` both died
on `float(dict)` behind a green suite).

### Does the demo need watchlist rows?

**Yes, seeded in slice 3: NVDA and SPY plus one deliberately barren symbol.**

`grep -c watchlist` over `demo.py`, `test_demo.py` and `test_rendered.py` returns
zero for all three, so today the tab renders only its empty state in `serve --demo`
and in every sweep run, and the sweep is the only executed check of this tab
(`test_rendered.py` renders only the Dashboard). NVDA and SPY specifically because
they are the only symbols whose real underlying series the demo holds (1,680
underlying bars against zero option bars), so they are the only ones that can produce
an indicator offline. The barren symbol puts the dash-with-a-reason path on screen
beside a populated row. Note that demo bars only exist after `optjournal bars` has run
against the demo database (`cli.py:296`), so no test or sweep expectation may assume a
populated indicator.

### How is "no cell claims an implied vol" actually tested?

**A word-bounded negative over `code_only()`-stripped rendered labels, paired with the
positive assertions that carry the honesty: the ring's caption names its window, its
figure, its low and high and the number of windows it ranked against, and the column
header contains "realised".**

The previous draft's wording ("the strings IV and IVR appear nowhere in the tab")
cannot run: `grep -c "\bIV\b"` over `page.html` returns 2 today, and both are the
comments stating the rule the test would be defending (`:703` and `:3155`, "Labelling
realised vol as IV would fit the column and be wrong"), prose the rewrite keeps. So
the check strips comments through `conftest.code_only`, the same line the payload-read
guard draws, and word-bounds the match so `DERIVES`, `ACTIVITY` and `SURVIVES` are not
hits. The positive half matters more than the negative one: an absence grep passes
just as happily over a ring with no caption at all, which is the real risk once a
0-to-100 arc occupies the slot the mockup labelled IV RANK.

## Slices

### 1. sessions-and-a-year-of-them

`fix(watchlist): Read sessions, not rows, and keep a year of them`

**Files**: `src/optjournal/bars.py`, `src/optjournal/serialize.py`,
`src/optjournal/sync.py`, `src/optjournal/page.html`, `src/optjournal/mutate.py`,
`README.md`, `tests/test_bars.py`, `tests/test_serialize.py`

**Work**. Three changes, one of them a live defect fix.

(1) `bars.WATCH_LOOKBACK_DAYS` goes 60 to 1100 with its docstring rewritten: the old
reason ("a 20-session realised vol needs 21 closes", `bars.py:137-141`) stops being
the reason, and the new one is the weekly B-Xtrender arm's ~120 ISO weeks (~840
calendar days), with 1100 matching `SNAPSHOT_FLOOR_DAYS`. Request cost is nil (the
manifest already emits one daily request per watched symbol, `bars.py:373`;
`_bar_size_for` resolves any span past `HOURLY_LIMIT_DAYS=40` to `'1d'` with no
special case; a live probe through this repo's own `fetch_bars` at 1100 days returned
755 closes with zero nulls for GOOG and PLTR).

(2) `sync.py:85-92`'s snapshot justification is re-measured and restated in the same
diff: it currently quantifies what cannot be refetched as "1,816 `price_bars` rows of
which 329 are hourly option bars", and the backfill multiplies the re-fetchable half
without touching the perishable count. The new sentence carries the new total, the
new split, and the point that the perishable 329 is what the snapshot is for.

(3) New `bars.watch_closes(conn, symbol, *, sessions=None) -> list[tuple[str,
float]]` of `(et_day, close)`, NEWEST FIRST (`vol.log_returns`' documented input
convention, `vol.py:57-60`), one row per ET trading day, duplicates collapsed to the
highest-ranked source using the `_RANK_CASE` expression at `bars.py:151` so the reader
and the upsert guard cannot drift, tie-broken on the later `ts`. This fixes a measured
defect: NVDA holds 41 rows under conid 4815747 and 43 under the synthetic `watch:NVDA`
key with 39 ET days present under both and identical closes, so
`serialize.py:781-786`'s 21 rows span 12 sessions and realised vol reads 30.15% where
21 real sessions say 40.71%. The comment at `bars.py:364-372` is rewritten in the same
diff: its last clause ("the real id wins nothing and loses nothing") is this defect
written down as a reassurance, and the invariant now is that the reader collapses on
the ET trading day and prefers the higher-ranked source, with a pointer to
`watch_closes`.

`serialize.watchlist_data` calls it; its `lookback: int = 21` parameter becomes
`sessions: int = 21` (the vol window) with a separate `history: int = 520` read cap,
and `realised_vol` still runs over its own 21-session slice so the existing number
does not change meaning as well as value. Payload: `closes` now means distinct
sessions held, and new `closes_through: string|null` carries the ET day of the newest
close used; one `@property` line each in the `Watch` typedef (`page.html:693`). The
page's `*` marker title becomes "stored close from 2026-08-12; press Refresh for a
quote". Add mutant `watch-sessions` to `mutate.py` (the per-day collapse removed),
verified lethal against the new test before it joins the registry, with the README's
registry total updated in the same diff. One README line on watched history.

**Tests**. `tests/test_bars.py::test_two_conids_covering_one_session_read_as_one_close`
(seeds the measured NVDA shape, asserts session count and close list);
`::test_the_higher_ranked_source_wins_a_duplicated_session` (a computed bar and a
fetched bar on one day, per `marketdata.SOURCE_RANK`);
`::test_the_watch_window_covers_a_year_of_sessions` (the constant cannot be trimmed
back silently). `tests/test_serialize.py::test_a_duplicated_session_no_longer_halves_the_vol`
(the end-to-end version, realised vol read off the payload). The existing
`test_web.py` contract tests pick up the two new keys once the typedef declares them.

**Done when**. `optjournal watch` and the tab report the same realised vol for a
symbol whose bars arrive under two conids; `bars_manifest` asks for 1100 days;
`closes` and `closes_through` are on the wire, declared in the typedef and rendered in
the `*` marker's title; `sync.py`'s snapshot figure describes the database as it now
is; `uv run pytest -q` at least 817 passed and `uv run ruff check src tests cron`
clean.

### 2. trend-leaf

`feat(trend): Add the B-Xtrender leaf, gated on a settled window`

**Files**: `src/optjournal/trend.py`, `tests/test_trend.py`,
`tests/test_layering.py`, `src/optjournal/mutate.py`, `README.md`

**Work**. New leaf `src/optjournal/trend.py` importing only `math`, exactly like
`vol.py`. Constants with attribution in the docstring (algorithm by Bharat
Jhunjhunwala, IFTA Journal 2019; TradingView implementation by QuantTherapy):
`SHORT_L1=5`, `SHORT_L2=20`, `SHORT_L3=15`, `LONG_L1=20`, `LONG_L2=15`, `CENTRE=50`.
`ema(values, length)` and `rsi(values, length)`, both seeded with an SMA of the first
`length` values (TradingView's `ta.ema` and `ta.rma`), with the measurement in the
comment: over TSLA's 761 closes a 44-close window reads -1.3487 with SMA seeding
against +9.4472 with recursive seeding, a 10.80 point gap that flips the bucket,
narrowing to 0.0003 at 200 closes. `bxtrender_short(closes) = rsi(ema(c,5) - ema(c,20),
15) - 50` and `bxtrender_long(closes) = rsi(ema(c,20), 15) - 50`; the docstring states
that the RSI reads the EMA DIFFERENCE, not price, because that is the step every wrong
copy drops (a popular repo computes `ema(rsi(close,5) - 50, 3)`, a well-formed number
in the right range and the wrong indicator).

`MIN_CLOSES=35` as the mathematical floor (SMA-seeded EMA(20) yields n-19 values,
RSI(15) needs 16 inputs) and `MIN_SETTLED=120` as the gate actually enforced, with the
measurements beside it (errors of 2.2 to 9.6 points at 45 closes, a sign flip on TSLA
at 44, the long arm pegged at exactly -50.0000 from 35 to 39). `BX_OVERSOLD=-20.0` and
`BX_OVERBOUGHT=20.0` each with their measured share of sessions (9.5% and 11.1% for the
daily short arm over TSLA's 727 values, against 36.3% and 29.0% for the long arm, which
is why one band set cannot serve both), plus `BANDS_SOURCE` as a string so the page's
caption and the constants cannot drift: "Wilder's RSI 70/30, re-centred by the
indicator's own -50; 'oversold' and 'overbought' are that convention's words for below
and above the band, not this journal's view of what a share is worth". `bucket(value)
-> "low"|"mid"|"high"|None`, whose keys deliberately carry no valuation vocabulary.
`PARAMS_CAPTION` as a formatted string naming the periods, the centre and the
attribution, so the tile caption is generated rather than typed.

`"trend"` joins `tests/test_layering.py`'s LEAVES; `MAY_MODEL` is untouched at two
modules. One README module-table row, required in this diff by
`tests/test_readme.py:38`. New mutant `bx-gate` (`MIN_SETTLED` ignored, so a 40-close
symbol reports a pegged extreme), verified lethal against the new test file before it
joins the registry.

**Tests**. `tests/test_trend.py::test_the_short_arm_matches_a_hand_computed_vector` (a
fixed series with the expected value to four decimals, the hand computation in the
docstring); `::test_the_rsi_reads_the_ema_difference_not_the_price` (a series where the
correct formula and the popular wrong one disagree in sign);
`::test_the_ema_is_seeded_with_an_sma_not_the_first_value` (both seedings, the 10.80
point gap named); `::test_fewer_than_the_floor_returns_none_rather_than_a_pegged_extreme`
(34 closes to None, 36 closes to None rather than -50.0);
`::test_the_bucket_boundaries_are_inclusive_and_the_bands_are_named`;
`::test_the_band_vocabulary_is_attributed_in_one_string` (BANDS_SOURCE names both the
RSI import and that the valuation words are the convention's). `tests/test_layering.py`'s
existing `test_a_leaf_imports_nothing_from_the_package` and
`test_only_the_replay_layer_may_import_blackscholes` then cover `trend.py` with no new
test.

**Done when**. `trend.py` exists, is a leaf importing only `math`, is pinned to a
hand-computed vector, refuses to answer below `MIN_SETTLED`, and names both its band
numbers and its band vocabulary in constants. `MAY_MODEL` still reads `{"replay",
"demo"}`. No user-visible change, which is the ordering rule. Suite green, ruff clean.

### 3. derived-numbers-on-the-wire

`feat(watchlist): Put B-Xtrender, the vol rank and the company name on the wire`

**Files**: `src/optjournal/bars.py`, `src/optjournal/vol.py`,
`src/optjournal/serialize.py`, `src/optjournal/marketdata.py`,
`src/optjournal/web.py`, `src/optjournal/render.py`, `src/optjournal/cli.py`,
`src/optjournal/demo.py`, `src/optjournal/mutate.py`, `src/optjournal/page.html`,
`README.md`, `tests/test_bars.py`, `tests/test_vol.py`, `tests/test_serialize.py`,
`tests/test_marketdata.py`, `tests/test_render.py`, `tests/test_demo.py`

**Work**. `bars.weekly_closes(conn, symbol) -> list[tuple[str, float, int]]` of
`(week_key, close, sessions)` oldest first, bucketing the deduplicated ET-day series
by ISO week (Monday start, matching TradingView's weekly bars for US equities) and
taking the week's last session's close; no gap filling and no holiday calendar,
because the sessions present in the data are the definition (the same principle the
perishable audit's holiday oracle uses). Measured: 755 daily closes aggregate to 158
ISO weeks, the current bucket holding 3 sessions and prior ones 5.

`vol.realised_vol_series(closes, *, window=21, history=253) -> list[float]` (newest
first, matching the module's input convention) and `vol.rank(series) -> float | None`,
the MIN-MAX position `(now - lo) / (hi - lo) * 100`. Both belong in `vol.py` because
they are realised vol's own distribution and they keep the module importing only
`math`. `RANK_MIN_WINDOWS = 120` with the reason in the comment (the denominator is a
max minus a min, both single observations, so it needs enough windows for the extremes
to be plausible bounds, and 120 matches `trend.MIN_SETTLED` so the tab's four derived
figures appear together rather than lighting up in stages). A degenerate year where
`hi == lo` returns None, never 50.0: a flat year is a claim, not a midpoint.
`RANK_MIDPOINT = 50.0` and `RANK_BANDS_SOURCE` ("the middle of this symbol's own year
of realised vol; IV rank's conventional 30 is calibrated on implied vol across a
different population and is deliberately not carried over"), with the measured share
of windows above the midpoint over a real symbol's fetched year written into the
constant's comment during this slice.

`serialize.watchlist_data` gains per row: `bx_daily`, `bx_daily_delta` (value[-1] minus
value[-2], one extra session), `bx_bucket` (computed server-side from `trend`'s
constants so the cut points live once beside their percentiles), `bx_weekly`,
`bx_weekly_week`, `bx_weekly_sessions`, `weeks`, `rv_rank`, `rv_rank_low`,
`rv_rank_high`, `rv_rank_windows`, `rv_rank_band` (`"upper"|"lower"|null`, computed
server-side like `bx_bucket`). All nullable, all None rather than 0.0, each paired with
the count or the pair of bounds that explains its dash.

`marketdata.Quote` gains `name: str | None`; `parse_quote` reads meta `longName`
falling back to `shortName` (live probe: present for all seven probed symbols including
the ETF, DELL's value byte for byte the mockup's string, at zero extra request cost
since `fetch_quote` already hits that URL). `web._quotes` emits `name`; the `Quote`
typedef (`page.html:873`) gains one `@property` line. `render.render_watchlist` gains
the new columns with its header list, body rows and align string changed together
(`render.py:489-493`). `cli.cmd_watch`'s help stops claiming only "prices, realised
vol" (`cli.py:950`). `demo.py` seeds the watchlist with NVDA and SPY (the only symbols
whose real underlying series the demo holds) plus one deliberately barren symbol so the
dash-with-a-reason path renders beside a populated row. New mutant `rank-degenerate` (a
flat year returns 50.0 instead of None), verified lethal, README total updated.

**Tests**. `tests/test_bars.py::test_a_week_is_its_last_session_and_a_short_week_is_still_a_week`
and `::test_the_current_week_reports_how_many_sessions_are_in`.
`tests/test_vol.py::test_the_rank_is_a_min_max_position_in_the_symbols_own_year` (hand
computed from a series whose low, high and current are known),
`::test_a_flat_year_ranks_nothing_rather_than_fifty`,
`::test_a_quarter_of_a_year_of_windows_ranks_nothing`, and
`::test_the_rank_band_source_refuses_iv_ranks_thirty` (the constant says so, so nobody
re-imports it); the existing imports-only-math assertion at `test_vol.py:138` covers
both new functions for free. `tests/test_serialize.py`: a seeded 200-session series
producing non-null `bx_daily`, `rv_rank`, `rv_rank_low`/`_high` and `rv_rank_band`, and
a 45-session series producing None for all of them with the counts that say why (the
honest state of five of six real symbols today).
`tests/test_marketdata.py::test_a_quote_carries_the_company_name` plus the `shortName`
fallback and the None case. `tests/test_render.py`: the first cases `render_watchlist`
has ever had, built with the real serializer over a seeded watchlist, plus an entry in
the empty-payload parametrisation at `test_render.py:136`. `tests/test_demo.py`: the
demo seeds watched rows and the barren one reports None rather than 0.0 on a fresh
demo. `test_web.py`'s contract tests cover the new `Watch` and `Quote` keys both ways
once declared (the `Watch` sample is already anchored to the TSLA row at
`test_web.py:94` and `:367`).

**Done when**. `optjournal watch --json` and `/api/state` carry every derived figure;
the terminal table prints them with a test behind it for the first time; a quote
carries a company name at no extra request; every figure is a dash with a count on a
thin symbol; `optjournal serve --demo` shows a populated Watchlist tab in the sweep.
Suite green, ruff clean.

### 4. user-entered-facts

`feat(watchlist): Record an earnings date, and let a note be cleared`

**Files**: `src/optjournal/db.py`, `src/optjournal/web.py`, `src/optjournal/cli.py`,
`src/optjournal/serialize.py`, `src/optjournal/render.py`,
`src/optjournal/page.html`, `tests/test_db.py`, `tests/test_web.py`,
`tests/test_serialize.py`, `tests/test_render.py`

**Work**. `db.SCHEMA_VERSION` 8 to 9. `watchlist` gains `earnings_on TEXT` in
`_SCHEMA` AND an `("watchlist", "earnings_on", "TEXT")` entry in `_ADDED_COLUMNS`
(`db.py:62`), because `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table and
every journal on disk already has this one; nothing goes in `_LATE_INDEXES` since there
is no index on it. The column belongs here because `db.py:346` says `watchlist` is the
user-input table, and a typed date is user input; no derived or fetched figure gains a
column.

`web._watchlist_write` learns key-present semantics: a field absent from the body is
left alone (which preserves the documented "a bare re-add does not blank a note"
behaviour, since the add form sends no `note` key), and a field present and empty is
written NULL. This closes a real hole: today `str(note) if note else None` maps an
empty string to None, which the COALESCE reads as "keep the old value", so a note can
be set and never cleared. `earnings_on` is validated as YYYY-MM-DD and rejected with
kind `"date"` otherwise, a format check only, in the spirit of `_SYMBOL_OK` which
deliberately does not validate against a ticker universe.

`serialize.watchlist_data` emits `earnings_on` and a derived `earnings_in_days: int |
null` computed against `clock.et_day` (already imported) and never stored, so it cannot
drift from the date beside it; `watchlist_data` takes an optional `now` so the test does
not depend on the calendar. A past date reports a negative count and the surfaces render
it as the date plus "recorded, now past" rather than a negative countdown.
`cli.cmd_watch` gains `--earnings YYYY-MM-DD` and `--clear-note` through the same SQL;
`render_watchlist` gains the earnings column. `page.html` gains one `<td>` in today's
single-table watchlist plus the `Watch` typedef lines, so the tab is usable with this
slice shipped and the rewrite unshipped.

**Tests**. `tests/test_db.py::test_a_pre_migration_journal_gains_the_earnings_column`,
over a journal created at the older schema. `tests/test_web.py`: a bad date rejected by
kind (extending the parametrised `test_the_watchlist_endpoint_refuses_a_bad_request` at
`:1498`); a note cleared by an explicit empty string; a bare re-add still not blanking a
note, which is `:1517`'s existing assertion and must stay green.
`tests/test_serialize.py::test_the_countdown_is_derived_from_the_date_and_the_et_day`,
including the past-date case. `tests/test_render.py`: the earnings column in the
terminal table, dash and value.

**Done when**. `optjournal watch DELL --earnings 2026-08-27` round-trips; the countdown
appears in both the terminal report and the page; a note can be cleared and a bare
re-add still cannot blank one; a malformed date is refused with a reason; a
pre-migration journal gains the column on the next open. Suite green, ruff clean.

### 5. two-pane-panel

`feat(watchlist): Rebuild the tab as a selectable list beside a detail pane`

**Files**: `src/optjournal/static/watch.js`, `src/optjournal/page.html`,
`src/optjournal/static/app.css`, `README.md`, `tests/frontend/watch.test.mjs`,
`tests/test_frontend.py`, `tests/test_web.py`

**Work**. New pure module `src/optjournal/static/watch.js`, exporting
`shownPrice(w, quote)` returning `{price, live, chg1}` and `bxDirection(delta)`
returning `"up"|"down"|"flat"|null`. It takes data and returns data, which is
`README.md:104`'s rule for what lives here. `tests/test_frontend.py`'s module
existence check and its `_BROWSER_ONLY` parametrisation widen from one path to a
`MODULES` tuple in the same diff, and `README.md`'s module table gains a row (required
by `tests/test_readme.py:38`, which globs `static/*.js`).

Replace `watchlist()` (`page.html:3157`) with a composition: `watchlist()` builds the
card header, `watchTable(rows)` and `watchDetail(row)`. `quoteNote()` is untouched and
stays the tab's one freshness sentence, one per table, keeping the phrases
`test_web.py:3929` pins by `_fn("quoteNote")`. The two footer sentences at
`page.html:3230-3239` are carried forward verbatim in meaning: the
realised-versus-implied attribution as the panel's footer, and the "no vol yet for X:
run `optjournal bars`" remedy naming the thin symbols.

Header: `<h3>` becomes `<h2>` (`app.css:410` styles `.card h2`; nothing styles h3, so
today's title wears the user agent's size and 1em margins), a BETA `.pill.gap` beside
it (`app.css:463`), and `.crow` regrouped into exactly TWO flex children (title
cluster, actions cluster holding the add form and a labelled Refresh `.btn.sm`),
because `.crow` is `justify-content:space-between` with a two-child assumption and
today's four children are why the symbol-count pill floats to mid-header. The count
pill is retired; slice 6's filter row carries "showing N of M" instead.

`S` gains `wsym` and `wsort`, each with its reason beside it in the `S.marketday`
voice (`page.html:963`). `applyHash` reads `wsym`; `draw()` heals it against
`State.watchlist` before `syncHash`, following the `S.replay` discipline.
`selectWatch(symbol)` sets `S.wsym` and calls `draw()` with no request.

`watchTable(rows)`: five columns (symbol plus company name, close, B-Xtrender, RVR,
earnings), `<table class="wtab">` inside `<div class="wtpane">`. The symbol cell holds
`<button data-wsel="SYM">` whose text is the symbol and, once quotes have arrived, the
company name, truncated with the `min-width:0` plus `white-space:nowrap` plus
`text-overflow:ellipsis` trio `.mkdot` already uses (`app.css:632-633`) and a `title`
carrying the full name. The whole row delegates click to the same handler, so mouse
and keyboard reach the same code. The selected `<tr>` carries `class="wsel"` and
`aria-current="true"`. Sorting: `S.wsort` is `"column:dir"`, a click on the sorted
column reverses, a click on a new one starts descending for the four numeric columns
and ascending for symbol, nulls last in BOTH directions, `aria-sort` set to match and
a text glyph that is an inactive double arrow unless sorted. Numeric cells use `n`
(`app.css:609`) and sign colours use `cls()` (`page.html:1089`) instead of today's `r`
and `g`, which match no rule at all and render left-aligned in plain cream (the
class-contract test passes because `_css_classes()` regexes tokens out of whole
selectors, so `.day.g` donates `g` and `r`). The B-Xtrender cell renders the value plus
a direction glyph from `bxDirection(w.bx_daily_delta)` with a `title` naming the delta,
which is the published indicator's second state carried by a glyph rather than by a
fourth hue.

`watchDetail(row)`: a `.wdet` grid (`1fr 1fr 1.1fr`) rather than `.stats`, with tiles
from a new `wtile(label, value, opts)` helper emitting `.stat.wtile` so the padding,
radius, ground and border names stay `.stat`'s, plus a label-left value-right header
row, a delta slot and a children slot. Contents: the kicker and the symbol with its
price and name, taken from the SAME `shownPrice()` result the row uses and carrying the
same `*` marker and title when the price is a stored close; the B-Xtrender daily tile
(`.wide`, spanning both left columns, value right-aligned, delta with its glyph,
caption generated from `trend.PARAMS_CAPTION`); the ring tile (`.tall`, spanning both
rows in column 3, its arc arriving in slice 6 and its caption already stating window,
figure, low, high and window count); EARNINGS and BXTRENDER WEEKLY tiles below; a
`.stats` row of `wtile`s for 1d, 5d, realised vol with the expected move; a YOUR
POSITIONS block from `w.options`; a YOUR NOTE block with a `<textarea id="wnote">`
editor; and the "stop watching" button (the only `[data-wrm]`, so `.wrm` at
`app.css:738` stays reachable). `preserveInputs`/`restoreInputs` widen to `#body
input,#body textarea` so the editor's text, focus and cursor survive a redraw.

Layout in `app.css`: `.wsplit{display:grid;grid-template-columns:minmax(0,1fr)
minmax(0,1fr);gap:14px}`; at 1180px (existing breakpoint, `app.css:439`) one column
with the detail below the table and the `.wide`/`.tall` spans reset, which is the
page's only master-detail shape (Calendar, Market and Replay all do it; `.duo` at
`app.css:465` is a fixed 50/50 nowrap flex and never stacks);
`.wtpane{max-height:620px;overflow-y:auto}` with the measured detail-card height in the
comment and `.wtab thead th{position:sticky;top:0;background:var(--panel)}`; the
platform scrollbar left alone, with the reason in the comment (`app.css:254-257`
already sets `color-scheme`). Selection: `.wtab tbody tr.wsel{background:linear-
gradient(160deg,var(--seg1),var(--seg2))}` and `.wtab tbody tr.wsel td:first-child{box-
shadow:inset 3px 0 0 var(--accentedge)}` (inset rather than a border so selecting does
not shift the row), plus `.wtab tbody tr{cursor:pointer}` and `.wtab tbody
tr:hover{background:var(--bg2)}`, every rule scoped to `.wtab` so it cannot repaint the
page's other tables. Every absent value is an em dash with a title naming the count
("43 of 120 sessions stored", "10 of 120 weeks", "94 of 120 windows", "no earnings date
recorded for DELL; add one in the panel"), because `sweep.check_no_junk_bindings` fails
on `undefined`, `NaN` or `[object Object]` anywhere in the rendered text and the sweep
is the only executed check of this tab.

**Tests**. `tests/frontend/watch.test.mjs`: `shownPrice` with a live quote (chg1 from
`previous_close`), with no quote (both from the bars), with a quote whose
`previous_close` is null (falls back), and with neither (price null, not 0);
`bxDirection` for a positive, negative, zero and null delta. `tests/test_web.py` new by
hand: `test_the_row_and_the_panel_print_one_price` (both call `shownPrice`, asserted
over the source with the `_fn(...)` idiom at `:2586` and `:2901`, since one derivation
in two places is the defect `page.html:3192-3200` records);
`test_exactly_one_row_is_marked_current_for_a_selection`;
`test_the_selected_symbol_round_trips_through_the_hash` (applyHash and syncHash, plus
the heal running before syncHash); `test_the_symbol_cell_is_a_real_control` (a
`<button>` with an accessible name, so Enter reaches it);
`test_the_symbol_cell_carries_the_ellipsis_trio` (all three properties, since any one
missing disables the others); `test_the_sort_reverses_on_a_second_click` and
`test_nulls_sort_last_in_both_directions`; `test_no_watchlist_cell_claims_an_implied_vol`
(word-bounded over `code_only()`-stripped labels, paired with the positive assertion
that the RVR header contains "realised");
`test_the_attribution_sentence_survives_the_rewrite`;
`test_the_note_editor_is_preserved_across_a_render` (the widened selector, generic
rather than naming `#wnote`). Existing structural guards apply unedited and will reject
a careless diff: `:1209` (every class has rules), `:1228` (every rule is reachable),
`:1119` (no double class attribute), `:1142` (no inline style), `:1264` (no grid
property on a non-grid selector), `:3385` and `:3409` (no colour literal in either
file), `:3437` (focus ring), `:1579` (preserveInputs straddles the innerHTML assignment
and stays generic), `:3929` (quoteNote's vocabulary). Plus `uv run optjournal sweep` on
both journals: the tab is in `sweep.TABS` (`sweep.py:736`) so `check_page_rendered` and
`check_no_junk_bindings` apply with no edit, and slice 3's demo rows are what make the
sweep see a populated tab.

**Done when**. The tab renders two panes above 1180px and one below with the detail
under the table; the table pane scrolls under a sticky header and the panes end level;
a row selects by mouse and by keyboard, survives a reload and a tab switch via
`#wsym`, is not cleared by a re-click, and is visibly marked; sorting works, reverses,
and puts nulls last both ways; the row and the panel print the same price and the same
1d basis; the note can be written from the panel and survives a redraw mid-keystroke;
no cell anywhere says IV; the footer still says what realised vol is and what to run;
the sweep is green on both journals; suite green and ruff clean.

### 6. filters-and-gauges

`feat(watchlist): Add the filter row, the B-Xtrender meter and the RVR ring`

**Files**: `src/optjournal/static/watch.js`, `src/optjournal/page.html`,
`src/optjournal/static/app.css`, `tests/frontend/watch.test.mjs`,
`tests/test_web.py`

**Work**. `watch.js` gains the geometry: `meterKnob(value, box)` mapping [-50, +50]
onto the track with a clamp at both ends, `ringArc(rank, radius)` returning the
dasharray pair from the circumference, and `ringPoint(pct, radius)` returning the `{x,
y}` at a fraction around the circle measured clockwise from 12 o'clock, which the
threshold tick uses.

Filter row: `.filters.wf` with four tracks. SEARCH (`#wsearch`, placeholder saying
whether company search is available yet, since the name only exists after Refresh).
BXTRENDER as a `.seg` of All, "below -20", "mid", "above +20", each non-All button
carrying a `.wdot` (`.lo` `--imphi`, `.mid` `--dim2`, `.hi` `--ok`), with the caption
generated from `trend.BX_OVERSOLD`, `trend.BX_OVERBOUGHT` and `trend.BANDS_SOURCE`,
pinned to the daily short arm. RVR as a `.seg` of All, "RVR > 50", "RVR <= 50",
generated from one page constant `RVR_MID` bound by test to `vol.RANK_MIDPOINT`, with
the caption from `vol.RANK_BANDS_SOURCE`. EARNINGS as a `.chip` with `aria-pressed`
reading "exclude <= 28d", whose title states that it only excludes dates you recorded
and that a symbol with no date is kept, disabled with its own title when no row carries
a date at all. The row also carries "showing N of M", which replaces the retired count
pill. Two gotchas written into the CSS comments: the modifier must share a class with a
rule declaring `display:grid` or `test_web.py:1264` rejects it, and `.filters.wf` at
(0,2,0) beats the 760px query's `.filters` at (0,1,0) regardless of order, so the
modifier needs its own entry inside that media query.

`S` gains `wq`, `wbx`, `wrvr` and `wearn`, each with its reason beside it, and none of
them in the hash: they narrow an already-delivered list, which is the
`mkcountries`/`mkimpacts` precedent. `watchRows()` applies search (symbol always,
company name only once quotes have arrived, and the placeholder says which), then the
bucket, band and earnings filters, then slice 5's sort. A filtered set that is EMPTY
renders one `colspan` row of prose naming the control that is hiding everything with a
`data-wclear` button beside it, and the detail pane renders a `.wnone` block rather
than a half-populated card; `S.wsym` is left standing so clearing the filter restores
the selection.

The meter, inside the B-Xtrender daily tile: inline SVG, a track `<rect>` filled from a
three-stop `<linearGradient>` whose stops carry classes and no `stop-color` attribute
(`.wmeter stop.lo{stop-color:var(--badfill1)}`, `.mid{stop-color:var(--bg2)}`,
`.hi{stop-color:var(--okfill1)}`), a centre tick at zero in `--dim2`, and a knob
`<circle>` whose `cx` comes from `meterKnob()` and whose class is `pos` or `neg` by
sign. Scale ends are the literal -50 and +50, never auto-scaled. The ring, in its tile:
a track `<circle>` in `--line2`, a value arc in `--chartline` with `stroke-dasharray`
from `ringArc()` and `transform="rotate(-90 cx cy)"` so it starts at 12 o'clock and runs
clockwise, and a threshold tick from `ringPoint(RVR_MID/100, r)` in `--dim2`. The
caption beneath states the sentence: the window, the figure, the year's low and high and
the number of windows ranked against, from `rv_rank_low`, `rv_rank_high` and
`rv_rank_windows`. No verdict word. No new palette name is introduced; if one later is,
it goes into `:root` and all three theme blocks in the same diff.

**Tests**. `tests/frontend/watch.test.mjs`: `meterKnob` at -50, 0 and +50 (left,
centre, right), at +/-80 (clamped, not extrapolated), and its monotonicity in sign;
`ringArc` at 0, 50 and 100 (empty, half, full circumference) and its clamp;
`ringPoint` at 0 (top), 0.25 (right), 0.5 (bottom), so a sign error in the rotation
fails. `tests/test_web.py`: `test_the_meter_scale_is_fixed_at_the_indicators_own_bounds`
(the literal -50 and +50 reach `meterKnob`, so it cannot become auto-scaling);
`test_the_band_caption_matches_the_constants_that_produced_it` (page numbers against
`trend.BX_OVERSOLD`/`BX_OVERBOUGHT` and page sentence against `trend.BANDS_SOURCE`, the
idiom of the tab-list test at `:2445`);
`test_the_indicator_parameters_are_on_screen` (the tile caption against
`trend.PARAMS_CAPTION`);
`test_the_rvr_chips_and_the_ring_tick_read_one_constant` (both from `RVR_MID`, bound to
`vol.RANK_MIDPOINT`, so the picture and the filter cannot disagree);
`test_the_ring_caption_names_its_window_and_its_bounds`;
`test_the_gradient_stops_carry_no_colour_attribute`;
`test_an_unknown_earnings_date_is_not_excluded` (over a mixed set: one inside 28 days,
one outside, one null);
`test_a_filter_matching_nothing_says_which_control_is_hiding_the_rows` (both panes);
`test_the_search_input_carries_an_id` (or `preserveInputs`, `page.html:3617`, drops what
the reader typed on the next redraw, which is exactly what happened to `#wadd`). Plus
`uv run optjournal sweep` on both journals again, since the filter row and the gauges
are new rendered text.

**Done when**. All four filter groups narrow the list, the caption under each names
where its numbers came from, and a filter matching nothing says so in both panes; the
meter reads left to right on a fixed -50 to +50 scale with a themed gradient and no
colour literal anywhere; the ring draws a track, an arc from 12 o'clock clockwise and a
threshold tick at the same constant the chips use, over a caption stating the window,
the figure, the year's low and high and the window count; every one of those numbers is
absent as a dash with a reason on a thin symbol; the sweep is green on both journals;
suite green and ruff clean.

## Deliberately not built as mocked

- **The 84.3 IVR ring, the sortable IVR column, the "Elevated" verdict and the IV RANK
  filter band**. Built as the same ring and column filled with `rv_rank`, a realised
  vol RANK: today's 20-session realised vol as a min-max position inside the symbol's
  own trailing year of stored closes. Header reads RVR with "realised vol rank, 1y",
  the caption states the window, the figure, the year's low and high and how many
  windows it ranked against, and the mockup's two bands survive as two chips cut at
  `vol.RANK_MIDPOINT` (50), not at IV rank's 30. The verdict word does not survive: the
  slot prints the measurement in words. (Unreachable twice over, re-measured: the
  option chain, `quoteSummary` and `v7/quote` are all HTTP 401, the chart meta block's
  25 keys contain no implied vol, and even with a chain the only IV this journal can
  compute is per-contract from an option's own closes (382 bars for the held LEAP, 3 to
  9 for traded legs, zero option contracts for SPY) which drifts in moneyness and time
  to expiry. Computing it would require `blackscholes` in the watchlist path, growing
  exactly the allowlist `vol.py:22-27` names as "the expensive kind of small decision".
  And 30 is calibrated on implied vol's distribution across a different population, so
  carrying it onto a realised-vol figure would be the same defect one level up, in a
  threshold rather than in a value.)
- **"Premium selling favored" under the ring**. Built as a line stating what was
  measured. (It is advice about the price of option premium, and realised vol does not
  price premium. A position of what the stock DID cannot recommend selling what the
  market CHARGES.)
- **"VIA CLOUD" attribution under the ring**. Built as the same slot stating the real
  provenance, "from stored closes". (There is no cloud provider seam to attach it to.
  Grepping `cloud|provider|urlopen|http|api_key|token` across `src/optjournal/*.py`
  finds exactly two outbound hosts (`marketdata.py:77` and `events.py:63`), two
  dependencies, no HTTP client, no config surface, no settings table among `db.py`'s
  13, and no credential store beyond `flex.py`'s IBKR keyring.)
- **A configured-provider seam (Protocol, registry, cache table, refresh route)
  standing in for IV rank, earnings and the shared read**. Built as nothing. The shape
  is written down in the Decisions above so the reader can see what was declined. (The
  registry would ship empty. The README's calendar-feed rule says an abstraction with
  one implementation is untested by construction; zero implementations is also
  unfalsifiable. It is a credential decision, a config surface that does not exist
  here, a fetch/parse/store trio and a test suite, for a column no reader can populate.
  The condition that changes the answer is named: a second real source in prospect, the
  same bar `sources.py` cleared.)
- **The EARNINGS countdown as a fetched figure**. Built as a user-entered
  `watchlist.earnings_on` date, labelled as entered, with the countdown derived from
  `clock.et_day` at serialize time and a past date rendering as the date plus
  "recorded, now past". (No keyless source exists: the chart meta block has no earnings
  key, the endpoint's `events` parameter returns dividends and splits at every window
  tested (1mo, 1y, 2y, 5y), `quoteSummary` `calendarEvents` is 401, `api.nasdaq.com` is
  404, the keyless search endpoint has no earnings field, and `market_events` is a
  macro calendar whose country column holds currency codes with zero rows mentioning a
  ticker or the word Earnings. Filing earnings there would need an invented impact
  grade that `events.py:197-203` raises on and that `db.py:326` documents as the feed's
  judgement rather than ours. Deriving a next date from a quarterly cadence over past
  dates is a guess wearing a date's clothes.)
- **"Oversold" and "Overbought" as bucket labels**. Built as "below -20", "mid" and
  "above +20", with the convention's own words kept in the attributed caption and the
  payload keys carrying no valuation vocabulary at all. (Those two words are claims
  that a share is cheaper or dearer than it should be; an RSI of an EMA difference
  re-centred by 50 is a statement about the shape of recent closes. Attributing the
  numbers does not attribute the words, and this plan deletes "Premium selling
  favored" on the same argument. The precedent is `market_events.impact`, stored as
  "the FEED's judgement, not the journal's" and printed with that sentence beside it.)
- **The BXTRENDER column's second shade of green**. Built as a direction glyph from
  `bx_daily_delta` with a `title` naming it, beside the existing sign hue. (The second
  state is real, it is the published indicator's rising-or-falling dimension, and it
  ships. It does not ship as a fourth hue: `app.css:282-293` is this page's own rule
  that hue alone does not survive red-green colour blindness, two greens are a harder
  discrimination than green against rose, and it would cost two new palette names in
  three theme blocks then policed by the distinctness and contrast tests, for a
  distinction a glyph makes at any acuity. The mockup itself draws the detail tile's
  delta with an arrow.)
- **The "IAG READ" panel and its empty state**. Built as a YOUR NOTE panel rendering
  `watchlist.note`, with the true empty state ("no note on DELL yet") and the field
  that writes one. (A case-insensitive grep for IAG across `src`, `tests` and `cron`
  returns zero matches, and the only allowlists in the repo are static file extensions
  (`web.py:1087`) and the modelled-number quarantine. The mockup's empty state
  narrates an external service with an access tier that does not exist, which is the
  page asserting a mechanism into being. The note is per-symbol user free text that is
  already persisted, already in the payload as `Watch.note`, already upserted through
  `POST /api/watchlist`, and populated on four of the six real watched rows.)
- **The "Updated 13 Aug 2026, 13:30" footer**. Built as `quoteNote()` staying the one
  freshness sentence for prices, with the derived tiles carrying `closes_through`. (The
  tab mixes three ages: a live quote's `asked_at`, a stored daily close, and a vol over
  21 of those closes. One line above them claims a single freshness the columns do not
  share, which is the failure `quoteNote()` and the `*` stale marker were both built to
  prevent. `dayLabel` also cannot produce the mockup's stamp (it renders "13 Aug" or
  "13 Aug '26", no time), and there are three different clocks it could be naming.)
- **The per-row leading cloud icon**. Built as a held marker driven by `Watch.held`,
  titled with what you hold on the name. (It is identical on every row in the mockup,
  so it carries no information, and this page has no icon set beyond the header's two
  buttons and `mark.svg`. The same pixels can carry a fact only this journal knows.)
- **The section refresh as an icon-only button**. Built as a labelled `.btn.sm` with the
  existing `.busy` affordance, in a regrouped two-child `.crow` header. (`.icobtn`
  (`app.css:384`) is 38px and lives in the page header, and the precedent for a
  section-level action is the Economic Calendar's text button (`page.html:3133`). An
  icon-only control also needs an accessible name, which the text already is. The
  header regroup is needed regardless: `.crow` is `justify-content:space-between` with
  a two-child assumption, and today's four children are why the "3 symbol(s)" pill
  floats to mid-header.)
- **The mockup's three separate surfaces (bare heading row, filter card, two panes)**.
  Built as this page's one-card convention: heading and actions in the card's `.crow`,
  the filter row nested inside like the Calendar's `.mkfilter`, and the two panes as a
  grid inside the same card. (Every other tab on this page is one card. Floating one
  tab's heading outside a card is a page-wide visual decision taken on one tab, which
  is how a UI stops reading as one idea.)
- **The "N symbol(s)" pill**. Retired, its information replaced by "showing N of M" in
  the filter row. (Once a filter exists, "12 symbol(s)" beside a table showing 7 rows
  contradicts itself. The Market tab already prints "N hidden by the filter" for the
  same reason.)
- **The mockup's slim brass scrollbar thumb**. Built as the platform scrollbar, with
  the reason in the CSS comment. (`app.css:254-257` already sets `color-scheme` so the
  UA thumb picks up the dark ground. A themed thin scrollbar needs two colour names the
  68-name palette does not have, in three theme blocks, behind a `::-webkit-`
  pseudo-element that `test_no_colour_literal_lives_outside_a_theme_block` would police
  and no other test would exercise. The scroll region itself is built, since none
  existed: `grep -c max-height` and `grep -c overflow-y` over `app.css` both return 0
  today.)
- **The five-column table as the whole tab (dropping 5d, the expected move and "your
  options")**. Built as the mockup's five columns in the table, with 1d, 5d, realised
  vol plus expected move, and the option positions moved into the detail pane.
  (Deleting them would quietly shrink what the tab measures. `page.html:3149` calls the
  options column load-bearing in as many words: "A broker app shows a price; only this
  journal can say you are short the 520 put on it." The detail pane gives those facts
  more room than a table cell does.)
- **BXTRENDER WEEKLY as a bare value, and any B-Xtrender value on today's data**. Built
  as an em dash titled with the count held until the arm has about 120 units of its own
  granularity, then the value with its week named and its session count ("2026-W33, 3
  of 5 sessions in"). (Five of six real watched symbols hold 45 or 46 daily closes and
  10 ISO weeks. At 45 closes the newest short-arm value is wrong by 2.2 to 9.6 points
  and on TSLA a 44-close window flips the sign; at 35 to 39 closes the long arm reports
  exactly -50.0, the pegged extreme, which reads as the strongest signal on the page.
  The weekly value also repaints daily (TSLA: -19.02, -18.63, -19.71 across one week's
  three sessions), and an unlabelled figure that silently changes is the same defect as
  an undated price.)
- **An always-present company name (a cached `watchlist.name` column, or
  `securities.description` as a fallback)**. Built as `Quote.name` from the chart meta
  block, absent until Refresh, with the row falling back to the bare symbol.
  (`watchlist` is the user-input table and a name is fetched fact (`db.py:346`), and a
  cached name would then need an age story like every other cached figure here.
  `securities.description` covers one of the six real watched symbols, has zero rows
  for SPY, and spells TSLA "TESLA INC" against the live source's "Tesla, Inc.", so
  mixing them would make one column show two data qualities. The layout is designed for
  a nameless row rather than reserving space that stays blank.)
- **A wider content column to fit the mockup's two 850px panes**. Built as the same
  50/50 proportion inside the real 1436px card, collapsing to one column at 1180px.
  (`app.css:296` caps `.wrap` at 1480px, measured in a browser as a 1436px card at a
  1600px viewport. Moving the cap changes every other tab, so it is a page-wide
  decision and outside this task's scope. The ratio is what the mockup encodes; the
  absolute width is this page's.)
- **Arrow-key movement between rows**. Not built. Enter and Space on a focused row ARE,
  through a real `<button>` in the symbol cell. (A roving-tabindex list is its own
  keyboard model with its own bugs, and nothing else on this page has one. The bar this
  plan holds itself to is that the master is reachable and announced, not that it
  reimplements a listbox.)
- **Window chrome, "IAG Journal", "INVESTING AGAINST THE GRAIN", the version chip and
  the five-tab strip**. Nothing changes. Bitácora keeps its name and its nine tabs.
  (Stated in the brief: the mockup is a target for the Watchlist panel, not a rebrand.
  The tab key `watchlist` is additionally pinned in three files at once
  (`page.html:1138`, `sweep.py:736`, and tests holding the two together), so it must
  not change.)
