/* Unit tests for the watchlist's arithmetic, run by `node --test`.
 *
 * Driven from tests/test_frontend.py so one `pytest` covers both languages, the
 * same arrangement replay.test.mjs has. node:test and node:assert are builtins,
 * so this adds no dependency to a project whose point is having almost none.
 *
 * `shownPrice` is the reason this file exists. Its rule -- the 1d change comes
 * from the same session as the price beside it -- was written in the page and
 * nothing in the suite executed it: `previous_close` appeared in tests/test_web.py
 * only as a payload key name, never as an assertion about how the change is
 * derived. Each case below is one branch of that rule.
 *
 * The gauge geometry is here for the mirror-image reason: nothing ELSE can see
 * it. A knob mapped backwards, a clamp on the wrong domain or an arc computed
 * against a diameter are all well-formed drawings, so the rendered page contains
 * no `undefined`, no `NaN` and nothing a markup assertion can catch. These are
 * the only checks in the project that would go red.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  PRICE_TIERS,
  alertState,
  bxDirection,
  earningsSoon,
  histBars,
  meterKnob,
  priceTier,
  ringArc,
  ringPoint,
  shownPrice,
} from "../../src/optjournal/static/watch.js";

/* A row whose stored bars say one thing and whose quote says another, which is
   the situation the derivation exists for: the bars ended on Thursday and the
   quote is Friday's. */
const ROW = { symbol: "DELL", last: 480.0, change_1d: -0.16, change_5d: 2.4 };
const QUOTE = { price: 484.6, previous_close: 470.0, at: 1_755_000_000 };

test("a live quote is printed with the change its own session implies", () => {
  const { price, live, chg1 } = shownPrice(ROW, QUOTE);
  assert.equal(price, 484.6);
  assert.equal(live, true);
  // From the QUOTE's previous_close, not from the bars' -0.16%: mixing them put
  // two different sessions in one row.
  assert.ok(Math.abs(chg1 - 3.1063829787) < 1e-9, `chg1 was ${chg1}`);
});

test("with no quote the price and the change both come from the bars", () => {
  const { price, live, chg1 } = shownPrice(ROW, undefined);
  assert.equal(price, 480.0);
  assert.equal(live, false);
  assert.equal(chg1, -0.16);
});

test("a quote with no previous close falls back to the bars for the change", () => {
  // The price is still the quote -- it is dated and current -- but the change it
  // would imply cannot be computed, so the bars' figure is the honest one.
  const { price, live, chg1 } = shownPrice(ROW, { price: 484.6, previous_close: null });
  assert.equal(price, 484.6);
  assert.equal(live, true);
  assert.equal(chg1, -0.16);
});

test("a previous close of zero is absent rather than a divisor", () => {
  // 0 is not a price. Dividing by it yields Infinity, which renders as a
  // well-formed-looking figure and is the defect shape this project hunts.
  const { chg1 } = shownPrice(ROW, { price: 484.6, previous_close: 0 });
  assert.equal(chg1, -0.16);
});

test("no quote and no bars is null, never zero", () => {
  // A symbol added a minute ago has stated no price. 0.00 in a price column and
  // +0.00% beside it would both be claims nothing measured.
  const bare = { symbol: "ZZZ", last: null, change_1d: null };
  assert.deepEqual(shownPrice(bare, null), { price: null, live: false, chg1: null });
});

test("a rising, a falling and a measured-flat delta are three states", () => {
  assert.equal(bxDirection(12.3), "up");
  assert.equal(bxDirection(-0.4), "down");
  assert.equal(bxDirection(0), "flat");
});

test("an unknown delta is null, which is not the same as flat", () => {
  // The session a symbol first crosses trend.MIN_SETTLED has a value and no
  // earlier window, so the payload sends null. "flat" would claim it was measured
  // and found unchanged.
  assert.equal(bxDirection(null), null);
  assert.equal(bxDirection(undefined), null);
  assert.equal(bxDirection(NaN), null);
});

test("an unknown earnings date is not a near one", () => {
  // THE MIXED SET, which is the normal state of this column rather than an edge:
  // `earnings_on` is typed, so most rows carry none for a long time. One date inside
  // the window, one outside it, one absent.
  assert.equal(earningsSoon(14, 28), true);
  assert.equal(earningsSoon(80, 28), false);
  assert.equal(earningsSoon(null, 28), false);
  // The trap this function exists for: `null >= 0` is TRUE in JavaScript, so the
  // obvious inline spelling `days >= 0 && days <= within` hides every symbol with no
  // date the moment the filter goes on -- presenting "nothing reports within 28 days"
  // where the truth is "nothing reports within 28 days that you told me about".
  assert.equal(null >= 0, true, "the language changed; the guard's reason has not");
  assert.equal(earningsSoon(undefined, 28), false);
});

test("the window's own edges, and a date already past", () => {
  assert.equal(earningsSoon(0, 28), true, "reporting today is as near as it gets");
  assert.equal(earningsSoon(28, 28), true, "the threshold is inside the window");
  assert.equal(earningsSoon(29, 28), false);
  // A past date is not an upcoming report, and the date stands until the next one is
  // recorded -- so it is kept rather than hidden, which would delete what was typed.
  assert.equal(earningsSoon(-1, 28), false);
});

/* The meter's real box, copied from the page's own WMETER so these cases are
   checked against the geometry that actually ships. [-50, +50] is B-Xtrender's
   own domain by construction (an RSI minus 50), not a range measured off the
   visible rows -- which is the property the clamp cases below defend. */
const BOX = { x: 6, w: 156, lo: -50, hi: 50 };

test("the meter's scale ends at the indicator's bounds and zero is the middle", () => {
  assert.equal(meterKnob(-50, BOX), 6);
  assert.equal(meterKnob(0, BOX), 84);
  assert.equal(meterKnob(50, BOX), 162);
});

test("a value past the bound is pinned to the end, never extrapolated", () => {
  // B-Xtrender cannot reach +/-80, so this is a wrong input rather than a wrong
  // reading -- and a knob drawn 46 units off the end of its track would be a
  // drawing error rendered as data. Pinned, it reads "at the limit".
  assert.equal(meterKnob(80, BOX), meterKnob(50, BOX));
  assert.equal(meterKnob(-80, BOX), meterKnob(-50, BOX));
  assert.ok(meterKnob(80, BOX) <= BOX.x + BOX.w, "the knob left its own track");
});

test("the knob moves right as the reading rises", () => {
  // The cheapest check that would catch an inverted map, which is otherwise a
  // perfectly plausible picture: every position is on the track, the gradient
  // still reads left-to-right, and every reading means its opposite.
  const walk = [-40, -12, 0, 12, 40].map((v) => meterKnob(v, BOX));
  for (let i = 1; i < walk.length; i += 1) {
    assert.ok(walk[i] > walk[i - 1], `${walk[i]} is not right of ${walk[i - 1]}`);
  }
});

test("an unmeasured reading places no knob at all", () => {
  // Not the left end: that is a position, and "-50" is the strongest reading the
  // meter can show. A symbol inside its warm-up window has no reading to place.
  assert.equal(meterKnob(null, BOX), null);
  assert.equal(meterKnob(undefined, BOX), null);
  assert.equal(meterKnob(NaN, BOX), null);
});

test("the ring's arc is a fraction of the CIRCUMFERENCE, not of the radius", () => {
  const r = 26;
  const c = 2 * Math.PI * r;
  assert.deepEqual(ringArc(0, r), { on: 0, off: c });
  const half = ringArc(50, r);
  assert.ok(Math.abs(half.on - c / 2) < 1e-9, `on was ${half.on}`);
  assert.ok(Math.abs(half.off - c / 2) < 1e-9, `off was ${half.off}`);
  const full = ringArc(100, r);
  assert.ok(Math.abs(full.on - c) < 1e-9, `on was ${full.on}`);
  assert.equal(full.off, 0);
  // A diameter or a bare radius would give a plausible arc at every rank and a
  // correct one at none, which is why the expectation is spelled out from 2*pi*r
  // here rather than copied from the implementation.
  assert.ok(Math.abs(half.on - r) > 1, "the arc looks like it was scaled by the radius");
});

test("a rank outside 0..100 is clamped rather than wrapped round the circle", () => {
  const r = 26;
  assert.deepEqual(ringArc(140, r), ringArc(100, r));
  assert.deepEqual(ringArc(-8, r), ringArc(0, r));
  assert.equal(ringArc(null, r), null);
  assert.equal(ringArc(50, 0), null);
});

test("the ring is measured clockwise from 12 o'clock", () => {
  const r = 26;
  const near = (got, want, why) =>
    assert.ok(Math.abs(got.x - want.x) < 1e-9 && Math.abs(got.y - want.y) < 1e-9,
      `${why}: got (${got.x}, ${got.y})`);
  // SVG y grows DOWNWARD, so the top of the circle is NEGATIVE y. Dropping that
  // minus returns points on the circle at the mirrored angle, so the threshold
  // tick would sit at 100 minus the threshold -- a picture that disagrees with
  // the filter chips while looking entirely reasonable.
  near(ringPoint(0, r), { x: 0, y: -r }, "a quarter of nothing is not the top");
  near(ringPoint(0.25, r), { x: r, y: 0 }, "a quarter turn is not the right");
  near(ringPoint(0.5, r), { x: 0, y: r }, "a half turn is not the bottom");
  near(ringPoint(0.75, r), { x: -r, y: 0 }, "three quarters is not the left");
});

test("the threshold tick and the arc read the same fraction", () => {
  // The tick is drawn from ringPoint(RVR_MID/100, r) while the arc is drawn from
  // ringArc(rank, r), so the two are only guaranteed to agree if a rank equal to
  // the threshold ends exactly where the tick is. Half of 100 is half a turn.
  const r = 26;
  const c = 2 * Math.PI * r;
  assert.ok(Math.abs(ringArc(50, r).on - c * 0.5) < 1e-9);
  near0(ringPoint(50 / 100, r).x);
  assert.ok(ringPoint(50 / 100, r).y > 0, "half a turn must be the bottom of the ring");
});

function near0(v) {
  assert.ok(Math.abs(v) < 1e-9, `expected 0, got ${v}`);
}

test("an alert is hit at or beyond either level, and needs a price to be hit", () => {
  assert.equal(alertState(100, null, null), null);
  assert.equal(alertState(100, 110, null), "set");
  assert.equal(alertState(110, 110, null), "hit");
  assert.equal(alertState(90, null, 90), "hit");
  assert.equal(alertState(95, 110, 90), "set");
  assert.equal(alertState(null, 110, 90), "set", "no price, no verdict");
  assert.equal(alertState(100, 0, null), null, "a zero level is no alert");
});

test("price tiers cut at 50 and 200, and an unpriced row has none", () => {
  assert.deepEqual(PRICE_TIERS, [50, 200]);
  assert.equal(priceTier(49.99), "1");
  assert.equal(priceTier(50), "2");
  assert.equal(priceTier(199.99), "2");
  assert.equal(priceTier(200), "3");
  assert.equal(priceTier(null), null);
  assert.equal(priceTier(0), null);
});

test("histogram bars grow from the baseline, scale to the arm's bounds, and fade toward zero", () => {
  const box = {w: 50, h: 20, gap: 2.5, lo: -50, hi: 50, min: 1};
  const bars = histBars([null, 10, 25, -50, -20], box);
  assert.equal(bars.length, 4, "a warm-up session draws nothing");
  const [b1, b2, b3, b4] = bars;
  assert.equal(b1.x, 1 * (8 + 2.5), "the slot is kept, so the newest is rightmost");
  assert.equal(b4.x, 4 * (8 + 2.5));
  assert.ok(b2.pos && b2.y + b2.h === 20, "every bar stands on the baseline");
  assert.equal(b2.h, 10, "25 of 50 is half the height");
  assert.ok(!b3.pos && b3.y === 0 && b3.h === 20, "the limit is a full bar, signed by colour");
  assert.ok(b4.faded && !b3.faded, "moving toward zero fades");
  assert.equal(histBars([0.1], box)[0].h, 1, "a tiny reading still draws a dash");
});
