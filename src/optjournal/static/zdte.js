/* The 0DTE calculator's arithmetic: the strike ladder a same-day seller reads.
 *
 * A leaf on the seam every other module here sits on (see tests/test_frontend.py):
 * plain numbers in, plain data out, no DOM and no globals, so `node --test` runs
 * every figure on this tab. It lives in JavaScript rather than in Python -- where
 * it started life as `zdte.py` -- because the two inputs are TYPED. A seller
 * checks the ladder against a moving tape and overrides the close or the VIX by
 * hand, so the numbers are recomputed on a keystroke; a server round trip per
 * character, or a second copy of these formulas in the page, were the two worse
 * answers. The payload now carries only the two readings the feed supplies, and
 * this module is the one place that turns them into levels.
 *
 * Nothing here is modelled in the pricing sense (see the pricing quarantine in
 * docs/design-notes.md): every figure is the close times a percentage. The
 * percentages are either FIXED -- the 1% to 5% rails a 0DTE desk quotes as habit
 * -- or VIX read as exactly what it already is.
 *
 * WHY `VIX / 16`, AND NOT `VIX / sqrt(252)`. VIX is an annualised standard
 * deviation in percent, and volatility scales with the square root of time, so the
 * textbook one-session figure is `VIX / sqrt(252)` = `VIX / 15.87`. Desks quote
 * `VIX / 16` instead -- the same de-annualisation with the root rounded to a number
 * you can do in your head -- and that is the convention this journal follows,
 * because the figure's whole purpose is to be the one a seller is already looking
 * at. Checked against the reference implementation on two sessions: VIX 15.25
 * gives 0.9531% (16) not 0.9607% (sqrt 252), and 14.43 gives 0.9019% not 0.9090%,
 * both matching to four decimals on `/16`. The gap is ~0.8% of the figure, which
 * is invisible in a band and would still be the wrong number on a screen next to a
 * platform quoting the other one.
 *
 * The 1σ rail is a ONE-SIGMA move: about a 68% chance the close lands inside it,
 * which is the reading, not a guarantee, and the page says so.
 *
 * TWO NUMBERS PER ROW, AND THEY DISAGREE ON PURPOSE. `exact` is where the
 * percentage actually lands (7781.5009...), and `strike` is the listed contract
 * nearest it (7780). Every row therefore carries `points` and `pct` measured from
 * the STRIKE, not from the rail: what a seller is choosing between is strikes, so
 * "how far out is this contract" has to be answered about the thing that can be
 * sold. The 3% rail on a 7706.03 close reads 7475 at 2.998%, and printing 3.00%
 * against a strike that is not quite 3% away would be the small lie this avoids.
 *
 * ONE DEVIATION FROM THE REFERENCE, DELIBERATE. Its ladder starts at the 1σ and
 * works outward, and rails TIGHTER than the 1σ -- every rail under 2% on a
 * VIX-32 day -- are dropped from the table and cannot be recovered. Here they are
 * merely collapsed: the opening view is the reference's (the 1σ outward, five rows
 * a side), and `showAll` reveals everything the rails produced, inner ones
 * included. A rail that exists and cannot be shown is a rail the reader cannot
 * check.
 */

/** The desk divisor that turns annualised VIX into a one-session move. */
export const VIX_DIVISOR = 16;

/** The fixed rails, in percent. A desk quotes these whatever the VIX is doing,
 * and where the 1σ sits among them is the read. */
export const RAIL_PCTS = [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];

/** SPX index options list on $5 centres, so every rail resolves to one of these
 * before it can be sold. Named because it is a market fact, not a rounding taste. */
export const STRIKE_STEP = 5;

/** Rows per side in the opening view, counted from the 1σ outward. */
export const ROWS_PER_SIDE = 5;

/* ---- the session's releases -------------------------------------------------
 *
 * The tab's other policy, and it is here rather than in the page for the reason
 * everything else on this seam is: it is a rule that can be wrong while the markup
 * renders perfectly. It also has nothing to do with the ladder, so it is fenced
 * off below rather than woven into it.
 *
 * WHAT IT IS FOR. This tab sells SPX, and the journal's calendar feed is
 * worldwide: 23 entries on an ordinary Wednesday, most of them a Swiss rate
 * decision, a British retail print and four speeches by the same central bank. All
 * of it as chips was six rows of noise above the two readings. The Market tab is
 * the diary and shows every country; this is a warning light.
 *
 * THE FOUR SPEAKERS ARE ONE EVENT, which is the whole reason this is a function
 * and not a filter. A feed that lists "FOMC Member Williams Speaks", "FOMC Member
 * Barkin Speaks", "FOMC Member Hammack Speaks" and "FOMC Member Paulson Speaks" as
 * four Low-impact rows has described one thing four times, and a strip capped at
 * four chips then shows nothing else. Merged, they read as the day's actual shape
 * -- and the reference implementation, whose calendar is curated by a language
 * model rather than a feed, names that same day "Fed speakers (Barkin, others)".
 * This gets there from the feed alone.
 */

/** The only country whose releases move this index. The feed's own key, the same
 * one `events.DEFAULT_COUNTRIES` and the Market tab's chips use, so there is no
 * second spelling of "American" anywhere. */
export const SESSION_COUNTRY = "USD";

/** Chips before the strip starts counting instead. */
export const SESSION_CHIPS = 4;

/** The feed's three grades, highest first. An unknown grade sorts LAST rather than
 * first, so a feed that invents a fourth cannot push an FOMC print off the strip. */
const GRADES = ["High", "Medium", "Low"];

const grade = (impact) => {
  const at = GRADES.indexOf(impact);
  return at === -1 ? GRADES.length : at;
};

/* WHAT MOVES THE S&P, IN THIS JOURNAL'S OWN JUDGEMENT -- and it is the journal's,
 * which is why the page attributes it as such and prints the feed's grade beside
 * it. The feed is a general economic calendar and grades for a general audience;
 * on this account's own data it calls CPI, PCE, the FOMC statement, payrolls and
 * the unemployment rate High, which is right, and then grades EVERY Fed speaker Low
 * -- level with Natural Gas Storage, the Current Account and New Home Sales. That
 * is the one thing an index seller cannot accept: four regional presidents talking
 * into an FOMC meeting is the dominant event risk on a day with no print, and a
 * gas-storage number has never moved a same-day SPX range.
 *
 * So each row gets a TIER, the strip sorts on it, and the feed's grade breaks ties
 * inside a tier. Three tiers, because a longer scale would be a judgement nobody
 * can check:
 *
 *   1  the session is about this -- policy and the inflation/labour prints
 *   2  moves the open, or the range -- second-tier data, and anyone at the Fed
 *      with a microphone
 *   3  background -- housing, trade, inventories, weekly energy
 *
 * MATCHED ON THE FEED'S OWN TITLES, which are stable ("Core PCE Price Index m/m",
 * "FOMC Member Barkin Speaks"), and the patterns are deliberately loose about the
 * suffixes it appends. An UNMATCHED title is tier 2 when the feed grades it High
 * and tier 3 otherwise: a release nobody here has seen before must not be buried
 * because this table has not met it, and must not lead the strip on the strength of
 * a name alone. */
const TIERS = [
  [1, new RegExp([
    "FOMC (Statement|Press Conference|Meeting Minutes|Economic Projections)",
    "Federal Funds Rate", "Interest Rate Decision",
    // Anchored, because ADP's private estimate is titled "ADP Non-Farm
    // Employment Change" and would otherwise be read as the payrolls report.
    "\\bCPI\\b", "\\bPCE\\b", "^\\s*Non-Farm Employment", "Unemployment Rate",
    "Fed Chair(man|woman)? \\w+ Speaks", "Payrolls Revision",
  ].join("|"), "i")],
  [2, new RegExp([
    "Unemployment Claims", "Retail Sales", "\\bPPI\\b", "\\bGDP\\b",
    "ISM (Manufacturing|Services) PMI", "Consumer Confidence",
    "UoM (Consumer Sentiment|Inflation Expectations)", "ADP Non-Farm",
    "JOLTS", "Average Hourly Earnings", "Durable Goods", "Philly Fed",
    // Everyone at the Fed with a microphone, plus the two other podiums whose
    // remarks move a US index -- the feed grades all of them Low.
    "Speaks", "Fed speakers", "Treasury (Note|Bond|Bill) Auction",
  ].join("|"), "i")],
  [3, new RegExp([
    "Home Sales", "Building Permits", "Housing Starts", "House Price",
    "Current Account", "Trade Balance", "Inventories", "Natural Gas Storage",
    "Baker Hughes", "Mortgage", "Redbook", "Factory Orders", "Wholesale",
  ].join("|"), "i")],
];

/** The journal's tier for one release, 1 (highest) to 3. */
function tier(title, impact) {
  for (const [rank, pattern] of TIERS) {
    if (pattern.test(String(title))) return rank;
  }
  return grade(impact) === 0 ? 2 : 3;
}

/* The feed's verb for a speech, which is how a speaker's surname is found. */
const SPEAKS = /\bspeaks\b/i;

/* A central banker with a microphone: an FOMC member or the Chair, speaking.
 * Matched on the feed's own titles, "FOMC Member Barkin Speaks" and "Fed Chair
 * Powell Speaks", which are the same kind of event. Applied only AFTER the country
 * filter, so an MPC or SNB speaker never reaches it.
 *
 * DELIBERATELY NARROW, in two directions. "FOMC Statement", "FOMC Press
 * Conference" and a rate decision do not match, and must not: those are the day's
 * main event, and folding one into a group captioned "speakers" would bury the
 * only release that reprices the whole curve. And a speaker who is not at the Fed
 * does not match either: "President Trump Speaks" merged into the group once and
 * captioned it "Fed speakers (Trump, +3)". Those rows keep their own chip. */
const FED_SPEAKER = /\b(FOMC Member|Fed Chair(man|woman)?)\b.*\bspeaks\b/i;

/* "FOMC Member Barkin Speaks" -> "Barkin". The surname is what a reader
 * recognises, and the feed's phrasing is stable enough to take the word before
 * the verb. Falls back to the whole title when it is phrased some other way,
 * which keeps an unfamiliar row readable instead of blank. */
function speaker(title) {
  const words = String(title).trim().split(/\s+/);
  const at = words.findIndex((word) => SPEAKS.test(word));
  return at > 0 ? words[at - 1] : String(title);
}

/** The releases to show for one session: `{shown, hidden}`.
 *
 * `shown` is chip-ready -- `{at, title, impact, tier, count}`, ranked by the
 * journal's own `tier` with the feed's grade and then the clock breaking ties, and
 * `count > 1` on a merged row. `hidden` is how many US releases the strip is not
 * showing, so the page can account for the rest rather than silently dropping it.
 */
export function sessionEvents(events, options) {
  const { country = SESSION_COUNTRY, chips = SESSION_CHIPS } = options || {};
  const mine = (events || []).filter((event) => event.country === country);
  const talks = mine.filter((event) => FED_SPEAKER.test(event.title));
  const rest = mine.filter((event) => !FED_SPEAKER.test(event.title));
  const chip = (event, title, count) => ({
    at: event.at,
    title: title == null ? event.title : title,
    impact: event.impact,
    tier: tier(event.title, event.impact),
    count: count == null ? 1 : count,
  });
  const rows = rest.map((event) => chip(event));
  if (talks.length === 1) {
    /* One speaker is not a group, so the feed's own title stands verbatim: the
       journal invents no wording it was not given. */
    rows.push(chip(talks[0]));
  } else if (talks.length > 1) {
    const order = [...talks].sort((a, b) => String(a.at).localeCompare(String(b.at)));
    /* The FIRST speaker names the group, and the group carries that speaker's
       time, so the two halves of the chip describe one event. Which of four
       regional presidents matters most is a judgement the feed does not carry:
       the reference implementation's calendar says "Barkin" because a language
       model chose him, and inventing that here would be this journal asserting
       something it cannot know. */
    const first = speaker(order[0].title);
    /* The group takes the BEST of everything its members carry -- grade and tier
       both. A Chair speaking at an inflation conference is not a Low because three
       regional presidents spoke that morning, and it is not tier 2 either. */
    const led = order.reduce(
      (held, event) => (grade(event.impact) < grade(held.impact) ? event : held),
      order[0],
    );
    const merged = chip(led, `Fed speakers (${first}, +${order.length - 1})`,
      order.length);
    merged.at = order[0].at;
    merged.tier = Math.min(...order.map((event) => tier(event.title, event.impact)));
    rows.push(merged);
  }
  rows.sort((a, b) => a.tier - b.tier
    || grade(a.impact) - grade(b.impact)
    || String(a.at).localeCompare(String(b.at)));
  return { shown: rows.slice(0, chips), hidden: Math.max(0, rows.length - chips) };
}

/* ---- scoring the rails, once the session has closed -------------------------
 *
 * WHAT THIS IS FOR. Everything above draws a band. Nothing above says whether the
 * band was ever any good, and a journal that prints a 68% reading without ever
 * checking it against its own history is asserting a number rather than measuring
 * one. This scores the rails against sessions that have since settled: the 1σ is
 * supposed to hold about two days in three, and the only way to know whether it
 * does on THIS account's index is to count.
 *
 * NO NEW TABLE, AND NO SECOND COPY OF THE ARITHMETIC. The reference
 * implementation persists a row per session with all ten of its derived levels
 * (`zdte_snapshots`: the 1σ pair, the 2% pair, the 3% pair, the points, rounded to
 * two decimals as strings). It therefore starts counting the day the feature was
 * switched on, and can never re-score a rail it did not store. This journal
 * already has the only two inputs a band needs -- `price_bars` keeps the daily
 * ^GSPC and ^VIX closes and is a cache that only ever grows -- so the levels are
 * recomputed from `expectedMove` and `RAIL_PCTS` rather than stored beside them,
 * and the whole bar history is scorable retroactively. Persisting a derived level
 * is what makes the stored copy and the drawn copy able to disagree.
 *
 * COMPARED AS PERCENTAGES, WHICH IS THE SAME TEST DONE WITHOUT LEVELS. A rail sits
 * at `prev_close * (1 ± pct/100)`, so "the close landed inside it" is exactly
 * "the session moved no more than `pct`". Measuring the move against the close the
 * band was drawn from keeps the rails as the single definition of where they are.
 *
 * ONE HONEST LIMIT, AND IT IS IN THE DATA RATHER THAN HERE. The live tab reads the
 * CURRENT VIX against the prior close (see `serialize.odte_context_data`), and a
 * daily bar history cannot reproduce an intraday reading -- the only settled VIX
 * available before a session opens is the previous session's close. So a score is
 * what the rails would have said drawn from the last settled readings, which is
 * not quite the ladder the tab showed at 09:40 that morning. Near enough to be
 * worth counting, and different enough that it must be said rather than implied:
 * the reference's own row for 2026-09-24 carries VIX 16.33 where the tab was read
 * at 15.67 on the same close.
 */

/** The label the VIX rail scores under, which no fixed rail can collide with. */
export const SIGMA_LABEL = "1σ";

/** How often each rail held, over sessions that have settled.
 *
 * Takes `{date, prev_close, vix, close}` per session -- the close the band was
 * drawn from, the VIX it was drawn at, and what the index actually did -- and
 * returns `{sessions, rails}`. `rails` is one row per rail, the 1σ first and the
 * fixed ladder after it, each `{label, tested, held, rate}`. `rails` is empty when
 * no session was usable, rather than a table of zeros claiming every rail failed.
 * `sessions` carries the per-day detail so the page can
 * show the misses rather than only the ratio: a rail that holds 65% of the time is
 * a different story depending on whether the third day is a near miss or a gap.
 *
 * A session the readings cannot support is SKIPPED rather than counted as a miss.
 * A missing VIX is an absence, and scoring it as a broken band would quietly make
 * every rail look worse than it is.
 */
export function railScores(sessions) {
  const tally = new Map();
  const scored = [];
  for (const session of sessions || []) {
    const move = expectedMove(session.prev_close, session.vix);
    const from = parseNumber(session.prev_close);
    const close = parseNumber(session.close);
    if (!move || from == null || close == null || close <= 0) continue;
    const points = Math.abs(close - from);
    const pct = points / from * 100;
    const rails = [
      { label: SIGMA_LABEL, pct: move.pct },
      ...RAIL_PCTS.map((rail) => ({ label: `${rail}%`, pct: rail })),
    ];
    const held = {};
    for (const rail of rails) {
      /* Inclusive: a close that lands exactly ON the rail is inside it. The rail
         is where the move stops being expected, not where it starts. */
      const inside = pct <= rail.pct;
      held[rail.label] = inside;
      const row = tally.get(rail.label)
        || { label: rail.label, tested: 0, held: 0 };
      row.tested += 1;
      if (inside) row.held += 1;
      tally.set(rail.label, row);
    }
    scored.push({
      date: session.date,
      from,
      close,
      vix: parseNumber(session.vix),
      points,
      pct,
      sigmaPct: move.pct,
      held,
    });
  }
  /* Every rail in the tally was tested by the session that put it there, so a
     rate is always available: there is no "scored nothing" rail to guard against,
     only an empty table when no session was usable. */
  const rails = [...tally.values()].map((row) => ({
    ...row,
    rate: row.held / row.tested,
  }));
  return { sessions: scored, rails };
}

/** A finite number from a typed string, or null. Never NaN, which is the value a
 * half-typed field yields and the one that renders as "NaN" in a cell. */
export function parseNumber(text) {
  if (text == null || text === "") return null;
  const value = typeof text === "number" ? text : Number.parseFloat(String(text).trim());
  return Number.isFinite(value) ? value : null;
}

/** An input mask: digits and at most one decimal point.
 *
 * Applied on every keystroke rather than validated on blur, because the ladder is
 * recomputed as you type -- a stray letter has to be refused at the moment it
 * would otherwise blank the table, not once the field is left.
 */
export function sanitizeLevel(text) {
  const cleaned = String(text ?? "").replace(/[^\d.]/g, "");
  const dot = cleaned.indexOf(".");
  return dot === -1
    ? cleaned
    : cleaned.slice(0, dot + 1) + cleaned.slice(dot + 1).replace(/\./g, "");
}

/** The listed strike nearest a level, or null when the level is not a number.
 *
 * A level exactly between two strikes resolves AWAY from the money: up for a
 * "call", down for a "put", the further of the two contracts a seller could
 * mean. `Math.round` alone sends every half up, which is away from the money for
 * a call and toward it for a put, so the two sides of one ladder broke ties in
 * opposite directions. Any other `side` rounds a tie up, as a call does.
 */
export function strikeNear(level, side) {
  const value = parseNumber(level);
  if (value == null) return null;
  const steps = value / STRIKE_STEP;
  const below = Math.floor(steps);
  if (steps - below === 0.5) return (side === "put" ? below : below + 1) * STRIKE_STEP;
  return Math.round(steps) * STRIKE_STEP;
}

/** Whether a close and a VIX can produce a ladder at all.
 *
 * A zero VIX is USABLE and means "no expected move" -- a real, if never-seen,
 * reading. A zero or negative close is a bad row and a negative VIX is
 * impossible, so both fail closed, and so does a field mid-edit.
 */
function readings(spx, vix) {
  const close = parseNumber(spx);
  const level = parseNumber(vix);
  if (close == null || close <= 0) return null;
  if (level == null || level < 0) return null;
  return { close, vix: level };
}

/** The one-session expected move: `{pct, points}`, or null on an unusable input.
 *
 * Unrounded, both of them. The page prints two decimals; rounding here would put
 * a figure in the caption that the rails were not drawn from.
 */
export function expectedMove(spx, vix) {
  const read = readings(spx, vix);
  if (!read) return null;
  const pct = read.vix / VIX_DIVISOR;
  return { pct, points: read.close * pct / 100 };
}

function rowAt(close, exact, base, side) {
  const strike = strikeNear(exact, side);
  const points = Math.abs(close - strike);
  return { side, base, current: false, exact, strike, points, pct: points / close * 100 };
}

/** One row per STRIKE, not one per rail.
 *
 * Two rails round to one contract often -- on a 7706.03 close the 1σ (0.9794%)
 * and the 1% rail both land on 7630 below the market -- and two rows naming the
 * same strike would read as two things to sell. The survivor is the 1σ where one
 * of them is it, because that is the row the opening view is measured from;
 * otherwise it is whichever rail lands nearest the listed strike, so the printed
 * `exact` is the best claim available about where that contract sits.
 */
function dedupe(rows) {
  const best = new Map();
  for (const row of rows) {
    if (row.strike == null) continue;
    const held = best.get(row.strike);
    if (!held || beats(row, held)) best.set(row.strike, row);
  }
  return [...best.values()];
}

function beats(row, held) {
  if (row.base !== held.base) return row.base;
  return Math.abs(row.exact - row.strike) < Math.abs(held.exact - held.strike);
}

/** The rows to show on one side, nearest the money first. */
function shown(rows, showAll) {
  const ordered = [...rows].sort((a, b) => a.points - b.points);
  if (showAll) return ordered;
  /* From the 1σ OUTWARD. The rails inside it are the ones a seller has already
     decided against by looking at the VIX, so they are the first thing to
     collapse -- and the opening view then always starts at the expected move
     whatever the VIX is doing, rather than at a fixed percentage that is inside
     it on a quiet day and outside it on a loud one. */
  const base = ordered.find((row) => row.base);
  const outward = base ? ordered.filter((row) => row.points >= base.points) : ordered;
  return outward.slice(0, ROWS_PER_SIDE);
}

/** The ladder for one session: `{rows, hidden}`.
 *
 * `rows` is in DISPLAY order -- by strike, ascending unless `desc` -- and carries
 * the current level as a row of its own (`current: true`) so the page renders one
 * list and the marker cannot drift away from the rows it separates. Puts sit
 * above it and calls below, because that is what sorting by strike does and
 * because it is how a ladder is read: further from the money is further from the
 * middle.
 *
 * `hidden` counts the rows a fuller view would add, so the page can label the
 * toggle honestly rather than always offering more.
 */
export function ladderRows(spx, vix, options) {
  const read = readings(spx, vix);
  if (!read) return { rows: [], hidden: 0 };
  const { showAll = false, desc = false } = options || {};
  const { close } = read;
  const rails = [
    { pct: read.vix / VIX_DIVISOR, base: true },
    ...RAIL_PCTS.map((pct) => ({ pct, base: false })),
  ];
  const calls = [];
  const puts = [];
  for (const rail of rails) {
    const away = close * rail.pct / 100;
    calls.push(rowAt(close, close + away, rail.base, "call"));
    puts.push(rowAt(close, close - away, rail.base, "put"));
  }
  const held = [dedupe(calls), dedupe(puts)];
  const keep = held.map((side) => shown(side, showAll));
  const rows = [
    ...keep[0],
    ...keep[1],
    { side: "", base: false, current: true, exact: close, strike: close, points: 0, pct: 0 },
  ].sort((a, b) => a.strike - b.strike);
  if (desc) rows.reverse();
  const hidden = held[0].length + held[1].length - (keep[0].length + keep[1].length);
  return { rows, hidden };
}

/** What a scratch level is, measured from the close: `{level, points, pct,
 * strike, side}`, or null when either number is unusable.
 *
 * `side` is "above", "below" or "at" rather than "call"/"put": this function is
 * told a level, not an intention, and a level typed into the call pad that sits
 * below the market is a fact the page should be able to show as one.
 */
export function scratchRead(spx, level) {
  const close = parseNumber(spx);
  const entry = parseNumber(level);
  if (close == null || close <= 0 || entry == null || entry <= 0) return null;
  const points = Math.abs(close - entry);
  return {
    level: entry,
    points,
    pct: points / close * 100,
    strike: strikeNear(entry, entry < close ? "put" : "call"),
    side: entry > close ? "above" : entry < close ? "below" : "at",
  };
}

/** Where a sold level falls in the ladder, as a decoration per row.
 *
 * Returns one `{call, put}` per row of `rows`, each either "" (nothing), "on"
 * (this row IS the level's strike) or "top"/"bottom" (the level lies past this
 * row's upper or lower edge, so a line is drawn there). A boundary rather than a
 * highlight because a sold level is almost never a shown strike, and the useful
 * fact is which rows it sits between: the ones above the line are still yours.
 *
 * Computed against the DISPLAY list, so it is correct under either sort without
 * knowing which one is in force. Placed by where the level SITS in that list,
 * whichever side's rows those are: a call typed below the market is an
 * in-the-money call, and its line belongs below the market row, not on the
 * nearest call.
 */
export function scratchLines(rows, callLevel, putLevel) {
  const list = rows || [];
  const lines = list.map(() => ({ call: "", put: "" }));
  for (const [side, level] of [["call", callLevel], ["put", putLevel]]) {
    const mark = edgeFor(list, side, level);
    if (mark) lines[mark.index][side] = mark.edge;
  }
  return lines;
}

function edgeFor(rows, side, level) {
  const target = strikeNear(level, side);
  if (target == null || !rows.length) return null;
  const hit = rows.findIndex((row) => !row.current && row.strike === target);
  if (hit !== -1) return { index: hit, edge: "on" };
  /* The two neighbouring rows the level lies between, which may be the current
     level and a row of either side: a put sold inside the expected move belongs
     between the innermost put shown and the market. The line goes on whichever
     of the two is a strike row and nearer the level, on the edge facing the
     other. A level AT the market goes between it and the pad's own side. */
  for (let at = 0; at + 1 < rows.length; at += 1) {
    const pair = [rows[at], rows[at + 1]];
    const [low, high] = [Math.min(pair[0].strike, pair[1].strike),
      Math.max(pair[0].strike, pair[1].strike)];
    const atMarket = pair.some((row) => row.current && row.strike === target)
      && pair.some((row) => row.side === side);
    if (!(low < target && target < high) && !atMarket) continue;
    const upper = !pair[0].current && (pair[1].current
      || Math.abs(pair[0].strike - target) <= Math.abs(pair[1].strike - target));
    return upper ? { index: at, edge: "bottom" } : { index: at + 1, edge: "top" };
  }
  /* Nothing shown lies either side, so the level is past an end of the ladder:
     the line goes on the far edge of the row at that end, which is the direction
     the reader is looking when a sold strike is further out than anything on
     screen. */
  const last = rows.length - 1;
  return Math.abs(target - rows[0].strike) < Math.abs(target - rows[last].strike)
    ? { index: 0, edge: "top" }
    : { index: last, edge: "bottom" };
}
