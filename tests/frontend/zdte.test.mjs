import assert from "node:assert/strict";
import test from "node:test";

import {
  RAIL_PCTS,
  SESSION_CHIPS,
  VIX_DIVISOR,
  expectedMove,
  ladderRows,
  parseNumber,
  sanitizeLevel,
  scratchLines,
  scratchRead,
  sessionEvents,
  strikeNear,
} from "../../src/optjournal/static/zdte.js";

/* The session the reference implementation was read on: its own screen, copied
   figure by figure, so this suite is a cross-check against a second program
   rather than against itself. Every column it prints is here -- the strike, the
   distance in points, the percentage of the close that distance is, and the exact
   level the rail landed on before it was rounded to a listed strike. */
const SPX = 7706.03;
const VIX = 15.67;
const REFERENCE = [
  { side: "put", strike: 7475, points: 231.03, pct: 3.00, exact: 7474.85 },
  { side: "put", strike: 7515, points: 191.03, pct: 2.48, exact: 7513.38 },
  { side: "put", strike: 7550, points: 156.03, pct: 2.02, exact: 7551.91 },
  { side: "put", strike: 7590, points: 116.03, pct: 1.51, exact: 7590.44 },
  { side: "put", strike: 7630, points: 76.03, pct: 0.99, exact: 7630.56 },
  { side: "", strike: SPX, points: 0, pct: 0, exact: SPX },
  { side: "call", strike: 7780, points: 73.97, pct: 0.96, exact: 7781.50 },
  { side: "call", strike: 7785, points: 78.97, pct: 1.02, exact: 7783.09 },
  { side: "call", strike: 7820, points: 113.97, pct: 1.48, exact: 7821.62 },
  { side: "call", strike: 7860, points: 153.97, pct: 2.00, exact: 7860.15 },
  { side: "call", strike: 7900, points: 193.97, pct: 2.52, exact: 7898.68 },
];

const shape = (row) => ({
  side: row.side,
  strike: row.strike,
  points: Number(row.points.toFixed(2)),
  pct: Number(row.pct.toFixed(2)),
  exact: Number(row.exact.toFixed(2)),
});

test("the opening ladder reproduces the reference implementation's own screen", () => {
  const { rows } = ladderRows(SPX, VIX);
  assert.deepEqual(rows.map(shape), REFERENCE.map(shape));
});

test("puts sit above the current level and calls below it", () => {
  const { rows } = ladderRows(SPX, VIX);
  const at = rows.findIndex((row) => row.current);
  assert.ok(at > 0 && at < rows.length - 1, "the marker is between the two sides");
  assert.ok(rows.slice(0, at).every((row) => row.side === "put" && row.strike < SPX));
  assert.ok(rows.slice(at + 1).every((row) => row.side === "call" && row.strike > SPX));
});

test("descending sort reverses the same rows rather than rebuilding them", () => {
  const up = ladderRows(SPX, VIX).rows;
  const down = ladderRows(SPX, VIX, { desc: true }).rows;
  assert.deepEqual(down.map(shape), [...up].reverse().map(shape));
});

test("VIX de-annualises by 16, the desk divisor, not by sqrt(252)", () => {
  /* Both rows read from the reference implementation's own stored snapshots. A
     number 0.8% off is invisible in a band and still the wrong one beside a
     platform quoting the other convention. */
  for (const [spx, vix, pct, points] of [
    [7711.76, 15.25, 0.9531, 73.50],
    [7711.76, 14.43, 0.9019, 69.55],
  ]) {
    const move = expectedMove(spx, vix);
    assert.equal(Number(move.pct.toFixed(4)), pct);
    assert.equal(Number(move.points.toFixed(2)), points);
    assert.notEqual(
      Number((vix / Math.sqrt(252)).toFixed(4)),
      pct,
      "sqrt(252) is the textbook divisor and NOT the one in use here",
    );
  }
  assert.equal(expectedMove(5000, 16).pct, 1, `VIX ${VIX_DIVISOR} is a 1% session`);
});

test("the 1σ rails land on the reference's own levels, to the cent", () => {
  /* The session the serializer's prior-close test uses (tests/test_serialize.py),
     where the reference read Friday's 7711.76 against a live 15.25 VIX and drew
     its rails at 7638.26 and 7785.26. Asserted on `exact` -- the level the rail
     landed on -- because the strike beside it is the $5 contract nearest that,
     which is a different (and deliberate) number. */
  const rails = ladderRows(7711.76, 15.25).rows.filter((row) => row.base);
  assert.deepEqual(
    rails.map((row) => Number(row.exact.toFixed(2))),
    [7638.26, 7785.26],
  );
});

test("the 1σ rail is the row the opening view is measured from", () => {
  const { rows } = ladderRows(SPX, VIX);
  const base = rows.filter((row) => row.base);
  assert.deepEqual(base.map((row) => row.strike), [7630, 7780], "one a side");
  /* Nothing shown on a side is nearer the money than that side's 1σ row. Stated
     against the ROW rather than against the expected move itself, because
     rounding to a listed strike pulls a rail inside by up to half a step: the 1σ
     lands at 75.47 points and its contract at 73.97. */
  for (const side of ["call", "put"]) {
    const mine = rows.filter((row) => row.side === side);
    const anchor = mine.find((row) => row.base);
    assert.ok(mine.every((row) => row.points >= anchor.points), side);
  }
});

test("two rails landing on one contract are one row, and the 1σ is the survivor", () => {
  /* On this close the 1σ (0.9794%) and the 1% rail both round to 7630 below the
     market. Two rows naming the same strike would read as two things to sell. */
  const { rows } = ladderRows(SPX, VIX);
  const at7630 = rows.filter((row) => row.strike === 7630);
  assert.equal(at7630.length, 1);
  assert.equal(at7630[0].base, true);
  assert.equal(Number(at7630[0].exact.toFixed(2)), 7630.56, "the 1σ level, not the 1% one");
});

test("show-all reveals every rail, including the ones inside the expected move", () => {
  /* A VIX of 32 puts the 1σ at 2%, so the 1% and 1.5% rails are inside it. The
     reference drops those permanently; here they are only collapsed. */
  const opening = ladderRows(5000, 32);
  const all = ladderRows(5000, 32, { showAll: true });
  const pcts = (rows) => rows.filter((row) => row.side === "call").map((row) => row.pct);
  assert.ok(Math.min(...pcts(opening.rows)) >= 2, "the opening view starts at the 1σ");
  assert.ok(Math.min(...pcts(all.rows)) < 1.1, "the 1% rail is recoverable");
  assert.equal(
    all.rows.filter((row) => !row.current).length,
    opening.rows.filter((row) => !row.current).length + opening.hidden,
    "the toggle offers exactly what it then shows",
  );
  assert.equal(all.hidden, 0, "nothing is left to offer once everything is shown");
});

test("hidden counts the rows a fuller view would add", () => {
  const { rows, hidden } = ladderRows(SPX, VIX);
  /* Ten rails a side, less the put-side collision at 7630, less the ten shown. */
  assert.equal(rows.filter((row) => !row.current).length, 10);
  assert.equal(hidden, (RAIL_PCTS.length + 1) * 2 - 1 - 10);
});

test("a zero VIX is a flat ladder, not an absence", () => {
  /* VIX can legitimately be reported low, and zero means "no expected move" --
     a real if never-seen reading rather than a missing feed. */
  const move = expectedMove(5000, 0);
  assert.equal(move.pct, 0);
  assert.equal(move.points, 0);
  const { rows } = ladderRows(5000, 0);
  assert.deepEqual(
    rows.filter((row) => row.base).map((row) => row.strike),
    [5000, 5000],
    "both 1σ rails land on the money",
  );
});

test("an unusable reading is an absence, not a ladder of zero width", () => {
  for (const [spx, vix] of [
    [null, 16],       // no index close fetched yet
    [5000, null],     // no VIX fetched yet
    ["", 16],         // the field was cleared
    ["7.", ""],       // both mid-edit
    [0, 16],          // a bad index row
    [-1, 16],         // a bad index row
    [5000, -1],       // an impossible VIX
  ]) {
    assert.deepEqual(ladderRows(spx, vix), { rows: [], hidden: 0 }, `${spx} / ${vix}`);
    assert.equal(expectedMove(spx, vix), null, `${spx} / ${vix}`);
  }
});

test("a half-typed number is null rather than NaN", () => {
  assert.equal(parseNumber("."), null);
  assert.equal(parseNumber(""), null);
  assert.equal(parseNumber(null), null);
  assert.equal(parseNumber("7706.03"), 7706.03);
  assert.equal(parseNumber(" 15.67 "), 15.67);
  assert.equal(parseNumber(7706.03), 7706.03);
});

test("the input mask keeps digits and one decimal point", () => {
  assert.equal(sanitizeLevel("7706.03"), "7706.03");
  assert.equal(sanitizeLevel("7,706.03"), "7706.03");
  assert.equal(sanitizeLevel("77o6..0.3"), "776.03", "the FIRST dot survives");
  assert.equal(sanitizeLevel("-15.67"), "15.67");
  assert.equal(sanitizeLevel(null), "");
});

test("a level resolves to the listed strike nearest it", () => {
  assert.equal(strikeNear(7781.5009), 7780);
  assert.equal(strikeNear(7783.09), 7785);
  assert.equal(strikeNear(7667.5), 7670, "a tie rounds up, as the platform does");
  assert.equal(strikeNear("7630.56"), 7630);
  assert.equal(strikeNear(""), null);
});

test("a scratch level is measured from the close, and says which side it is", () => {
  const call = scratchRead(SPX, "7781");
  assert.equal(Number(call.points.toFixed(2)), 74.97);
  assert.equal(Number(call.pct.toFixed(2)), 0.97);
  assert.equal(call.strike, 7780);
  assert.equal(call.side, "above");
  const put = scratchRead(SPX, 7630);
  assert.equal(put.side, "below");
  assert.equal(put.strike, 7630);
  assert.equal(scratchRead(SPX, SPX).side, "at");
  assert.equal(scratchRead(SPX, ""), null, "an empty pad reads nothing");
  assert.equal(scratchRead(SPX, 0), null, "and neither does a zero");
  assert.equal(scratchRead(null, 7780), null);
});

test("a sold level marks the row it is, or the edge it falls past", () => {
  const { rows } = ladderRows(SPX, VIX);
  const edge = (call, put) => scratchLines(rows, call, put)
    .map((line, index) => ({ index, ...line }))
    .filter((line) => line.call || line.put);

  /* A level that IS a shown strike marks that row. */
  assert.deepEqual(edge(7780, null), [{ index: 6, call: "on", put: "" }]);
  assert.deepEqual(edge(null, 7550), [{ index: 2, call: "", put: "on" }]);

  /* Between two shown strikes: the line goes on the edge facing the level. 7800
     sits between 7785 (index 7) and 7820 (index 8). */
  assert.deepEqual(edge(7800, null), [{ index: 7, call: "bottom", put: "" }]);

  /* Inside the expected move, where no rail is shown: the line lands between the
     innermost strike on that side and the current level. */
  assert.deepEqual(edge(7750, null), [{ index: 6, call: "top", put: "" }]);
  assert.deepEqual(edge(null, 7680), [{ index: 4, call: "", put: "bottom" }]);

  /* Past the end of the ladder: the far edge of the last row shown. */
  assert.deepEqual(edge(8200, null), [{ index: 10, call: "bottom", put: "" }]);
  assert.deepEqual(edge(null, 7100), [{ index: 0, call: "", put: "top" }]);

  /* Both pads at once, each on its own side. */
  assert.deepEqual(edge(7780, 7630), [
    { index: 4, call: "", put: "on" },
    { index: 6, call: "on", put: "" },
  ]);
});

test("decorations survive a descending ladder and an empty one", () => {
  const down = ladderRows(SPX, VIX, { desc: true }).rows;
  const lines = scratchLines(down, 7800, null);
  const marked = lines.findIndex((line) => line.call);
  assert.equal(down[marked].strike, 7785);
  assert.equal(lines[marked].call, "top", "further out is UP the list when descending");
  assert.deepEqual(scratchLines([], 7800, 7600), []);
  assert.deepEqual(
    scratchLines(ladderRows(SPX, VIX).rows, null, null).filter((l) => l.call || l.put),
    [],
  );
});

/* The day the reference implementation's own calendar described as "Fed speakers
   (Barkin, others)" -- eight US rows in this journal's feed, seven of them graded
   Low, four of them the same event four times. Plus the rest of the world, which
   this tab is not about. */
const FEED = [
  { at: "03:30", country: "CHF", title: "SNB Monetary Policy Assessment", impact: "High" },
  { at: "04:00", country: "EUR", title: "ECB Economic Bulletin", impact: "Low" },
  { at: "04:10", country: "USD", title: "FOMC Member Williams Speaks", impact: "Low" },
  { at: "05:30", country: "GBP", title: "MPC Member Dhingra Speaks", impact: "Low" },
  { at: "08:30", country: "USD", title: "Current Account", impact: "Low" },
  { at: "08:30", country: "USD", title: "FOMC Member Barkin Speaks", impact: "Low" },
  { at: "08:30", country: "USD", title: "Unemployment Claims", impact: "Medium" },
  { at: "08:50", country: "USD", title: "FOMC Member Hammack Speaks", impact: "Low" },
  { at: "10:00", country: "USD", title: "New Home Sales", impact: "Low" },
  { at: "10:10", country: "USD", title: "FOMC Member Paulson Speaks", impact: "Low" },
  { at: "10:30", country: "USD", title: "Natural Gas Storage", impact: "Low" },
];

test("the strip is US releases only, ranked by what moves the S&P", () => {
  const { shown } = sessionEvents(FEED);
  assert.equal(shown.length, SESSION_CHIPS);
  assert.equal(shown[0].title, "Unemployment Claims", "the only Medium leads");
  assert.ok(
    shown.every((row) => !/SNB|ECB|MPC/.test(row.title)),
    "a Swiss rate decision is not news on a tab that sells SPX",
  );
  /* Time breaks a tie, so same-grade rows read in the order the session meets them. */
  assert.deepEqual(shown.slice(1).map((row) => row.at), ["04:10", "08:30", "10:00"]);
});

test("four Fed speakers are one chip, named and counted", () => {
  const { shown, hidden } = sessionEvents(FEED);
  const fed = shown.find((row) => row.count > 1);
  /* The FIRST speaker names the chip, because the time beside it is also the
     first: a label reading "Barkin" over "04:10" would describe two different
     events. Which of four regional presidents matters most is a judgement the
     feed does not carry and this journal will not invent -- the reference's
     calendar names Barkin because a language model curated it. */
  assert.equal(fed.title, "Fed speakers (Williams, +3)");
  assert.equal(fed.count, 4);
  assert.equal(fed.at, "04:10", "the earliest of the group is when it starts");
  /* Five chips' worth of day, four shown. Without the merge the four speakers
     would BE the strip and Unemployment Claims would be the hidden one. */
  assert.equal(hidden, 1);
  assert.deepEqual(
    shown.map((row) => row.title),
    ["Unemployment Claims", "Fed speakers (Williams, +3)", "Current Account", "New Home Sales"],
  );
});

test("a group takes the highest grade any member carries", () => {
  const { shown } = sessionEvents([
    { at: "09:00", country: "USD", title: "FOMC Member Barkin Speaks", impact: "Low" },
    { at: "14:00", country: "USD", title: "Fed Chair Powell Speaks", impact: "High" },
  ]);
  assert.equal(shown.length, 1);
  assert.equal(shown[0].impact, "High", "a Chair is not a Low because a president was");
  assert.equal(shown[0].title, "Fed speakers (Barkin, +1)");
});

test("one speaker keeps the feed's own wording", () => {
  /* The journal invents no title it was not given: a group of one is not a group. */
  const { shown } = sessionEvents([
    { at: "08:30", country: "USD", title: "FOMC Member Barkin Speaks", impact: "Low" },
    { at: "10:00", country: "USD", title: "New Home Sales", impact: "Low" },
  ]);
  assert.deepEqual(shown.map((row) => row.title),
    ["FOMC Member Barkin Speaks", "New Home Sales"]);
  assert.ok(shown.every((row) => row.count === 1));
});

test("a rate decision is never folded into the speakers", () => {
  /* The one release that reprices the whole curve must not disappear into a chip
     captioned "speakers" -- so the match is on the verb, narrowly. */
  const { shown } = sessionEvents([
    { at: "14:00", country: "USD", title: "FOMC Statement", impact: "High" },
    { at: "14:00", country: "USD", title: "Federal Funds Rate", impact: "High" },
    { at: "14:30", country: "USD", title: "FOMC Press Conference", impact: "High" },
    { at: "09:00", country: "USD", title: "FOMC Member Barkin Speaks", impact: "Low" },
    { at: "11:00", country: "USD", title: "FOMC Member Hammack Speaks", impact: "Low" },
  ]);
  assert.deepEqual(shown.map((row) => row.title), [
    "FOMC Statement", "Federal Funds Rate", "FOMC Press Conference",
    "Fed speakers (Barkin, +1)",
  ]);
});

test("an unknown grade sorts last, and an empty day is empty", () => {
  const { shown } = sessionEvents([
    { at: "10:00", country: "USD", title: "Something New", impact: "Critical" },
    { at: "11:00", country: "USD", title: "New Home Sales", impact: "Low" },
  ]);
  assert.deepEqual(shown.map((row) => row.title), ["New Home Sales", "Something New"],
    "a grade the feed invented cannot outrank one it documents");
  assert.deepEqual(sessionEvents([]), { shown: [], hidden: 0 });
  assert.deepEqual(sessionEvents(null), { shown: [], hidden: 0 });
  assert.deepEqual(sessionEvents(FEED, { country: "JPY" }), { shown: [], hidden: 0 });
});

test("the journal's tier outranks the feed's grade, which breaks ties inside it", () => {
  /* The defect this table exists for: the feed grades every Fed speaker Low, level
     with Natural Gas Storage and New Home Sales, so a strip sorted on the feed's
     judgement put a gas-storage number above four regional presidents talking into
     an FOMC meeting. */
  const { shown } = sessionEvents([
    { at: "10:30", country: "USD", title: "Natural Gas Storage", impact: "Low" },
    { at: "10:00", country: "USD", title: "New Home Sales", impact: "Low" },
    { at: "09:00", country: "USD", title: "FOMC Member Barkin Speaks", impact: "Low" },
  ]);
  assert.deepEqual(shown.map((row) => row.title),
    ["FOMC Member Barkin Speaks", "New Home Sales", "Natural Gas Storage"]);
  assert.deepEqual(shown.map((row) => row.tier), [2, 3, 3]);
});

test("policy and the inflation and labour prints are the first tier", () => {
  const { shown } = sessionEvents([
    { at: "08:30", country: "USD", title: "Unemployment Claims", impact: "Medium" },
    { at: "08:30", country: "USD", title: "Core PCE Price Index m/m", impact: "High" },
    { at: "14:00", country: "USD", title: "FOMC Statement", impact: "High" },
    { at: "10:00", country: "USD", title: "Existing Home Sales", impact: "Low" },
  ]);
  assert.deepEqual(shown.map((row) => row.tier), [1, 1, 2, 3]);
  assert.deepEqual(shown.map((row) => row.title), [
    "Core PCE Price Index m/m", "FOMC Statement", "Unemployment Claims",
    "Existing Home Sales",
  ], "two first-tier prints, then the clock decides between them");
});

test("a release this table has never met is neither buried nor promoted on a name", () => {
  const { shown } = sessionEvents([
    { at: "09:00", country: "USD", title: "Emergency Liquidity Facility", impact: "High" },
    { at: "09:30", country: "USD", title: "Widget Shipments m/m", impact: "Low" },
    { at: "08:30", country: "USD", title: "CPI m/m", impact: "High" },
    { at: "11:00", country: "USD", title: "Crude Oil Inventories", impact: "Low" },
  ]);
  assert.deepEqual(shown.map((row) => [row.title, row.tier]), [
    ["CPI m/m", 1],
    ["Emergency Liquidity Facility", 2],   // unmatched, but the feed says High
    ["Widget Shipments m/m", 3],           // unmatched and Low: background
    ["Crude Oil Inventories", 3],
  ]);
});

test("a group takes the best tier its members carry, not the first one's", () => {
  const { shown } = sessionEvents([
    { at: "09:00", country: "USD", title: "FOMC Member Barkin Speaks", impact: "Low" },
    { at: "14:00", country: "USD", title: "Fed Chairman Warsh Speaks", impact: "High" },
  ]);
  assert.equal(shown.length, 1);
  assert.equal(shown[0].tier, 1, "the Chair speaking makes the group first-tier");
  assert.equal(shown[0].impact, "High");
  assert.equal(shown[0].at, "09:00", "and it still starts when the first one speaks");
});
