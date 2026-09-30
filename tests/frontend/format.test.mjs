import assert from "node:assert/strict";
import test from "node:test";

import {
  cls,
  compact,
  dayLabel,
  dp,
  esc,
  money,
  monthLabel,
  strike,
} from "../../src/optjournal/static/format.js";

test("a compact amount fits five characters and keeps its sign", () => {
  assert.equal(compact(715.74), "716");
  assert.equal(compact(-198.14), "−198");
  assert.equal(compact(-1729.42), "−1.7k");
  assert.equal(compact(-3266.6), "−3.3k");
  assert.equal(compact(12345), "12k");
  assert.equal(compact(-123456), "−123k");
  for (const v of [715.74, -198.14, -1729.42, -9949, -99499]) {
    assert.ok(compact(v).length <= 5, `${compact(v)} is too wide for a phone's day`);
  }
});

test("a compact amount rolls over at its edges instead of printing 1000 or 10.0k", () => {
  assert.equal(compact(999.4), "999");
  assert.equal(compact(999.5), "1.0k");
  assert.equal(compact(9949), "9.9k");
  assert.equal(compact(9950), "10k");
});

test("a compact amount that rounds to zero has no sign, and absence is a dash", () => {
  assert.equal(compact(-0.4), "0");
  assert.equal(compact(0), "0");
  assert.equal(compact(null), "—");
  assert.equal(compact(Number.NaN), "—");
});

test("escaping covers every character interpolated into markup", () => {
  assert.equal(esc('&<>"'), "&amp;&lt;&gt;&quot;");
});

test("money keeps the minus outside the currency symbol", () => {
  assert.equal(money(-1.84, "EUR"), "−€1.84");
  assert.equal(money(null, "EUR"), "—");
  assert.equal(money(Number.NaN, "EUR"), "—");
});

test("tiny non-zero charges remain visible", () => {
  assert.equal(dp(0.004), 4);
  assert.equal(money(0.004, "EUR"), "€0.0040");
});

test("strikes keep meaningful fractional precision", () => {
  assert.equal(strike(267.5), "267.5");
  assert.equal(strike(268), "268");
});

test("sign classes distinguish gain, loss, and measured zero", () => {
  assert.equal(cls(1), "pos signed");
  assert.equal(cls(-1), "neg signed");
  assert.equal(cls(0), "");
  assert.equal(cls(null), "");
});

test("date labels are short and unambiguous", () => {
  assert.equal(monthLabel("2026-08"), "Aug 2026");
  assert.equal(dayLabel("2026-08-07", false), "7 Aug");
  assert.equal(dayLabel("2026-08-07", true), "7 Aug '26");
});
