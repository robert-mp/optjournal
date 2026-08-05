/* Unit tests for the replay chart's arithmetic, run by `node --test`.
 *
 * node:test and node:assert are both builtin, so this adds no dependency to a
 * project whose whole point is having almost none. The suite drives it through
 * tests/test_frontend.py so a single `pytest` run covers both languages.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  bandEdges,
  clampIndex,
  deltaDomain,
  domainOf,
  frameAt,
  markAt,
  plotGeometry,
  sessionBreaks,
  strikeSpan,
} from "../../src/optjournal/static/replay.js";

const BOX = { left: 52, top: 12, width: 792, height: 228 };
const PRICE = [
  [1000, 100],
  [2000, 110],
  [3000, 105],
];

function geometry(points = PRICE, strikes = [], band = []) {
  return plotGeometry(points, strikes, band, BOX);
}

test("the domain contains every strike, however far out of the money", () => {
  const { lo, hi } = domainOf(PRICE, [{ strike: 270 }, { strike: 700 }]);
  assert.ok(lo < 100, "the price low fell outside the domain");
  assert.ok(hi > 700, "a far-OTM strike was clipped off-canvas");
});

test("the domain contains the band", () => {
  const { lo, hi } = domainOf(PRICE, [], [[1000, 40, 160]]);
  assert.ok(lo < 40 && hi > 160);
});

test("a flat series still gets a usable domain", () => {
  // Zero range would divide by zero and collapse every point onto one line.
  const { lo, hi, span } = domainOf([
    [1, 50],
    [2, 50],
  ]);
  assert.ok(span > 0);
  assert.ok(lo < 50 && hi > 50);
});

test("x is ordinal: every bar is the same width", () => {
  const many = Array.from({ length: 40 }, (_, i) => [i * 3600, 100 + i]);
  const { xs } = geometry(many);
  const steps = xs.slice(1).map((x, i) => x - xs[i]);
  const spread = Math.max(...steps) - Math.min(...steps);
  assert.ok(spread < 1e-9, `steps vary by ${spread}; the axis is not ordinal`);
});

test("x spans the full plot width", () => {
  const { xs } = geometry();
  assert.equal(xs[0], BOX.left);
  assert.equal(xs[xs.length - 1], BOX.left + BOX.width);
});

test("an overnight gap costs no horizontal room", () => {
  // One hour, then eighteen. A linear time axis would give the second 18x the
  // width and draw it as a long diagonal across a closed market.
  const { xs } = geometry([
    [0, 100],
    [3600, 101],
    [3600 + 18 * 3600, 102],
  ]);
  assert.equal(xs[1] - xs[0], xs[2] - xs[1]);
});

test("y is inverted: a higher price sits higher on screen", () => {
  const { yOf } = geometry();
  assert.ok(yOf(110) < yOf(100), "the y axis is upside down");
});

test("a timestamp between two bars interpolates across them", () => {
  const geo = geometry();
  const mid = geo.at(1500);
  assert.ok(mid.x > geo.xs[0] && mid.x < geo.xs[1]);
  // Halfway in time is halfway in price: 100 -> 110 at 1500 of [1000, 2000].
  assert.ok(Math.abs(mid.y - geo.yOf(105)) < 1e-9);
});

test("a timestamp outside the window clamps to an edge", () => {
  const geo = geometry();
  assert.equal(geo.at(0).x, geo.xs[0]);
  assert.equal(geo.at(9e9).x, geo.xs[2]);
  assert.equal(geo.at(null), null);
  assert.equal(geo.at(NaN), null);
});

test("a scrub value is clamped to a real bar", () => {
  assert.equal(clampIndex("2", 3), 2);
  assert.equal(clampIndex("2.7", 3), 2);
  assert.equal(clampIndex(-5, 3), 0);
  assert.equal(clampIndex(99, 3), 2);
  assert.equal(clampIndex("nonsense", 3), 0);
  assert.equal(clampIndex(0, 0), 0);
});

test("scrubbing to a bar reveals exactly up to that bar", () => {
  const geo = geometry();
  const state = { points: PRICE, marks: [], xs: geo.xs };
  const first = frameAt(state, 0);
  const last = frameAt(state, 2);
  assert.equal(first.index, 0);
  assert.equal(last.index, 2);
  assert.ok(first.revealWidth < last.revealWidth, "the reveal did not advance");
  // The future must be hidden: bar 0's reveal cannot reach bar 1.
  assert.ok(first.revealWidth < geo.xs[1]);
});

test("a frame reports its own bar's price and timestamp", () => {
  const geo = geometry();
  const frame = frameAt({ points: PRICE, marks: [], xs: geo.xs }, 1);
  assert.equal(frame.ts, 2000);
  assert.equal(frame.price, 110);
});

test("P&L is looked up by timestamp, not by shared index", () => {
  // The marks series is SHORTER than the bars whenever a leading bar had no
  // solvable vol. Index alignment would report bar 0's P&L against bar 1's
  // price -- the exact defect this asserts against.
  const geo = geometry();
  const marks = [
    [2000, 250, 0.4],
    [3000, 300, 0.5],
  ];
  const state = { points: PRICE, marks, xs: geo.xs };
  assert.equal(frameAt(state, 0).pnl, null, "bar 0 has no mark and must say so");
  assert.equal(frameAt(state, 1).pnl, 250);
  assert.equal(frameAt(state, 2).pnl, 300);
  assert.equal(frameAt(state, 2).delta, 0.5);
});

test("a missing mark is null rather than a neighbour's figure", () => {
  assert.equal(markAt([[100, 1, 0]], 200), null);
  assert.equal(markAt([[300, 1, 0]], 200), null);
  assert.deepEqual(markAt([[200, 5, 0.1]], 200), [200, 5, 0.1]);
  assert.equal(markAt([], 200), null);
});

test("session breaks land on the first bar of each new day", () => {
  const dayOf = (ts) => (ts < 100 ? "d1" : ts < 200 ? "d2" : "d3");
  const points = [
    [0, 1],
    [50, 2],
    [100, 3],
    [150, 4],
    [200, 5],
  ];
  assert.deepEqual(sessionBreaks(points, dayOf), [2, 4]);
  assert.deepEqual(sessionBreaks(points, () => "same"), []);
});

test("the delta axis is symmetric about zero", () => {
  // Delta-neutral is the state a strangle is opened in; it should read as the
  // middle of the axis rather than as some arbitrary height.
  const domain = deltaDomain([
    [1, 0, 0.4],
    [2, 0, -0.1],
  ]);
  assert.equal(domain.lo, -0.4);
  assert.equal(domain.hi, 0.4);
});

test("a flat delta series is not magnified into noise", () => {
  const domain = deltaDomain([
    [1, 0, 0.001],
    [2, 0, -0.001],
  ]);
  assert.equal(domain.reach, 0.05, "the floor did not apply");
});

test("no marks means no delta axis", () => {
  assert.equal(deltaDomain([]), null);
  assert.equal(deltaDomain([[1, 5, null]]), null);
});

test("a strike held to the end of the chart runs to the right edge", () => {
  const geo = geometry();
  const box = BOX;
  const open = strikeSpan({ strike: 100, frm: 2000, to: null }, geo, box);
  assert.equal(open.x2, box.left + box.width, "an open leg stopped short");
  assert.ok(open.x1 > box.left, "the segment should start at entry, not the edge");
});

test("a strike with no known entry spans the whole plot", () => {
  // The snapshot-only case: stopping where the data does would imply the
  // position did too.
  const span = strikeSpan({ strike: 700, frm: null, to: null }, geometry(), BOX);
  assert.equal(span.x1, BOX.left);
  assert.equal(span.x2, BOX.left + BOX.width);
});

test("a closed strike's segment ends at its buyback", () => {
  const geo = geometry();
  const span = strikeSpan({ strike: 100, frm: 1000, to: 2000 }, geo, BOX);
  assert.equal(span.x1, geo.xs[0]);
  assert.equal(span.x2, geo.xs[1]);
  assert.ok(span.x2 < BOX.left + BOX.width, "it should not reach the right edge");
});

test("band edges pair upper with lower at the same x", () => {
  const band = [
    [1000, 90, 110],
    [2000, 95, 125],
  ];
  const geo = geometry(PRICE, [], band);
  const { upper, lower } = bandEdges(band, geo);
  assert.equal(upper.length, 2);
  assert.equal(lower.length, 2);
  assert.equal(upper[0][0], lower[0][0], "the two edges drifted apart in x");
  assert.ok(upper[0][1] < lower[0][1], "the upper edge is not above the lower");
});
