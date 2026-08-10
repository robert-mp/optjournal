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
  barsPerMs,
  clampIndex,
  clampPosition,
  deltaDomain,
  deltaSegments,
  domainOf,
  frameAt,
  xAtPosition,
  indexOfTs,
  markAt,
  niceTicks,
  nextStop,
  plotGeometry,
  reachedEvents,
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

/* ------------------------------------------------------- delta, only when held
 *
 * `modelled_marks` reports delta as null on any bar holding nothing -- before the
 * opening fill, and after a close goes flat -- because on a SYMMETRIC axis 0.0
 * means delta-neutral, a real state, rather than absent. These pin the drawing
 * half of that: the line must BREAK across those bars rather than glide over them.
 */

test("the delta line breaks where the position was not held", () => {
  /* One polyline over a null stretch would run straight from the last real delta
     to the first one after it, drawing exposure across bars that had none. */
  const geo = geometry();
  const marks = [[1000, 0, 0.4], [2000, 0, null], [3000, 0, 0.2]];
  const segments = deltaSegments(marks, geo, (v) => v);
  assert.equal(segments.length, 2, "the gap did not split the line");
  assert.deepEqual(segments.map((s) => s.length), [1, 1]);
});

test("a continuous holding is one line, not one per bar", () => {
  const geo = geometry();
  const marks = [[1000, 0, 0.4], [2000, 0, 0.3], [3000, 0, 0.2]];
  const segments = deltaSegments(marks, geo, (v) => v);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].length, 3);
});

test("leading and trailing flat bars contribute no segment", () => {
  // The two real shapes: context before entry, and every bar after a close.
  const geo = geometry();
  const marks = [[1000, 0, null], [2000, 0, 0.3], [3000, 0, null]];
  const segments = deltaSegments(marks, geo, (v) => v);
  assert.equal(segments.length, 1, "a flat edge became its own segment");
  assert.equal(segments[0].length, 1);
  assert.deepEqual(deltaSegments([[1000, 0, null]], geo, (v) => v), [],
                   "a series holding nothing drew a line anyway");
});

test("the y mapping is applied to the delta, not to the row", () => {
  const geo = geometry();
  const segments = deltaSegments([[2000, 0, 0.5]], geo, (v) => v * 100);
  assert.equal(segments[0][0][1], 50);
  assert.equal(segments[0][0][0], geo.at(2000).x, "x did not come from the bar");
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

test("an event maps to the bar that contains it, not the nearest one", () => {
  // A fill at 1900 sits inside the bar stamped 1000 (which spans 1000-2000).
  // Nearest-bar rounding would call it bar 1 and place it after later bars.
  assert.equal(indexOfTs(PRICE, 1900), 0);
  assert.equal(indexOfTs(PRICE, 2000), 1, "a fill exactly on a bar is that bar");
  assert.equal(indexOfTs(PRICE, 500), 0, "before the first bar clamps to it");
  assert.equal(indexOfTs(PRICE, 99999), 2, "after the last bar clamps to it");
});

test("an event is reached only once the replay passes it", () => {
  const events = [{ ts: 1500 }, { ts: 2500 }];
  assert.deepEqual(reachedEvents(events, 1000), [], "an event leaked early");
  assert.deepEqual(reachedEvents(events, 2000), [1500]);
  assert.deepEqual(reachedEvents(events, 3000), [1500, 2500]);
});

test("a reached card and its fill dot agree at every frame", () => {
  /* The property that matters, not just the arithmetic: cards are revealed by
     timestamp while dots are clipped by pixel width, so the two could disagree
     and have the panel contradict itself mid-scrub. */
  const geo = geometry();
  const fillTs = 1900;
  for (let i = 0; i < PRICE.length; i++) {
    const frame = frameAt({ points: PRICE, marks: [], xs: geo.xs }, i);
    const carded = reachedEvents([{ ts: fillTs }], frame.ts).length > 0;
    const dotted = geo.at(fillTs).x <= frame.revealWidth;
    assert.equal(carded, dotted, `card and dot disagree at bar ${i}`);
  }
});

test("an event with no timestamp is never reached", () => {
  // A malformed event must not render as having happened at the epoch.
  assert.deepEqual(reachedEvents([{ ts: null }, {}], 9999), []);
});

/* ------------------------------------------------------------------ smoothing
 *
 * Playback used to step one whole bar per setInterval tick, so the marker jumped
 * from data point to data point. It now advances by a FRACTION of a bar per
 * animation frame, which means frameAt takes fractional positions -- and the
 * line these tests defend is which outputs may follow that fraction. Geometry
 * may; values may not, because a price between two closes is a price the option
 * never traded at and a P&L between two marks is one markAt exists to refuse.
 */

test("a fractional position keeps its fraction, and is still clamped", () => {
  assert.equal(clampPosition(1.5, 3), 1.5);
  assert.equal(clampPosition("0.25", 3), 0.25);
  assert.equal(clampPosition(-2, 3), 0);
  assert.equal(clampPosition(99, 3), 2);
  assert.equal(clampPosition("nonsense", 3), 0);
  assert.equal(clampPosition(1.5, 0), 0);
});

test("x interpolates linearly between neighbouring bars", () => {
  const xs = [0, 10, 20];
  assert.equal(xAtPosition(xs, 0), 0);
  assert.equal(xAtPosition(xs, 1), 10);
  assert.equal(xAtPosition(xs, 0.5), 5);
  assert.equal(xAtPosition(xs, 1.25), 12.5);
  // Clamped at both ends rather than extrapolating off-canvas.
  assert.equal(xAtPosition(xs, 2.9), 20);
  assert.equal(xAtPosition(xs, -1), 0);
  assert.equal(xAtPosition([], 1.5), 0);
});

test("the marker glides but the readout does not lie", () => {
  const geo = geometry();
  const state = { points: PRICE, marks: [], xs: geo.xs };
  const half = frameAt(state, 0.5);
  const zero = frameAt(state, 0);
  const one = frameAt(state, 1);

  // Geometry follows the fraction: strictly between the two bars.
  assert.ok(half.x > zero.x && half.x < one.x, "x did not interpolate");
  assert.ok(half.revealWidth > zero.revealWidth
            && half.revealWidth < one.revealWidth);

  // Values snap to the nearest REAL bar. 0.5 rounds to bar 1.
  assert.equal(half.price, one.price);
  assert.equal(half.ts, one.ts);
  assert.equal(half.index, 1);
  // And 0.4 rounds the other way, so the readout is always some bar's own.
  const low = frameAt(state, 0.4);
  assert.equal(low.price, zero.price);
  assert.equal(low.ts, zero.ts);
  assert.ok(PRICE.some(([ts, px]) => ts === low.ts && px === low.price),
            "the frame reported a price no bar recorded");
});

test("P&L and delta never interpolate", () => {
  // markAt is exact-or-null by design: "a neighbouring bar's P&L presented as
  // this bar's would be a quiet fabrication". A fractional position must not
  // become the loophole in that rule.
  const geo = geometry();
  const marks = [[1000, 10, 0.1], [2000, 20, 0.2], [3000, 30, 0.3]];
  const state = { points: PRICE, marks, xs: geo.xs };
  for (const at of [0, 0.25, 0.5, 0.75, 1, 1.5, 2]) {
    const frame = frameAt(state, at);
    const mark = marks.find((m) => m[0] === frame.ts);
    assert.ok(mark, `no mark for ts ${frame.ts}`);
    assert.equal(frame.pnl, mark[1]);
    assert.equal(frame.delta, mark[2]);
  }
});

test("an integer position behaves exactly as it did before", () => {
  // The regression guard: smoothing must not change what a scrubber at bar i
  // shows, since the range input still selects whole bars.
  const geo = geometry();
  const state = { points: PRICE, marks: [], xs: geo.xs };
  for (let i = 0; i < PRICE.length; i++) {
    const frame = frameAt(state, i);
    assert.equal(frame.index, i);
    assert.equal(frame.x, geo.xs[i]);
    assert.equal(frame.revealWidth, geo.xs[i] + 1.2);
  }
});

/* ------------------------------------------------------------- pausing & pace
 *
 * Two rules about PLAYBACK rather than about drawing. Both are arithmetic, so
 * they live here: "which bar must playback stop on" and "how fast should it go"
 * are decisions a test can pin, unlike whether a card looks right.
 */

test("playback stops on the bar an event's own card seeks to", () => {
  // The pause and the annotation must agree, or the replay halts a bar away
  // from the card it is halting FOR. indexOfTs is the single source of both.
  const events = [{ ts: 1900 }, { ts: 2500 }];
  assert.equal(nextStop(events, PRICE, -1), indexOfTs(PRICE, 1900));
  assert.equal(nextStop(events, PRICE, 0), indexOfTs(PRICE, 2500),
               "a stop already standing on was not passed");
});

test("a stop is exclusive of where playback already stands", () => {
  /* The property that makes play resumable: were `from` inclusive, pressing
     play while parked on an event would re-stop on that same event forever. */
  const events = [{ ts: 2000 }];
  const at = nextStop(events, PRICE, -1);
  assert.equal(at, 1);
  assert.equal(nextStop(events, PRICE, at), null,
               "playback would be trapped on its own stop");
});

test("play resumes from a bar it is parked on", () => {
  /* The bug this pins, found by driving the real page: the caller passed
     `position - 1`, and since nextStop is ALREADY exclusive that asked "what
     comes after the bar before this one" and returned the stop being stood on.
     Playback halted instantly at the same bar, forever. `from` is the CURRENT
     bar, so the sequence of stops must strictly advance. */
  const events = [{ ts: 1000 }, { ts: 2000 }, { ts: 3000 }];
  const seen = [];
  let at = 0;                                   /* parked on bar 0's event */
  for (let guard = 0; guard < 10; guard++) {
    const stop = nextStop(events, PRICE, at);
    if (stop === null) break;
    assert.ok(stop > at, `playback did not advance past bar ${at}`);
    seen.push(stop);
    at = stop;
  }
  assert.deepEqual(seen, [1, 2], "the remaining events were not reached in turn");
});

test("no event ahead means run to the end", () => {
  assert.equal(nextStop([], PRICE, 0), null);
  assert.equal(nextStop([{ ts: 1000 }], PRICE, 2), null);
  // A malformed event is not a stop, for the same reason it is not a card.
  assert.equal(nextStop([{ ts: null }, {}], PRICE, -1), null);
});

test("the earliest event ahead wins, whatever order they arrive in", () => {
  const events = [{ ts: 3000 }, { ts: 1000 }, { ts: 2000 }];
  assert.equal(nextStop(events, PRICE, -1), 0);
});

test("every replay takes the same wall-clock, whatever its length", () => {
  /* The bug this fixes: a fixed ms-per-bar made "1x" a different promise per
     trade, so a 24-bar strangle finished while a 461-bar LEAP crawled. What
     should be constant is the DURATION, so the pace has to follow the length. */
  const spanOf = (rate, length) => (length - 1) / rate;      /* ms to traverse */
  const short = barsPerMs(24, 20);
  const leap = barsPerMs(461, 20);
  assert.ok(Math.abs(spanOf(short, 24) - spanOf(leap, 461)) < 1e-6,
            "a longer replay took longer at the same setting");
  assert.ok(Math.abs(spanOf(short, 24) - 20000) < 1e-6, "1x was not 20s");
});

test("the speed multiplier divides the duration", () => {
  const base = barsPerMs(50, 20, 1);
  assert.ok(Math.abs(barsPerMs(50, 20, 4) - base * 4) < 1e-9);
});

test("a degenerate series has a finite pace", () => {
  // A one-bar series would divide by zero and advance infinitely fast.
  assert.ok(Number.isFinite(barsPerMs(1, 20)));
  assert.ok(Number.isFinite(barsPerMs(0, 20)));
  assert.ok(barsPerMs(2, 0) > 0, "a zero duration must not stall playback");
});

/* ---------------------------------------------------------------- niceTicks
   The performance axis used to label the data's own padded extremes, so a reader
   got "€1,626 / €726 / −€174" -- three numbers nobody chose, all of which move on
   every fill. */

test("every tick is a round multiple a person would say out loud", () => {
  // The reported chart's own domain, and the demo journal's.
  assert.deepEqual(niceTicks(-174, 1626), [0, 500, 1000, 1500]);
  assert.deepEqual(niceTicks(-443, 4138), [0, 2000, 4000]);
  assert.deepEqual(niceTicks(0, 37), [0, 10, 20, 30]);
});

test("zero lands on a gridline whenever it is in range", () => {
  // Not special-cased in the implementation -- zero is a multiple of every step
  // -- but it is the value that matters most on a cumulative P&L chart, so it is
  // worth pinning that the rule actually delivers it.
  for (const [lo, hi] of [[-174, 1626], [-45, 62], [-1, 1], [-12000, 184000]]) {
    assert.ok(niceTicks(lo, hi).includes(0), `zero missing for ${lo}..${hi}`);
  }
});

test("ticks stay inside the domain, so none is drawn off the plot", () => {
  // The domain is deliberately NOT extended to whole steps: rounding
  // [-174, 1626] out to [-500, 2000] would leave the series using 69% of the
  // card's height and read as a smaller move than happened.
  for (const [lo, hi] of [[-174, 1626], [-820, -12], [-0.4, 2.1], [3, 9]]) {
    for (const t of niceTicks(lo, hi)) {
      assert.ok(t >= lo && t <= hi, `${t} outside ${lo}..${hi}`);
    }
  }
});

test("an all-negative range is labelled without inventing a positive tick", () => {
  const got = niceTicks(-820, -12);
  assert.ok(got.length > 0, "a losing account still needs an axis");
  assert.ok(got.every((t) => t <= 0));
});

test("a tick is exactly its own round value, not a float that drifts", () => {
  // Accumulating `v += step` drifts: a tick at 1499.9999999999998 formats as a
  // round number while sitting a hair off its own gridline.
  for (const t of niceTicks(0, 3)) {
    assert.equal(t, Math.round(t * 1e6) / 1e6);
  }
  assert.deepEqual(niceTicks(0, 1.2), [0, 0.5, 1]);
});

test("a degenerate domain yields no ticks rather than throwing", () => {
  // The chart pads a flat series before calling this, so these are guards rather
  // than live cases -- but an axis that throws blanks the whole card.
  assert.deepEqual(niceTicks(5, 5), []);
  assert.deepEqual(niceTicks(10, 1), []);
  assert.deepEqual(niceTicks(NaN, 10), []);
  assert.deepEqual(niceTicks(0, Infinity), []);
});

test("the tick count stays near the target across six orders of magnitude", () => {
  // A round step is worth little if it yields one tick on one chart and eleven on
  // the next; the axis has to look like the same axis at every scale.
  for (const hi of [1, 10, 100, 1000, 10000, 100000, 1e6]) {
    const n = niceTicks(0, hi).length;
    assert.ok(n >= 2 && n <= 7, `${n} ticks for 0..${hi}`);
  }
});
