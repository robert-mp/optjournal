import assert from "node:assert/strict";
import test from "node:test";

import {
  cls,
  dayLabel,
  dp,
  esc,
  money,
  monthLabel,
  strike,
} from "../../src/optjournal/static/format.js";

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
