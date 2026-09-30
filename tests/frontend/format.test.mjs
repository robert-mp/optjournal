import assert from "node:assert/strict";
import test from "node:test";

import {
  cls,
  dayLabel,
  dp,
  esc,
  level,
  money,
  monthLabel,
  num,
  pct,
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

test("a strike is ungrouped on every tab, as the 0DTE ladder prints it", () => {
  /* The ladder prints a listed strike through `level` (7755), and the Trades
     tab, the replay chart and the Watchlist printed the same contract through
     `num` (7,755). `level`'s own rule is that a strike carries no grouping. */
  assert.equal(strike(7755), "7755");
  assert.equal(strike(7755), level(7755, 0));
  assert.equal(strike(7250.125), "7250.13");
  assert.equal(strike("12500"), "12500");
  assert.equal(strike(null), "");
});

test("sign classes distinguish gain, loss, and measured zero", () => {
  assert.equal(cls(1), "pos signed");
  assert.equal(cls(-1), "neg signed");
  assert.equal(cls(0), "");
  assert.equal(cls(null), "");
});

test("a value that prints as zero prints no sign", () => {
  /* Measured on the real 2026-09-22 vertical: a delta of -0.0028 read "Δ -0.00",
     a watchlist change of -0.004% read a red "-0.00%", and money kept its minus
     on a figure it printed as zero. */
  assert.equal(num(-0.0028, 2), "0.00");
  assert.equal(num(-0), "0.00");
  assert.equal(pct(-0.04), "0.0%");
  assert.equal(pct(-0.004, 2), "0.00%");
  assert.equal(money(-0.00001, "USD"), "$0.0000");
  assert.equal(money(-0.001, "USD", 2), "$0.00");
  assert.equal(level(-0.001), "0.00");
  /* A real negative still has its sign, at every precision it shows. */
  assert.equal(money(-0.004, "USD"), "−$0.0040");
  assert.equal(num(-0.006, 2), "−0.01");
});

test("the class that colours a figure agrees with the figure", () => {
  /* Red and a leading + are both claims about the sign, so they follow what is
     PRINTED: at money's own precision by default, at the caller's when given. */
  assert.equal(cls(-0.00001), "", "money prints $0.0000 for it");
  assert.equal(cls(-0.004), "neg signed", "money prints −$0.0040 for it");
  assert.equal(cls(-0.004, 2), "", "num(v, 2) prints 0.00 for it");
  assert.equal(cls(0.04, 1), "", "num(v, 1) prints 0.0 for it");
  assert.equal(cls(0.05, 1), "pos signed");
  assert.equal(cls(-0), "");
  assert.equal(cls(Number.NaN), "");
});

test("one minus sign, the typographic one, across every number formatter", () => {
  /* money has always used U+2212; num and pct used the hyphen, so one card could
     read "−€174" beside "-12.5%". Nothing parses these strings back: inputs are
     filled with toFixed (see the 0DTE pads), never with a formatted figure. */
  assert.equal(num(-1.5), "−1.50");
  assert.equal(pct(-12.46), "−12.5%");
  assert.equal(level(-3, 0), "−3");
  assert.equal(money(-174, "EUR", 0), "−€174");
});

test("date labels are short and unambiguous", () => {
  assert.equal(monthLabel("2026-08"), "Aug 2026");
  assert.equal(dayLabel("2026-08-07", false), "7 Aug");
  assert.equal(dayLabel("2026-08-07", true), "7 Aug '26");
});
